"""Bus wiring for the Claude trial judge (logic in ``..trial_judge``).

  event_log eval.trial_awaiting_outcome (live) on a REAL robot -> snapshots -> Claude -> trial_outcome
                                                                  command + eval.trial_judged

Idempotent: one judgment per (eval_run_id, index): an in-process claim set, plus an existing
``eval.trial_judged`` row or ``eval_trial`` row for that trial skips it. Boot snapshots / resyncs never
fire (``include_existing=False``, live only). ``RDF_EVAL_JUDGE=0`` turns it off.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import agent_steps as ag
from .. import trial_judge as tj
from ..bus import Event, on_insert, watch
from ..log import get_logger
from ..stream_api import robot_is_sim

log = get_logger("trial_judge")

for _t in ("event_log", "eval_run", "eval_trial", "robot", "ticket"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[tuple[int, int]] = set()


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


# Swappable for tests.
make_stdb: Callable[[], Any] = _default_stdb
fetch_frames: Callable[[dict], list] = tj.fetch_frames
ask: Callable[..., tj.Verdict] = tj.ask_claude


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def already_judged(bus, run_id: int, index: int) -> bool:
    for r in bus.rows("event_log"):
        if r.get("type") == tj.EVENT_JUDGED:
            p = tj._json(r.get("payload_json"))
            if _i(p.get("eval_run_id")) == run_id and _i(p.get("index")) == index:
                return True
    return any(_i(t.get("eval_run_id")) == run_id and _i(t.get("trial_index")) == index for t in bus.rows("eval_trial"))


def _event(stdb, type_: str, payload: dict) -> None:
    try:
        stdb.call_reducer("append_event", tj.ACTOR, type_, json.dumps(payload, default=str))
    except Exception as e:  # noqa: BLE001
        log.warning("eval_judge.event_failed", extra={"fields": {"type": type_, "error": str(e)[:300]}})


@on_insert("event_log", where=lambda r: r.type == tj.EVENT_AWAITING, include_existing=False, name="eval_trial_judge")
def judge_trial(ev: Event) -> None:
    if ev.source != "live" or not tj.enabled():
        return
    p = tj._json(ev.row.get("payload_json"))
    run_id, index = _i(p.get("eval_run_id")), _i(p.get("index"))
    if not run_id:
        return
    run = ev.bus.get("eval_run", run_id) or {}
    robot_id = _i(p.get("robot_id")) or _i(run.get("robot_id"))
    robot = ev.bus.get("robot", robot_id) if robot_id else None
    if robot is None:
        log.info("eval_judge.skip", extra={"fields": {"eval_run_id": run_id, "index": index, "reason": "no robot row"}})
        return
    if robot_is_sim(robot) or p.get("sim"):
        return  # [SIM] the sim judges itself from sim state
    key = (run_id, index)
    with _lock:
        if key in _claimed or already_judged(ev.bus, run_id, index):
            return
        _claimed.add(key)
    ticket_id = _i(p.get("ticket_id")) or _i(run.get("ticket_id"))
    ticket = ev.bus.get("ticket", ticket_id) if ticket_id else None
    task = str((ticket or {}).get("task_description") or "").strip()
    n = _i(p.get("trials")) or _i(run.get("trials"))
    trial = f"{index + 1}/{n}" if n else f"{index + 1}"
    station = "baseline" if run.get("kind") == "baseline" else "eval"
    base = {"eval_run_id": run_id, "index": index, "robot_id": robot_id, "ticket_id": ticket_id}
    stdb = make_stdb()
    try:
        with ag.work(ticket_id, station, eval_run_id=run_id, trial=index + 1):
            ag.step(f"Claude is judging trial {trial} from the cameras…", ticket_id=ticket_id, station=station,
                    eval_run_id=run_id, trial=index + 1)
            try:
                frames = fetch_frames(robot)
                verdict = ask(task, frames, trial=trial)
            except tj.JudgeError as e:
                log.warning("eval_judge.failed", extra={"fields": {**base, "stage": e.stage, "error": str(e)[:300]}})
                _event(stdb, tj.EVENT_FAILED, {**base, "stage": e.stage, "error": str(e)[:500],
                                               "note": "no verdict sent; the robot's outcome timeout still applies"})
                ag.step(f"Judge could not decide trial {trial}: {e.stage} failed", ticket_id=ticket_id,
                        station=station, eval_run_id=run_id, error=True)
                return
            cmd = {"success": verdict.success, "note": verdict.command_note(), "eval_run_id": run_id, "index": index}
            try:
                stdb.call_reducer("issue_robot_command", robot_id, "trial_outcome", json.dumps(cmd), ticket_id)
            except Exception as e:  # noqa: BLE001
                log.warning("eval_judge.command_failed", extra={"fields": {**base, "error": str(e)[:300]}})
                _event(stdb, tj.EVENT_FAILED, {**base, "stage": "command", "error": str(e)[:500],
                                               "success": verdict.success, "confidence": verdict.confidence,
                                               "judge_note": verdict.note})
                return
            _event(stdb, tj.EVENT_JUDGED, {**base, "success": verdict.success, "confidence": verdict.confidence,
                                           "note": verdict.note, "model": verdict.model, "frames": len(frames),
                                           "task": task[:300]})
            ag.thought(f"Judge: {'pass' if verdict.success else 'fail'} · {verdict.note}", ticket_id=ticket_id,
                       station=station, eval_run_id=run_id, trial=index + 1)
            log.info("eval_judge.judged", extra={"fields": {**base, "success": verdict.success,
                                                            "confidence": verdict.confidence, "note": verdict.note}})
    finally:
        close = getattr(stdb, "close", None)
        if close:
            close()
