"""TDM-93: LLM QA for purchased episodes, run once TDM-92's deterministic QA has passed.

Trigger: a ``qa_result`` row ``deterministic_qa`` (config ``gate.summary_check``) with ``passed=true``
is inserted. TDM-92 writes it LAST, after its per-check rows. ``llm_qa.run_after_deterministic`` then
re-reads the gate from SQL (purchased, pending, no review, no failing latest check), checks the
per-offer call cap, makes one Claude call and writes llm_relevance + llm_success + ``submit_review``.

Idempotent across restarts and resyncs. ``include_existing`` re-fires for summary rows already in the
DB at boot, and the gate skips episodes that already have a review. An in-process claim set stops two
pool workers taking the same episode, and ``submit_review`` refuses a second review. An outage writes
only a ``qa.error`` event: the episode stays pending.

Retries are bounded and restart-safe (``llm_retry.py``): the attempt count is the episode's
``qa.error`` rows (stage llm_qa), the next attempt runs after a backoff (after a refusal with a context
preface), and after ``RDF_LLM_QA_MAX_ATTEMPTS`` (default 3) one ``qa.failed`` is logged and nothing
else is written: an outage never becomes a rejection. ``append_event qa.retry_requested
{episode_id}`` restarts the count. Failed calls count toward the per-offer call cap, also from
event_log (it used to be an in-process counter that a restart forgot).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .. import llm_qa as lq
from .. import llm_retry
from ..bus import Event, on_insert, watch
from ..log import get_logger

log = get_logger("llm_qa")

for _t in ("ticket", "episode", "qa_result", "event_log"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()  # episodes being judged, or waiting on a scheduled retry
_attempts = llm_retry.Attempts()
RETRY = llm_retry.RetryPolicy.from_env("LLM_QA")
_config: lq.LlmQaConfig | None = None


def _cfg() -> lq.LlmQaConfig:
    global _config
    if _config is None:
        _config = lq.load_config()
    return _config


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


# Swappable for tests and the proof script.
make_config: Callable[[], lq.LlmQaConfig] = _cfg
make_judge: Callable[[lq.LlmQaConfig], lq.Judge] = lambda cfg: lq.ClaudeLlmQaJudge(cfg)  # noqa: E731
make_stdb: Callable[[], Any] = _default_stdb


def _is_gate_row(r) -> bool:
    return r.check_name == make_config().summary_check and bool(r.passed)


def _llm_errors(p: dict) -> bool:
    return p.get("stage") == "llm_qa"  # TDM-92's deterministic qa.error rows have no stage


def failures(bus, eid: int) -> list[str]:
    logged = llm_retry.prior_failures(bus.rows("event_log"), lq.EVENT_ERROR,
                                      lambda p: _llm_errors(p) and int(p.get("episode_id") or 0) == eid,
                                      lq.EVENT_RETRY_REQUESTED)
    return _attempts.kinds(eid, logged)


def gave_up(bus, eid: int) -> dict | None:
    return llm_retry.find_event(bus.rows("event_log"), lq.EVENT_FAILED,
                                lambda p: int(p.get("episode_id") or 0) == eid, lq.EVENT_RETRY_REQUESTED)


def failed_calls(bus, offer_id: int) -> int:
    """Failed LLM QA attempts on this offer's episodes (all time): they count toward the call cap."""
    if not offer_id:
        return 0
    n = 0
    for r in bus.rows("event_log"):
        if r.get("type") == lq.EVENT_ERROR:
            p = llm_retry.payload(r)
            n += _llm_errors(p) and int(p.get("dataset_offer_id") or 0) == offer_id
    return n


@on_insert("qa_result", where=_is_gate_row, include_existing=True, name="tdm93_llm_qa")
def llm_qa_after_deterministic(ev: Event) -> None:
    _run(ev.bus, int(ev.row["episode_id"]), ev.source)


@on_insert("event_log", where=lambda r: r.type == lq.EVENT_RETRY_REQUESTED, name="tdm93_llm_qa_retry_requested")
def llm_qa_retry_requested(ev: Event) -> None:
    """A human asked to try a given-up episode again: fresh count, immediate attempt."""
    eid = int(llm_retry.payload(ev.row).get("episode_id") or 0)
    if not eid:
        return
    _attempts.clear(eid)
    with _lock:
        _claimed.discard(eid)  # held since the give-up
    _run(ev.bus, eid, "retry_requested")


def _run(bus, eid: int, source: str) -> None:
    ep = bus.get("episode", eid)
    if ep is not None and (ep.get("kind") != "purchased" or ep.get("review_status") != "pending"):
        return  # cheap pre-filter from the mirror; run_after_deterministic re-checks from SQL
    with _lock:
        if eid in _claimed:
            return
        if source != "retry_requested" and gave_up(bus, eid):
            log.info("llm_qa.skip", extra={"fields": {"episode_id": eid, "reason": "gave up (qa.failed); "
                                                      "append qa.retry_requested to try again"}})
            return
        _claimed.add(eid)
    kinds = [] if source == "retry_requested" else failures(bus, eid)
    stdb = make_stdb()
    offer_id = int((ep or {}).get("dataset_offer_id") or 0)
    ep = ep or {"id": eid}
    if RETRY.exhausted(len(kinds)):  # the last attempt failed but its qa.failed never landed
        try:
            _give_up(stdb, ep, kinds, "retries exhausted before this boot")
        finally:
            _close(stdb)
        return
    try:
        cfg = make_config()
        ticket = bus.get("ticket", ep.get("ticket_id")) if ep.get("ticket_id") else None
        if ticket is None and ep.get("ticket_id"):
            rows = stdb.sql(f"SELECT * FROM ticket WHERE id = {int(ep['ticket_id'])}")
            ticket = rows[0] if rows else {"id": ep["ticket_id"]}
        log.info("llm_qa.start", extra={"fields": {"episode_id": eid, "offer_id": offer_id, "source": source,
                                                   "attempt": len(kinds) + 1}})
        judge = make_judge(cfg)
        if llm_retry.REFUSAL in kinds:
            judge = llm_retry.PrefacedJudge(judge)
        d = lq.run_after_deterministic(stdb, ep, dict(ticket or {}), cfg=cfg, judge=judge,
                                       extra_calls=failed_calls(bus, offer_id), log_errors=False)
        _attempts.clear(eid)
        if d is not None:
            log.info("llm_qa.done", extra={"fields": {"episode_id": eid, "accepted": d.accepted,
                                                      "reason": d.reason[:300]}})
    except Exception as e:
        if "already reviewed" in str(e):  # another orchestrator won; reviews are final
            log.info("llm_qa.skip", extra={"fields": {"episode_id": eid, "reason": str(e)[:200]}})
            return
        kinds = kinds + [llm_retry.classify(e)]
        _attempts.record(eid, kinds)
        fields = llm_retry.error_fields(e, kinds, RETRY)
        try:
            lq.log_error(stdb, ep, fields.pop("error"), **fields)
        except Exception:  # noqa: BLE001
            pass
        if RETRY.exhausted(len(kinds)):
            _give_up(stdb, ep, kinds, f"{type(e).__name__}: {e}")
        else:
            log.warning("llm_qa.retry_scheduled", extra={"fields": {
                "episode_id": eid, "attempt": len(kinds), "kind": kinds[-1], "retry_in_s": fields["retry_in_s"]}})
            llm_retry.schedule(fields["retry_in_s"], lambda: _retry(bus, eid), name=f"llm_qa-{eid}")
        raise
    finally:
        _close(stdb)


def _close(stdb) -> None:
    close = getattr(stdb, "close", None)
    if close:
        close()


def _give_up(stdb, ep, kinds: list[str], last_error: str) -> None:
    eid = int(ep["id"])
    fields = llm_retry.failed_fields(kinds, last_error, RETRY,
                                     "no review written (an outage is never a rejection); the episode stays "
                                     "pending for a human: append qa.retry_requested {episode_id} to try again")
    log.error("llm_qa.failed", extra={"fields": {"episode_id": eid, **fields}})
    try:
        lq.log_failed(stdb, ep, **fields)  # eid stays claimed: only a retry request runs it again
    except Exception:  # noqa: BLE001 - the next trigger sees the exhausted count and logs it again
        with _lock:
            _claimed.discard(eid)
        raise


def _retry(bus, eid: int) -> None:
    """Timer callback: release the claim and try again (the gate is re-read from SQL)."""
    with _lock:
        _claimed.discard(eid)
    _run(bus, eid, "retry")
