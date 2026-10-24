"""TDM-95: bus wiring for the planner's dataset-offer suggestion (logic in ``..plan_offers``).

    data_plan enters approved/executing (approve_spend; boot snapshot too)
        planner evidence dataset_offer.consider  ==>  spend gate (plan_offers.check_spend)
            ok   ==>  OffersService.offer_from_plan(ticket, True, max_price)   -> plan_offer.created
            else ==>  plan_offer.skipped (why)
        ==>  dispatcher.dispatch_plan(plan): paid tasks only for the slots no offer holds
    dataset_offer changes status (cancelled / settled)  ==>  dispatch_plan again (freed slots -> paid
        demos, logged as plan_offer.fallback when tasks are created)
    tick (RDF_OFFER_TICK_S, default 15 s)  ==>  open offers past RDF_OFFER_TIMEOUT_S: cancel, or mark the
        delivery complete so TDM-91 settles what QA accepted (plan_offer.timeout); decisions a crash
        missed are retried; held plans are re-dispatched once the hold ends.

Idempotent: the decision is one event per plan (checked in the mirror before acting, plus an
in-process claim), ``offer_from_plan`` skips when an open offer exists, the timeout acts only on
open offers without a delivery_complete event, and ``dispatch_plan`` decides from rows.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any, Callable

from .. import dispatcher as dsp
from .. import plan_offers as po
from ..bus import Event, on_enter, on_update, watch
from ..log import get_logger, ticket_id_var

log = get_logger("plan_offers")

for _t in ("data_plan", "dataset_offer", "episode", "event_log", "ticket", "run_settings", "dataset", "contributor",
           "task", "payment"):
    watch(_t)

_lock = threading.Lock()
_claims: set[int] = set()  # data_plan ids being decided in this process
_timed_out: set[int] = set()  # offer ids acted on in this process
_tick: asyncio.Task | None = None
_tick_bus: Any = None


def _default_async_stdb(bus):
    """An async client on the same database the bus is subscribed to."""
    from rdf_common.spacetime_client import AsyncSpacetimeClient

    c = getattr(bus, "client", None)
    host, db = getattr(c, "host", None), getattr(c, "db", None)
    if host and db:
        return AsyncSpacetimeClient(host, db, getattr(c, "token", None) or "")
    from rdf_common.settings import OrchestratorSettings, load_settings

    return AsyncSpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"))


# Swappable for tests / the proof.
def _default_offers_service(stdb):
    from ..offers import OffersService

    return OffersService(stdb)


make_async_stdb: Callable[[Any], Any] = _default_async_stdb
make_offers_service: Callable[[Any], Any] = _default_offers_service


def tick_s() -> float:
    try:
        return float(os.environ.get("RDF_OFFER_TICK_S") or 15.0)
    except ValueError:
        return 15.0


def _event(bus, type_: str, payload: dict) -> None:
    bus.call_reducer_threadsafe("append_event", [po.ACTOR, type_, json.dumps(payload, default=str)])
    log.info(type_, extra={"fields": payload})


def _wait(pred: Callable[[], Any], timeout: float = 5.0) -> Any:
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() > end:
            return v
        time.sleep(0.05)


def _current(bus, plan) -> bool:
    t = bus.get("ticket", int(plan["ticket_id"]))
    return bool(t) and t.get("status") not in po.CLOSED_TICKET and int(plan.get("iteration") or 0) >= int(
        t.get("iteration") or 0)


def _plan_task_committed_cents(d: dsp.Dispatcher, plan) -> int:
    tid, pid = int(plan["ticket_id"]), int(plan["id"])
    eps, paid = d._episodes_by_task(tid), d._reset_paid_refs(tid)
    return sum(int(t.get("price_cents") or 0) for t in d._tasks(tid) if int(t.get("data_plan_id") or 0) == pid
               and d._task_state(t, eps, paid) in ("in_flight", "accepted"))


def decide_offer(bus, plan_id: int, *, why: str = "") -> dict | None:
    """Make (and log) the offer decision for an approved plan. Returns the logged payload, or None
    when there is nothing to decide (no suggestion, already decided, not approved, stale plan)."""
    plan = bus.get("data_plan", int(plan_id))
    if plan is None or plan.get("status") not in po.SPEND_APPROVED or not _current(bus, plan):
        return None
    hint = po.plan_hint(bus, plan)
    if not po.wants_offer(hint) or po.decision(bus, plan_id) is not None:
        return None
    with _lock:
        if plan_id in _claims:
            return None
        _claims.add(plan_id)
    tid = int(plan["ticket_id"])
    tok = ticket_id_var.set(tid)
    try:
        price = int(hint["max_price_per_episode_cents"])
        base = {"ticket_id": tid, "data_plan_id": int(plan_id), "iteration": int(plan.get("iteration") or 0),
                "data_type": plan.get("data_type"), "plan_amount": int(plan.get("amount") or 0),
                "planner_hint": hint, "approved_by": plan.get("approved_by"), "trigger": why}
        d = dsp.get_dispatcher(bus)
        chk = po.check_spend(bus, plan, price, plan_task_committed_cents=_plan_task_committed_cents(d, plan),
                             ticket_remaining_cents=d.budget_remaining(tid))
        if not chk.ok:
            payload = {**base, "reason": chk.reason, "spend_check": chk.to_dict()}
            _event(bus, po.EVENT_SKIPPED, payload)
            return payload
        sellers = [c for c in bus.rows("contributor") if c.get("kind") == "dataset_seller"]
        if not sellers:
            payload = {**base, "reason": "no dataset seller is registered", "spend_check": chk.to_dict()}
            _event(bus, po.EVENT_SKIPPED, payload)
            return payload

        async def create() -> dict | None:
            stdb = make_async_stdb(bus)
            async with stdb:
                return await make_offers_service(stdb).offer_from_plan(tid, True, price)

        try:
            made = asyncio.run(create())
        except Exception as e:  # noqa: BLE001 - logged; the plan then dispatches as paid demos
            made, err = None, f"{type(e).__name__}: {getattr(e, 'message', None) or e}"
        else:
            err = None
        if made is None:
            payload = {**base, "reason": err or "offer_from_plan created no offer (an open offer exists, or the "
                                                "reducer found no episodes needed)", "spend_check": chk.to_dict()}
            _event(bus, po.EVENT_SKIPPED, payload)
            return payload
        offer = made["offer"]
        oid, needed = int(offer["id"]), int(offer["needed_count"])
        cost = needed * int(offer["price_per_episode_cents"])
        if cost > chk.approved_headroom_cents or cost > chk.ticket_remaining_cents:  # the reducer's count won a race
            try:
                bus.call_reducer_threadsafe("cancel_dataset_offer", [oid, (f"[TDM-95] {needed} x {price}c = {cost}c "
                                                                           "exceeds the approved spend")])
            finally:
                payload = {**base, "reason": f"offer {oid} needs {needed} x {price}c = {cost}c > approved headroom "
                                             f"{chk.approved_headroom_cents}c; cancelled", "offer_id": oid,
                           "spend_check": chk.to_dict()}
                _event(bus, po.EVENT_SKIPPED, payload)
            return payload
        payload = {**base, "offer_id": oid, "seller": made.get("seller"), "needed_count": needed,
                   "price_per_episode_cents": int(offer["price_per_episode_cents"]), "max_cost_cents": cost,
                   "offer_status": offer.get("status"), "timeout_s": po.offer_timeout_s(),
                   "spend_check": chk.to_dict(), "budget_warning": made.get("budget_warning")}
        _event(bus, po.EVENT_CREATED, payload)
        return payload
    finally:
        _wait(lambda: po.decision(bus, plan_id) is not None)
        with _lock:
            _claims.discard(plan_id)
        ticket_id_var.reset(tok)


def redispatch(bus, plan_id: int, *, cause: str, offer: Any = None) -> dsp.PlanStatus:
    st = dsp.get_dispatcher(bus).dispatch_plan(int(plan_id))
    if offer is not None and st.created_task_ids:
        _event(bus, po.EVENT_FALLBACK, {
            "ticket_id": st.ticket_id, "data_plan_id": int(plan_id), "offer_id": int(offer["id"]),
            "offer_status": offer.get("status"), "cause": cause, "created_task_ids": st.created_task_ids,
            "paid_target": st.target, "offer_reserved": st.offer_reserved})
    return st


def handle_timeout(bus, offer: Any, *, now: float | None = None) -> po.TimeoutAction | None:
    a = po.timeout_action(bus, offer, now=now)
    if a is None:
        return None
    with _lock:
        if a.offer_id in _timed_out:
            return None
        _timed_out.add(a.offer_id)
    plan = po.plan_for_offer(bus, offer)
    payload = {"ticket_id": a.ticket_id, "offer_id": a.offer_id, "data_plan_id": int(plan["id"]) if plan else None,
               "action": a.action, "waited_s": a.waited_s, "timeout_s": po.offer_timeout_s(),
               "delivered": a.delivered, "live": a.live, "reason": a.reason}
    try:
        if a.action == "cancel":
            bus.call_reducer_threadsafe("cancel_dataset_offer", [a.offer_id, a.reason[:500]])
        else:
            bus.call_reducer_threadsafe("append_event", [po.ACTOR, po.DELIVERY_COMPLETE, json.dumps(
                {"offer_id": a.offer_id, "ticket_id": a.ticket_id, "by": "timeout", "reason": a.reason})])
    except Exception as e:  # noqa: BLE001 - e.g. the seller delivered meanwhile; retried next tick
        with _lock:
            _timed_out.discard(a.offer_id)
        log.warning("plan_offer.timeout_failed", extra={"fields": {**payload, "error": str(e)[:300]}})
        return None
    _event(bus, po.EVENT_TIMEOUT, payload)
    return a


def sweep(bus, *, now: float | None = None) -> None:
    """One tick: timeouts, missed decisions, held plans."""
    for o in list(bus.rows("dataset_offer")):
        if o.get("status") in po.OPEN:
            handle_timeout(bus, o, now=now)
    for p in list(bus.rows("data_plan")):
        if p.get("status") not in po.SPEND_APPROVED or not _current(bus, p) or not po.wants_offer(po.plan_hint(bus, p)):
            continue
        if po.decision(bus, int(p["id"])) is None:
            decide_offer(bus, int(p["id"]), why="tick")
        dsp.get_dispatcher(bus).dispatch_plan(int(p["id"]))


async def _tick_loop(bus) -> None:
    while True:
        await asyncio.sleep(tick_s())
        try:
            await asyncio.to_thread(sweep, bus)
        except Exception:  # noqa: BLE001 - the tick must never die
            log.exception("plan_offer.tick_error")


def ensure_tick(bus) -> None:
    global _tick, _tick_bus
    if _tick is not None and not _tick.done() and _tick_bus is bus:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _tick_bus = bus
    _tick = loop.create_task(_tick_loop(bus), name="tdm95-plan-offer-tick")


# --------------------------------------------------------------------------- handlers

@on_enter("data_plan", where=lambda r: r.status in po.SPEND_APPROVED, name="tdm95_plan_offer")
def on_plan_approved(ev: Event) -> None:
    out = decide_offer(ev.bus, int(ev.row["id"]), why=f"plan:{ev.source}")
    if out is not None:
        dsp.get_dispatcher(ev.bus).dispatch_plan(int(ev.row["id"]))


@on_update("dataset_offer", name="tdm95_offer_changed")
def on_offer_changed(ev: Event) -> None:
    if ev.old is not None and ev.old.get("status") == ev.row.get("status") \
            and ev.old.get("accepted_count") == ev.row.get("accepted_count"):
        return
    offer = ev.bus.get("dataset_offer", int(ev.row["id"])) or ev.row
    plan = po.plan_for_offer(ev.bus, offer)
    if plan is not None and plan.get("status") in dsp.DISPATCHABLE_PLAN:
        redispatch(ev.bus, int(plan["id"]), cause=f"offer {offer['id']} {offer.get('status')}", offer=offer)


@on_enter("dataset_offer", where=lambda r: r.status in po.OPEN, name="tdm95_offer_tick")
async def on_offer_open(ev: Event) -> None:
    ensure_tick(ev.bus)


@on_enter("data_plan", where=lambda r: r.status in po.SPEND_APPROVED, name="tdm95_plan_tick")
async def on_plan_tick(ev: Event) -> None:
    ensure_tick(ev.bus)
