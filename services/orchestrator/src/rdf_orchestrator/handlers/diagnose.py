"""TDM-23: when an eval run is finalized (baseline or candidate), cluster its failures with Claude.

``eval_run`` entering ``status == 'done'`` (set only by ``finalize_eval_run``) -> gather the failed
``eval_trial`` rows + their episodes from the bus mirror -> ``diagnose.diagnose_eval_run`` (Claude,
vision, structured output) -> ``record_failure_cluster`` per cluster + one ``diagnose.clusters``
event_log row with the full payload (shape in ``rdf_orchestrator/diagnose.py``). 0 failures ->
no clusters, a ``diagnose.no_failures`` event.

Iteration: ``record_failure_cluster`` stamps ``ticket.iteration``, and leaving ``eval`` for
``diagnose`` is what bumps it. So a *baseline* run is diagnosed as soon as it is finalized, but a
*candidate* run waits until the ticket enters ``diagnose`` (second trigger below); that keeps its
clusters on the new iteration the planner reads, and a candidate that ships is never diagnosed.

Idempotent across restarts / resyncs: an eval run that already has a ``diagnose.clusters`` /
``diagnose.no_failures`` event is skipped, and on a boot snapshot only the ticket's newest finished
eval run is diagnosed, and only while the ticket is still open. A failure logs ``diagnose.error``
(it is retried on the next boot, not in a loop).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import diagnose as dx
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("diagnose")

TERMINAL_TICKET = {"shipped", "failed", "cancelled"}

for _t in ("eval_trial", "episode", "ticket", "event_log"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()

# Swappable for tests: build the Claude clusterer, the frame source and the reducer client.
make_clusterer: Callable[[], dx.Clusterer] = lambda: dx.ClaudeClusterer()  # noqa: E731
make_frames: Callable[[], dx.FrameSource | None] = lambda: dx.EpisodeFrameSource()  # noqa: E731


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


make_stdb: Callable[[], Any] = _default_stdb


def already_diagnosed(event_log_rows, eval_run_id: int) -> bool:
    for r in event_log_rows:
        if r.get("type") not in (dx.EVENT_CLUSTERS, dx.EVENT_NO_FAILURES):
            continue
        try:
            if json.loads(r.get("payload_json") or "{}").get("eval_run_id") == eval_run_id:
                return True
        except (ValueError, AttributeError):
            continue
    return False


def inputs_from_bus(bus, eval_run) -> dx.DiagnoseInputs:
    rid = eval_run["id"]
    ticket = bus.get("ticket", eval_run["ticket_id"])
    if ticket is None:
        raise dx.DiagnoseError(f"eval run {rid}: ticket {eval_run['ticket_id']} not in mirror")
    trials = [t for t in bus.rows("eval_trial") if t.get("eval_run_id") == rid]
    want = {int(t.get("episode_id") or 0) for t in trials if not t.get("success")} - {0}
    episodes = {e: bus.get("episode", e) for e in want if bus.get("episode", e) is not None}
    return dx.DiagnoseInputs(dict(ticket), dict(eval_run), [dict(t) for t in trials], {k: dict(v) for k, v in episodes.items()})


def _skip_reason(bus, run, source: str) -> str | None:
    if already_diagnosed(bus.rows("event_log"), run["id"]):
        return "already diagnosed"
    if source == "live":
        return None
    # Boot snapshot / resync: only pick up in-flight work, not history.
    ticket = bus.get("ticket", run["ticket_id"])
    if ticket is None or ticket.get("status") in TERMINAL_TICKET:
        return "ticket closed"
    newer = [r for r in bus.rows("eval_run") if r.get("ticket_id") == run["ticket_id"]
             and r.get("status") == "done" and (r.get("finished_at") or 0) > (run.get("finished_at") or 0)]
    return "newer eval run exists" if newer else None


def _newest_done_run(bus, ticket_id):
    runs = [r for r in bus.rows("eval_run") if r.get("ticket_id") == ticket_id and r.get("status") == "done"]
    return max(runs, key=lambda r: (r.get("finished_at") or 0, r["id"]), default=None)


@on_enter("eval_run", where=lambda r: r.status == "done", name="diagnose_failures")
def diagnose_failures(ev: Event) -> None:
    run = ev.row
    if run.get("kind") != "baseline":
        ticket = ev.bus.get("ticket", run["ticket_id"])
        if ticket is None or ticket.get("status") != "diagnose":
            log.info("diagnose.deferred", extra={"fields": {
                "eval_run_id": run["id"], "kind": run.get("kind"), "reason": "waits for ticket status diagnose"}})
            return
    _diagnose(ev.bus, run, ev.source)


@on_enter("ticket", where=lambda r: r.status == "diagnose", name="diagnose_on_ticket_diagnose")
def diagnose_on_ticket_diagnose(ev: Event) -> None:
    run = _newest_done_run(ev.bus, ev.row["id"])
    if run is None:
        log.info("diagnose.skip", extra={"fields": {"ticket_id": ev.row["id"], "reason": "no finished eval run"}})
        return
    _diagnose(ev.bus, run, "live")  # the ticket is open and this is its newest run: no history guard needed


def _diagnose(bus, run, source: str) -> None:
    rid = int(run["id"])
    with _lock:
        if rid in _claimed:
            return
        why = _skip_reason(bus, run, source)
        if why:
            log.info("diagnose.skip", extra={"fields": {"eval_run_id": rid, "reason": why, "source": source}})
            return
        _claimed.add(rid)
    stdb = make_stdb()
    try:
        inputs = inputs_from_bus(bus, run)
        log.info("diagnose.start", extra={"fields": {
            "eval_run_id": rid, "trials": len(inputs.trials), "failures": len(inputs.failed)}})
        diag = dx.diagnose_eval_run(inputs, claude=make_clusterer(), frames=make_frames())
        payload = dx.record_diagnosis(diag, stdb)
        log.info("diagnose.done", extra={"fields": {
            "eval_run_id": rid, "clusters": [(c["label"], c["count"]) for c in payload["clusters"]],
            "frames_sent": payload["media"]["frames_sent"], "repairs": payload["repairs"]}})
    except Exception as e:
        with _lock:
            _claimed.discard(rid)
        try:
            dx.log_error(stdb, run.get("ticket_id"), rid, f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        close = getattr(stdb, "close", None)
        if close:
            close()
