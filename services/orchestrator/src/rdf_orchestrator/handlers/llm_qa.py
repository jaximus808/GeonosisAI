"""TDM-93: LLM QA for purchased episodes, run once TDM-92's deterministic QA has passed.

Trigger: a ``qa_result`` row ``deterministic_qa`` (config ``gate.summary_check``) with ``passed=true``
is inserted. TDM-92 writes it LAST, after its per-check rows. ``llm_qa.run_after_deterministic`` then
re-reads the gate from SQL (purchased, pending, no review, no failing latest check), checks the
per-offer call cap, makes one Claude call and writes llm_relevance + llm_success + ``submit_review``.

Idempotent across restarts and resyncs. ``include_existing`` re-fires for summary rows already in the
DB at boot, and the gate skips episodes that already have a review. An in-process claim set stops two
pool workers taking the same episode, and ``submit_review`` refuses a second review. An outage writes
only a ``qa.error`` event: the episode stays pending and is retried on the next boot.
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any, Callable

from .. import llm_qa as lq
from ..bus import Event, on_insert, watch
from ..log import get_logger

log = get_logger("llm_qa")

for _t in ("ticket", "episode", "qa_result"):
    watch(_t)

_lock = threading.Lock()
_claimed: set[int] = set()
_failed_calls: Counter[int] = Counter()  # offer_id -> failed attempts this process (count toward the cap)
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


@on_insert("qa_result", where=_is_gate_row, include_existing=True, name="tdm93_llm_qa")
def llm_qa_after_deterministic(ev: Event) -> None:
    eid = int(ev.row["episode_id"])
    ep = ev.bus.get("episode", eid)
    if ep is not None and (ep.get("kind") != "purchased" or ep.get("review_status") != "pending"):
        return  # cheap pre-filter from the mirror; run_after_deterministic re-checks from SQL
    with _lock:
        if eid in _claimed:
            return
        _claimed.add(eid)
    stdb = make_stdb()
    offer_id = int((ep or {}).get("dataset_offer_id") or 0)
    try:
        cfg = make_config()
        ep = ep or {"id": eid}
        ticket = ev.bus.get("ticket", ep.get("ticket_id")) if ep.get("ticket_id") else None
        if ticket is None and ep.get("ticket_id"):
            rows = stdb.sql(f"SELECT * FROM ticket WHERE id = {int(ep['ticket_id'])}")
            ticket = rows[0] if rows else {"id": ep["ticket_id"]}
        log.info("llm_qa.start", extra={"fields": {"episode_id": eid, "offer_id": offer_id, "source": ev.source}})
        d = lq.run_after_deterministic(stdb, ep, dict(ticket or {}), cfg=cfg, judge=make_judge(cfg),
                                       extra_calls=_failed_calls[offer_id])
        if d is not None:
            log.info("llm_qa.done", extra={"fields": {"episode_id": eid, "accepted": d.accepted,
                                                      "reason": d.reason[:300]}})
    except Exception as e:
        if "already reviewed" in str(e):  # another orchestrator won; reviews are final
            log.info("llm_qa.skip", extra={"fields": {"episode_id": eid, "reason": str(e)[:200]}})
            return
        with _lock:
            _claimed.discard(eid)
            _failed_calls[offer_id] += 1
        if not isinstance(e, lq.rv.ReviewError):  # outages were already logged as qa.error
            try:
                lq.log_error(stdb, ep or {"id": eid}, f"{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        close = getattr(stdb, "close", None)
        if close:
            close()
