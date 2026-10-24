"""TDM-95: the planner's dataset-offer suggestion, wired into the loop (logic; bus wiring in
``handlers/plan_offers.py``).

The user's idea: when a dataset for the task may exist, the factory offers to buy
N - (already uploaded) episodes instead of paying for every demo. The pieces already exist:
TDM-24's planner writes ``evidence.dataset_offer = {consider, max_price_per_episode_cents,
max_episodes}`` into its ``planner.data_plan`` event, TDM-91's ``OffersService.offer_from_plan``
creates the offer (the TDM-14 reducer sets ``needed_count`` = N - existing - reserved), TDM-92/93 QA
the deliveries and TDM-91's handler settles + reserves the seller payout. This module decides
*when* and *how much*, and tells the dispatcher which plan slots an offer holds:

  1. Spend gate. Only a plan that ``approve_spend`` approved (status approved/executing) gets an
     offer. ``check_spend`` predicts ``needed_count`` from the mirror (the reducer's maths) and
     requires ``needed x price`` to fit both the plan's approved cents (minus what its paid tasks
     already commit) and the ticket's remaining budget. Otherwise ``plan_offer.skipped`` with the
     numbers; the plan is dispatched as paid demos. The payout itself is still budget-guarded by
     ``reserve_dataset_payment`` (``blocked_budget`` over budget).
  2. One decision per plan, logged: ``plan_offer.created`` (offer id, needed, price, max cost,
     headroom, timeout) or ``plan_offer.skipped`` (why). While the decision is pending the dispatcher
     holds the plan (``awaiting_offer``, at most ``HOLD_S``) so it does not post paid tasks for slots the
     offer is about to reserve.
  3. Reservation (``reservation``): the plan's offers (offers on the ticket created at or after the
     plan, before the next plan) cover plan slots: an open offer its ``needed_count`` (delivered or
     not: a rejected delivery leaves the slot open for a replacement), a settled one its
     ``accepted_count``, a cancelled one the purchased episodes it delivered that are not rejected.
     The dispatcher posts paid tasks only for ``amount - covered`` and pays them from
     ``approved_cents - offer cents``.
  4. Timeout fallback (``timeout_action``): an open offer with no activity (created / accepted /
     last delivery) for ``offer_timeout_s()`` (env ``RDF_OFFER_TIMEOUT_S``, default 1800 s) is ended:
     nothing delivered -> ``cancel_dataset_offer`` (slots go back to paid demos); some delivered ->
     ``dataset_offer.delivery_complete`` so TDM-91 settles what QA accepted, and the rest of the slots
     go back to paid demos. Logged as ``plan_offer.timeout``.

Accepted purchased episodes are training episodes everywhere already (kind ``purchased`` is in
TDM-11's dataset kinds, the flywheel's ``DATASET_KINDS`` and ``queue_training_run``), and since
TDM-95 a LeRobot delivery is imported into our episode format at ingest (``offers.py``), so the
TDM-39 converter reads them like any other episode.

Pure: every function takes the bus mirror (``rows(table)`` / ``get(table, id)``) or rows.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

ACTOR = "orchestrator:plan_offers"
EVENT_CREATED = "plan_offer.created"
EVENT_SKIPPED = "plan_offer.skipped"
EVENT_TIMEOUT = "plan_offer.timeout"
EVENT_FALLBACK = "plan_offer.fallback"
DECISION_EVENTS = (EVENT_CREATED, EVENT_SKIPPED)
EVENT_TYPES = (EVENT_CREATED, EVENT_SKIPPED, EVENT_TIMEOUT, EVENT_FALLBACK)
PLANNER_EVENT = "planner.data_plan"
DELIVERY_COMPLETE = "dataset_offer.delivery_complete"  # = offers.EVENT_DELIVERY_COMPLETE

OPEN = ("proposed", "accepted", "ingesting", "qa")  # = offers.OPEN_STATUSES / TDM14_OPEN_OFFER
INGESTABLE = ("accepted", "ingesting", "qa")
TRAINING_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
SPEND_APPROVED = ("approved", "executing")
CLOSED_TICKET = ("shipped", "failed", "cancelled")
HOLD_S = 120.0  # max time the dispatcher waits for the offer decision of an approved plan
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_TARGET_EPISODES = 5  # = enums.ts DEFAULT_RUN_SETTINGS.target_episodes


def offer_timeout_s() -> float:
    try:
        return float(os.environ.get("RDF_OFFER_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _payload(row: Any) -> dict:
    try:
        p = json.loads(row.get("payload_json") or "{}")
        return p if isinstance(p, dict) else {}
    except (TypeError, ValueError):
        return {}


def _events(m, types: tuple[str, ...]) -> list[tuple[Any, dict]]:
    out = [(r, _payload(r)) for r in m.rows("event_log") if r.get("type") in types]
    return sorted(out, key=lambda x: _i(x[0].get("id")))


# --------------------------------------------------------------------------- planner hint + decision

def plan_hint(m, plan: Any) -> dict | None:
    """``evidence.dataset_offer`` of the plan's ``planner.data_plan`` event (None if absent)."""
    pid = _i(plan.get("id"))
    for _, p in reversed(_events(m, (PLANNER_EVENT,))):
        if _i(p.get("data_plan_id")) == pid:
            hint = (p.get("evidence") or {}).get("dataset_offer")
            return dict(hint) if isinstance(hint, dict) else None
    return None


def wants_offer(hint: dict | None) -> bool:
    return bool(hint and hint.get("consider") and _i(hint.get("max_price_per_episode_cents")) > 0)


def decision(m, plan_id: int) -> tuple[Any, dict] | None:
    """The plan's ``plan_offer.created`` / ``plan_offer.skipped`` event, if one was logged."""
    for r, p in _events(m, DECISION_EVENTS):
        if _i(p.get("data_plan_id")) == int(plan_id):
            return r, p
    return None


# --------------------------------------------------------------------------- which offers belong to a plan

def plan_window(m, plan: Any) -> tuple[int, int | None]:
    """[plan.created_at, next plan's created_at) on the same ticket."""
    tid, start = _i(plan.get("ticket_id")), _i(plan.get("created_at"))
    later = sorted(_i(p.get("created_at")) for p in m.rows("data_plan")
                   if _i(p.get("ticket_id")) == tid and _i(p.get("id")) != _i(plan.get("id"))
                   and _i(p.get("created_at")) > start)
    return start, (later[0] if later else None)


def offers_for_plan(m, plan: Any) -> list[Any]:
    tid = _i(plan.get("ticket_id"))
    start, end = plan_window(m, plan)
    out = [o for o in m.rows("dataset_offer") if _i(o.get("ticket_id")) == tid
           and _i(o.get("created_at")) >= start and (end is None or _i(o.get("created_at")) < end)]
    return sorted(out, key=lambda o: _i(o.get("id")))


def plan_for_offer(m, offer: Any) -> Any | None:
    """The data_plan whose window holds the offer (the latest plan created at or before it)."""
    tid, at = _i(offer.get("ticket_id")), _i(offer.get("created_at"))
    plans = [p for p in m.rows("data_plan") if _i(p.get("ticket_id")) == tid and _i(p.get("created_at")) <= at]
    return max(plans, key=lambda p: (_i(p.get("created_at")), _i(p.get("id")))) if plans else None


def offer_episodes(m, offer_id: int) -> list[Any]:
    return [e for e in m.rows("episode") if _i(e.get("dataset_offer_id")) == int(offer_id)
            and e.get("kind") == "purchased"]


# --------------------------------------------------------------------------- reservation (dispatcher)

@dataclass
class Reservation:
    hold: bool = False  # the offer decision for this approved plan is still pending
    hold_reason: str = ""
    slots: int = 0  # plan slots covered by purchased episodes (open offers' needed, settled accepted, ...)
    cents: int = 0  # approved cents those offers can still draw (open: needed x price; settled: accepted x price)
    offers: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def reservation(m, plan: Any, *, now: float | None = None) -> Reservation:
    now = time.time() if now is None else now
    res = Reservation()
    pid = _i(plan.get("id"))
    if plan.get("status") in SPEND_APPROVED and wants_offer(plan_hint(m, plan)) and decision(m, pid) is None:
        since = _i(plan.get("approved_at")) or _i(plan.get("created_at"))
        waited = now - since / 1e6 if since else 0.0
        if waited < HOLD_S:
            res.hold = True
            res.hold_reason = (f"the planner suggested a dataset offer; waiting for the offer decision "
                               f"({waited:.0f}s of at most {HOLD_S:.0f}s)")
    for o in offers_for_plan(m, plan):
        oid, st = _i(o["id"]), o.get("status")
        price, needed = _i(o.get("price_per_episode_cents")), _i(o.get("needed_count"))
        live = [e for e in offer_episodes(m, oid) if e.get("review_status") != "rejected"]
        if st in OPEN:
            slots, cents = needed, needed * price
        elif st == "done":
            slots, cents = _i(o.get("accepted_count")), _i(o.get("accepted_count")) * price
        else:  # cancelled: what it delivered still counts (it trains if accepted), nothing is paid
            slots, cents = len(live), 0
        res.slots += slots
        res.cents += cents
        res.offers.append({"offer_id": oid, "status": st, "needed_count": needed, "slots": slots, "cents": cents,
                           "delivered_live": len(live), "price_per_episode_cents": price})
    res.slots = min(res.slots, _i(plan.get("amount")))
    return res


# --------------------------------------------------------------------------- spend gate (offer creation)

@dataclass
class SpendCheck:
    ok: bool
    reason: str
    predicted_needed: int
    price_per_episode_cents: int
    max_cost_cents: int
    approved_cents: int
    plan_task_committed_cents: int
    approved_headroom_cents: int
    ticket_remaining_cents: int
    math: dict

    def to_dict(self) -> dict:
        return asdict(self)


def predicted_needed(m, ticket_id: int) -> tuple[int, dict]:
    """``create_dataset_offer``'s needed_count, from the mirror (``tdm14Commitment``; the reducer decides)."""
    rs = m.get("run_settings", ticket_id) or {}
    target = _i(rs.get("target_episodes")) or DEFAULT_TARGET_EPISODES
    eps = [e for e in m.rows("episode") if _i(e.get("ticket_id")) == ticket_id and e.get("kind") in TRAINING_KINDS]
    existing = sum(1 for e in eps if e.get("review_status") != "rejected")
    ds = next((d for d in m.rows("dataset") if _i(d.get("ticket_id")) == ticket_id), None)
    ds_acc = _i((ds or {}).get("accepted_count"))
    reserved = 0
    for o in m.rows("dataset_offer"):
        if _i(o.get("ticket_id")) != ticket_id or o.get("status") not in OPEN:
            continue
        live = sum(1 for e in offer_episodes(m, _i(o["id"])) if e.get("review_status") != "rejected")
        reserved += max(0, _i(o.get("needed_count")) - live)
    committed = max(existing, ds_acc) + reserved
    return max(0, target - committed), {"target_episodes": target, "existing": existing, "dataset_accepted": ds_acc,
                                        "reserved_by_open_offers": reserved, "committed": committed}


def check_spend(m, plan: Any, price: int, *, plan_task_committed_cents: int, ticket_remaining_cents: int) -> SpendCheck:
    tid = _i(plan.get("ticket_id"))
    needed, math = predicted_needed(m, tid)
    approved = _i(plan.get("approved_cents"))
    headroom = approved - plan_task_committed_cents
    cost = needed * price
    base = dict(predicted_needed=needed, price_per_episode_cents=price, max_cost_cents=cost, approved_cents=approved,
                plan_task_committed_cents=plan_task_committed_cents, approved_headroom_cents=headroom,
                ticket_remaining_cents=ticket_remaining_cents, math=math)
    if plan.get("status") not in SPEND_APPROVED or approved <= 0:
        return SpendCheck(False, f"data_plan {plan.get('id')} is {plan.get('status')}: approve_spend first", **base)
    if price <= 0:
        return SpendCheck(False, "the plan suggests no offer price", **base)
    if needed <= 0:
        return SpendCheck(False, f"no episodes needed: target {math['target_episodes']}, committed "
                                 f"{math['committed']} (uploaded/accepted + reserved by open offers)", **base)
    if cost > headroom:
        return SpendCheck(False, f"approve_spend: buying {needed} x {price}c = {cost}c exceeds the plan's approved "
                                 f"{approved}c minus {plan_task_committed_cents}c its paid tasks commit", **base)
    if cost > ticket_remaining_cents:
        return SpendCheck(False, f"budget: buying {needed} x {price}c = {cost}c exceeds the ticket's remaining "
                                 f"{ticket_remaining_cents}c", **base)
    return SpendCheck(True, f"{needed} x {price}c = {cost}c fits the approved {headroom}c and the remaining "
                            f"{ticket_remaining_cents}c", **base)


# --------------------------------------------------------------------------- timeout fallback

@dataclass
class TimeoutAction:
    offer_id: int
    ticket_id: int
    action: str  # cancel | delivery_complete
    waited_s: float
    delivered: int
    live: int
    reason: str


def last_activity_us(m, offer: Any) -> int:
    eps = offer_episodes(m, _i(offer["id"]))
    return max([_i(offer.get("created_at")), _i(offer.get("updated_at"))] + [_i(e.get("created_at")) for e in eps])


def timeout_action(m, offer: Any, *, now: float | None = None, timeout_s: float | None = None) -> TimeoutAction | None:
    now = time.time() if now is None else now
    timeout_s = offer_timeout_s() if timeout_s is None else timeout_s
    st = offer.get("status")
    if st not in OPEN:
        return None
    ticket = m.get("ticket", _i(offer.get("ticket_id"))) or {}
    if ticket.get("status") in CLOSED_TICKET:
        return None
    waited = now - last_activity_us(m, offer) / 1e6
    if waited < timeout_s:
        return None
    oid = _i(offer["id"])
    eps = offer_episodes(m, oid)
    live = sum(1 for e in eps if e.get("review_status") != "rejected")
    base = dict(offer_id=oid, ticket_id=_i(offer.get("ticket_id")), waited_s=round(waited, 1), delivered=len(eps),
                live=live)
    if st == "proposed" or not eps:
        why = "the seller never accepted" if st == "proposed" else "the seller accepted but delivered nothing"
        return TimeoutAction(action="cancel", reason=f"[TDM-95 timeout] {why} within {timeout_s:.0f}s; falling back "
                                                     "to paid demos", **base)
    done = any(r.get("type") == DELIVERY_COMPLETE and _i(p.get("offer_id")) == oid
               for r, p in _events(m, (DELIVERY_COMPLETE,)))
    if done:
        return None
    return TimeoutAction(action="delivery_complete", reason=(
        f"[TDM-95 timeout] no new delivery within {timeout_s:.0f}s; settling the {len(eps)} delivered "
        f"episode(s) after QA, the open slots fall back to paid demos"), **base)


# --------------------------------------------------------------------------- report / evidence summary

def offer_line(offer: Any, episodes: list[Any], reviews: dict[int, Any], refused: list[dict],
               target_episodes: int | None = None) -> str:
    """'bought 3 of 3 needed (target 5); 1 rejected: off-task; 1 refused at ingest (offer full)'."""
    accepted = _i(offer.get("accepted_count")) if offer.get("status") == "done" else sum(
        1 for e in episodes if e.get("review_status") == "accepted")
    rejected = [e for e in episodes if e.get("review_status") == "rejected"]
    parts = [f"bought {accepted} of {_i(offer.get('needed_count'))} needed"
             + (f" (target {target_episodes})" if target_episodes else "")]
    if rejected:
        why = "; ".join(str((reviews.get(_i(e["id"])) or {}).get("reason") or "no reason")[:120] for e in rejected)
        parts.append(f"{len(rejected)} rejected: {why}")
    if refused:
        codes = sorted({str(r.get("code") or "refused") for r in refused})
        parts.append(f"{len(refused)} refused at ingest ({', '.join(codes)})")
    return "; ".join(parts)
