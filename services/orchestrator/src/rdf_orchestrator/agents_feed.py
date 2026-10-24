"""TDM-35: one normalised, ordered event stream per agent for the dashboard's agents panel.

Merges three sources into ``AgentFeed`` v1 (shape in ``services/factory/README.md``,
"Agents panel feed"):

* ``agent_session`` rows: live status, current_step, factory session ids (TDM-13);
* ``droid.*`` event_log rows that TDM-30's ``FactoryClient`` writes (shape unchanged);
* the orchestrator's own agents (planner, clustering, reviewer, QA, dispatcher, eval,
  flywheel), which are known only from the event_log rows they write.

Plus ``agent.*`` reducer echoes and the resume bridge's ``agent.resume_*`` rows, attached to
their session. A recorded droid run (TDM-30's ``<id>.events.jsonl``) can be loaded as a
*replay* feed, with every agent and event flagged ``replay: true``.

Pure functions only; ``agents_api`` does the I/O. The browser builds the same feed from its
SpacetimeDB subscriptions with ``apps/web/src/lib/agentfeed/normalize.ts``; that file mirrors
this one, and ``tests/test_tdm35_agents_feed.py`` checks both against one golden file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

FEED_VERSION = 1
TEXT_MAX = 300
DEFAULT_LIMIT = 20
SERVICE_ACTIVE_US = 5 * 60 * 1_000_000  # a service with an event in the last 5 min is "active"

# (service key, label, type prefixes, actors). First match on type prefix wins, then actor.
SERVICES: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = [
    ("clustering", "Failure clustering", ("diagnose.", "planner.failure_cluster"), ("orchestrator.diagnose",)),
    ("planner", "Planner", ("planner.",), ("planner", "orchestrator.planner")),
    ("reviewer", "Reviewer", ("review.",), ("reviewer", "orchestrator.reviewer")),
    ("qa", "Dataset QA", ("qa.",), ("orchestrator.dataset_qa", "orchestrator.llm_qa")),
    ("dispatcher", "Dispatcher", ("dispatch.",), ("dispatcher", "orchestrator:dispatcher")),
    ("eval", "Eval runner", ("eval.",), ("orchestrator:eval",)),
    ("flywheel", "Flywheel", ("flywheel.",), ()),
]
SERVICE_LABELS = {k: label for k, label, _, _ in SERVICES}
# Rows written while building/testing (same list as the event-log page, TDM-77).
TEST_PREFIXES = ("test.", "infra.", "spike.", "probe.")
# A service row from a person or a robot is not an agent step (e.g. eval.trial from robot:1).
NON_AGENT_ACTOR_PREFIXES = ("human:", "contributor:", "robot:", "fake-robot:")

# droid.<kind> (TDM-30 v1) -> feed kind. thinking/state/usage are dropped (usage feeds `usage`).
DROID_KIND = {
    "session_started": "started",
    "session_restarted": "started",
    "turn_started": "step",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "assistant_text": "message",
    "permission_denied": "error",
    "waiting_for": "waiting",
    "error": "error",
}
# Reducer echoes already covered by droid.* rows for droid sessions.
DROID_DUPLICATES = frozenset({"agent.step", "agent.session_updated", "agent.waiting_for"})


def clip(s: Any, n: int = TEXT_MAX) -> str:
    s = " ".join(("" if s is None else str(s)).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _json(s: Any) -> Any:
    if not isinstance(s, str) or not s:
        return s if isinstance(s, (dict, list)) else None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def is_droid_role(role: str) -> bool:
    return role.endswith("_droid")


def session_label(role: str, sid: int, name: str = "") -> str:
    base = (role[: -len("_droid")] + " droid") if is_droid_role(role) else role.replace("_", " ")
    return f"{name} ({base} #{sid})" if name else f"{base} #{sid}"


def service_for(actor: str, type_: str) -> str | None:
    if type_.startswith(TEST_PREFIXES) or actor.startswith(NON_AGENT_ACTOR_PREFIXES):
        return None
    for key, _, prefixes, _ in SERVICES:
        if type_.startswith(prefixes):
            return key
    for key, _, _, actors in SERVICES:
        if actor in actors:
            return key
    return None


def _is_error_type(type_: str) -> bool:
    tail = type_.rsplit(".", 1)[-1]
    return tail in ("error", "blocked") or tail.endswith(("_failed", "_error"))


def _f(v: Any) -> str:
    """Value -> text, identical in Python and the TS mirror (None -> ?, bools lower-case, 5.0 -> 5)."""
    if v is None:
        return "?"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)
    return str(v)


def _scalars(p: dict, skip: tuple[str, ...] = ()) -> str:
    bits = []
    for k, v in p.items():
        if k in skip or isinstance(v, (dict, list)) or v is None or v == "":
            continue
        if isinstance(v, str) and len(v) > 60:
            continue
        bits.append(f"{k}={_f(v)}")
        if len(bits) == 4:
            break
    return ", ".join(bits)


def service_text(type_: str, p: dict) -> str:
    """One-line summary of an orchestrator agent's event_log row."""
    g = p.get
    if type_ == "planner.data_plan":
        return clip(f"data plan: {_f(g('amount'))} × {_f(g('data_type'))} (iteration {_f(g('iteration'))}, "
                    f"est ${_num(g('est_cost_cents')) / 100:.2f}) — {_f(g('reasoning') or '')}")
    if type_ == "planner.failure_cluster":
        return clip(f"failure cluster \"{_f(g('label'))}\" ({_f(g('count'))} trials, iteration {_f(g('iteration'))})")
    if type_ == "diagnose.clusters":
        cl = g("clusters")
        n = len(cl) if isinstance(cl, list) else 0
        return clip(f"{n} failure cluster{'s' if n != 1 else ''}"
                    + (f" from eval {_f(g('eval_run_id'))}" if g("eval_run_id") else ""))
    if type_ in ("review.accepted", "review.rejected"):
        verdict = type_.split(".")[1]
        return clip(f"episode {_f(g('episode_id'))} {verdict}" + (f": {_f(g('reason'))}" if g("reason") else ""))
    if g("error"):
        return clip(f"{type_}: {_f(g('error'))}")
    if g("reason") and isinstance(g("reason"), str):
        return clip(f"{type_}: {g('reason')}")
    rest = _scalars(p, skip=("sender",))
    return clip(f"{type_}" + (f" ({rest})" if rest else ""))


def _agent_event_kind(type_: str, p: dict) -> tuple[str, str]:
    """agent.* reducer echoes and resume-bridge rows -> (kind, text)."""
    if type_ == "agent.session_created":
        return "info", clip(f"session created ({_f(p.get('role'))}) {_f(p.get('name') or '')}")
    if type_ == "agent.session_updated":
        to = p.get("to")
        kind = {"done": "done", "failed": "failed"}.get(to, "info")
        return kind, clip(f"status {_f(p.get('from'))} → {_f(to)}"
                          + (f": {_f(p.get('current_step'))}" if p.get("current_step") else ""))
    if type_ == "agent.waiting_for":
        pred = p.get("predicate")
        raw = pred.get("raw") if isinstance(pred, dict) else pred
        return "waiting", clip(f"waiting for {_f(raw or pred)}")
    if type_ == "agent.resumed":
        return "resumed", clip(f"resumed ({_f(p.get('mode'))})" + (f": {_f(p.get('reason'))}" if p.get("reason") else ""))
    if type_ == "agent.step":
        return "step", clip(_f(p.get("step") or ""))
    if type_ == "agent.resume_delivering":
        return "info", "resume bridge: delivering wake-up"
    if type_ == "agent.resume_delivered":
        return "resumed", clip("resume bridge: delivered" + (f" ({_f(p.get('protocol'))})" if p.get("protocol") else ""))
    if type_ in ("agent.resume_failed", "agent.wait_error"):
        return "error", clip(f"{type_}: {_f(p.get('error') or p.get('predicate') or '')}")
    return "info", clip(type_)


def normalize_event(ev: dict, *, replay: bool = False) -> dict | None:
    """One event_log row ``{id, ts_us, actor, type, payload_json}`` -> FeedEvent, or None.

    A recorded jsonl line is passed as ``{"id": "rec:<asid>:<line>", "ts_us", "actor",
    "type": "droid.<kind>", "payload": <DroidEvent dict>}``.
    """
    type_ = str(ev.get("type") or "")
    actor = str(ev.get("actor") or "")
    p = ev.get("payload")
    if p is None:
        p = _json(ev.get("payload_json"))
    if not isinstance(p, dict):
        p = {}
    rid = ev.get("id")
    eid = rid if isinstance(rid, str) else f"ev:{rid}"
    ts = _int(ev.get("ts_us")) or 0
    base = {"id": eid, "ts_us": ts, "type": type_, "actor": actor, "tool": None, "turn": None,
            "ticket_id": _int(p.get("ticket_id")) or None, "is_error": False, "replay": replay}

    if type_.startswith("droid."):
        dk = type_[len("droid."):]
        sid = _int(p.get("agent_session_id"))
        if sid is None:
            return None
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        if dk == "turn_completed":
            st = data.get("status")
            kind = {"done": "done", "failed": "failed"}.get(st, "info")
        else:
            kind = DROID_KIND.get(dk)
        if kind is None:
            return None
        is_err = kind in ("error", "failed") or bool(dk == "tool_result" and data.get("is_error"))
        return {**base, "agent_key": f"session:{sid}", "kind": kind, "text": clip(p.get("text") or dk),
                "ts_us": _int(p.get("ts_us")) or ts,  # the droid's own clock keeps seq order
                "tool": p.get("tool"), "turn": _int(p.get("turn")), "is_error": is_err,
                "data": {**data, "droid_kind": dk, "role": p.get("role"), "seq": p.get("seq"),
                         "mode": p.get("mode"), "factory_session_id": p.get("factory_session_id")}}

    if type_.startswith("agent."):
        sid = _int(p.get("session_id"))
        if sid is None:
            return None
        kind, text = _agent_event_kind(type_, p)
        return {**base, "agent_key": f"session:{sid}", "kind": kind, "text": text,
                "is_error": kind in ("error", "failed"), "data": p}

    svc = service_for(actor, type_)
    if svc is None:
        return None
    err = _is_error_type(type_)
    return {**base, "agent_key": f"service:{svc}", "kind": "error" if err else "step",
            "text": service_text(type_, p), "is_error": err, "data": p}


def _empty_usage() -> dict:
    return {"credits_total": 0.0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}


def _session_view(s: dict, replay: bool) -> dict:
    sid = int(s["id"])
    role = str(s.get("role") or "")
    ids = _json(s.get("factory_session_ids_json"))
    return {
        "key": f"session:{sid}", "kind": "droid" if is_droid_role(role) else "session",
        "label": session_label(role, sid, str(s.get("name") or "")), "role": role,
        "agent_session_id": sid, "ticket_id": _int(s.get("ticket_id")) or None,
        "status": str(s.get("status") or "unknown"), "current_step": str(s.get("current_step") or ""),
        "last_update_us": _int(s.get("last_update_us")) or 0,
        "factory_session_id": s.get("factory_session_id") or None,
        "factory_session_ids": ids if isinstance(ids, list) else [],
        "waiting_for": _json(s.get("waiting_for_json")),
        "usage": None, "replay": replay, "event_count": 0, "events": [],
    }


def _service_view(key: str, replay: bool) -> dict:
    return {
        "key": f"service:{key}", "kind": "service", "label": SERVICE_LABELS.get(key, key), "role": key,
        "agent_session_id": None, "ticket_id": None, "status": "idle", "current_step": "",
        "last_update_us": 0, "factory_session_id": None, "factory_session_ids": [], "waiting_for": None,
        "usage": None, "replay": replay, "event_count": 0, "events": [],
    }


def _order_key(e: dict) -> tuple:
    eid = e["id"]
    tail = eid.rsplit(":", 1)[-1]
    return (e["ts_us"], int(tail) if tail.isdigit() else 0, eid)


def build_feed(sessions: Iterable[dict], events: Iterable[dict], *, ticket_id: int | None = None,
               limit: int = DEFAULT_LIMIT, replay: bool = False, now_us: int | None = None) -> dict:
    """``agent_session`` rows (snake_case, ``last_update_us`` as int) + event_log rows -> AgentFeed.

    Agents sort droids first, then other sessions, then services; within a group, newest
    activity first. ``events`` per agent are oldest -> newest, the last ``limit``.
    """
    now = now_us if now_us is not None else time.time_ns() // 1000
    agents: dict[str, dict] = {}
    for s in sessions:
        v = _session_view(s, replay)
        agents[v["key"]] = v

    per: dict[str, list[dict]] = {}
    for raw in events:
        e = normalize_event(raw, replay=replay)
        if e is not None:
            per.setdefault(e["agent_key"], []).append(e)

    for key, evs in per.items():
        evs.sort(key=_order_key)
        if key not in agents:
            if key.startswith("service:"):
                agents[key] = _service_view(key.split(":", 1)[1], replay)
            else:  # events for a session row we cannot see (deleted, or a replay without the row)
                sid = int(key.split(":", 1)[1])
                first = next((x for x in evs if x["type"].startswith("droid.")), None)
                role = str((first["data"].get("role") if first else None) or "")
                agents[key] = _session_view({"id": sid, "role": role, "status": "unknown"}, replay)
        a = agents[key]
        if a["kind"] == "droid" or any(x["type"].startswith("droid.") for x in evs):
            evs = [x for x in evs if x["type"] not in DROID_DUPLICATES]
        if ticket_id is not None and a["kind"] == "service":
            evs = [x for x in evs if x["ticket_id"] == ticket_id]
        per[key] = evs

    for key, a in agents.items():
        evs = per.get(key, [])
        usage = _empty_usage()
        has_usage = False
        credit_sum = 0.0
        for x in evs:
            if x["type"] == "droid.turn_completed":
                d = x["data"]
                has_usage = True
                usage["turns"] += 1
                credit_sum += _num(d.get("credits"))
                usage["credits_total"] = max(usage["credits_total"], _num(d.get("credits_total")), credit_sum)
                usage["input_tokens"] += int(_num(d.get("input_tokens") or (d.get("tokens") or {}).get("input_tokens")))
                usage["output_tokens"] += int(_num(d.get("output_tokens") or (d.get("tokens") or {}).get("output_tokens")))
                usage["seconds"] += _num(d.get("seconds"))  # unrounded: same float sum in the TS mirror
            if a["kind"] != "service" and a["ticket_id"] is None and x["ticket_id"]:
                a["ticket_id"] = x["ticket_id"]
        a["usage"] = usage if has_usage else None
        a["event_count"] = len(evs)
        a["events"] = evs[-limit:] if limit > 0 else []
        if evs:
            last = evs[-1]
            a["last_update_us"] = max(a["last_update_us"], last["ts_us"])
            if a["kind"] == "service":
                a["current_step"] = last["text"]
                a["status"] = ("error" if last["is_error"]
                               else "active" if now - last["ts_us"] <= SERVICE_ACTIVE_US else "idle")
            elif a["status"] == "unknown":  # replay or orphaned session: infer from the stream
                a["status"] = _infer_status(evs)
                a["current_step"] = a["current_step"] or last["text"]

    out = list(agents.values())
    if ticket_id is not None:
        out = [a for a in out if (a["event_count"] > 0 if a["kind"] == "service" else a["ticket_id"] == ticket_id)]
    group = {"droid": 0, "session": 1, "service": 2}
    out.sort(key=lambda a: (group[a["kind"]], -a["last_update_us"], a["key"]))
    return {"v": FEED_VERSION, "replay": replay, "generated_at_us": now, "agents": out}


def _infer_status(evs: list[dict]) -> str:
    for x in reversed(evs):
        if x["type"] == "droid.turn_completed":
            return str(x["data"].get("status") or "paused")
        if x["kind"] in ("done", "failed", "waiting"):
            return {"waiting": "waiting_for"}.get(x["kind"], x["kind"])
    return "running"


# ----------------------------------------------------------------------------- replay (recorded runs)

REPLAY_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "tdm35_replay"


def load_recorded_events(paths: Iterable[Path]) -> tuple[list[dict], list[dict]]:
    """TDM-30 ``<agent_session_id>.events.jsonl`` files -> (pseudo sessions, event rows).

    Every DroidEvent line becomes an event row typed ``droid.<kind>`` (thinking/state lines are
    dropped by ``normalize_event``; usage lines carry no extra totals beyond turn_completed).
    The pseudo session row is rebuilt from the stream so the replayed agent has a role and label.
    """
    sessions: dict[int, dict] = {}
    rows: list[dict] = []
    for path in sorted(paths):
        for i, line in enumerate(Path(path).read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = _int(d.get("agent_session_id"))
            if sid is None or not d.get("kind"):
                continue
            role = str(d.get("role") or "")
            s = sessions.setdefault(sid, {"id": sid, "role": role, "name": "", "status": "unknown",
                                          "current_step": "", "factory_session_ids_json": "[]",
                                          "last_update_us": 0, "ticket_id": 0})
            fsid = d.get("factory_session_id")
            ids = json.loads(s["factory_session_ids_json"])
            if fsid and fsid not in ids:
                ids.append(fsid)
                s["factory_session_ids_json"] = json.dumps(ids)
                s["factory_session_id"] = fsid
            actor = "droid:" + (role[: -len("_droid")] if is_droid_role(role) else role)
            rows.append({"id": f"rec:{sid}:{i}", "ts_us": d.get("ts_us"), "actor": actor,
                         "type": f"droid.{d['kind']}", "payload": d})
    return list(sessions.values()), rows


def replay_feed(directory: Path | None = None, *, limit: int = DEFAULT_LIMIT,
                now_us: int | None = None) -> dict:
    """The recorded feed. Raises FileNotFoundError when no recording exists."""
    d = Path(directory) if directory else REPLAY_DIR
    paths = sorted(d.glob("*.events.jsonl")) if d.is_dir() else []
    if not paths:
        raise FileNotFoundError(f"no *.events.jsonl recordings in {d}")
    sessions, rows = load_recorded_events(paths)
    feed = build_feed(sessions, rows, limit=limit, replay=True, now_us=now_us)
    feed["replay_source"] = [p.name for p in paths]
    return feed
