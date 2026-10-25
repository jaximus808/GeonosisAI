"""One switchable transport for every Claude call: ``RDF_LLM_BACKEND`` = ``api`` | ``droid`` | ``claude_code``.

    from rdf_common.llm import default_llm
    r = default_llm.complete_structured(step="planner", system=SYSTEM, messages=msgs, schema=SCHEMA,
                                        model="claude-opus-5-5", effort="high")
    parsed, usage, meta = r            # or r.parsed / r.usage / r.meta / r.content (echo for a follow-up turn)

Backends (each step keeps its own prompts, schemas and guardrails; only the transport changes):

* ``api`` (default; **forced in cloud/production**): the Anthropic SDK, exactly the call the steps
  made before: ``client.beta.messages.create`` with structured output (``output_config.format``),
  adaptive thinking, vision, and the server-side refusal fallback (``fallbacks="default"``). SDK
  exceptions propagate unchanged, so ``llm_retry.classify`` and the HTTP error mapping still work.
* ``droid``: Factory credits through the local droid CLI, ``droid exec -m <RDF_LLM_DROID_MODEL> -o json``
  in its default read-only autonomy with ``--only-tools Read`` (images are files in a scratch dir the
  model reads), ``--cwd`` = a fresh scratch dir outside the repo. The schema goes in the prompt and the
  answer is validated here; one corrective retry on a schema violation.
* ``claude_code``: the local Claude Code harness, ``claude -p`` with ``--tools ""`` (no tools at all),
  ``--json-schema`` for structured output, images as stream-json image blocks, ``--safe-mode``, no
  setting sources or MCP, no session persistence, cwd = a scratch dir. ``ANTHROPIC_API_KEY`` is removed
  from its environment so it bills the local Claude Code login, not the API. Local heavy testing only.

**Switching on the fly.** The backend is resolved on every call: production lock -> ``api``; else the
runtime choice persisted by ``set_backend`` (``PUT /admin/llm``) in ``RDF_LLM_STATE_FILE`` (default
``~/.local/state/rdf/llm_backend.json``), as long as ``RDF_LLM_BACKEND`` still has the value it had when
the choice was made (changing the env var on purpose wins); else ``RDF_LLM_BACKEND``; else ``api``. A call
already in flight finishes on the backend it started with.

**Production lock.** ``FLY_APP_NAME`` / ``FLY_MACHINE_ID`` / ``K_SERVICE`` / ``RAILWAY_ENVIRONMENT`` /
``RENDER`` or ``RDF_DEPLOY_ENV`` in (cloud, prod, production, staging) -> the backend is ``api``, full stop;
``set_backend`` raises ``LLMLocked``; ``startup_check`` exits non-zero if the env asks for anything else.

**Every call logs** ``llm.call`` {backend, model, served_model, step, latency_ms, input_tokens,
output_tokens, cost_estimate_usd, cost_basis, ok, error?} on logger ``rdf.llm`` and hands the same record
to any sink registered with ``add_call_sink`` (the orchestrator appends it to event_log).

Errors: ``LLMError`` (``.reason`` = refusal | max_tokens | no_output | bad_json | schema | timeout |
cli_error | config, ``.llm_kind`` = the ``llm_retry`` kind: refusal | transient | error). Refusals say
"refused (refusal, category=...)", which ``llm_retry.classify`` maps to ``refusal``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from pydantic import SecretStr

from .settings import REPO_ROOT, RdfSettings

log = logging.getLogger("rdf.llm")

BACKENDS = ("api", "droid", "claude_code")
LABELS = {"api": "Claude API", "droid": "Factory droid", "claude_code": "Claude Code (local)"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"
REFUSAL, TRANSIENT, ERROR = "refusal", "transient", "error"  # = llm_retry kinds
CLOUD_ENVS = ("cloud", "prod", "production", "staging")
CLOUD_MARKERS = ("FLY_APP_NAME", "FLY_MACHINE_ID", "K_SERVICE", "RAILWAY_ENVIRONMENT", "RENDER")
# USD per million tokens (input, output, cache read); list price, for the cost estimate only.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
          "claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-sonnet-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-fable-5-1": (10.0, 50.0, 0.25),
          "claude-opus-4-8": (5.0, 25.0, 0.50)}


class LLMSettings(RdfSettings):
    """Read on every call (env first, then the repo .env). Key names match .env.example."""

    rdf_llm_backend: Literal["api", "droid", "claude_code"] | None = None
    rdf_llm_droid_model: str = "claude-opus-5-5"
    rdf_llm_claude_code_model: str = "opus"
    rdf_llm_cli_timeout_s: float = 300.0
    rdf_llm_schema_retries: int = 1
    rdf_llm_state_file: str | None = None
    rdf_llm_scratch_dir: str | None = None
    rdf_llm_droid_bin: str = "~/.local/bin/droid"
    rdf_llm_claude_bin: str = "~/.local/bin/claude"
    rdf_deploy_env: str | None = None
    factory_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None


def settings() -> LLMSettings:
    from pydantic import ValidationError

    try:
        return LLMSettings()
    except ValidationError as e:
        from .settings import settings_errors

        raise LLMError("LLM config invalid: " + "; ".join(settings_errors(LLMSettings, e)), reason="config") from None


# --------------------------------------------------------------------------- errors / results


class LLMError(RuntimeError):
    def __init__(self, msg: str, *, reason: str, kind: str = ERROR, category: str | None = None,
                 backend: str = "", model: str = "", detail: str = "") -> None:
        super().__init__(msg)
        self.reason, self.llm_kind, self.category = reason, kind, category
        self.backend, self.model, self.detail = backend, model, detail


class LLMLocked(LLMError):
    def __init__(self, msg: str = "LLM backend is locked in production (api)") -> None:
        super().__init__(msg, reason="config", backend="api")


@dataclass
class LLMResult:
    parsed: Any  # dict (schema) or the output_model instance
    text: str
    usage: dict  # input_tokens, output_tokens, ..., cost_estimate_usd, cost_basis, backend
    meta: dict  # backend, backend_label, model, served_model, requested_model, step, latency_ms, ...
    content: list = field(default_factory=list)  # echo as the assistant turn of a follow-up request
    raw: Any = None  # the SDK response (api) or the CLI's final JSON (droid / claude_code)

    def __iter__(self) -> Iterator[Any]:
        return iter((self.parsed, self.usage, self.meta))

    # response-like accessors, so callers that read resp.id / resp.model keep working
    @property
    def id(self) -> str | None:
        return self.meta.get("message_id")

    @property
    def model(self) -> str | None:
        return self.meta.get("served_model")

    @property
    def backend(self) -> str:
        return self.meta.get("backend", "")


@dataclass
class LLMRequest:
    step: str
    system: str
    messages: list[dict]
    schema: dict | None = None
    output_model: Any = None  # a pydantic model class (api: messages.parse(output_format=...))
    model: str = "claude-opus-5-5"  # the step's model (the api model id; a hint for the CLIs)
    effort: str | None = "high"
    max_tokens: int = 16000
    thinking: bool = True
    timeout_s: float | None = None

    def json_schema(self) -> dict:
        if self.schema is not None:
            return self.schema
        if self.output_model is None:
            raise LLMError("complete_structured needs schema or output_model", reason="config")
        try:
            from anthropic.lib._parse._transform import transform_schema  # strict-compatible form

            return transform_schema(self.output_model)
        except Exception:  # noqa: BLE001
            return _strictify(self.output_model.model_json_schema())

    def parse(self, d: Any) -> Any:
        return self.output_model.model_validate(d) if self.output_model is not None else d


def _strictify(s: Any) -> Any:
    if isinstance(s, dict):
        out = {k: _strictify(v) for k, v in s.items()}
        if out.get("type") == "object" and "properties" in out:
            out.setdefault("additionalProperties", False)
            out.setdefault("required", list(out["properties"]))
        return out
    if isinstance(s, list):
        return [_strictify(v) for v in s]
    return s


# --------------------------------------------------------------------------- environment / switching


def deploy_env(env: dict | None = None) -> str:
    e = os.environ if env is None else env
    v = (e.get("RDF_DEPLOY_ENV") or "").strip().lower()
    if v:
        return v
    if any(e.get(k) for k in CLOUD_MARKERS):
        return "cloud"
    return "local"


def is_locked(env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    return deploy_env(e) in CLOUD_ENVS or any(e.get(k) for k in CLOUD_MARKERS)


def state_file(s: LLMSettings | None = None) -> Path:
    s = s or settings()
    return Path(os.path.expanduser(s.rdf_llm_state_file or "~/.local/state/rdf/llm_backend.json"))


_state_lock = threading.Lock()


def _read_state(s: LLMSettings) -> dict:
    try:
        d = json.loads(state_file(s).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class Selection:
    backend: str
    model: str | None  # runtime model override (None: the backend's default)
    source: str  # default | env | runtime | locked
    locked: bool
    env: str
    updated_at: str | None = None
    updated_by: str | None = None

    def effective_model(self, hint: str | None = None, s: LLMSettings | None = None) -> str:
        s = s or settings()
        if self.model:
            return self.model
        if self.backend == "droid":
            return s.rdf_llm_droid_model
        if self.backend == "claude_code":
            return s.rdf_llm_claude_code_model
        return hint or "claude-opus-5-5"

    def describe(self, s: LLMSettings | None = None) -> dict:
        return {"backend": self.backend, "model": self.model, "effective_model": self.effective_model(None, s),
                "allowed": ["api"] if self.locked else list(BACKENDS), "env": self.env, "locked": self.locked,
                "source": self.source, "labels": dict(LABELS), "updated_at": self.updated_at,
                "updated_by": self.updated_by}


def current(s: LLMSettings | None = None) -> Selection:
    """The backend the next call will use (resolved fresh every time)."""
    s = s or settings()
    env_name, locked = deploy_env(), is_locked()
    if locked:
        return Selection("api", None, "locked", True, env_name)
    st = _read_state(s)
    if st.get("backend") in BACKENDS and st.get("env_backend") == s.rdf_llm_backend:
        return Selection(st["backend"], st.get("model") or None, "runtime", False, env_name,
                         st.get("updated_at"), st.get("updated_by"))
    if s.rdf_llm_backend:
        return Selection(s.rdf_llm_backend, None, "env", False, env_name)
    return Selection("api", None, "default", False, env_name)


def set_backend(backend: str, model: str | None = None, by: str = "operator") -> tuple[Selection, Selection]:
    """Switch at runtime (takes effect on the next call) and persist it. Returns (before, after)."""
    if is_locked():
        raise LLMLocked()
    if backend not in BACKENDS:
        raise LLMError(f"unknown LLM backend {backend!r}; one of {BACKENDS}", reason="config")
    s = settings()
    with _state_lock:
        before = current(s)
        p = state_file(s)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"backend": backend, "model": (model or "").strip() or None, "env_backend": s.rdf_llm_backend,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "updated_by": by[:64]}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2))
        tmp.replace(p)
        after = current(s)
    return before, after


def startup_check(service: str = "orchestrator") -> Selection:
    """Fail fast (exit 2) when a production/cloud deploy asks for a non-api backend. Returns the selection."""
    import sys

    try:
        s = settings()
    except LLMError as e:  # a bad RDF_LLM_* value, named without its value
        print(f"[{service}] config error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    if is_locked() and s.rdf_llm_backend not in (None, "api"):
        print(f"[{service}] config error: RDF_LLM_BACKEND={s.rdf_llm_backend} is not allowed in a "
              f"{deploy_env()} deploy; production always uses the Claude API (set RDF_LLM_BACKEND=api or unset it).",
              file=sys.stderr)
        raise SystemExit(2)
    sel = current(s)
    log.info("llm.backend", extra={"fields": {"backend": sel.backend, "source": sel.source, "locked": sel.locked,
                                                 "env": sel.env, "model": sel.effective_model(None, s)}})
    return sel


# --------------------------------------------------------------------------- call records


_sinks: list[Callable[[dict], None]] = []


def add_call_sink(fn: Callable[[dict], None]) -> None:
    if fn not in _sinks:
        _sinks.append(fn)


def remove_call_sink(fn: Callable[[dict], None]) -> None:
    if fn in _sinks:
        _sinks.remove(fn)


def _emit(rec: dict) -> None:
    (log.info if rec.get("ok") else log.warning)("llm.call", extra={"fields": rec})
    for fn in list(_sinks):
        try:
            fn(dict(rec))
        except Exception:  # noqa: BLE001 - a sink never breaks the call
            log.debug("llm.sink_error", exc_info=True)


# --------------------------------------------------------------------------- agent thoughts (workbench)
#
# The ticket cockpit's AgentWorkbench shows a droid per station and a Claude-Code-style transcript. The
# lines come from event_log rows ``agent.step`` / ``agent.thought`` {ticket_id, station, agent, text, ...}.
# The orchestrator registers a sink (``add_agent_sink``) that appends each record to event_log. With no
# sink registered every call below is a cheap no-op. ``RDF_AGENT_THOUGHTS=0`` turns them all off.
#
#   with agent_context(ticket_id=4, station="diagnose", agent="clusterer"):
#       agent_event("step", "Reading 10 baseline trials")
#       default_llm.complete_structured(step="diagnose", ...)   # start / heartbeat / thought snippets
#
# Pacing: at most one record per AGENT_GAP_S (1.5 s) per (ticket, agent). Records that come faster wait
# in a short per-agent queue and go out in order; when more than AGENT_QUEUE_MAX wait, the oldest
# thought (then the oldest record) is dropped. Nothing ever blocks the caller.

import collections
import contextlib
import contextvars

AGENT_GAP_S = 1.5
AGENT_QUEUE_MAX = 6
AGENT_TEXT_MAX = 280
AGENT_HEARTBEAT_S = 10.0
# llm.call step -> (station, agent) when the caller set no agent_context station/agent.
STEP_AGENT: dict[str, tuple[str, str]] = {
    "diagnose": ("diagnose", "clusterer"),
    "planner": ("plan_data", "planner"),
    "review": ("review", "reviewer"),
    "reviewer": ("review", "reviewer"),
    "llm_qa": ("review", "dataset_qa"),
}

_agent_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("rdf_agent_ctx", default=None)
_agent_sinks: list[Callable[[dict], None]] = []


def agent_thoughts_on() -> bool:
    return os.environ.get("RDF_AGENT_THOUGHTS", "1").strip().lower() not in ("0", "false", "off", "no")


def add_agent_sink(fn: Callable[[dict], None]) -> None:
    if fn not in _agent_sinks:
        _agent_sinks.append(fn)


def remove_agent_sink(fn: Callable[[dict], None]) -> None:
    if fn in _agent_sinks:
        _agent_sinks.remove(fn)


@contextlib.contextmanager
def agent_context(*, ticket_id: Any = None, station: str | None = None, agent: str | None = None, **extra: Any):
    """Tag every agent event (and every LLM call's start / heartbeat / thoughts) made inside the block."""
    cur = dict(_agent_ctx.get() or {})
    cur.update({k: v for k, v in {"ticket_id": ticket_id, "station": station, "agent": agent, **extra}.items()
                if v is not None})
    token = _agent_ctx.set(cur)
    try:
        yield cur
    finally:
        _agent_ctx.reset(token)


def current_agent() -> dict:
    return dict(_agent_ctx.get() or {})


def _clip(text: Any, n: int = AGENT_TEXT_MAX) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


class _Pacer:
    """Per-key spacing of at least ``gap`` seconds; a short ordered queue for bursts (see above)."""

    def __init__(self, gap: float = AGENT_GAP_S, max_queue: int = AGENT_QUEUE_MAX) -> None:
        self.gap, self.max_queue = gap, max_queue
        self._lock = threading.Lock()
        self._q: dict[Any, collections.deque] = {}
        self._next: dict[Any, float] = {}
        self._timer: dict[Any, threading.Timer] = {}

    def push(self, key: Any, rec: dict, deliver: Callable[[dict], None]) -> None:
        send = False
        with self._lock:
            now = time.monotonic()
            dq = self._q.setdefault(key, collections.deque())
            if not dq and now >= self._next.get(key, 0.0):
                self._next[key] = now + self.gap
                send = True
            else:
                dq.append(rec)
                while len(dq) > self.max_queue:
                    old = next((r for r in dq if r.get("type") == "agent.thought"), dq[0])
                    dq.remove(old)
                self._arm(key, now)
        if send:
            deliver(rec)

    def _arm(self, key: Any, now: float) -> None:  # under self._lock
        if key in self._timer:
            return
        t = threading.Timer(max(0.0, self._next.get(key, now) - now), self._fire, args=(key,))
        t.daemon = True
        self._timer[key] = t
        t.start()

    def _fire(self, key: Any) -> None:
        with self._lock:
            self._timer.pop(key, None)
            dq = self._q.get(key)
            rec = dq.popleft() if dq else None
            now = time.monotonic()
            self._next[key] = now + self.gap
            if dq:
                self._arm(key, now)
        if rec is not None:
            _deliver_agent(rec)

    def pending(self) -> int:
        with self._lock:
            return sum(len(d) for d in self._q.values())


_pacer = _Pacer()


def _deliver_agent(rec: dict) -> None:
    for fn in list(_agent_sinks):
        try:
            fn(dict(rec))
        except Exception:  # noqa: BLE001 - a sink never breaks the caller
            log.debug("llm.agent_sink_error", exc_info=True)


def agent_event(kind: str, text: str, *, ticket_id: Any = None, station: str | None = None,
                agent: str | None = None, **extra: Any) -> bool:
    """Queue one ``agent.<kind>`` record (kind = step | thought). Returns False when it was not sent
    (thoughts off, no sink, or no ticket to attach it to)."""
    if not _agent_sinks or not agent_thoughts_on():
        return False
    ctx = current_agent()
    tid = ticket_id if ticket_id is not None else ctx.get("ticket_id")
    try:
        tid = int(tid) if tid is not None else None
    except (TypeError, ValueError):
        tid = None
    if not tid:
        return False
    rec = {k: v for k, v in ctx.items() if k not in ("ticket_id", "station", "agent")}
    rec.update({"type": f"agent.{kind}", "ticket_id": tid, "station": station or ctx.get("station") or "",
                "agent": agent or ctx.get("agent") or "", "text": _clip(text)})
    rec.update({k: v for k, v in extra.items() if v is not None})
    rec["at"] = round(time.time(), 3)
    _pacer.push((tid, rec["agent"]), rec, _deliver_agent)
    return True


def _sentences(text: str, k: int = 3) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", re.sub(r"\s+", " ", text or "")) if p.strip()]
    out: list[str] = []
    for p in parts:
        if len(p) < 12 and out:
            out[-1] = f"{out[-1]} {p}"
            continue
        out.append(p)
        if len(out) >= k:
            break
    return out


_GIST_KEYS = ("reasoning", "reason", "observations", "summary", "rationale", "diagnosis", "explanation", "notes")


def _gist(parsed: Any) -> str:
    """The model's own words to quote as its thought: the first reasoning-like string field."""
    if hasattr(parsed, "model_dump"):
        try:
            parsed = parsed.model_dump()
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(parsed, dict):
        return ""
    for k in _GIST_KEYS:
        v = parsed.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            return " ".join(v)
    return ""


def _thinking_text(r: LLMResult | None) -> str:
    if r is None:
        return ""
    ex = r.meta.get("thinking_excerpt")
    if isinstance(ex, str) and ex.strip():
        return ex
    for b in r.content or []:
        t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if t == "thinking":
            txt = b.get("thinking") if isinstance(b, dict) else getattr(b, "thinking", "")
            if isinstance(txt, str) and txt.strip():
                return txt
    return ""


class _Think:
    """One LLM call's agent events: start, a heartbeat while it runs, then what it concluded."""

    def __init__(self, req: LLMRequest, sel: Selection, model: str) -> None:
        self.on = bool(_agent_sinks) and agent_thoughts_on() and bool(current_agent().get("ticket_id"))
        if not self.on:
            return
        ctx = current_agent()
        station, agent = STEP_AGENT.get(req.step, ("", req.step))
        self.where = {"ticket_id": ctx.get("ticket_id"), "station": ctx.get("station") or station,
                      "agent": ctx.get("agent") or agent}
        self.ctx = contextvars.copy_context()
        self.step, self.t0, self.n = req.step, time.monotonic(), 0
        self.label = f"{LABELS.get(sel.backend, sel.backend)} · {model}"
        agent_event("step", f"Asking Claude ({self.label})", **self.where, phase="llm_start", llm_step=req.step,
                    backend=sel.backend, model=model)
        self._stop = threading.Event()
        try:
            hb = float(os.environ.get("RDF_AGENT_HEARTBEAT_S", AGENT_HEARTBEAT_S))
        except ValueError:
            hb = AGENT_HEARTBEAT_S
        self._t = threading.Thread(target=self.ctx.run, args=(self._beat, hb), daemon=True, name="agent-heartbeat")
        self._t.start()

    def _beat(self, every: float) -> None:
        while not self._stop.wait(every):
            s = int(time.monotonic() - self.t0)
            agent_event("thought", f"Still thinking… {s}s", **self.where, phase="llm_wait", llm_step=self.step,
                        elapsed_s=s)

    def end(self, r: LLMResult | None, err: BaseException | None) -> None:
        if not self.on:
            return
        self._stop.set()
        secs = round(time.monotonic() - self.t0, 1)
        if err is not None:
            agent_event("step", f"Claude call failed after {secs:g}s: {_clip(err, 160)}", **self.where,
                        phase="llm_error", llm_step=self.step, elapsed_s=secs, is_error=True)
            return
        agent_event("step", f"Claude answered in {secs:g}s", **self.where, phase="llm_end", llm_step=self.step,
                    elapsed_s=secs, output_tokens=(r.usage or {}).get("output_tokens") if r else None)
        said = 0
        for snip in _sentences(_thinking_text(r), 2):
            agent_event("thought", snip, **self.where, phase="thinking", llm_step=self.step)
            said += 1
        for snip in _sentences(_gist(r.parsed if r else None), 4 - said):
            agent_event("thought", snip, **self.where, phase="reasoning", llm_step=self.step)


def cost_estimate(model: str | None, usage: dict) -> float | None:
    p = PRICES.get((model or "").split("[")[0])
    if not p or usage.get("input_tokens") is None:
        return None
    i = (usage.get("input_tokens") or 0) + 1.25 * (usage.get("cache_creation_input_tokens") or 0)
    return round((i * p[0] + (usage.get("output_tokens") or 0) * p[1]
                  + (usage.get("cache_read_input_tokens") or 0) * p[2]) / 1e6, 6)


# --------------------------------------------------------------------------- tiny JSON-schema check


def schema_errors(v: Any, s: dict, path: str = "$", defs: dict | None = None) -> list[str]:
    """The subset of JSON Schema the steps use (type, enum, const, required, properties,
    additionalProperties, items, anyOf/oneOf, $ref, min/max, minItems/maxItems, minLength)."""
    defs = defs if defs is not None else (s.get("$defs") or s.get("definitions") or {})
    if "$ref" in s:
        s = defs.get(str(s["$ref"]).split("/")[-1], {})
    errs: list[str] = []
    for key in ("anyOf", "oneOf"):
        if key in s:
            if all(schema_errors(v, sub, path, defs) for sub in s[key]):
                errs.append(f"{path}: matches none of {key}")
            return errs
    t = s.get("type")
    types = t if isinstance(t, list) else [t] if t else []
    pyt = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}

    def ok(tt: str) -> bool:
        if tt == "integer":
            return isinstance(v, int) and not isinstance(v, bool)
        if tt == "number":
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        return isinstance(v, pyt.get(tt, object))

    if types and not any(ok(tt) for tt in types):
        return [f"{path}: expected {'/'.join(types)}, got {type(v).__name__}"]
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not one of {s['enum']}")
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: must be {s['const']!r}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: {v} < minimum {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{path}: {v} > maximum {s['maximum']}")
    if isinstance(v, str) and "minLength" in s and len(v) < s["minLength"]:
        errs.append(f"{path}: shorter than {s['minLength']}")
    if isinstance(v, dict):
        props = s.get("properties") or {}
        for k in s.get("required") or []:
            if k not in v:
                errs.append(f"{path}: missing required property {k!r}")
        for k, sub in props.items():
            if k in v:
                errs += schema_errors(v[k], sub, f"{path}.{k}", defs)
        if s.get("additionalProperties") is False:
            errs += [f"{path}: unexpected property {k!r}" for k in v if k not in props]
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            errs.append(f"{path}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            errs.append(f"{path}: more than {s['maxItems']} items")
        if isinstance(s.get("items"), dict):
            for i, x in enumerate(v):
                errs += schema_errors(x, s["items"], f"{path}[{i}]", defs)
    return errs


def extract_json(text: str) -> Any:
    """Parse a JSON object out of model text (tolerates ```json fences and a sentence around it)."""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


# --------------------------------------------------------------------------- api backend


_api_lock = threading.Lock()
_api_clients: dict[str, Any] = {}


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None  # the SDK reads it from the environment
    k = settings().anthropic_api_key
    return k.get_secret_value() if k else None


def _default_api_client(asynchronous: bool = False) -> Any:
    name = "async" if asynchronous else "sync"
    with _api_lock:
        if name not in _api_clients:
            import anthropic

            key = _api_key()
            cls = anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic
            kw: dict = {"max_retries": 3, "timeout": 600}
            if key:
                kw["api_key"] = key
            _api_clients[name] = cls(**kw)
        return _api_clients[name]


def _api_kwargs(req: LLMRequest, model: str) -> dict:
    kw: dict = {"model": model, "max_tokens": req.max_tokens, "betas": [FALLBACK_BETA], "fallbacks": "default"}
    if req.thinking:
        kw["thinking"] = {"type": "adaptive"}
    oc: dict = {"effort": req.effort} if req.effort else {}
    if req.output_model is not None:
        kw["output_format"] = req.output_model
    else:
        oc["format"] = {"type": "json_schema", "schema": req.schema}
    if oc:
        kw["output_config"] = oc
    kw["system"] = req.system
    kw["messages"] = req.messages
    return kw


def _api_result(req: LLMRequest, resp: Any, model: str) -> LLMResult:
    u = getattr(resp, "usage", None)
    usage = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens")} if u is not None else {}
    usage = {k: v for k, v in usage.items() if isinstance(v, int)}
    served = getattr(resp, "model", None) or model
    if not isinstance(served, str):
        served = model
    meta = {"message_id": getattr(resp, "id", None), "request_id": getattr(resp, "_request_id", None),
            "served_model": served, "stop_reason": getattr(resp, "stop_reason", None)}
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise _with(LLMError(f"model refused (refusal, category={cat})", reason="refusal", kind=REFUSAL,
                             category=cat, backend="api", model=model), usage, meta)
    if stop == "max_tokens":
        raise _with(LLMError("model output hit max_tokens", reason="max_tokens", backend="api", model=model),
                    usage, meta)
    content = list(getattr(resp, "content", None) or [])
    text = next((b.text for b in content if getattr(b, "type", None) == "text"), None)
    if req.output_model is not None and getattr(resp, "parsed_output", None) is not None:
        return LLMResult(resp.parsed_output, text or "", usage, meta, content, resp)
    if not text:
        raise _with(LLMError(f"no text block in response (stop_reason={stop})", reason="no_output",
                             backend="api", model=model), usage, meta)
    try:
        parsed = json.loads(text)
    except ValueError as e:
        raise _with(LLMError(f"answer is not JSON: {e}", reason="bad_json", backend="api", model=model),
                    usage, meta) from e
    try:
        value = req.parse(parsed)
    except Exception as e:  # noqa: BLE001 - pydantic ValidationError on an output_model
        raise _with(LLMError(f"answer did not validate: {e}", reason="schema", backend="api", model=model),
                    usage, meta) from e
    return LLMResult(value, text, usage, meta, content, resp)


def _with(e: LLMError, usage: dict, meta: dict) -> LLMError:
    e.usage, e.meta = usage, meta  # type: ignore[attr-defined]
    return e


class ApiBackend:
    name = "api"

    def __init__(self, client: Any = None, async_client: Any = None) -> None:
        self._client, self._async = client, async_client

    def complete(self, req: LLMRequest, model: str) -> LLMResult:
        c = self._client if self._client is not None else _default_api_client()
        kw = _api_kwargs(req, model)
        resp = c.beta.messages.parse(**kw) if req.output_model is not None else c.beta.messages.create(**kw)
        return _api_result(req, resp, model)

    async def acomplete(self, req: LLMRequest, model: str) -> LLMResult:
        if self._async is None and self._client is not None:
            return await asyncio.to_thread(self.complete, req, model)
        c = self._async if self._async is not None else _default_api_client(asynchronous=True)
        kw = _api_kwargs(req, model)
        resp = await (c.beta.messages.parse(**kw) if req.output_model is not None else c.beta.messages.create(**kw))
        return _api_result(req, resp, model)


# --------------------------------------------------------------------------- CLI backends


@dataclass
class Proc:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., Proc]  # runner(argv, *, input, cwd, env, timeout) -> Proc; raises TimeoutError


def subprocess_runner(argv: list[str], *, input: str | None, cwd: str, env: dict, timeout: float) -> Proc:
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.communicate()
        raise TimeoutError(f"{Path(argv[0]).name} did not answer within {timeout:.0f}s") from None
    return Proc(p.returncode, out or "", err or "")


def _blocks(content: Any) -> list[dict]:
    """Normalise message content (str, dicts or SDK blocks) to dict blocks; thinking blocks dropped."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for b in content or []:
        t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": b["text"] if isinstance(b, dict) else b.text})
        elif t == "image":
            src = b["source"] if isinstance(b, dict) else b.source
            src = src if isinstance(src, dict) else src.model_dump()
            out.append({"type": "image", "source": src})
    return out


_TRANSIENT_HINTS = ("rate limit", "rate_limit", "overloaded", "429", "503", "529", "timed out", "timeout",
                    "econnreset", "network", "temporarily", "try again")


def _kind_from_text(text: str) -> str:
    low = (text or "").lower()
    return TRANSIENT if any(h in low for h in _TRANSIENT_HINTS) else ERROR


class _CliBackend:
    name = ""
    bin_setting = ""

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or subprocess_runner

    def binary(self, s: LLMSettings) -> str:
        return os.path.expanduser(getattr(s, self.bin_setting))

    def _scratch(self, s: LLMSettings, step: str) -> Path:
        base = s.rdf_llm_scratch_dir or None
        d = Path(tempfile.mkdtemp(prefix=f"rdf-llm-{step}-", dir=base)).resolve()
        if d == REPO_ROOT or REPO_ROOT in d.parents:  # never run a CLI agent inside the repo
            shutil.rmtree(d, ignore_errors=True)
            raise LLMError(f"RDF_LLM_SCRATCH_DIR must be outside the repo ({d})", reason="config", backend=self.name)
        return d

    def _render(self, req: LLMRequest, d: Path, inline_images: bool) -> tuple[list[dict], int]:
        """Flatten the conversation into one user turn. Returns (blocks, n_images)."""
        out: list[dict] = []
        n = 0
        multi = len(req.messages) > 1
        for i, m in enumerate(req.messages):
            if multi:
                who = "Your previous answer" if m.get("role") == "assistant" else (
                    "Request" if i == 0 else "Follow-up from the caller")
                out.append({"type": "text", "text": f"\n=== {who} ===\n"})
            for b in _blocks(m.get("content")):
                if b["type"] == "text":
                    out.append(b)
                    continue
                n += 1
                src = b["source"]
                if inline_images or src.get("type") != "base64":
                    out.append(b)
                    continue
                ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(src.get("media_type"), "jpg")
                f = d / f"image_{n:02d}.{ext}"
                f.write_bytes(base64.standard_b64decode(src["data"]))
                out.append({"type": "text", "text": f"[Image {n}: use the Read tool on the file {f} to view it]"})
        if multi:
            out.append({"type": "text", "text": "\n=== Answer the latest message above. ==="})
        return out, n

    def complete(self, req: LLMRequest, model: str) -> LLMResult:
        s = settings()
        binary = self.binary(s)
        if not Path(binary).exists() and not shutil.which(binary):
            raise LLMError(f"{self.name} backend: {binary} not found", reason="config", backend=self.name, model=model)
        schema = req.json_schema()
        d = self._scratch(s, req.step)
        timeout = float(req.timeout_s or s.rdf_llm_cli_timeout_s)
        try:
            feedback: str | None = None
            totals: dict = {}
            for attempt in range(1 + max(0, s.rdf_llm_schema_retries)):
                parsed, text, usage, meta, raw = self._once(s, req, model, schema, d, timeout, feedback)
                for k, v in usage.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        totals[k] = totals.get(k, 0) + v
                    else:
                        totals[k] = v
                errs = ["answer is not JSON"] if parsed is _NOT_JSON else schema_errors(parsed, schema)
                if not errs:
                    try:
                        value = req.parse(parsed)
                    except Exception as e:  # noqa: BLE001 - pydantic ValidationError
                        errs = [str(e)[:1500]]
                    else:
                        meta["schema_retries"] = attempt
                        return LLMResult(value, text, totals, meta, [{"type": "text", "text": text}], raw)
                feedback = ("Your previous answer did not match the required JSON schema:\n- " + "\n- ".join(errs[:20])
                            + f"\nYour previous answer was:\n{text[:4000]}\nReturn ONLY the corrected JSON object.")
                log.info("llm.schema_retry", extra={"fields": {"backend": self.name, "step": req.step,
                                                               "errors": errs[:5]}})
            reason = "bad_json" if parsed is _NOT_JSON else "schema"
            raise _with(LLMError(f"{self.name} answer did not match the schema after "
                                 f"{s.rdf_llm_schema_retries + 1} tries: {'; '.join(errs[:5])}",
                                 reason=reason, backend=self.name, model=model), totals, meta)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    async def acomplete(self, req: LLMRequest, model: str) -> LLMResult:
        return await asyncio.to_thread(self.complete, req, model)

    def _once(self, s, req, model, schema, d, timeout, feedback):  # pragma: no cover - abstract
        raise NotImplementedError

    def _run(self, argv: list[str], *, input: str | None, cwd: Path, env: dict, timeout: float, model: str) -> Proc:
        try:
            return self.runner(argv, input=input, cwd=str(cwd), env=env, timeout=timeout)
        except TimeoutError as e:
            raise LLMError(f"{self.name} timed out after {timeout:.0f}s: {e}", reason="timeout", kind=TRANSIENT,
                           backend=self.name, model=model) from e
        except OSError as e:
            raise LLMError(f"{self.name} could not start: {e}", reason="cli_error", backend=self.name,
                           model=model) from e


class _NotJson:
    pass


_NOT_JSON = _NotJson()


def _parse_text(text: str) -> Any:
    try:
        return extract_json(text)
    except ValueError:
        return _NOT_JSON


def _refusal_check(text: str, backend: str, model: str, stop_reason: Any = None, category: Any = None) -> None:
    if stop_reason == "refusal":
        raise LLMError(f"model refused (refusal, category={category})", reason="refusal", kind=REFUSAL,
                       category=category, backend=backend, model=model)


class ClaudeCodeBackend(_CliBackend):
    """``claude -p`` (stream-json in/out) with no tools, ``--json-schema`` and image blocks inline."""

    name = "claude_code"
    bin_setting = "rdf_llm_claude_bin"

    def _once(self, s, req, model, schema, d, timeout, feedback):
        blocks, _ = self._render(req, d, inline_images=True)
        if feedback:
            blocks.append({"type": "text", "text": "\n" + feedback})
        (d / "system.txt").write_text(req.system or "")
        argv = [self.binary(s), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--tools", "", "--json-schema", json.dumps(schema), "--system-prompt-file", str(d / "system.txt"),
                "--model", model, "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--disable-slash-commands", "--safe-mode"]
        if req.effort:
            argv += ["--effort", req.effort]
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}) + "\n"
        p = self._run(argv, input=msg, cwd=d, env=env, timeout=timeout, model=model)
        events = []
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        res = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if res is None:
            tail = (p.stderr or p.stdout or "")[-800:]
            raise LLMError(f"claude_code gave no result (exit {p.returncode}): {tail}", reason="cli_error",
                           kind=_kind_from_text(tail), backend=self.name, model=model)
        u = res.get("usage") or {}
        usage = {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens") if isinstance(u.get(k), int)}
        served = next(iter(res.get("modelUsage") or {}), None) or model
        if isinstance(res.get("total_cost_usd"), (int, float)):
            usage["cost_estimate_usd"] = round(float(res["total_cost_usd"]), 6)
            usage["cost_basis"] = "claude_code total_cost_usd (list-price estimate; billed to the local Claude Code login)"
        meta = {"message_id": res.get("session_id"), "served_model": served, "num_turns": res.get("num_turns"),
                "duration_api_ms": res.get("duration_api_ms"), "stop_reason": res.get("stop_reason")}
        thinking = [b.get("thinking") for e in events if e.get("type") == "assistant"
                    for b in ((e.get("message") or {}).get("content") or []) if isinstance(b, dict)
                    and b.get("type") == "thinking" and isinstance(b.get("thinking"), str)]
        if thinking:
            meta["thinking_excerpt"] = " ".join(thinking)[:1200]
        stop = res.get("stop_reason")
        cat = None
        for e in events:  # a refusal shows on the assistant message
            m = e.get("message") if e.get("type") == "assistant" else None
            if isinstance(m, dict) and m.get("stop_reason") == "refusal":
                stop, cat = "refusal", (m.get("stop_details") or {}).get("category")
        try:
            _refusal_check("", self.name, model, stop, cat)
        except LLMError as e:
            raise _with(e, usage, meta)
        if res.get("is_error"):
            detail = f"{res.get('subtype')}: {res.get('api_error_status') or ''} {str(res.get('result') or '')[:600]}"
            if "structured_output" in str(res.get("subtype") or ""):
                return _NOT_JSON, str(res.get("result") or ""), usage, meta, res
            kind = TRANSIENT if (isinstance(res.get("api_error_status"), int) and (
                res["api_error_status"] in (408, 409, 429) or res["api_error_status"] >= 500)) else _kind_from_text(detail)
            raise _with(LLMError(f"claude_code error ({detail.strip()})", reason="cli_error", kind=kind,
                                 backend=self.name, model=model), usage, meta)
        so = res.get("structured_output")
        text = json.dumps(so) if so is not None else str(res.get("result") or "")
        return (so if so is not None else _parse_text(text)), text, usage, meta, res


class DroidBackend(_CliBackend):
    """``droid exec -o json`` in read-only autonomy with only the Read tool; schema stated in the prompt."""

    name = "droid"
    bin_setting = "rdf_llm_droid_bin"

    def _once(self, s, req, model, schema, d, timeout, feedback):
        blocks, n_img = self._render(req, d, inline_images=False)
        parts = [b["text"] for b in blocks]
        parts.append("\n\nRespond with ONLY one JSON object that validates against this JSON Schema: no prose, "
                     "no markdown fences, no tool calls other than reading the image files named above.\n"
                     + json.dumps(schema))
        if feedback:
            parts.append("\n\n" + feedback)
        (d / "prompt.md").write_text("\n".join(parts))
        (d / "system.md").write_text(req.system or "")
        argv = [self.binary(s), "exec", "-m", model, "-o", "json", "--cwd", str(d), "--only-tools", "Read",
                "--disable-builtin-skills", "--append-system-prompt-file", str(d / "system.md"), "-f", str(d / "prompt.md")]
        if req.effort:
            argv += ["-r", req.effort]
        env = dict(os.environ)
        if s.factory_api_key and not env.get("FACTORY_API_KEY"):
            env["FACTORY_API_KEY"] = s.factory_api_key.get_secret_value()
        p = self._run(argv, input=None, cwd=d, env=env, timeout=timeout, model=model)
        try:
            res = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
        except (ValueError, IndexError):
            res = None
        if not isinstance(res, dict):
            tail = (p.stderr or p.stdout or "")[-800:]
            raise LLMError(f"droid gave no JSON result (exit {p.returncode}): {tail}", reason="cli_error",
                           kind=_kind_from_text(tail), backend=self.name, model=model)
        u = res.get("usage") or {}
        usage = {k: u.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens", "factory_credits") if isinstance(u.get(k), int)}
        usage["cost_basis"] = "factory_credits (Factory plan credits; no USD price)"
        meta = {"message_id": res.get("session_id"), "served_model": model, "num_turns": res.get("num_turns"),
                "images": n_img}
        text = str(res.get("result") or "")
        if res.get("is_error") or p.returncode != 0:
            kind = _kind_from_text(text + p.stderr)
            if "refus" in text.lower():
                raise _with(LLMError(f"model refused (refusal, category=None): {text[:300]}", reason="refusal",
                                     kind=REFUSAL, backend=self.name, model=model), usage, meta)
            raise _with(LLMError(f"droid error ({res.get('subtype')}): {text[:600]}", reason="cli_error", kind=kind,
                                 backend=self.name, model=model), usage, meta)
        return _parse_text(text), text, usage, meta, res


# --------------------------------------------------------------------------- the client the steps use


class LLM:
    """Resolves the backend on every call (unless pinned) and logs ``llm.call``.

    ``api_client`` / ``async_api_client``: inject an Anthropic-like client (tests, or a pinned key).
    ``runners``: {"droid": runner, "claude_code": runner} fake subprocess runners for tests.
    ``backend``: pin one backend (``as_llm(client)`` pins ``api`` for an injected SDK client)."""

    def __init__(self, *, api_client: Any = None, async_api_client: Any = None, runners: dict | None = None,
                 backend: str | None = None) -> None:
        self._api = ApiBackend(api_client, async_api_client)
        r = runners or {}
        self._cli = {"droid": DroidBackend(r.get("droid")), "claude_code": ClaudeCodeBackend(r.get("claude_code"))}
        self.pinned = backend

    def selection(self) -> Selection:
        sel = current()
        if self.pinned and self.pinned != sel.backend:
            if is_locked() and self.pinned != "api":
                raise LLMLocked()
            return Selection(self.pinned, None, "pinned", sel.locked, sel.env)
        return sel

    def _backend(self, name: str) -> Any:
        return self._api if name == "api" else self._cli[name]

    def _prepare(self, step: str, system: str, messages: list[dict] | None, user_content: Any, **kw: Any):
        if messages is None:
            messages = [{"role": "user", "content": user_content}]
        req = LLMRequest(step=step, system=system, messages=messages, **kw)
        sel = self.selection()
        return req, sel, sel.effective_model(req.model)

    def _record(self, req: LLMRequest, sel: Selection, model: str, t0: float, r: LLMResult | None,
                err: BaseException | None) -> None:
        usage = r.usage if r is not None else dict(getattr(err, "usage", {}) or {})
        meta = r.meta if r is not None else dict(getattr(err, "meta", {}) or {})
        served = meta.get("served_model") or model
        if sel.backend == "api" and "cost_estimate_usd" not in usage:
            c = cost_estimate(served, usage)
            if c is not None:
                usage["cost_estimate_usd"], usage["cost_basis"] = c, "api list-price estimate"
        latency = int((time.monotonic() - t0) * 1000)
        meta.update({"backend": sel.backend, "backend_label": LABELS[sel.backend], "model": model,
                     "served_model": served, "requested_model": req.model, "step": req.step, "latency_ms": latency,
                     "selection_source": sel.source})
        usage["backend"] = sel.backend
        rec = {"backend": sel.backend, "model": model, "served_model": served, "step": req.step,
               "latency_ms": latency, "input_tokens": usage.get("input_tokens"),
               "output_tokens": usage.get("output_tokens"), "cost_estimate_usd": usage.get("cost_estimate_usd"),
               "cost_basis": usage.get("cost_basis"), "factory_credits": usage.get("factory_credits"),
               "ok": err is None}
        if err is not None:
            try:
                err.usage, err.meta = usage, meta  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            rec["error"] = f"{type(err).__name__}: {err}"[:500]
            rec["reason"] = getattr(err, "reason", None)
        _emit({k: v for k, v in rec.items() if v is not None})

    def complete_structured(self, *, step: str, system: str, messages: list[dict] | None = None,
                            user_content: Any = None, schema: dict | None = None, output_model: Any = None,
                            model: str = "claude-opus-5-5", effort: str | None = "high", max_tokens: int = 16000,
                            thinking: bool = True, timeout_s: float | None = None) -> LLMResult:
        req, sel, m = self._prepare(step, system, messages, user_content, schema=schema, output_model=output_model,
                                    model=model, effort=effort, max_tokens=max_tokens, thinking=thinking,
                                    timeout_s=timeout_s)
        t0 = time.monotonic()
        think = _Think(req, sel, m)
        try:
            r = self._backend(sel.backend).complete(req, m)
        except BaseException as e:
            self._record(req, sel, m, t0, None, e)
            think.end(None, e)
            raise
        self._record(req, sel, m, t0, r, None)
        think.end(r, None)
        return r

    async def acomplete_structured(self, *, step: str, system: str, messages: list[dict] | None = None,
                                   user_content: Any = None, schema: dict | None = None, output_model: Any = None,
                                   model: str = "claude-opus-5-5", effort: str | None = "high",
                                   max_tokens: int = 16000, thinking: bool = True,
                                   timeout_s: float | None = None) -> LLMResult:
        req, sel, m = self._prepare(step, system, messages, user_content, schema=schema, output_model=output_model,
                                    model=model, effort=effort, max_tokens=max_tokens, thinking=thinking,
                                    timeout_s=timeout_s)
        t0 = time.monotonic()
        think = _Think(req, sel, m)
        try:
            r = await self._backend(sel.backend).acomplete(req, m)
        except BaseException as e:
            self._record(req, sel, m, t0, None, e)
            think.end(None, e)
            raise
        self._record(req, sel, m, t0, r, None)
        think.end(r, None)
        return r


default_llm = LLM()


def as_llm(client: Any = None) -> LLM:
    """``None`` -> the shared switchable client; an ``LLM`` -> itself; an Anthropic-like client
    (``.beta.messages.create``) -> an ``LLM`` pinned to ``api`` over that client."""
    if client is None:
        return default_llm
    if isinstance(client, LLM) or hasattr(client, "complete_structured"):
        return client
    if asyncio.iscoroutinefunction(getattr(getattr(getattr(client, "beta", None), "messages", None), "create", None)):
        return LLM(async_api_client=client, backend="api")
    return LLM(api_client=client, backend="api")


def complete_structured(**kw: Any) -> LLMResult:
    return default_llm.complete_structured(**kw)


def describe_result(meta: dict) -> str:
    """'claude-opus-5-5' for the API, 'claude-opus-5-5 via Claude Code (local)' otherwise."""
    served = meta.get("served_model") or meta.get("model") or "?"
    b = meta.get("backend") or "api"
    return served if b == "api" else f"{served} via {LABELS.get(b, b)}"


def usage_summary(r: Any) -> dict:
    """The per-call usage dict the steps store with a decision (an ``LLMResult`` or a raised error):
    tokens, the served model, and which backend / backend model produced it."""
    usage = (r.usage if isinstance(r, LLMResult) else getattr(r, "usage", None)) or {}
    meta = (r.meta if isinstance(r, LLMResult) else getattr(r, "meta", None)) or {}
    out = {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
           "model": meta.get("served_model")}
    for k, v in (("backend", meta.get("backend")), ("backend_model", meta.get("model")),
                 ("cache_read_input_tokens", usage.get("cache_read_input_tokens")),
                 ("cache_creation_input_tokens", usage.get("cache_creation_input_tokens")),
                 ("latency_ms", meta.get("latency_ms")), ("cost_estimate_usd", usage.get("cost_estimate_usd")),
                 ("cost_basis", usage.get("cost_basis")), ("factory_credits", usage.get("factory_credits"))):
        if v is not None:
            out[k] = v
    return out
