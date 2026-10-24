"""TDM-91: settle dataset-purchase offers once QA has spoken, then reserve the seller's payout.

    qa_result / review / purchased episode change, delivery_complete event, offer (re)enters
    accepted|ingesting|qa  ==>  offers.finalize_readiness (from the bus mirror)
        ready  ==>  finalize_offer(offer_id)            (TDM-14: accepted_count = QA+review accepted, <= needed)
    dataset_offer enters done with accepted_count > 0
        ==>  reserve_dataset_payment(offer_id, "settle") (TDM-11: accepted_count x price, budget-checked)
             -> payment(pending) -> TDM-63 handler moves the test-mode money

Ready means every delivered episode has a verdict, every non-rejected one carries the
``offers.required_checks()`` (TDM-92/93 check names), and either all ``needed_count`` episodes are
accepted or the seller marked delivery complete (``POST /offers/{id}/delivered`` / ``final: true``).
Until then a rejected slot stays open for a replacement.

Idempotent: decisions come from the mirror, ``finalize_offer`` refuses a settled offer (swallowed), and
``reserve_dataset_payment`` is a no-op for an existing ``settle`` batch. An in-process claim set stops two
pool workers settling the same offer at once.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .. import offers as of
from ..bus import Event, on_enter, on_insert, on_update, watch
from ..log import get_logger

log = get_logger("offers")

for _t in ("dataset_offer", "episode", "qa_result", "review", "payment", "event_log"):
    watch(_t)

_lock = threading.Lock()
_busy: set[int] = set()


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


make_stdb: Callable[[], Any] = _default_stdb  # swappable for tests


def mirror_state(bus, offer: Any) -> tuple[of.OfferState, bool]:
    oid = int(offer["id"])
    eps = [e for e in bus.rows("episode") if int(e.get("dataset_offer_id") or 0) == oid]
    ids = {int(e["id"]) for e in eps}
    qa: dict[int, list] = {}
    for r in bus.rows("qa_result"):
        if int(r["episode_id"]) in ids:
            qa.setdefault(int(r["episode_id"]), []).append(r)
    revs: dict[int, list] = {}
    for r in bus.rows("review"):
        if int(r["episode_id"]) in ids:
            revs.setdefault(int(r["episode_id"]), []).append(r)
    done = any(r.get("type") == of.EVENT_DELIVERY_COMPLETE and of._offer_of_event(r) == oid
               for r in bus.rows("event_log"))
    return of.offer_state(offer, eps, qa, revs), done


def try_settle(bus, offer_id: int, trigger: str) -> bool:
    offer = bus.get("dataset_offer", offer_id)
    if offer is None or offer["status"] not in of.INGESTABLE:
        return False
    st, delivered = mirror_state(bus, offer)
    ready, why = of.finalize_readiness(st, delivered)
    if not ready:
        log.info("offers.not_ready", extra={"fields": {"offer_id": offer_id, "trigger": trigger, "why": why}})
        return False
    with _lock:
        if offer_id in _busy:
            return False
        _busy.add(offer_id)
    try:
        log.info("offers.finalize", extra={"fields": {"offer_id": offer_id, "ticket_id": offer["ticket_id"],
                                                     "trigger": trigger, "why": why, "billable": st.billable,
                                                     "needed": st.needed}})
        try:
            make_stdb().call_reducer("finalize_offer", offer_id)
        except Exception as e:  # noqa: BLE001 - a settled offer / a verdict that landed meanwhile
            log.info("offers.finalize_refused", extra={"fields": {"offer_id": offer_id, "error": str(e)[:300]}})
            return False
        return True
    finally:
        with _lock:
            _busy.discard(offer_id)


def reserve_payout(bus, offer: Any) -> bool:
    oid = int(offer["id"])
    if int(offer.get("accepted_count") or 0) <= 0:
        log.info("offers.no_payout", extra={"fields": {"offer_id": oid, "reason": "no accepted episodes"}})
        return False
    have = [p for p in bus.rows("payment") if int(p.get("dataset_offer_id") or 0) == oid
            and p.get("batch_ref") == of.SETTLE_BATCH_REF and p.get("status") not in ("failed", "blocked_budget")]
    if have:
        return False
    try:
        make_stdb().call_reducer("reserve_dataset_payment", oid, of.SETTLE_BATCH_REF)
    except Exception as e:  # noqa: BLE001
        log.warning("offers.reserve_failed", extra={"fields": {"offer_id": oid, "error": str(e)[:300]}})
        return False
    log.info("offers.payout_reserved", extra={"fields": {
        "offer_id": oid, "ticket_id": offer["ticket_id"], "accepted_count": offer["accepted_count"],
        "amount_cents": int(offer["accepted_count"]) * int(offer["price_per_episode_cents"])}})
    return True


def _offer_of_episode(bus, episode_id: Any) -> int | None:
    ep = bus.get("episode", int(episode_id))
    if ep is None or ep.get("kind") != "purchased":
        return None
    return int(ep.get("dataset_offer_id") or 0) or None


@on_enter("dataset_offer", where=lambda r: r.status in of.INGESTABLE, name="tdm91_offer_open")
def on_offer_open(ev: Event) -> None:
    try_settle(ev.bus, int(ev.row["id"]), f"offer:{ev.source}")


@on_enter("dataset_offer", where=lambda r: r.status == "done", name="tdm91_offer_settled")
def on_offer_settled(ev: Event) -> None:
    reserve_payout(ev.bus, ev.bus.get("dataset_offer", int(ev.row["id"])) or ev.row)


@on_insert("qa_result", name="tdm91_qa_result")
def on_qa_result(ev: Event) -> None:
    oid = _offer_of_episode(ev.bus, ev.row["episode_id"])
    if oid:
        try_settle(ev.bus, oid, "qa_result")


@on_insert("review", name="tdm91_review")
def on_review(ev: Event) -> None:
    oid = _offer_of_episode(ev.bus, ev.row["episode_id"])
    if oid:
        try_settle(ev.bus, oid, "review")


@on_update("episode", where=lambda r: r.kind == "purchased", name="tdm91_purchased_episode")
def on_purchased_episode(ev: Event) -> None:
    if ev.old is not None and ev.old.get("review_status") == ev.row.get("review_status"):
        return
    oid = int(ev.row.get("dataset_offer_id") or 0)
    if oid:
        try_settle(ev.bus, oid, "episode")


@on_insert("event_log", where=lambda r: r.type == of.EVENT_DELIVERY_COMPLETE, name="tdm91_delivery_complete")
def on_delivery_complete(ev: Event) -> None:
    oid = of._offer_of_event(ev.row)
    if oid:
        try_settle(ev.bus, oid, "delivery_complete")
