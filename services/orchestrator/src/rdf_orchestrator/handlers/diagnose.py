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
eval run is diagnosed, and only while the ticket is still open.

Failures are retried, bounded and restart-safe (``llm_retry.py``): each failed attempt logs one
``diagnose.error`` {eval_run_id, attempt, kind, max_attempts, retry_in_s}; the next attempt runs after
a backoff (a refusal is retried text-only with a context preface). The count comes from those
event_log rows, so a restart carries on instead of starting over. After ``RDF_DIAGNOSE_MAX_ATTEMPTS``
(default 3) failures one ``diagnose.failed`` is logged and nothing else is tried: the flywheel fails
the ticket with that reason (``flywheel.decide``, stage diagnose).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import diagnose as dx
from .. import llm_retry
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("diagnose")

TERMINAL_TICKET = {"shipped", "failed", "cancelled"}

for _t in ("eval_trial", "episode", "ticket", "event_log"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()  # eval runs being diagnosed, or waiting on a scheduled retry
_attempts = llm_retry.Attempts()
RETRY = llm_retry.RetryPolicy.from_env("DIAGNOSE")

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


def _for_run(rid: int):
    return lambda p: p.get("eval_run_id") == rid


def gave_up(event_log_rows, eval_run_id: int) -> dict | None:
    return llm_retry.find_event(event_log_rows, dx.EVENT_FAILED, _for_run(eval_run_id))


def failures(bus, eval_run_id: int) -> list[str]:
    """Kinds of the failed attempts so far (event_log, or this process while the mirror catches up)."""
    logged = llm_retry.prior_failures(bus.rows("event_log"), dx.EVENT_ERROR, _for_run(eval_run_id))
    return _attempts.kinds(eval_run_id, logged)


def _skip_reason(bus, run, source: str) -> str | None:
    if already_diagnosed(bus.rows("event_log"), run["id"]):
        return "already diagnosed"
    if gave_up(bus.rows("event_log"), run["id"]):
        return "gave up (diagnose.failed logged)"
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
    kinds = failures(bus, rid)
    stdb = make_stdb()
    if RETRY.exhausted(len(kinds)):  # the last attempt failed but its diagnose.failed never landed
        try:
            _give_up(stdb, bus, run, kinds, "retries exhausted before this boot")
        finally:
            _close(stdb)
        return
    try:
        reduced = llm_retry.REFUSAL in kinds
        inputs = inputs_from_bus(bus, run)
        log.info("diagnose.start", extra={"fields": {
            "eval_run_id": rid, "trials": len(inputs.trials), "failures": len(inputs.failed),
            "attempt": len(kinds) + 1, "reduced_prompt": reduced, "source": source}})
        diag = dx.diagnose_eval_run(inputs, claude=make_clusterer(), frames=None if reduced else make_frames(),
                                    reduced=reduced)
        payload = dx.record_diagnosis(diag, stdb)
        _attempts.clear(rid)
        log.info("diagnose.done", extra={"fields": {
            "eval_run_id": rid, "clusters": [(c["label"], c["count"]) for c in payload["clusters"]],
            "frames_sent": payload["media"]["frames_sent"], "repairs": payload["repairs"]}})
    except Exception as e:
        kinds = kinds + [llm_retry.classify(e)]
        _attempts.record(rid, kinds)
        fields = llm_retry.error_fields(e, kinds, RETRY)
        try:
            dx.log_error(stdb, run.get("ticket_id"), rid, fields.pop("error"), **fields)
        except Exception:  # noqa: BLE001
            pass
        if RETRY.exhausted(len(kinds)):
            _give_up(stdb, bus, run, kinds, f"{type(e).__name__}: {e}")  # rid stays claimed: no more tries
        else:
            log.warning("diagnose.retry_scheduled", extra={"fields": {
                "eval_run_id": rid, "attempt": len(kinds), "kind": kinds[-1], "retry_in_s": fields["retry_in_s"]}})
            llm_retry.schedule(fields["retry_in_s"], lambda: _retry(bus, rid), name=f"diagnose-{rid}")
        raise
    finally:
        _close(stdb)


def _close(stdb) -> None:
    close = getattr(stdb, "close", None)
    if close:
        close()


def _give_up(stdb, bus, run, kinds: list[str], last_error: str) -> None:
    rid = int(run["id"])
    ticket = bus.get("ticket", run.get("ticket_id")) or {}
    fields = llm_retry.failed_fields(kinds, last_error, RETRY,
                                     "no failure clusters for this eval run; the flywheel fails the ticket")
    log.error("diagnose.failed", extra={"fields": {"eval_run_id": rid, **fields}})
    try:
        dx.log_failed(stdb, run.get("ticket_id"), rid, iteration=ticket.get("iteration"),
                      eval_kind=run.get("kind"), **fields)
    except Exception:  # noqa: BLE001 - the next boot sees the exhausted count and logs it again
        with _lock:
            _claimed.discard(rid)
        raise


def _retry(bus, rid: int) -> None:
    """Timer callback: release the claim and try again from current rows."""
    with _lock:
        _claimed.discard(rid)
    run = bus.get("eval_run", rid)
    if run is None or run.get("status") != "done":
        return
    if run.get("kind") != "baseline":
        ticket = bus.get("ticket", run["ticket_id"])
        if ticket is None or ticket.get("status") != "diagnose":
            return  # it moved on (a human advanced it, or it was failed); the next entry re-triggers
    _diagnose(bus, run, "retry")
