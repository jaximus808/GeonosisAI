"""TDM-25: review every training-data episode as it lands.

``episode`` entering ``review_status == 'pending'`` with kind clean_teleop / recovery_takeover
(purchased episodes go through dataset QA, TDM-92 + TDM-93; baseline and eval rollouts are created ``not_reviewed`` and never come here) ->
``reviewer.review_episode`` (deterministic checks, then Claude) -> ``record_review`` (qa_result rows +
``submit_review``; an accept reserves the payout in the same reducer).

Idempotent across restarts / resyncs: ``include_existing`` picks up episodes still pending at boot; an
episode that already has a ``review`` row (bus mirror) is skipped, an in-process claim set stops two
pool workers taking the same episode, and ``submit_review`` itself refuses a second review. A
``ReviewError`` (storage / Claude outage) writes nothing but a ``review.error`` event; the episode stays
pending and is retried on the next boot.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .. import reviewer as rv
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("reviewer")

for _t in ("ticket", "review"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()
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


@on_enter("episode", where=lambda r: r.review_status == "pending" and r.kind in HANDLER_KINDS,
          name="tdm25_review_episode")
def review_episode(ev: Event) -> None:
    ep = ev.row
    eid = int(ep["id"])
    with _lock:
        if eid in _claimed:
            return
        if already_reviewed(ev.bus, eid):
            log.info("review.skip", extra={"fields": {"episode_id": eid, "reason": "already reviewed"}})
            return
        _claimed.add(eid)
    stdb = make_stdb()
    try:
        cfg = make_config()
        ticket = ev.bus.get("ticket", ep["ticket_id"]) or {"id": ep["ticket_id"]}
        log.info("review.start", extra={"fields": {"episode_id": eid, "kind": ep.get("kind"), "source": ev.source}})
        decision = rv.review_episode(dict(ep), dict(ticket), config=cfg, judge=make_judge(cfg))
        rv.record_review(stdb, decision)
        log.info("review.done", extra={"fields": {
            "episode_id": eid, "accepted": decision.accepted, "decided_by": decision.decided_by,
            "reason": decision.reason[:300]}})
    except Exception as e:
        if "already reviewed" in str(e):  # another orchestrator won the race; reviews are final
            log.info("review.skip", extra={"fields": {"episode_id": eid, "reason": str(e)[:200]}})
            return
        with _lock:
            _claimed.discard(eid)
        try:
            rv.log_error(stdb, ep, f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        close = getattr(stdb, "close", None)
        if close:
            close()
