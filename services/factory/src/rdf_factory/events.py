"""Normalized droid events (TDM-30): one shape for local droid-sdk and cloud Sessions API runs.

Every event a droid session produces is turned into a :class:`DroidEvent`. The same dict goes
to three places:

* in-process subscribers (``FactoryClient.stream_events`` / ``on_event``),
* ``<state_dir>/<agent_session_id>.events.jsonl`` (debugging, cross-process tail),
* for the kinds in :data:`LOGGED_KINDS`, an ``event_log`` row with
  ``actor = "droid:<role>"`` (e.g. ``droid:data``) and ``type = "droid.<kind>"``; the
  row's ``payload_json`` is :meth:`DroidEvent.to_dict` (text fields truncated).

The agents panel (TDM-35/76) reads ``agent_session`` for the live status and ``current_step``
and these ``droid.*`` event_log rows (filter on ``payload.agent_session_id``) for the step
history. See ``services/factory/README.md`` for the full contract.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

EVENT_VERSION = 1

# kind -> meaning (data keys)
KINDS = {
    "session_started": "a new Factory session exists (data: model, cwd, workdir, protocol)",
    "session_restarted": "protocol B: a new Factory session replaced a gone one (data: previous_factory_session_id)",
    "turn_started": "a message was sent to the droid (data: protocol, prompt_preview)",
    "thinking": "reasoning started (in-process only)",
    "assistant_text": "a complete assistant message (data: chars)",
    "tool_call": "the droid invoked a tool (tool, data: tool_use_id, input)",
    "tool_result": "a tool returned (tool, data: tool_use_id, is_error, preview)",
    "permission_denied": "our policy refused a tool action (tool, data: reason)",
    "state": "droid working state changed (data: state) (in-process only)",
    "usage": "token/credit counters (data: input_tokens, output_tokens, factory_credits) (in-process only)",
    "waiting_for": "turn ended with WAITING_FOR (data: predicate, raw, resume_payload)",
    "turn_completed": "turn finished (data: subtype, success, status, credits, seconds, final_text)",
    "error": "the droid or transport failed (data: error_type, detail)",
}
# Mirrored to event_log. thinking/state/usage are too chatty: in-process + jsonl only.
LOGGED_KINDS = frozenset(KINDS) - {"thinking", "state", "usage"}
# Kinds that also move agent_session.current_step (via agent_step).
STEP_KINDS = frozenset({"session_started", "session_restarted", "turn_started", "tool_call",
                        "assistant_text", "permission_denied", "error"})

TEXT_MAX = 300  # DroidEvent.text: one line for the panel
PREVIEW_MAX = 600  # previews inside data


def now_us() -> int:
    return time.time_ns() // 1000


def clip(s: Any, n: int = TEXT_MAX) -> str:
    s = "" if s is None else str(s)
    s = " ".join(s.split()) if n <= TEXT_MAX else s
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class DroidEvent:
    kind: str
    agent_session_id: int
    role: str
    mode: str  # local | cloud
    factory_session_id: str | None
    turn: int  # 1-based turn number within this agent_session (across restarts)
    seq: int  # monotonically increasing per agent_session within one client process
    ts_us: int
    text: str  # one-line human summary; what the panel shows
    tool: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    v: int = EVENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, limit: int = 16_000) -> str:
        d = self.to_dict()
        s = json.dumps(d, default=str)
        if len(s) > limit:  # event_log caps payloads at 64 KB; keep rows small
            d["data"] = {k: (clip(v, 400) if isinstance(v, str) else v) for k, v in d["data"].items()}
            d["data"]["truncated"] = True
            s = json.dumps(d, default=str)
            if len(s) > limit:
                d["data"] = {"truncated": True}
                s = json.dumps(d, default=str)
        return s


def actor_for(role: str) -> str:
    """event_log actor for a droid role: data_droid -> droid:data."""
    return "droid:" + (role[: -len("_droid")] if role.endswith("_droid") else role)


# ----------------------------------------------------------------------------- tool summaries

_PATH_KEYS = ("file_path", "path", "filePath", "directory_path", "folder")
_ARG_KEYS = ("command", "pattern", "query", "url", "glob_pattern", "patterns")
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (\S+)", re.MULTILINE)


def _short(path: str, repo: str | None) -> str:
    if repo and path.startswith(repo.rstrip("/") + "/"):
        return path[len(repo.rstrip("/")) + 1:]
    return path


def tool_summary(name: str, tool_input: Any, repo: str | None = None) -> str:
    """'Read services/factory/pyproject.toml', 'Execute: ls services', 'Grep "WAITING_FOR"'."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    patch = next((v for v in inp.values() if isinstance(v, str) and "*** Begin Patch" in v), None)
    if patch:  # ApplyPatch: name the files it touches
        files = [(op, _short(f, repo)) for op, f in _PATCH_FILE_RE.findall(patch)]
        if files:
            return clip(f"{name} " + ", ".join(f"{op.lower()} {f}" for op, f in files))
    for k in _PATH_KEYS:
        if isinstance(inp.get(k), str):
            return clip(f"{name} {_short(inp[k], repo)}")
    for k in _ARG_KEYS:
        v = inp.get(k)
        if isinstance(v, str):
            return clip(f"{name}: {v}" if k == "command" else f'{name} "{v}"')
        if isinstance(v, list) and v:
            return clip(f"{name} {' '.join(map(str, v))}")
    return clip(name)


def input_preview(tool_input: Any) -> dict[str, Any]:
    """Tool input with long strings (file contents, patches) cut to PREVIEW_MAX."""
    if not isinstance(tool_input, dict):
        return {"value": clip(tool_input, PREVIEW_MAX)}
    out: dict[str, Any] = {}
    for k, v in tool_input.items():
        if isinstance(v, str):
            out[k] = v if len(v) <= PREVIEW_MAX else v[:PREVIEW_MAX] + f"… (+{len(v) - PREVIEW_MAX} chars)"
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            out[k] = clip(json.dumps(v, default=str), PREVIEW_MAX)
    return out


def content_preview(content: Any) -> str:
    if isinstance(content, str):
        return clip(content, PREVIEW_MAX)
    parts = []
    for item in content or ():
        if isinstance(item, dict):
            parts.append(str(item.get("text") or item.get("content") or ""))
        else:
            parts.append(str(item))
    return clip("\n".join(parts), PREVIEW_MAX)


# ----------------------------------------------------------------------------- WAITING_FOR / DONE

# Assistant messages are joined without newlines, so a marker can trail a sentence on the same
# line ("...I'll stop.WAITING_FOR x"); accept it after line start or sentence punctuation.
WAITING_RE = re.compile(r"(?:^|(?<=[.!?;:`]))\s*WAITING_FOR\s+(.+?)\s*$", re.MULTILINE)
DONE_RE = re.compile(r"(?:^|(?<=[.!?;:`]))\s*DONE\s*\.?\s*$", re.MULTILINE)
# dataset:accepted_count>=5   episode:accepted_count >= 5   dataset.accepted_count==3
_PRED_RE = re.compile(r"^(?P<table>[a-z_][a-z0-9_]*)[:.](?P<field>[a-z_][a-z0-9_]*)\s*"
                      r"(?P<op>==|!=|>=|<=|>|<|=)\s*(?P<value>\S+)")
# TDM-31: optional wait deadline after the predicate: "... timeout=30m" / "timeout 2h" / "timeout=90"
_TIMEOUT_RE = re.compile(r"\s+timeout\s*[=: ]\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>[smhd]?)\b", re.IGNORECASE)
_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}

# TDM-31: how a leaf is scoped to the droid's ticket. Most tables have a ticket_id column; the ticket
# table is keyed by id; these have no ticket column at all, so adding ticket_id would make the leaf
# never true (the bridge's equality filter needs the column). Keep in sync with spacetime/schema.md.
TICKET_KEY_COLUMN = {"ticket": "id"}
NO_TICKET_COLUMN = frozenset({"robot", "contributor", "contributor_presence", "qa_result", "eval_trial",
                              "event_log", "service_identity"})
# Keys that describe the wait, not a row filter (the bridge also ignores them inside a leaf).
WAIT_META_KEYS = ("timeout_s", "deadline_us")


def scope_to_ticket(pred: dict[str, Any], ticket_id: int) -> dict[str, Any]:
    """Add the droid's ticket as a filter on every leaf that has none (recursing into all/any).

    ``ticket`` leaves get ``id``; tables with no ticket column are left alone. A leaf that already
    names ``ticket_id`` (or ``id`` for ``ticket``), at top level or in ``where``, is not touched.
    """
    if not ticket_id or not isinstance(pred, dict):
        return pred
    for kind in ("all", "any"):
        if isinstance(pred.get(kind), list):
            pred[kind] = [scope_to_ticket(c, ticket_id) for c in pred[kind]]
            return pred
    table = pred.get("table")
    if not isinstance(table, str) or table in NO_TICKET_COLUMN:
        return pred
    col = TICKET_KEY_COLUMN.get(table, "ticket_id")
    where = pred.get("where") if isinstance(pred.get("where"), dict) else {}
    if col not in pred and col not in where:
        pred[col] = ticket_id
    return pred


def parse_timeout(raw: str) -> tuple[str, float | None]:
    """('dataset:accepted_count>=5', 1800.0) from 'dataset:accepted_count>=5 timeout=30m'."""
    m = _TIMEOUT_RE.search(raw)
    if not m:
        return raw, None
    secs = float(m["n"]) * _UNIT_S[m["unit"].lower()]
    return (raw[: m.start()] + raw[m.end():]).strip(), secs


def parse_waiting_for(text: str, *, ticket_id: int = 0) -> dict[str, Any] | None:
    """The LAST ``WAITING_FOR ...`` line of a turn as an agent_wait_for predicate, else None.

    Accepted forms (the droid contract asks for the first two):
      ``WAITING_FOR dataset:accepted_count>=5``      -> {table, field, op, value[, ticket_id]}
      ``WAITING_FOR {"table": "dataset", ...}``       -> used verbatim (needs "table" or all/any);
                                                         agg / where / all / any as in TDM-27's bridge
      ``WAITING_FOR episodes>=5`` / free text          -> {table: <first word>, predicate: <raw>}
    Either form may carry a deadline (TDM-31): ``... timeout=30m`` after the text form, or
    ``"timeout_s": 1800`` in the JSON; it is stored as ``timeout_s`` and the bridge wakes the droid
    with a "timed out" message when it passes. With ``ticket_id``, every leaf without a ticket filter
    is scoped to that ticket (``scope_to_ticket``). ``raw`` always holds the original line.
    """
    matches = WAITING_RE.findall(text or "")
    if not matches:
        return None
    raw = matches[-1].strip().strip("`")
    if raw.startswith("{"):
        try:
            p = json.loads(raw)
            if isinstance(p, dict) and ("table" in p or "all" in p or "any" in p):
                p.setdefault("raw", raw)
                return scope_to_ticket(p, ticket_id)
        except json.JSONDecodeError:
            pass
    body, timeout_s = parse_timeout(raw)
    m = _PRED_RE.match(body)
    if m:
        value: Any = m["value"].rstrip(".,;")
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
        p = {"table": m["table"], "field": m["field"], "op": "==" if m["op"] == "=" else m["op"],
             "value": value, "raw": raw}
    else:
        word = re.match(r"[A-Za-z_][A-Za-z0-9_]*", body)
        p = {"table": (word.group(0).lower() if word else "unknown"), "predicate": body, "raw": raw}
    if timeout_s is not None:
        p["timeout_s"] = timeout_s
    return scope_to_ticket(p, ticket_id)


def waiting_for_line(predicate: dict[str, Any] | str, *, timeout_s: float | None = None) -> str:
    """The final line a droid (or a droid tool) prints to park: the inverse of ``parse_waiting_for``.

    A simple leaf {table, field, op, value} becomes the text form; anything else (all/any, agg,
    where, extra filters) is compact JSON. ``timeout_s`` adds the deadline.
    """
    if isinstance(predicate, str):
        line = predicate.removeprefix("WAITING_FOR ").strip()
        return f"WAITING_FOR {line}" + (f" timeout={int(timeout_s)}s" if timeout_s else "")
    p = {k: v for k, v in predicate.items() if k != "raw"}
    if timeout_s is not None:
        p["timeout_s"] = timeout_s
    simple = (set(p) <= {"table", "field", "op", "value", "timeout_s"} and {"table", "field", "value"} <= set(p)
              and isinstance(p["value"], (str, int, float, bool)) and not re.search(r"\s", str(p["value"])))
    if simple:
        value = p["value"] if isinstance(p["value"], str) else json.dumps(p["value"])
        t = p.get("timeout_s")
        return (f"WAITING_FOR {p['table']}:{p['field']}{p.get('op', '==')}{value}"
                + (f" timeout={int(t)}s" if t else ""))
    return "WAITING_FOR " + json.dumps(p, separators=(",", ":"), default=str)


def says_done(text: str) -> bool:
    return bool(DONE_RE.search(text or ""))
