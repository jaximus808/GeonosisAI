"""TDM-26 dispatcher: turns an approved ``data_plan`` into paid marketplace tasks, and asks for resets.

Bus wiring is in ``handlers/dispatcher.py``. This module holds the logic and the public interface
that the flywheel state machine (TDM-22) and the push pager (TDM-65) call into.

Interface for TDM-22 (flywheel)::

    from rdf_orchestrator import dispatcher as dsp
    d = dsp.get_dispatcher(bus)               # one per bus; reads the bus mirror, writes reducers
    st = d.dispatch_plan(plan_id)             # create the missing tasks for an approved plan (idempotent)
    st = d.plan_status(plan_id)               # read-only; st.done == target reached
    sts = d.ticket_status(ticket_id)          # PlanStatus for every plan of the ticket's current iteration
    d.collection_done(ticket_id)              # True when every dispatchable plan reached its target
    d.request_reset(robot_id, ticket_id, reason="...", ref="...")   # paid reset task + reset_request
    d.cancel_open_tasks(ticket_id, reason, data_plan_id=None)        # withdraw unclaimed tasks

The bus handlers already call ``dispatch_plan`` when a plan is approved and whenever a task or an
episode review changes, so the flywheel normally only reads ``plan_status`` / ``collection_done``
to decide when to leave ``dispatch``. Nothing here changes ``ticket.status``: that is the flywheel's.

Interface for TDM-65 (web push)::

    dsp.set_page_hook(fn)   # fn(PageRequest) -> dict | None, called after tasks are created

Interface for TDM-108 (job drops) and any other announcer: extra listeners run after the push hook::

    dsp.add_page_listener(fn)   # fn(PageRequest, rows) -> dict | None; rows(table) reads the bus mirror

Rules (``approve_spend`` is the gate):
  * Only plans with status approved/executing are dispatched. Never more than
    ``min(plan.amount, plan.approved_cents // unit_price)`` tasks are live-or-accepted at once.
  * Ticket budget: a task is created only if ``budget_cents - spent_cents - GPU - committed >= price``,
    where ``committed`` is the price of every task on the ticket that may still be paid but is not
    yet in ``spent_cents`` (open/claimed/submitted, or completed and awaiting review / payment) and
    GPU is the ticket's sum of ``training_run.gpu_cost_cents`` (TDM-42; the flywheel's budget term).
    Short of budget -> ``dispatch.budget_exhausted`` in event_log and no task.
  * Stop at the target: once ``plan.amount`` episodes from the plan's tasks are accepted, the
    plan's still-open tasks are cancelled (``target_reached``). A rejected episode frees its slot,
    so the next reconcile tops the plan back up (still inside the approved cents).
  * Dataset offers (TDM-95, ``plan_offers.reservation``): slots a dataset offer of the plan holds
    (open: its needed_count; settled: its accepted_count) are not posted as paid tasks, and the
    approved cents they can draw are not spent on tasks. While the planner's offer suggestion for an
    approved plan is still being decided the plan waits (``awaiting_offer``). Open tasks beyond the
    reduced target are cancelled (``offer_reserved``). A cancelled / settled offer frees its unfilled
    slots and the next dispatch tops the plan up with paid demos (the timeout fallback).
  * Resets: one live reset task per robot. ``reset_request`` is issued only when no job command is
    open on the robot (TDM-13 job lock); during a ``run_eval`` the robot's own job waits for the
    reset, and the completed reset task releases it with ``resume_policy``.

Dedup is from rows, not memory (restart-safe): plan tasks by ``task.data_plan_id``; resets by a
``[ref ...]`` tag in ``task.instructions``; resumes by ``task_id`` in ``robot_command.payload_json``.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .log import get_logger

log = get_logger("dispatcher")

ACTOR = "orchestrator:dispatcher"
# Must match spacetime enums.ts DEFAULT_PRICES_CENTS (CONTEXT.md: demo $10, takeover $5, reset $2).
PRICES_CENTS = {"clean_teleop": 1000, "recovery_takeover": 500, "reset": 200}
MARKET_DATA_TYPES = {"clean_teleop", "recovery_takeover"}
DISPATCHABLE_PLAN = {"approved", "executing"}
TERMINAL_TICKET = {"shipped", "failed", "cancelled"}
LIVE_TASK = {"open", "claimed", "submitted"}
OPEN_JOB = {"issued", "acked"}
JOB_TYPES = {"run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint"}
RESERVED_PAYMENT = {"pending", "awaiting_onboarding", "paid"}
# A completed data task with no episode yet still holds its slot (and budget) for this long.
NO_EPISODE_GRACE_S = 300
# On a boot snapshot, only react to eval trials / completed resets newer than this.
SNAPSHOT_WINDOW_S = 600
MIRROR_WAIT_S = 3.0

TABLES = ("ticket", "run_settings", "data_plan", "task", "episode", "payment", "contributor",
          "eval_run", "eval_trial", "robot", "robot_command", "failure_cluster")

_REF_RE = re.compile(r"\[ref ([^\]]+)\]")


class Mirror(Protocol):
    """What the dispatcher reads: ``bus.Bus`` (or a test fake)."""

    def rows(self, table: str) -> list[dict]: ...
    def get(self, table: str, key: Any) -> dict | None: ...


class Writer(Protocol):
    """What the dispatcher writes with: ``rdf_common.spacetime_client.SpacetimeClient`` (sync HTTP,
    returns after the reducer committed) or a test fake."""

    def call_reducer(self, name: str, /, *args: Any) -> None: ...


# --------------------------------------------------------------------------- results

@dataclass
class PlanStatus:
    plan_id: int
    ticket_id: int
    data_type: str
    state: str  # awaiting_approval | awaiting_offer | collecting | target_reached | covered_by_offer |
    #             budget_exhausted | closed | not_marketplace
    # budget_exhausted: ticket budget (budget - spent - committed) or the approved cents cannot pay for more
    target: int = 0  # accepted task episodes wanted: plan.amount minus the slots dataset offers hold
    payable: int = 0  # approved_cents // unit_price: episodes the approval can pay for
    unit_price_cents: int = 0
    accepted: int = 0
    in_flight: int = 0  # live tasks + completed ones awaiting review
    open_task_ids: list[int] = field(default_factory=list)
    created_task_ids: list[int] = field(default_factory=list)  # by this call
    cancelled_task_ids: list[int] = field(default_factory=list)  # by this call
    budget_remaining_cents: int = 0
    reason: str = ""
    plan_amount: int = 0  # plan.amount (TDM-95: target = plan_amount - offer_reserved)
    offer_reserved: int = 0  # plan slots held by dataset offers
    offer_reserved_cents: int = 0  # approved cents those offers may draw
    offer_ids: list[int] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.accepted >= self.target > 0

    def to_dict(self) -> dict:
        return {**asdict(self), "done": self.done}


@dataclass
class ResetResult:
    task_id: int | None
    created: bool
    robot_command: str  # issued | inline (robot busy with a job that waits) | none
    reason: str = ""


@dataclass
class PageRequest:
    ticket_id: int
    task_ids: list[int]
    task_type: str
    price_cents: int
    contributor_ids: list[int]  # online contributors (kind contributor)


PageHook = Callable[[PageRequest], "dict | None"]
_page_hook: PageHook | None = None


def set_page_hook(fn: PageHook | None) -> None:
    """TDM-65 installs its web-push sender here. Called after new tasks exist; return value is logged."""
    global _page_hook
    _page_hook = fn


# TDM-108: more announcers next to the push hook. fn(req, rows) must return fast (queue slow work).
PageListener = Callable[[PageRequest, Callable[[str], list]], "dict | None"]
_page_listeners: list[PageListener] = []


def add_page_listener(fn: PageListener) -> None:
    if fn not in _page_listeners:
        _page_listeners.append(fn)


def remove_page_listener(fn: PageListener) -> None:
    if fn in _page_listeners:
        _page_listeners.remove(fn)


# --------------------------------------------------------------------------- helpers

def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _json(text: Any) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def task_ref(task: dict) -> str | None:
    m = _REF_RE.search(task.get("instructions") or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- dispatcher

class Dispatcher:
    def __init__(self, mirror: Mirror, writer_factory: Callable[[], Writer], *,
                 mirror_wait_s: float = MIRROR_WAIT_S, clock: Callable[[], float] = time.time) -> None:
        self.m = mirror
        self._writer_factory = writer_factory
        self._writer: Writer | None = None
        self._wlock = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        self._logged: dict[tuple, str] = {}
        self.mirror_wait_s = mirror_wait_s
        self.clock = clock

    # ---- plumbing
    @property
    def w(self) -> Writer:
        with self._wlock:
            if self._writer is None:
                self._writer = self._writer_factory()
            return self._writer

    def lock(self, ticket_id: int) -> threading.RLock:
        with self._locks_lock:
            return self._locks.setdefault(int(ticket_id), threading.RLock())

    def event(self, type_: str, payload: dict, *, dedup: tuple | None = None) -> None:
        """Append a ``dispatch.*`` row to event_log. ``dedup`` suppresses an identical repeat."""
        body = json.dumps(payload, sort_keys=True, default=str)
        if dedup is not None:
            sig = json.dumps({k: v for k, v in payload.items() if k not in ("created_task_ids",)},
                             sort_keys=True, default=str)
            if self._logged.get(dedup) == sig:
                return
            self._logged[dedup] = sig
        log.info(type_, extra={"fields": payload})
        try:
            self.w.call_reducer("append_event", ACTOR, type_, body)
        except Exception as e:  # noqa: BLE001 - the log line above is the fallback record
            log.warning("dispatch.event_failed", extra={"fields": {"type": type_, "error": str(e)}})

    def _wait_for(self, pred: Callable[[], Any]) -> Any:
        deadline = time.time() + self.mirror_wait_s
        while True:
            got = pred()
            if got or time.time() >= deadline:
                return got
            time.sleep(0.02)

    # ---- reads
    def _tasks(self, ticket_id: int) -> list[dict]:
        return [t for t in self.m.rows("task") if _i(t.get("ticket_id")) == ticket_id]

    def _episodes_by_task(self, ticket_id: int) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for e in self.m.rows("episode"):
            if _i(e.get("ticket_id")) == ticket_id and _i(e.get("task_id")):
                out.setdefault(_i(e["task_id"]), []).append(e)
        return out

    def _reset_paid_refs(self, ticket_id: int) -> set[str]:
        return {p.get("batch_ref") for p in self.m.rows("payment")
                if _i(p.get("ticket_id")) == ticket_id and _i(p.get("episode_id")) == 0
                and p.get("status") in RESERVED_PAYMENT and p.get("batch_ref")}

    def _task_state(self, t: dict, eps: dict[int, list[dict]], paid_refs: set[str]) -> str:
        """accepted | in_flight | free  (free = cannot be paid any more: frees its slot and budget)."""
        st = t.get("status")
        if st in LIVE_TASK:
            return "in_flight"
        if st != "completed":
            return "free"  # cancelled / expired
        if t.get("type") == "reset":
            return "accepted" if f"task:{_i(t['id'])}" in paid_refs else "in_flight"
        reviews = [e.get("review_status") for e in eps.get(_i(t["id"]), [])]
        if "accepted" in reviews:
            return "accepted"
        if "pending" in reviews:
            return "in_flight"
        if reviews:
            return "free"  # rejected / not_reviewed
        done_us = _i(t.get("completed_at"))
        return "in_flight" if done_us and self.clock() * 1e6 - done_us < NO_EPISODE_GRACE_S * 1e6 else "free"

    def budget_remaining(self, ticket_id: int) -> int:
        """budget_cents - spent_cents - GPU - committed (unpaid tasks that may still be paid)."""
        ticket = self.m.get("ticket", ticket_id)
        if ticket is None:
            return 0
        eps, paid = self._episodes_by_task(ticket_id), self._reset_paid_refs(ticket_id)
        committed = sum(_i(t.get("price_cents")) for t in self._tasks(ticket_id)
                        if self._task_state(t, eps, paid) == "in_flight")
        gpu = sum(_i(r.get("gpu_cost_cents")) for r in self.m.rows("training_run")
                  if _i(r.get("ticket_id")) == ticket_id)  # TDM-42: GPU spend counts against the budget
        return _i(ticket.get("budget_cents")) - _i(ticket.get("spent_cents")) - gpu - committed

    def _unit(self, plan: dict) -> int:
        return _i(plan.get("unit_price_cents")) or PRICES_CENTS.get(plan.get("data_type"), 0)

    def plan_status(self, plan_id: int) -> PlanStatus:
        plan = self.m.get("data_plan", plan_id)
        if plan is None:
            return PlanStatus(plan_id, 0, "", "closed", reason="data_plan not in mirror")
        tid = _i(plan.get("ticket_id"))
        unit = self._unit(plan)
        from . import plan_offers  # TDM-95

        res = plan_offers.reservation(self.m, plan, now=self.clock())
        amount = _i(plan.get("amount"))
        st = PlanStatus(plan_id=plan_id, ticket_id=tid, data_type=plan.get("data_type") or "",
                        state="collecting", target=max(0, amount - res.slots), unit_price_cents=unit,
                        payable=max(0, _i(plan.get("approved_cents")) - res.cents) // unit if unit else 0,
                        plan_amount=amount, offer_reserved=res.slots, offer_reserved_cents=res.cents,
                        offer_ids=[o["offer_id"] for o in res.offers])
        eps, paid = self._episodes_by_task(tid), self._reset_paid_refs(tid)
        for t in self._tasks(tid):
            if _i(t.get("data_plan_id")) != plan_id:
                continue
            s = self._task_state(t, eps, paid)
            if s == "accepted":
                st.accepted += 1
            elif s == "in_flight":
                st.in_flight += 1
                if t.get("status") == "open":
                    st.open_task_ids.append(_i(t["id"]))
        st.budget_remaining_cents = self.budget_remaining(tid)
        ticket = self.m.get("ticket", tid)
        if st.data_type not in MARKET_DATA_TYPES:
            st.state, st.reason = "not_marketplace", f"data_type {st.data_type} is not a marketplace task"
        elif ticket is None or ticket.get("status") in TERMINAL_TICKET:
            st.state, st.reason = "closed", f"ticket {(ticket or {}).get('status', 'missing')}"
        elif _i(plan.get("iteration")) < _i(ticket.get("iteration")):
            st.state, st.reason = "closed", "plan is from an earlier iteration"
        elif plan.get("status") not in DISPATCHABLE_PLAN:
            st.state = "closed" if plan.get("status") in ("done", "rejected") else "awaiting_approval"
            st.reason = f"data_plan status {plan.get('status')}"
        elif st.done:
            st.state = "target_reached"
        elif res.hold:
            st.state, st.reason = "awaiting_offer", res.hold_reason
        elif st.target == 0 and res.slots > 0:
            st.state = "covered_by_offer"
            st.reason = f"dataset offer(s) {st.offer_ids} hold all {amount} slots of the plan"
        return st

    def ticket_status(self, ticket_id: int) -> list[PlanStatus]:
        ticket = self.m.get("ticket", ticket_id)
        it = _i((ticket or {}).get("iteration"))
        plans = [p for p in self.m.rows("data_plan") if _i(p.get("ticket_id")) == ticket_id
                 and _i(p.get("iteration")) == it]
        return [self.plan_status(_i(p["id"])) for p in sorted(plans, key=lambda p: _i(p["id"]))]

    def collection_done(self, ticket_id: int) -> bool:
        """For the flywheel: every approved marketplace plan of this iteration hit its target."""
        sts = [s for s in self.ticket_status(ticket_id) if s.data_type in MARKET_DATA_TYPES
               and s.state not in ("awaiting_approval", "closed")]
        return bool(sts) and all(s.done for s in sts)

    # ---- writes
    def _instructions(self, plan: dict, ticket: dict, price: int) -> str:
        desc = (ticket.get("task_description") or "the task").strip()
        if plan.get("data_type") == "clean_teleop":
            text = (f"Record one full, clean demonstration of: {desc}. Start from the reset scene, "
                    f"do the whole task once, smoothly and without pauses, then stop recording.")
        else:
            labels = [c.get("label") for c in (self.m.get("failure_cluster", _i(i))
                                               for i in plan.get("failure_cluster_ids") or []) if c]
            fail = f" Failures we are fixing: {', '.join(l for l in labels if l)}." if labels else ""
            text = (f"The robot is attempting: {desc}. When it pauses in a failure, take over, "
                    f"recover and finish the task, then hand control back.{fail}")
        return f"{text} Pays {_usd(price)} once the reviewer accepts the episode."

    def _policy_prompt(self, ticket_id: int, ticket: dict) -> str:
        rs = self.m.get("run_settings", ticket_id) or {}
        return (_json(rs.get("hyperparams_json")).get("prompt") or ticket.get("task_description") or "").strip()

    def dispatch_plan(self, plan_id: int) -> PlanStatus:
        """Create the plan's missing tasks (or cancel the leftovers once the target is reached)."""
        plan = self.m.get("data_plan", plan_id)
        if plan is None:
            return self.plan_status(plan_id)
        tid = _i(plan["ticket_id"])
        with self.lock(tid):
            st = self.plan_status(plan_id)
            base = {"ticket_id": tid, "data_plan_id": plan_id, "data_type": st.data_type}
            if st.state == "target_reached":
                for task_id in st.open_task_ids:
                    try:
                        self.w.call_reducer("cancel_task", task_id, "target_reached")
                        st.cancelled_task_ids.append(task_id)
                    except Exception as e:  # noqa: BLE001 - claimed in the meantime: let it finish
                        log.info("dispatch.cancel_skipped", extra={"fields": {"task_id": task_id, "error": str(e)}})
                self.event("dispatch.target_reached", {**base, "accepted": st.accepted, "target": st.target,
                                                       "cancelled_task_ids": st.cancelled_task_ids},
                           dedup=("target", plan_id))
                return st
            if st.state in ("collecting", "covered_by_offer") and st.offer_reserved:
                excess = st.accepted + st.in_flight - st.target
                for task_id in sorted(st.open_task_ids, reverse=True)[:max(0, excess)]:
                    try:
                        self.w.call_reducer("cancel_task", task_id, "offer_reserved")
                        st.cancelled_task_ids.append(task_id)
                        st.in_flight -= 1
                    except Exception as e:  # noqa: BLE001 - claimed in the meantime: let it finish
                        log.info("dispatch.cancel_skipped", extra={"fields": {"task_id": task_id, "error": str(e)}})
                if st.cancelled_task_ids:
                    st.open_task_ids = [t for t in st.open_task_ids if t not in st.cancelled_task_ids]
                    self.event("dispatch.offer_reserved", {
                        **base, "offer_ids": st.offer_ids, "offer_reserved": st.offer_reserved,
                        "plan_amount": st.plan_amount, "paid_target": st.target,
                        "cancelled_task_ids": st.cancelled_task_ids})
            if st.state == "covered_by_offer":
                self.event("dispatch.covered_by_offer", {
                    **base, "offer_ids": st.offer_ids, "offer_reserved": st.offer_reserved,
                    "offer_reserved_cents": st.offer_reserved_cents, "plan_amount": st.plan_amount},
                    dedup=("offer", plan_id))
                return st
            if st.state != "collecting":
                return st
            unit = st.unit_price_cents
            need = min(st.target, st.payable) - st.accepted - st.in_flight
            if need <= 0:
                if st.payable < st.target and st.accepted + st.in_flight >= st.payable:
                    approved = _i(plan.get("approved_cents"))
                    st.state = "budget_exhausted"
                    st.reason = (f"approved spend {_usd(approved)} pays for {st.payable} of {st.target} x "
                                 f"{_usd(unit)}; {st.accepted} accepted, {st.in_flight} in flight")
                    self.event("dispatch.budget_exhausted", {
                        **base, "limit": "approved_cents", "need": st.target - st.accepted - st.in_flight,
                        "creating": 0, "payable": st.payable, "target": st.target, "approved_cents": approved,
                        "unit_price_cents": unit, "accepted": st.accepted, "in_flight": st.in_flight,
                        "budget_remaining_cents": st.budget_remaining_cents, "reason": st.reason},
                        dedup=("cap", plan_id))
                return st
            afford = max(st.budget_remaining_cents, 0) // unit if unit else 0
            n = min(need, afford)
            if n < need:
                st.state = "budget_exhausted"
                st.reason = (f"need {need} x {_usd(unit)} but only {_usd(max(st.budget_remaining_cents, 0))} "
                             f"of ticket budget is left")
                self.event("dispatch.budget_exhausted", {
                    **base, "limit": "ticket_budget", "need": need, "creating": n, "unit_price_cents": unit,
                    "budget_remaining_cents": st.budget_remaining_cents, "reason": st.reason}, dedup=("budget", plan_id))
            if n <= 0:
                return st
            ticket = self.m.get("ticket", tid) or {}
            instructions = self._instructions(plan, ticket, unit)
            payload = {"instructions": instructions, "data_plan_id": plan_id, "ticket_id": tid,
                       "robot_id": _i(ticket.get("robot_id")), "data_type": st.data_type,
                       "policy_prompt": self._policy_prompt(tid, ticket),
                       "task_description": ticket.get("task_description") or "", "source": "dispatcher"}
            def plan_ids() -> set[int]:
                return {_i(t["id"]) for t in self._tasks(tid) if _i(t.get("data_plan_id")) == plan_id}

            before = plan_ids()
            made = 0
            for _ in range(n):
                try:
                    self.w.call_reducer("create_task", tid, st.data_type, unit, json.dumps(payload))
                    made += 1
                except Exception as e:  # noqa: BLE001
                    self.event("dispatch.error", {**base, "op": "create_task", "error": str(e)[:500]})
                    break
            # Hold the ticket lock until the mirror shows the new rows, so the next reconcile counts them.
            self._wait_for(lambda: len(plan_ids() - before) >= made)
            st.created_task_ids = sorted(plan_ids() - before)
            st.in_flight += made
            st.open_task_ids += st.created_task_ids
            self.event("dispatch.plan_dispatched", {
                **base, "created": made, "created_task_ids": st.created_task_ids, "unit_price_cents": unit,
                "target": st.target, "payable": st.payable, "accepted": st.accepted, "in_flight": st.in_flight,
                "approved_cents": _i(plan.get("approved_cents")), "plan_amount": st.plan_amount,
                "offer_reserved": st.offer_reserved, "offer_reserved_cents": st.offer_reserved_cents,
                "offer_ids": st.offer_ids,
                "budget_remaining_cents": st.budget_remaining_cents - made * unit})
            if made:
                self.page(tid, st.created_task_ids, st.data_type, unit)
            return st

    def dispatch_ticket(self, ticket_id: int) -> list[PlanStatus]:
        return [self.dispatch_plan(s.plan_id) for s in self.ticket_status(ticket_id)
                if s.state in ("collecting", "target_reached", "covered_by_offer")]

    def cancel_open_tasks(self, ticket_id: int, reason: str, data_plan_id: int | None = None) -> list[int]:
        out = []
        with self.lock(ticket_id):
            for t in self._tasks(ticket_id):
                if t.get("status") != "open" or (data_plan_id is not None and _i(t.get("data_plan_id")) != data_plan_id):
                    continue
                try:
                    self.w.call_reducer("cancel_task", _i(t["id"]), reason)
                    out.append(_i(t["id"]))
                except Exception as e:  # noqa: BLE001
                    log.info("dispatch.cancel_skipped", extra={"fields": {"task_id": t["id"], "error": str(e)}})
            if out:
                self.event("dispatch.tasks_cancelled", {"ticket_id": ticket_id, "data_plan_id": data_plan_id,
                                                        "task_ids": out, "reason": reason})
        return out

    def page(self, ticket_id: int, task_ids: list[int], task_type: str, price: int) -> None:
        online = [_i(c["id"]) for c in self.m.rows("contributor")
                  if c.get("online") and (c.get("kind") or "contributor") == "contributor"]
        req = PageRequest(ticket_id, list(task_ids), task_type, price, online)
        result: Any = None
        channel = "none"
        if _page_hook is not None:
            channel = "push_hook"
            try:
                result = _page_hook(req)
            except Exception as e:  # noqa: BLE001 - paging is best-effort; tasks already exist
                result = {"error": f"{type(e).__name__}: {e}"}
        extra: dict[str, Any] = {}
        for fn in list(_page_listeners):  # TDM-108 job drops (social posts), etc.
            try:
                r = fn(req, self.m.rows)
            except Exception as e:  # noqa: BLE001 - best-effort, like the push hook
                r = {"error": f"{type(e).__name__}: {e}"}
            if r is not None:
                extra[getattr(fn, "__module__", "listener").rsplit(".", 1)[-1]] = r
        # task.notified_json does not exist in the schema; this event is the notification record.
        self.event("dispatch.paged", {**asdict(req), "channel": channel, "result": result,
                                      "note": None if _page_hook else "no push hook installed (TDM-65)",
                                      **({"listeners": extra} if extra else {})})

    # ---- resets
    def _robot_jobs_open(self, robot_id: int) -> list[dict]:
        return [c for c in self.m.rows("robot_command") if _i(c.get("robot_id")) == robot_id
                and c.get("status") in OPEN_JOB and c.get("type") in JOB_TYPES]

    def _live_reset(self, robot_id: int, ref: str | None) -> dict | None:
        for t in self.m.rows("task"):
            if t.get("type") != "reset":
                continue
            if ref and task_ref(t) == ref:
                return t
            if _i(t.get("robot_id")) == robot_id and t.get("status") in ("open", "claimed"):
                return t
        return None

    def request_reset(self, robot_id: int, ticket_id: int, *, reason: str, ref: str | None = None,
                      context: dict | None = None) -> ResetResult:
        """Paid reset task for the robot (one live one per robot) + ``reset_request`` if the robot is idle."""
        ref = ref or f"{reason}:{robot_id}:{_now_us()}"
        with self.lock(ticket_id):
            existing = self._live_reset(robot_id, ref)
            if existing is not None:
                return ResetResult(_i(existing["id"]), False, "none", "reset already requested")
            ticket = self.m.get("ticket", ticket_id)
            base = {"ticket_id": ticket_id, "robot_id": robot_id, "reason": reason, "ref": ref, **(context or {})}
            if ticket is None or ticket.get("status") in TERMINAL_TICKET:
                return ResetResult(None, False, "none", "ticket closed")
            price = PRICES_CENTS["reset"]
            remaining = self.budget_remaining(ticket_id)
            if remaining < price:
                why = f"reset costs {_usd(price)} but only {_usd(max(remaining, 0))} of ticket budget is left"
                self.event("dispatch.budget_exhausted", {**base, "task_type": "reset", "limit": "ticket_budget", "need": 1, "creating": 0,
                                                         "unit_price_cents": price,
                                                         "budget_remaining_cents": remaining, "reason": why},
                           dedup=("budget_reset", ticket_id))
                return ResetResult(None, False, "none", why)
            desc = (ticket.get("task_description") or "the task").strip()
            instructions = (f"Reset the scene for: {desc}. Put every object back at its start position and "
                            f"clear the workspace, then mark the task done. Pays {_usd(price)}. [ref {ref}]")
            payload = {"instructions": instructions, "ticket_id": ticket_id, "robot_id": robot_id,
                       "reason": reason, "ref": ref, "source": "dispatcher", **(context or {})}
            try:
                self.w.call_reducer("create_task", ticket_id, "reset", price, json.dumps(payload))
            except Exception as e:  # noqa: BLE001
                self.event("dispatch.error", {**base, "op": "create_task(reset)", "error": str(e)[:500]})
                return ResetResult(None, False, "none", str(e))
            row = self._wait_for(lambda: next((t for t in self.m.rows("task")
                                               if t.get("type") == "reset" and task_ref(t) == ref), None))
            task_id = _i(row["id"]) if row else None
            cmd, why = "inline", "robot is running a job that waits for the reset"
            if not self._robot_jobs_open(robot_id):
                try:
                    self.w.call_reducer("issue_robot_command", robot_id, "reset_request",
                                        json.dumps({"task_id": task_id, "reason": reason, "ref": ref}), ticket_id)
                    cmd, why = "issued", ""
                except Exception as e:  # noqa: BLE001 - lost a race with a job command: same as busy
                    why = f"reset_request refused: {str(e)[:200]}"
            self.event("dispatch.reset_requested", {**base, "task_id": task_id, "price_cents": price,
                                                    "robot_command": cmd, "note": why})
            if task_id:
                self.page(ticket_id, [task_id], "reset", price)
            return ResetResult(task_id, True, cmd, why)

    def on_eval_trial(self, trial: dict, *, snapshot: bool = False) -> ResetResult | None:
        run = self.m.get("eval_run", _i(trial.get("eval_run_id")))
        if run is None:
            log.info("dispatch.skip_trial", extra={"fields": {"trial_id": trial.get("id"), "reason": "eval_run not in mirror"}})
            return None
        if snapshot:
            newest = max((_i(t.get("trial_index")) for t in self.m.rows("eval_trial")
                          if _i(t.get("eval_run_id")) == _i(run["id"])), default=-1)
            fresh = self.clock() * 1e6 - _i(trial.get("created_at")) < SNAPSHOT_WINDOW_S * 1e6
            if run.get("status") != "running" or _i(trial.get("trial_index")) != newest or not fresh:
                return None
        return self.request_reset(_i(run.get("robot_id")), _i(run.get("ticket_id")), reason="eval_trial",
                                  ref=f"eval_trial:{_i(trial['id'])}",
                                  context={"eval_run_id": _i(run["id"]), "trial_index": _i(trial.get("trial_index")),
                                           "trial_success": bool(trial.get("success"))})

    def on_robot_paused(self, robot: dict) -> ResetResult | None:
        rid = _i(robot["id"])
        takeover = [t for t in self.m.rows("task") if _i(t.get("robot_id")) == rid
                    and t.get("type") == "recovery_takeover" and t.get("status") in ("open", "claimed")]
        if takeover:
            log.info("dispatch.pause_is_takeover", extra={"fields": {"robot_id": rid, "task_ids": [t["id"] for t in takeover]}})
            return None  # the paused failure is what the takeover contributor records
        tickets = [t for t in self.m.rows("ticket") if _i(t.get("robot_id")) == rid
                   and t.get("status") not in TERMINAL_TICKET]
        if not tickets:
            return None
        ticket = max(tickets, key=lambda t: _i(t["id"]))
        return self.request_reset(rid, _i(ticket["id"]), reason="robot_paused",
                                  ref=f"robot_paused:{rid}:{_i(robot.get('last_heartbeat')) or _now_us()}")

    def on_reset_completed(self, task: dict, *, snapshot: bool = False) -> str | None:
        """Pay the reset (no review for resets) and release the robot with ``resume_policy``."""
        task_id, rid, tid = _i(task["id"]), _i(task.get("robot_id")), _i(task.get("ticket_id"))
        if snapshot and self.clock() * 1e6 - _i(task.get("completed_at")) > SNAPSHOT_WINDOW_S * 1e6:
            return None
        with self.lock(tid):
            if _i(task.get("assignee_id")):
                try:
                    self.w.call_reducer("reserve_task_payment", task_id)
                except Exception as e:  # noqa: BLE001 - payment ticket can retry; resume regardless
                    self.event("dispatch.error", {"ticket_id": tid, "task_id": task_id,
                                                  "op": "reserve_task_payment", "error": str(e)[:500]})
            for c in self.m.rows("robot_command"):
                if (_i(c.get("robot_id")) == rid and c.get("type") == "resume_policy"
                        and _i(_json(c.get("payload_json")).get("task_id")) == task_id):
                    return "already_resumed"
            try:
                self.w.call_reducer("issue_robot_command", rid, "resume_policy",
                                    json.dumps({"reason": "reset_done", "task_id": task_id, "ref": task_ref(task)}), tid)
            except Exception as e:  # noqa: BLE001
                self.event("dispatch.error", {"ticket_id": tid, "task_id": task_id, "robot_id": rid,
                                              "op": "issue_robot_command(resume_policy)", "error": str(e)[:500]})
                return "error"
            self.event("dispatch.resume_issued", {"ticket_id": tid, "task_id": task_id, "robot_id": rid,
                                                  "ref": task_ref(task)})
            return "issued"


    def complete_on_accept(self, episode: dict) -> str | None:
        """The clean_teleop task's episode was accepted while the claimant still holds it: complete the
        task for them (one paid episode per task). That clears the arm-control lease, so the robot's
        next arm-control task can be claimed, and ends the teleop session (``on_teleop_task``)."""
        if episode.get("review_status") != "accepted":
            return None
        task = self.m.get("task", _i(episode.get("task_id")))
        if not task or task.get("type") != "clean_teleop" or task.get("status") != "claimed":
            return None
        try:
            self.w.call_reducer("complete_task", _i(task["id"]), json.dumps(
                {"by": ACTOR, "reason": "episode_accepted", "episode_id": _i(episode["id"])}))
        except Exception as e:  # noqa: BLE001 - e.g. the claimant completed it a moment earlier
            log.info("dispatch.complete_skipped", extra={"fields": {"task_id": task["id"], "error": str(e)[:300]}})
            return "skipped"
        self.event("dispatch.task_completed_on_accept", {"ticket_id": _i(task.get("ticket_id")), "task_id": _i(task["id"]),
                                                         "episode_id": _i(episode["id"])})
        return "completed"

    # ---- clean teleop sessions (TDM-52 executor)
    def on_teleop_task(self, task: dict, old: dict | None = None) -> str | None:
        """A clean_teleop task was claimed: start the robot's teleop session (``start_teleop`` {task_id},
        the TDM-52 job that drives from /ws/control); it left ``claimed``: ``stop_teleop``. Without this
        the claimant's commands reach the robot but nothing drives (the hub only drives inside a
        start_teleop job or a paused-rollout takeover). Recovery takeovers never get one (TDM-69: the
        rollout holds the job lock)."""
        if task.get("type") != "clean_teleop":
            return None
        task_id, rid, tid = _i(task["id"]), _i(task.get("robot_id")), _i(task.get("ticket_id"))
        st, was = task.get("status"), (old or {}).get("status")
        if not rid or st == was:
            return None
        with self.lock(tid):
            cmds = self.m.rows("robot_command")
            mine = [c for c in cmds if _i(c.get("robot_id")) == rid and c.get("type") == "start_teleop"
                    and c.get("status") in OPEN_JOB and _i(_json(c.get("payload_json")).get("task_id")) == task_id]
            base = {"ticket_id": tid, "task_id": task_id, "robot_id": rid}
            if st == "claimed":
                if mine:
                    return "already_started"
                busy = self._robot_jobs_open(rid)
                if busy:
                    self.event("dispatch.teleop_blocked", {**base, "open_jobs": [_i(c["id"]) for c in busy],
                                                           "note": "robot is running another job; no teleop session"})
                    return "robot_busy"
                try:
                    self.w.call_reducer("issue_robot_command", rid, "start_teleop", json.dumps(
                        {"task_id": task_id, "kind": "clean_teleop", "contributor_id": _i(task.get("assignee_id")),
                         "ref": f"teleop:{task_id}", "by": ACTOR}), tid)
                except Exception as e:  # noqa: BLE001
                    self.event("dispatch.error", {**base, "op": "issue_robot_command(start_teleop)", "error": str(e)[:500]})
                    return "error"
                self.event("dispatch.teleop_started", base)
                return "issued"
            if not mine:
                return None
            if any(c.get("type") == "stop_teleop" and c.get("status") in OPEN_JOB and _i(c.get("robot_id")) == rid
                   and _i(_json(c.get("payload_json")).get("task_id")) == task_id for c in cmds):
                return "already_stopping"
            try:
                self.w.call_reducer("issue_robot_command", rid, "stop_teleop",
                                    json.dumps({"task_id": task_id, "reason": f"task {st}", "by": ACTOR}), tid)
            except Exception as e:  # noqa: BLE001
                self.event("dispatch.error", {**base, "op": "issue_robot_command(stop_teleop)", "error": str(e)[:500]})
                return "error"
            self.event("dispatch.teleop_stopped", {**base, "task_status": st})
            return "stopped"

    # ---- TDM-86: lapsed claims and waiting teleop sessions
    def sweep_leases(self) -> dict:
        """A claim whose 60 s lease lapsed (the claimant closed the tab) stays ``claimed`` until some
        caller runs ``expire_task_leases``; until then its start_teleop job holds the robot and the next
        claimant's page gets ``wrong_task`` for every command. So: expire lapsed claims, then start the
        teleop session of any live clean_teleop claim that was blocked while the robot was busy."""
        now_us = int(self.clock() * 1_000_000)
        tasks = self.m.rows("task")
        out: dict = {"expired": False, "started": []}
        if any(t.get("status") == "claimed" and 0 < _i(t.get("lease_expires_at")) < now_us - 2_000_000 for t in tasks):
            self.w.call_reducer("expire_task_leases")
            out["expired"] = True
            return out  # the task updates come back through the bus; start waiting sessions next tick
        cmds = self.m.rows("robot_command")
        for t in tasks:
            if t.get("type") != "clean_teleop" or t.get("status") != "claimed" or _i(t.get("lease_expires_at")) <= now_us:
                continue
            rid, task_id = _i(t.get("robot_id")), _i(t["id"])
            if not rid or self._robot_jobs_open(rid):
                continue
            if any(c.get("type") == "start_teleop" and _i(_json(c.get("payload_json")).get("task_id")) == task_id
                   and c.get("status") in OPEN_JOB for c in cmds):
                continue
            if self.on_teleop_task(t, {"status": "open"}) == "issued":
                out["started"].append(task_id)
        return out

    def start_lease_sweeper(self, every_s: float) -> None:
        if every_s <= 0 or getattr(self, "_sweeper", None) is not None:
            return

        def loop() -> None:
            stop = threading.Event()
            while not stop.wait(every_s):
                try:
                    r = self.sweep_leases()
                    if r["expired"] or r["started"]:
                        log.info("dispatch.lease_sweep", extra={"fields": r})
                except Exception:  # noqa: BLE001 - the sweeper must never die
                    log.exception("dispatch.lease_sweep_error")

        self._sweeper = threading.Thread(target=loop, name="dispatch-lease-sweeper", daemon=True)
        self._sweeper.start()


# --------------------------------------------------------------------------- per-bus singleton

def _default_writer() -> Writer:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)


make_writer: Callable[[], Writer] = _default_writer  # swappable for tests
_instances: dict[int, Dispatcher] = {}
_instances_lock = threading.Lock()


def get_dispatcher(bus: Mirror) -> Dispatcher:
    with _instances_lock:
        d = _instances.get(id(bus))
        if d is None or d.m is not bus:
            d = _instances[id(bus)] = Dispatcher(bus, lambda: make_writer())
            d.start_lease_sweeper(float(os.environ.get("RDF_LEASE_SWEEP_S", "10") or 0))  # TDM-86
        return d
