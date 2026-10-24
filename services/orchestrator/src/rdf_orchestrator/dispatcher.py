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
  * Resets: one live reset task per robot. ``reset_request`` is issued only when no job command is
    open on the robot (TDM-13 job lock); during a ``run_eval`` the robot's own job waits for the
    reset, and the completed reset task releases it with ``resume_policy``.

Dedup is from rows, not memory (restart-safe): plan tasks by ``task.data_plan_id``; resets by a
``[ref ...]`` tag in ``task.instructions``; resumes by ``task_id`` in ``robot_command.payload_json``.
"""
from __future__ import annotations

import json
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
    state: str  # awaiting_approval | collecting | target_reached | budget_exhausted | closed | not_marketplace
    # budget_exhausted: ticket budget (budget - spent - committed) or the approved cents cannot pay for more
    target: int = 0  # plan.amount: accepted episodes wanted
    payable: int = 0  # approved_cents // unit_price: episodes the approval can pay for
    unit_price_cents: int = 0
    accepted: int = 0
    in_flight: int = 0  # live tasks + completed ones awaiting review
    open_task_ids: list[int] = field(default_factory=list)
    created_task_ids: list[int] = field(default_factory=list)  # by this call
    cancelled_task_ids: list[int] = field(default_factory=list)  # by this call
    budget_remaining_cents: int = 0
    reason: str = ""

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
        st = PlanStatus(plan_id=plan_id, ticket_id=tid, data_type=plan.get("data_type") or "",
                        state="collecting", target=_i(plan.get("amount")), unit_price_cents=unit,
                        payable=_i(plan.get("approved_cents")) // unit if unit else 0)
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
                "approved_cents": _i(plan.get("approved_cents")),
                "budget_remaining_cents": st.budget_remaining_cents - made * unit})
            if made:
                self.page(tid, st.created_task_ids, st.data_type, unit)
            return st

    def dispatch_ticket(self, ticket_id: int) -> list[PlanStatus]:
        return [self.dispatch_plan(s.plan_id) for s in self.ticket_status(ticket_id) if s.state == "collecting"
                or s.state == "target_reached"]

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
        # task.notified_json does not exist in the schema; this event is the notification record.
        self.event("dispatch.paged", {**asdict(req), "channel": channel, "result": result,
                                      "note": None if _page_hook else "no push hook installed (TDM-65)"})

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
        return d
