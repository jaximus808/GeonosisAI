"""TDM-25: review every training-data episode as it lands.

``episode`` entering ``review_status == 'pending'`` with kind clean_teleop / recovery_takeover
(purchased episodes go through dataset QA, TDM-92 + TDM-93; baseline and eval rollouts are created ``not_reviewed`` and never come here) ->
``reviewer.review_episode`` (deterministic checks, then Claude) -> ``record_review`` (qa_result rows +
``submit_review``; an accept reserves the payout in the same reducer).

Idempotent across restarts / resyncs: ``include_existing`` picks up episodes still pending at boot; an
episode that already has a ``review`` row (bus mirror) is skipped, an in-process claim set stops two
pool workers taking the same episode, and ``submit_review`` itself refuses a second review. A
``ReviewError`` (storage / Claude outage, refusal) writes nothing but a ``review.error`` event; the
episode stays pending.

Retries are bounded and restart-safe (``llm_retry.py``): the attempt count comes from the episode's
``review.error`` rows, the next attempt runs after a backoff (after a refusal with a context preface),
and after ``RDF_REVIEW_MAX_ATTEMPTS`` (default 3) one ``review.failed`` is logged. Nothing else is
written, ever: an outage never becomes a rejection. The episode waits for a human; an
``append_event review.retry_requested {episode_id}`` restarts the count (watched here).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .. import llm_retry
from .. import reviewer as rv
from ..bus import Event, on_enter, on_insert, watch
from ..log import get_logger

log = get_logger("reviewer")

for _t in ("ticket", "review", "event_log"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()  # episodes being reviewed, or waiting on a scheduled retry
_attempts = llm_retry.Attempts()
RETRY = llm_retry.RetryPolicy.from_env("REVIEW")
_config: rv.ReviewConfig | None = None


def _cfg() -> rv.ReviewConfig:
    global _config
    if _config is None:
        _config = rv.load_config()
    return _config


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


# Swappable for tests.
make_config: Callable[[], rv.ReviewConfig] = _cfg
make_judge: Callable[[rv.ReviewConfig], rv.Judge | None] = lambda cfg: rv.ClaudeJudge(cfg) if cfg.claude_enabled else None  # noqa: E731
make_stdb: Callable[[], Any] = _default_stdb


# Purchased (dataset-offer) episodes are reviewed by the dataset QA pipeline instead: TDM-92's
# deterministic checks, then TDM-93's LLM judgement (handlers/llm_qa.py), which writes the review.
HANDLER_KINDS = tuple(k for k in rv.REVIEW_KINDS if k != "purchased")


def already_reviewed(bus, episode_id: int) -> bool:
    return any(int(r.get("episode_id") or 0) == episode_id for r in bus.rows("review"))


def _for(eid: int):
    return lambda p: int(p.get("episode_id") or 0) == eid


def failures(bus, eid: int) -> list[str]:
    logged = llm_retry.prior_failures(bus.rows("event_log"), rv.EVENT_ERROR, _for(eid), rv.EVENT_RETRY_REQUESTED)
    return _attempts.kinds(eid, logged)


def gave_up(bus, eid: int) -> dict | None:
    return llm_retry.find_event(bus.rows("event_log"), rv.EVENT_FAILED, _for(eid), rv.EVENT_RETRY_REQUESTED)


@on_enter("episode", where=lambda r: r.review_status == "pending" and r.kind in HANDLER_KINDS,
          name="tdm25_review_episode")
def review_episode(ev: Event) -> None:
    _review(ev.bus, ev.row, ev.source)


@on_insert("event_log", where=lambda r: r.type == rv.EVENT_RETRY_REQUESTED, name="tdm25_review_retry_requested")
def review_retry_requested(ev: Event) -> None:
    """A human asked to try a given-up episode again: fresh count, immediate attempt."""
    eid = int(llm_retry.payload(ev.row).get("episode_id") or 0)
    ep = ev.bus.get("episode", eid) if eid else None
    if ep is None or ep.get("review_status") != "pending" or ep.get("kind") not in HANDLER_KINDS:
        return
    _attempts.clear(eid)
    with _lock:
        _claimed.discard(eid)  # held since the give-up
    _review(ev.bus, ep, "retry_requested")


def _review(bus, ep, source: str) -> None:
    eid = int(ep["id"])
    with _lock:
        if eid in _claimed:
            return
        if already_reviewed(bus, eid):
            log.info("review.skip", extra={"fields": {"episode_id": eid, "reason": "already reviewed"}})
            return
        if source != "retry_requested" and gave_up(bus, eid):
            log.info("review.skip", extra={"fields": {"episode_id": eid, "reason": "gave up (review.failed); "
                                                      "append review.retry_requested to try again"}})
            return
        _claimed.add(eid)
    kinds = [] if source == "retry_requested" else failures(bus, eid)
    stdb = make_stdb()
    if RETRY.exhausted(len(kinds)):  # the last attempt failed but its review.failed never landed
        try:
            _give_up(stdb, ep, kinds, "retries exhausted before this boot")
        finally:
            _close(stdb)
        return
    try:
        cfg = make_config()
        ticket = bus.get("ticket", ep["ticket_id"]) or {"id": ep["ticket_id"]}
        log.info("review.start", extra={"fields": {"episode_id": eid, "kind": ep.get("kind"), "source": source,
                                                   "attempt": len(kinds) + 1}})
        judge = make_judge(cfg)
        if judge is not None and llm_retry.REFUSAL in kinds:
            judge = llm_retry.PrefacedJudge(judge)
        from ..robot_mode import robot_mode  # TDM-107: label the review sim / real from the episode's robot

        ep_row = {**dict(ep), "robot_mode": robot_mode(bus.get("robot", ep.get("robot_id")))}
        decision = rv.review_episode(ep_row, dict(ticket), config=cfg, judge=judge)
        rv.record_review(stdb, decision)
        _attempts.clear(eid)
        log.info("review.done", extra={"fields": {
            "episode_id": eid, "accepted": decision.accepted, "decided_by": decision.decided_by,
            "reason": decision.reason[:300]}})
    except Exception as e:
        if "already reviewed" in str(e):  # another orchestrator won the race; reviews are final
            log.info("review.skip", extra={"fields": {"episode_id": eid, "reason": str(e)[:200]}})
            return
        kinds = kinds + [llm_retry.classify(e)]
        _attempts.record(eid, kinds)
        fields = llm_retry.error_fields(e, kinds, RETRY)
        try:
            rv.log_error(stdb, ep, fields.pop("error"), **fields)
        except Exception:  # noqa: BLE001
            pass
        if RETRY.exhausted(len(kinds)):
            _give_up(stdb, ep, kinds, f"{type(e).__name__}: {e}")
        else:
            log.warning("review.retry_scheduled", extra={"fields": {
                "episode_id": eid, "attempt": len(kinds), "kind": kinds[-1], "retry_in_s": fields["retry_in_s"]}})
            llm_retry.schedule(fields["retry_in_s"], lambda: _retry(bus, eid), name=f"review-{eid}")
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
                                     "pending for a human: append review.retry_requested {episode_id} to try again")
    log.error("review.failed", extra={"fields": {"episode_id": eid, **fields}})
    try:
        rv.log_failed(stdb, ep, **fields)  # eid stays claimed: only a retry request runs it again
    except Exception:  # noqa: BLE001 - the next trigger sees the exhausted count and logs it again
        with _lock:
            _claimed.discard(eid)
        raise


def _retry(bus, eid: int) -> None:
    """Timer callback: release the claim and try again if the episode is still pending."""
    with _lock:
        _claimed.discard(eid)
    ep = bus.get("episode", eid)
    if ep is None or ep.get("review_status") != "pending":
        return
    _review(bus, ep, "retry")
