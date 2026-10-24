"""Flywheel state machine (TDM-22): drives one ticket through the deployment loop from SpacetimeDB rows.

    created -> onboarding -> baseline -> diagnose -> plan_data -> dispatch -> review -> train -> eval
                                            ^                       ^          |                 |
                                            |                       +-- review rejected short ---+
                                            +------------- eval below threshold (iteration += 1) +
    eval -> shipped        (success rate meets run_settings.success_threshold)
    any  -> failed         (max_iterations reached, budget exhausted, a step failed too often)

Status names are the TDM-9 enum (``spacetime/schema.md``): diagnosing=diagnose, planning=plan_data,
collecting=dispatch, reviewing=review, training=train, evaluating=eval. Every edge here is an edge
of ``set_ticket_status``'s transition table; the reducer is the final authority and also bumps
``ticket.iteration`` on the eval loop-back.

Two halves, both restart-safe:

* **Exit conditions** (``decide``) are a pure function of the ticket's rows (``TicketView``), never
  of process memory. The handlers re-run it whenever a row of the ticket changes, plus a reconcile
  tick, so after a restart or a resync the machine re-derives where every ticket is and moves on.
  Each transition calls ``set_ticket_status`` (which logs ``ticket.status_changed`` with the reason
  as ``note``) and appends a ``flywheel.transition`` event with the evidence the decision used.
* **Entry actions** (``Step``) kick off a stage's work when a ticket enters a status (bus
  ``on_enter``, which also fires for existing rows at boot). Steps are pluggable: the planner,
  reviewer, dispatcher, Modal client and eval tickets register theirs with ``register_step``.
  Until they do, the stage's default is ``NotImplementedStep``, which logs ``step.not_implemented``
  and leaves the ticket waiting there. A stage whose owner wired its own bus handler instead
  (diagnose TDM-23, plan_data TDM-24, dispatch TDM-26) is a ``BusHandlerStep``: the flywheel only
  checks the handler is registered. Steps must be idempotent: they can run again for the same
  stage after a restart.

  To wire a stage:  ``from rdf_orchestrator.handlers.flywheel import FLYWHEEL``;
  ``FLYWHEEL.register_step("review", MyStep())`` where MyStep has ``stage``, ``owner`` and
  ``run(ctx)`` (sync or async; ``ctx.view`` is a fresh ``TicketView``, ``ctx.call``/``call_sync``
  call reducers). Or keep a plain bus handler and add its name to that stage's ``BusHandlerStep``.

Wired here: ``train`` (``QueueTrainingStep`` queues the fine-tune on every accepted episode; the
Modal client picks the queued row up) and ``shipped`` (``ShipStep``: before/after summary, then
TDM-70's ship: ``mark_checkpoint_deployed`` sets ``ticket.deployed_checkpoint_uri``, the policy
server is activated on it, and the TDM-71 report is saved). TDM-67 registers ``EvalStep`` for
``baseline``/``eval`` (handlers/evaluate.py).

TDM-70 loop rule: after a finalized candidate eval, ship if it meets the threshold; else, if
``iteration + 1 < max_iterations`` and budget remains, loop to ``diagnose`` (the new checkpoint
fails differently, so its failures are re-clustered before the planner picks the next data);
else ``failed`` with the stop reason. ``flywheel.wait`` is logged when a ticket starts waiting in
a stage and then at most every ``wait_log_s`` while only the reason (e.g. a count) changes.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from .log import get_logger, ticket_id_var
from .stats import explain_threshold, format_ci, meets_threshold

log = get_logger("flywheel")

# --------------------------------------------------------------------------- statuses

TERMINAL = frozenset({"shipped", "failed", "cancelled"})
ACTIVE = ("created", "onboarding", "baseline", "diagnose", "plan_data", "dispatch", "review", "train", "eval")
STEP_STAGES = ("onboarding", "baseline", "diagnose", "plan_data", "dispatch", "review", "train", "eval", "shipped")

# Mirror of TDM9_TICKET_TRANSITIONS (spacetime/spacetimedb/src/reducers/tickets.ts); any active
# status may also go to failed or cancelled. Used to refuse a bad manual nudge before calling.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("onboarding", "baseline"),
    "onboarding": ("baseline",),
    "baseline": ("diagnose",),
    "diagnose": ("plan_data",),
    "plan_data": ("dispatch",),
    "dispatch": ("review",),
    "review": ("train", "dispatch"),
    "train": ("eval",),
    "eval": ("shipped", "diagnose", "plan_data", "dispatch"),
}
LOOP_TARGETS = ("diagnose", "plan_data", "dispatch")
# Default forward edge for a manual nudge. eval has none on purpose: shipping or looping is a
# decision about eval numbers, so a human must name it.
FORWARD = {"created": "onboarding", "onboarding": "baseline", "baseline": "diagnose", "diagnose": "plan_data",
           "plan_data": "dispatch", "dispatch": "review", "review": "train", "train": "eval"}

DATASET_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
PLAN_LIVE = ("approved", "executing", "done")
PI05_BASE_CHECKPOINT = "gs://openpi-assets/checkpoints/pi05_base/params"  # = TDM12_PI05_BASE_CHECKPOINT
MAX_STEP_FAILURES = 2  # failed training runs / eval runs per stage before the ticket is failed
# Terminal "retries exhausted" events from the Claude-backed steps (llm_retry.py): the ticket is failed.
STEP_FAILED_EVENTS = ("diagnose.failed", "planner.failed")

WATCHED_TABLES = ("ticket", "run_settings", "robot", "dataset", "failure_cluster", "data_plan", "episode",
                  "training_run", "eval_run", "eval_trial", "event_log", "dataset_offer")
OPEN_OFFER = ("proposed", "accepted", "ingesting", "qa")  # TDM-14 offer statuses that still hold slots


def _allowed(frm: str, to: str) -> bool:
    if frm in TERMINAL:
        return False
    return to in TRANSITIONS.get(frm, ()) or to in ("failed", "cancelled")


# --------------------------------------------------------------------------- derived view

def _latest(rows: list, key: str = "id"):
    return max(rows, key=lambda r: r.get(key) or 0) if rows else None


@dataclass
class TicketView:
    """Everything the machine knows about one ticket, read from the bus mirror (never from memory)."""

    ticket: dict
    settings: dict | None = None
    robot: dict | None = None
    dataset: dict | None = None
    clusters: list = field(default_factory=list)  # all iterations
    plans: list = field(default_factory=list)
    episodes: list = field(default_factory=list)
    training_runs: list = field(default_factory=list)
    eval_runs: list = field(default_factory=list)
    offers: list = field(default_factory=list)  # dataset_offer rows (TDM-95)
    step_failed: list = field(default_factory=list)  # STEP_FAILED_EVENTS payloads (+ "type"), id order

    @classmethod
    def from_bus(cls, bus, ticket_id: int) -> "TicketView | None":
        t = bus.get("ticket", ticket_id)
        if t is None:
            return None

        def of(table):
            return sorted((r for r in bus.rows(table) if r.get("ticket_id") == ticket_id), key=lambda r: r.get("id") or 0)

        robot = bus.get("robot", t.get("robot_id"))
        ds = of("dataset")
        failed = [{**_payload(r), "type": r.get("type"), "event_id": r.get("id")} for r in sorted(
            (r for r in bus.rows("event_log") if r.get("type") in STEP_FAILED_EVENTS), key=lambda r: r.get("id") or 0)]
        v = cls(ticket=t, settings=bus.get("run_settings", ticket_id), robot=robot, dataset=ds[0] if ds else None,
                   clusters=of("failure_cluster"), plans=of("data_plan"), episodes=of("episode"),
                   training_runs=of("training_run"), eval_runs=of("eval_run"), offers=of("dataset_offer"))
        v.step_failed = [p for p in failed if p.get("ticket_id") == ticket_id]
        return v

    # ---- ticket + settings
    @property
    def id(self) -> int:
        return self.ticket["id"]

    @property
    def status(self) -> str:
        return self.ticket["status"]

    @property
    def iteration(self) -> int:
        return int(self.ticket.get("iteration") or 0)

    def setting(self, name: str, default: Any) -> Any:
        s = self.settings or {}
        v = s.get(name)
        return default if v is None else v

    @property
    def max_iterations(self) -> int:
        return int(self.setting("max_iterations", 3))

    @property
    def threshold_settings(self) -> dict:
        return {"success_threshold": float(self.setting("success_threshold", self.ticket.get("target_success") or 0.8)),
                "require_ci_low": bool(self.setting("require_ci_low", False))}

    # ---- money: payouts (reserved + paid) plus GPU spend, against the hard cap
    @property
    def gpu_cents(self) -> int:
        return sum(int(r.get("gpu_cost_cents") or 0) for r in self.training_runs)

    @property
    def spent_total_cents(self) -> int:
        return int(self.ticket.get("spent_cents") or 0) + self.gpu_cents

    @property
    def budget_cents(self) -> int:
        return int(self.ticket.get("budget_cents") or 0)

    @property
    def remaining_cents(self) -> int:
        return self.budget_cents - self.spent_total_cents

    def money(self) -> dict:
        return {"budget_cents": self.budget_cents, "spent_cents": int(self.ticket.get("spent_cents") or 0),
                "gpu_cents": self.gpu_cents, "remaining_cents": self.remaining_cents}

    # ---- robot
    @property
    def onboarded(self) -> bool:
        return bool(self.robot and (self.robot.get("adapter_version") or "").strip())

    # ---- evals
    def evals(self, *, kind: str | None = None, status: str | None = None, training_run_id: int | None = None) -> list:
        out = self.eval_runs
        if kind is not None:
            out = [r for r in out if r.get("kind") == kind]
        if status is not None:
            out = [r for r in out if r.get("status") == status]
        if training_run_id is not None:
            out = [r for r in out if r.get("training_run_id") == training_run_id]
        return out

    @property
    def baseline_eval(self):
        return _latest(self.evals(kind="baseline", status="done"))

    @property
    def last_done_eval(self):
        return _latest(self.evals(status="done"))

    # ---- this iteration
    def of_iteration(self, rows: list) -> list:
        return [r for r in rows if int(r.get("iteration") or 0) == self.iteration]

    @property
    def iteration_clusters(self) -> list:
        return self.of_iteration(self.clusters)

    @property
    def plan(self):
        """Latest non-rejected data plan of this iteration."""
        return _latest([p for p in self.of_iteration(self.plans) if p.get("status") != "rejected"])

    @property
    def iteration_training_runs(self) -> list:
        return self.of_iteration(self.training_runs)

    @property
    def training_run(self):
        """This iteration's live or finished run (queued/running/done), else the latest one."""
        runs = self.iteration_training_runs
        good = [r for r in runs if r.get("status") in ("queued", "running", "done")]
        return _latest(good) or _latest(runs)

    @property
    def done_training_run(self):
        return _latest([r for r in self.iteration_training_runs if r.get("status") == "done"])

    @property
    def candidate_eval(self):
        run = self.done_training_run
        return _latest(self.evals(status="done", training_run_id=run["id"])) if run else None

    # ---- collection since this iteration's plan
    def collected(self) -> list:
        p = self.plan
        if p is None:
            return []
        since = p.get("created_at") or 0
        return [e for e in self.episodes if e.get("kind") in DATASET_KINDS and (e.get("created_at") or 0) >= since]

    def collection(self) -> dict:
        eps = self.collected()
        by = {s: sum(1 for e in eps if e.get("review_status") == s) for s in ("pending", "accepted", "rejected")}
        return {"collected": len(eps), **by, "non_rejected": by["pending"] + by["accepted"],
                "amount": int(self.plan["amount"]) if self.plan else 0,
                "dataset_accepted_count": int((self.dataset or {}).get("accepted_count") or 0)}

    def open_offers(self) -> list[dict]:
        """TDM-95: open dataset offers made since this iteration's plan, with their delivery count.
        Purchased episodes count toward the plan like task episodes (``DATASET_KINDS``); an open
        offer means the seller may still deliver (``handlers/plan_offers`` times it out)."""
        p = self.plan
        if p is None:
            return []
        since = p.get("created_at") or 0
        out = []
        for o in self.offers:
            if o.get("status") in OPEN_OFFER and (o.get("created_at") or 0) >= since:
                eps = [e for e in self.episodes if e.get("dataset_offer_id") == o["id"] and e.get("kind") == "purchased"]
                out.append({"offer_id": o["id"], "status": o.get("status"), "needed_count": o.get("needed_count"),
                            "delivered": len(eps), "accepted": sum(1 for e in eps if e.get("review_status") == "accepted")})
        return out

    def gave_up(self, type_: str, **match: Any) -> dict | None:
        """The newest ``type_`` (STEP_FAILED_EVENTS) payload of this ticket matching ``match``."""
        return next((p for p in reversed(self.step_failed)
                     if p.get("type") == type_ and all(p.get(k) == want for k, want in match.items())), None)

    @property
    def accepted_episode_ids(self) -> list[int]:
        """The per-ticket training set: every accepted dataset episode, all iterations."""
        return [e["id"] for e in self.episodes if e.get("kind") in DATASET_KINDS and e.get("review_status") == "accepted"]


# --------------------------------------------------------------------------- decision

@dataclass
class Decision:
    to: str | None  # None: stay and wait
    reason: str
    evidence: dict = field(default_factory=dict)
    retry_step: bool = False  # re-run the current stage's step (a run failed, retries remain)
    approve_plan: dict | None = None  # plan_data: a proposed plan the auto-approver may approve


def _eval_ev(r) -> dict:
    return {"eval_run_id": r["id"], "kind": r.get("kind"), "successes": r.get("successes"), "trials": r.get("trials"),
            "ci_low": r.get("ci_low"), "ci_high": r.get("ci_high"), "checkpoint_uri": r.get("checkpoint_uri")}


def _gave_up_ev(p: dict) -> dict:
    return {k: p.get(k) for k in ("type", "event_id", "eval_run_id", "iteration", "attempts", "kinds", "last_error")}


def _budget_stop(v: TicketView, where: str) -> Decision | None:
    if v.remaining_cents <= 0:
        return Decision("failed", f"budget exhausted {where}: spent {v.spent_total_cents}c "
                                  f"(payouts {v.money()['spent_cents']}c + GPU {v.gpu_cents}c) of {v.budget_cents}c",
                        {"stop": "budget", **v.money()})
    return None


def decide(v: TicketView) -> Decision:
    """The exit condition of the ticket's current status, from its rows only."""
    s, it = v.status, v.iteration
    if s in TERMINAL:
        return Decision(None, f"terminal ({s})")

    if s in ("created", "onboarding"):
        if v.onboarded:
            return Decision("baseline", f"robot {v.robot['id']} onboarded (adapter {v.robot.get('adapter_version')})",
                            {"robot_id": v.robot["id"], "adapter_version": v.robot.get("adapter_version")})
        if s == "created":
            return Decision("onboarding", "robot has no adapter yet; onboarding",
                            {"robot_id": v.ticket.get("robot_id")})
        return Decision(None, "waiting for the onboarding step to set the robot adapter")

    if s == "baseline":
        b = v.baseline_eval
        if b:
            return Decision("diagnose", f"baseline eval {b['id']} finalized: {format_ci(b['successes'], b['trials'])}",
                            _eval_ev(b))
        failed = v.evals(kind="baseline", status="failed")
        if len(failed) >= MAX_STEP_FAILURES:
            return Decision("failed", f"baseline eval failed {len(failed)} times", {"stop": "step_failures",
                                                                                    "eval_run_ids": [r["id"] for r in failed]})
        running = v.evals(kind="baseline", status="running")
        return Decision(None, "waiting for the baseline eval" + (f" (run {running[-1]['id']}: "
                                                                f"{running[-1].get('completed')}/{running[-1].get('trials')})"
                                                                if running else ""),
                        retry_step=bool(failed) and not running)

    if s in ("diagnose", "plan_data", "dispatch", "review"):
        stop = _budget_stop(v, f"in {s}")
        if stop:
            return stop

    if s == "diagnose":
        cl = v.iteration_clusters
        if cl:
            return Decision("plan_data", f"{len(cl)} failure cluster(s) for iteration {it}: "
                                         + ", ".join(f"{c.get('label')} x{c.get('count')}" for c in cl),
                            {"failure_cluster_ids": [c["id"] for c in cl], "iteration": it})
        src = v.last_done_eval
        if src and int(src.get("successes") or 0) >= int(src.get("trials") or 0) > 0:
            return Decision("plan_data", f"eval {src['id']} had no failures to cluster", _eval_ev(src))
        gave_up = v.gave_up("diagnose.failed", eval_run_id=src["id"]) if src else None
        if gave_up:
            return Decision("failed", f"diagnose failed {gave_up.get('attempts')} times on eval {src['id']}: "
                                      f"{gave_up.get('last_error')}", {"stop": "step_failures", **_gave_up_ev(gave_up)})
        return Decision(None, f"waiting for failure clusters for iteration {it}")

    if s == "plan_data":
        p = v.plan
        if p is None:
            gave_up = v.gave_up("planner.failed", iteration=it)
            if gave_up:
                return Decision("failed", f"planner failed {gave_up.get('attempts')} times in iteration {it}: "
                                          f"{gave_up.get('last_error')}", {"stop": "step_failures", **_gave_up_ev(gave_up)})
            return Decision(None, f"waiting for the planner's data plan for iteration {it}")
        ev = {"data_plan_id": p["id"], "data_type": p.get("data_type"), "amount": p.get("amount"),
              "est_cost_cents": p.get("est_cost_cents"), "approved_cents": p.get("approved_cents"), **v.money()}
        if p.get("status") in PLAN_LIVE:
            return Decision("dispatch", f"data plan {p['id']} {p.get('status')}: {p.get('amount')} x "
                                        f"{p.get('data_type')} (est {p.get('est_cost_cents')}c)", ev)
        return Decision(None, f"waiting for approve_spend on data plan {p['id']} (est {p.get('est_cost_cents')}c)",
                        ev, approve_plan=p)

    if s == "dispatch":
        if v.plan is None:
            return Decision(None, f"no data plan for iteration {it}; nothing to dispatch")
        c = v.collection()
        if c["non_rejected"] >= c["amount"]:
            return Decision("review", f"{c['non_rejected']}/{c['amount']} episodes collected for plan {v.plan['id']}",
                            {"data_plan_id": v.plan["id"], **c})
        offers = v.open_offers()
        if offers:  # TDM-95: say what the wait is on (paid tasks only cover the slots no offer holds)
            c = {**c, "open_offers": offers}
            return Decision(None, f"collecting: {c['non_rejected']}/{c['amount']} episodes for plan {v.plan['id']}; "
                                  "waiting on dataset offer " + ", ".join(
                                      f"{o['offer_id']} ({o['status']}, {o['delivered']}/{o['needed_count']} delivered)"
                                      for o in offers), c)
        return Decision(None, f"collecting: {c['non_rejected']}/{c['amount']} episodes for plan {v.plan['id']}", c)

    if s == "review":
        if v.plan is None:
            return Decision(None, f"no data plan for iteration {it}")
        c = v.collection()
        ev = {"data_plan_id": v.plan["id"], **c}
        if c["accepted"] >= c["amount"]:
            return Decision("train", f"{c['accepted']}/{c['amount']} episodes accepted for plan {v.plan['id']} "
                                     f"(dataset accepted_count {c['dataset_accepted_count']})", ev)
        if c["pending"] == 0 and c["rejected"] > 0:
            return Decision("dispatch", f"rejections left the set short: {c['accepted']}/{c['amount']} accepted, "
                                        f"{c['rejected']} rejected; collecting more", ev)
        return Decision(None, f"reviewing: {c['accepted']}/{c['amount']} accepted, {c['pending']} pending", ev)

    if s == "train":
        done = v.done_training_run
        if done:
            return Decision("eval", f"training run {done['id']} done: {done.get('checkpoint_uri')}",
                            {"training_run_id": done["id"], "checkpoint_uri": done.get("checkpoint_uri"),
                             "gpu_cost_cents": done.get("gpu_cost_cents"), **v.money()})
        runs = v.iteration_training_runs
        failed = [r for r in runs if r.get("status") in ("failed", "cancelled")]
        live = [r for r in runs if r.get("status") in ("queued", "running")]
        if len(failed) >= MAX_STEP_FAILURES and not live:
            return Decision("failed", f"training failed {len(failed)} times in iteration {it}: "
                                      f"{failed[-1].get('error') or failed[-1].get('status')}",
                            {"stop": "step_failures", "training_run_ids": [r["id"] for r in failed]})
        return Decision(None, "waiting for training" + (f" (run {live[-1]['id']} {live[-1]['status']})" if live else ""),
                        retry_step=bool(failed) and not live)

    if s == "eval":
        run = v.done_training_run
        if run is None:
            return Decision(None, f"no finished training run for iteration {it}")
        e = v.candidate_eval
        if e is None:
            failed = v.evals(status="failed", training_run_id=run["id"])
            live = v.evals(status="running", training_run_id=run["id"])
            if len(failed) >= MAX_STEP_FAILURES and not live:
                return Decision("failed", f"candidate eval failed {len(failed)} times", {"stop": "step_failures"})
            return Decision(None, f"waiting for the candidate eval of training run {run['id']}",
                            retry_step=bool(failed) and not live)
        ts = v.threshold_settings
        ev = {**_eval_ev(e), **ts, "iteration": it, "max_iterations": v.max_iterations, **v.money()}
        b = v.baseline_eval
        if b:
            ev["baseline"] = _eval_ev(b)
        verdict = explain_threshold(e, ts)
        if meets_threshold(e, ts):
            return Decision("shipped", f"eval {e['id']}: {verdict}", ev)
        if it + 1 >= v.max_iterations:
            return Decision("failed", f"eval {e['id']}: {verdict}; max_iterations {v.max_iterations} reached "
                                      f"(iteration {it})", {**ev, "stop": "max_iterations"})
        stop = _budget_stop(v, f"after eval {e['id']} ({verdict})")
        if stop:
            stop.evidence.update(ev)
            return stop
        return Decision("diagnose", f"eval {e['id']}: {verdict}; looping to iteration {it + 1}", ev)

    return Decision(None, f"unknown status {s}")


# --------------------------------------------------------------------------- steps

class Step(Protocol):
    """An entry action for one stage. ``run`` may be sync (runs on a worker thread; use
    ``ctx.call_sync``) or async (runs on the bus loop; use ``await ctx.call``). It must be
    idempotent: after a restart it runs again for the stage the ticket is in."""

    stage: str
    owner: str  # the ticket that implements it, e.g. "TDM-24"

    def run(self, ctx: "StepContext") -> Any: ...


@dataclass
class StepContext:
    flywheel: "Flywheel"
    bus: Any
    stage: str
    ticket_id: int

    @property
    def view(self) -> TicketView:
        """Fresh view of the ticket's rows (re-read on every access)."""
        v = TicketView.from_bus(self.bus, self.ticket_id)
        assert v is not None
        return v

    async def call(self, reducer: str, args: list) -> int:
        return await self.bus.call_reducer(reducer, args)

    def call_sync(self, reducer: str, args: list) -> int:
        return self.bus.call_reducer_threadsafe(reducer, args)

    async def event(self, type_: str, payload: dict, actor: str | None = None) -> None:
        await self.call("append_event", [actor or self.flywheel.actor, type_,
                                         json.dumps({"ticket_id": self.ticket_id, **payload}, default=str)])

    def log(self, msg: str, **fields) -> None:
        log.info(msg, extra={"fields": {"stage": self.stage, "ticket_id": self.ticket_id, **fields}})

    async def wait_for(self, pred: Callable[[], Any], timeout: float = 30.0, every: float = 0.05) -> Any:
        """Poll the bus mirror until pred() is truthy (reducers return nothing; rows arrive later)."""
        end = time.monotonic() + timeout
        while True:
            v = pred()
            if v:
                return v
            if time.monotonic() > end:
                raise TimeoutError(f"{self.stage}: condition not met within {timeout}s")
            await asyncio.sleep(every)


@dataclass
class NotImplementedStep:
    """Placeholder until the owning ticket registers its step. The ticket waits in this stage."""

    stage: str
    owner: str
    does: str

    def run(self, ctx: StepContext) -> None:
        log.warning("step.not_implemented", extra={"fields": {
            "stage": self.stage, "owner": self.owner, "does": self.does, "ticket_id": ctx.ticket_id,
            "iteration": ctx.view.iteration}})


@dataclass
class BusHandlerStep:
    """A stage whose owner wired itself straight onto the bus (e.g. its own ``on_enter("ticket",
    status == stage)`` handler) instead of registering a Step. The flywheel does nothing but check
    that one of ``handlers`` is registered; if none is, it reports ``step.not_implemented``."""

    stage: str
    owner: str
    handlers: tuple[str, ...]
    does: str = ""

    def present(self, bus) -> list[str]:
        names = {h.name for h in getattr(getattr(bus, "registry", None), "handlers", [])}
        return [n for n in self.handlers if n in names]

    def run(self, ctx: StepContext) -> None:
        found = self.present(ctx.bus)
        if found:
            ctx.log("step.delegated", owner=self.owner, handlers=found)
        else:
            NotImplementedStep(self.stage, self.owner, self.does).run(ctx)


@dataclass
class QueueTrainingStep:
    """train: queue a fine-tune on every accepted episode of the ticket (the per-ticket dataset).
    The Modal client (TDM-21) launches it on ``training_run`` entering ``queued``. Skips if this
    iteration already has a queued/running/done run, so restarts never double-queue."""

    stage: str = "train"
    owner: str = "TDM-22 (launch: TDM-21)"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        run = v.training_run
        if run is not None and run.get("status") in ("queued", "running", "done"):
            ctx.log("step.skip", reason=f"training run {run['id']} already {run['status']}")
            return
        if sum(1 for r in v.iteration_training_runs if r.get("status") in ("failed", "cancelled")) >= MAX_STEP_FAILURES:
            ctx.log("step.skip", reason="training retries exhausted")
            return
        ids = v.accepted_episode_ids
        if not ids:
            log.warning("step.blocked", extra={"fields": {"stage": "train", "reason": "no accepted episodes"}})
            return
        ctx.log("step.queue_training", episode_ids=ids, iteration=v.iteration)
        await ctx.call("queue_training_run", [ctx.ticket_id, json.dumps(ids), ""])


PolicyActivator = Callable[[str, dict, float], None]  # (url, body, timeout) -> raises on failure
ShipReporter = Callable[[Any, int], Awaitable[dict]]  # (bus, ticket_id) -> report.saved payload; raises


_TDM70_NOT_SERVABLE = ("fake://", "sim://", "stub://")


def _tdm70_http_activate(url: str, body: dict, timeout: float) -> None:
    """POST /policy/activate on the TDM-41 policy server: the shared ``policy_activate.http_activate``
    (bearer POLICY_SERVER_TOKEN from env or .env), the same one ``evaluate.py`` uses."""
    from .policy_activate import http_activate

    http_activate(url, body, timeout)


async def _tdm70_save_report(bus, ticket_id: int) -> dict:
    """In-process POST /tickets/{id}/report/save (TDM-71): .md + .json to the media store, ``report.saved``.
    Reads the same database the bus is subscribed to (never a different one from the env)."""
    from rdf_common.spacetime_client import AsyncSpacetimeClient

    from .report_api import get_report_storage, save_report

    c = getattr(bus, "client", None)
    host, db = getattr(c, "host", None), getattr(c, "db", None)
    if not host or not db:
        raise LookupError("the bus client has no SpacetimeDB host/db to read the report from")
    async with AsyncSpacetimeClient(host, db, getattr(c, "token", None) or "") as stdb:
        return await save_report(ticket_id, stdb=stdb, store=get_report_storage())


@dataclass
class ShipStep:
    """shipped (TDM-22, TDM-70). Each part is idempotent on its own, so a restart redoes only
    what is missing:

    1. ``flywheel.shipped``: the before/after summary (once per ticket).
    2. ``mark_checkpoint_deployed``: sets ``ticket.deployed_checkpoint_uri`` from this
       iteration's done training run and its finished candidate eval (the reducer checks both).
    3. ``POST {POLICY_SERVER_URL}/policy/activate`` so the policy server serves the deployed
       checkpoint (``flywheel.policy_activated`` / ``_failed``; ``_skipped`` without a URL).
    4. The TDM-71 report is saved (``report.saved``; ``flywheel.report_failed`` on error).
       ``FLYWHEEL_SHIP_REPORT=off`` turns this off.
    """

    stage: str = "shipped"
    owner: str = "TDM-22/TDM-70"
    activate: PolicyActivator = _tdm70_http_activate
    reporter: ShipReporter | None = _tdm70_save_report
    policy_server_url: str | None = None  # None: POLICY_SERVER_URL from the env
    activate_timeout_s: float = 300.0
    confirm_s: float = 30.0

    @staticmethod
    def _events(ctx: StepContext, type_: str) -> list[tuple[dict, dict]]:
        out = []
        for r in ctx.bus.rows("event_log"):
            if r.get("type") == type_:
                p = _payload(r)
                if p.get("ticket_id") == ctx.ticket_id:
                    out.append((r, p))
        return sorted(out, key=lambda x: x[0].get("id") or 0)

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        run, e, b = v.done_training_run, v.candidate_eval, v.baseline_eval
        shipped = self._events(ctx, "flywheel.shipped")
        if shipped:
            ctx.log("step.skip", reason=f"already summarised in event {shipped[-1][0].get('id')}")
        else:
            await ctx.event("flywheel.shipped", {
                "iteration": v.iteration, "checkpoint_uri": run.get("checkpoint_uri") if run else None,
                "training_run_id": run["id"] if run else None, "eval_run_id": e["id"] if e else None,
                "baseline": _eval_ev(b) if b else None, "final": _eval_ev(e) if e else None,
                "accepted_episodes": len(v.accepted_episode_ids), **v.money(),
            })
        uri = (run or {}).get("checkpoint_uri")
        if run is None or e is None or not uri:
            if not self._events(ctx, "flywheel.ship_blocked"):
                await ctx.event("flywheel.ship_blocked", {
                    "reason": "no done training run with a checkpoint and a finished candidate eval in iteration "
                              f"{v.iteration}; nothing to deploy", "training_run_id": (run or {}).get("id"),
                    "eval_run_id": (e or {}).get("id")})
            log.warning("ship.blocked", extra={"fields": {"ticket_id": ctx.ticket_id, "iteration": v.iteration}})
            return
        if not await self._deploy(ctx, v, run, e, uri):
            return
        await self._activate_policy(ctx, run, e, uri)
        await self._report(ctx)

    async def _deploy(self, ctx: StepContext, v: TicketView, run: dict, e: dict, uri: str) -> bool:
        if v.ticket.get("deployed_checkpoint_uri") == uri:
            return True
        await ctx.call("mark_checkpoint_deployed", [ctx.ticket_id, run["id"], e["id"],
                                                    f"flywheel: eval {e['id']} {format_ci(e['successes'], e['trials'])}"])
        try:
            await ctx.wait_for(lambda: ctx.view.ticket.get("deployed_checkpoint_uri") == uri, timeout=self.confirm_s)
        except TimeoutError:
            log.warning("ship.deploy_unconfirmed", extra={"fields": {
                "ticket_id": ctx.ticket_id, "checkpoint_uri": uri,
                "hint": "mark_checkpoint_deployed refused or not published? see /healthz last_error"}})
            return False
        ctx.log("ship.deployed", checkpoint_uri=uri, training_run_id=run["id"], eval_run_id=e["id"])
        return True

    async def _activate_policy(self, ctx: StepContext, run: dict, e: dict, uri: str) -> None:
        done = [p for _, p in self._events(ctx, "flywheel.policy_activated") + self._events(ctx, "flywheel.policy_activate_skipped")
                if p.get("checkpoint_uri") == uri]
        if done:
            return
        base = {"checkpoint_uri": uri, "training_run_id": run["id"], "eval_run_id": e["id"]}
        url = self.policy_server_url
        if url is None:
            url = os.environ.get("POLICY_SERVER_URL", "")
        url = url.strip().rstrip("/")
        if not url:
            await ctx.event("flywheel.policy_activate_skipped", {**base, "note": "POLICY_SERVER_URL not set"})
            return
        if uri.startswith(_TDM70_NOT_SERVABLE):  # harness / simulation checkpoints: never wake a real GPU for them
            await ctx.event("flywheel.policy_activate_skipped", {**base, "note": "fake/simulated checkpoint; "
                                                                               "not sent to the policy server"})
            return
        body = {"checkpoint_uri": uri, "ticket_id": ctx.ticket_id, "robot_id": ctx.view.ticket.get("robot_id"),
                "kind": "deployed"}
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(self.activate, f"{url}/policy/activate", body, self.activate_timeout_s)
        except Exception as ex:  # noqa: BLE001 - the ticket stays shipped; the next boot retries
            await ctx.event("flywheel.policy_activate_failed", {**base, "url": url, "error": str(ex)[:500]})
            return
        await ctx.event("flywheel.policy_activated", {**base, "url": url, "ms": int((time.monotonic() - t0) * 1000)})

    async def _report(self, ctx: StepContext) -> None:
        if self.reporter is None or os.environ.get("FLYWHEEL_SHIP_REPORT", "").strip().lower() == "off":
            return
        shipped = self._events(ctx, "flywheel.shipped")
        since = (shipped[-1][0].get("id") or 0) if shipped else 0
        if any((r.get("id") or 0) > since for r, _ in self._events(ctx, "report.saved")):
            return
        # The report reads the database directly: let this step's own events land first.
        try:
            await ctx.wait_for(lambda: any(self._events(ctx, t) for t in (
                "flywheel.policy_activated", "flywheel.policy_activate_skipped", "flywheel.policy_activate_failed")),
                timeout=self.confirm_s)
        except TimeoutError:
            pass
        try:
            saved = await self.reporter(ctx.bus, ctx.ticket_id)
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - get_backend() exits on missing media env
            await ctx.event("flywheel.report_failed", {"error": f"{type(ex).__name__}: {ex}"[:500]})
            return
        ctx.log("ship.report_saved", uri=(saved or {}).get("uri"), json_uri=(saved or {}).get("json_uri"))


def _payload(event_row) -> dict:
    try:
        p = json.loads(event_row.get("payload_json") or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def default_steps() -> dict[str, Step]:
    stub = NotImplementedStep
    return {
        "onboarding": stub("onboarding", "E04 onboarding droid", "write/validate the robot adapter, set robot.adapter_version"),
        "baseline": stub("baseline", "TDM-67", "start_eval_run(base pi0.5) + run_baseline on the robot, finalize"),
        # These landed as their own bus handlers (handlers/diagnose.py, planner.py, dispatcher.py, reviewer.py).
        "diagnose": BusHandlerStep("diagnose", "TDM-23", ("diagnose_on_ticket_diagnose", "diagnose_failures"),
                                   "cluster failures of the last eval -> record_failure_cluster"),
        "plan_data": BusHandlerStep("plan_data", "TDM-24", ("tdm24_planner",),
                                    "planner picks data type + amount -> record_data_plan"),
        "dispatch": BusHandlerStep("dispatch", "TDM-26", ("dispatch_on_plan_approved",),
                                   "create paid tasks / resets / dataset offers for the approved plan"),
        "review": BusHandlerStep("review", "TDM-25", ("tdm25_review_episode",),
                                 "review each pending episode -> submit_review"),
        "train": QueueTrainingStep(),
        "eval": stub("eval", "TDM-67", "start_eval_run(candidate checkpoint) + run_eval, finalize"),
        "shipped": ShipStep(),
    }


# --------------------------------------------------------------------------- the machine

@dataclass
class _Pending:
    frm: str
    iteration: int
    to: str
    at: float


@dataclass
class _Wait:
    stage: tuple
    reason: str
    at: float
    suppressed: int = 0


class Flywheel:
    """Owns the step table and runs decisions. One instance per process; all state that matters
    lives in SpacetimeDB. In-memory fields only de-duplicate work within this process."""

    def __init__(self, steps: dict[str, Step] | None = None, *, actor: str = "orchestrator",
                 auto_approve_max_cents: int = 0, reconcile_s: float = 15.0, pending_ttl_s: float = 20.0,
                 wait_log_s: float = 60.0) -> None:
        self.steps: dict[str, Step] = default_steps()
        if steps:
            self.steps.update(steps)
        self.actor = actor
        self.auto_approve_max_cents = auto_approve_max_cents
        self.reconcile_s, self.pending_ttl_s, self.wait_log_s = reconcile_s, pending_ttl_s, wait_log_s
        self._pending: dict[int, _Pending] = {}
        self._approving: dict[int, float] = {}  # data_plan_id -> at
        self._entered: set[tuple] = set()  # (ticket_id, status, iteration, updated_at) whose step ran
        self._last_wait: dict[int, _Wait] = {}
        self._bus = None
        self._tick: asyncio.Task | None = None
        self.step_runs: dict[str, int] = {}
        self.step_errors: dict[str, str] = {}

    # ---- steps
    def register_step(self, stage: str, step: Step) -> None:
        if stage not in STEP_STAGES:
            raise ValueError(f"unknown stage {stage!r}; one of {STEP_STAGES}")
        self.steps[stage] = step

    def _wired(self, step: Step, bus) -> bool | list[str]:
        if isinstance(step, NotImplementedStep):
            return False
        if isinstance(step, BusHandlerStep):
            return step.present(bus or self._bus) or False
        return True

    def step_table(self, bus=None) -> list[dict]:
        return [{"stage": s, "step": type(self.steps[s]).__name__, "owner": getattr(self.steps[s], "owner", "?"),
                 "wired": self._wired(self.steps[s], bus), "runs": self.step_runs.get(s, 0),
                 "last_error": self.step_errors.get(s)} for s in STEP_STAGES]

    # ---- lifecycle
    def ensure_attached(self, bus) -> None:
        """Start the reconcile tick on the bus loop (first handler call does this)."""
        if self._bus is bus and self._tick and not self._tick.done():
            return
        self._bus = bus
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._tick = loop.create_task(self._reconcile_loop(bus), name="flywheel-reconcile")

    def detach(self) -> None:
        if self._tick is not None:
            self._tick.cancel()
        self._tick, self._bus = None, None

    async def _reconcile_loop(self, bus) -> None:
        while True:
            await asyncio.sleep(self.reconcile_s)
            try:
                await self.reconcile(bus, cause="tick")
            except Exception:  # noqa: BLE001 - the tick must never die
                log.exception("flywheel.reconcile_error")

    async def reconcile(self, bus, cause: str = "tick") -> None:
        for t in bus.rows("ticket"):
            if t.get("status") not in TERMINAL:
                await self.evaluate(bus, t["id"], cause=cause)

    # ---- entry: run the stage's step once per entry into the status
    async def on_enter(self, bus, ticket: dict, source: str = "live") -> None:
        self.ensure_attached(bus)
        key = (ticket["id"], ticket["status"], ticket.get("iteration"), ticket.get("updated_at"))
        if ticket["status"] in self.steps and key not in self._entered:
            self._entered.add(key)
            await self.run_step(bus, ticket["id"], ticket["status"], why=f"entered ({source})")
        await self.evaluate(bus, ticket["id"], cause=f"enter:{ticket['status']}")

    async def run_step(self, bus, ticket_id: int, stage: str, why: str = "") -> None:
        step = self.steps.get(stage)
        if step is None:
            return
        ctx = StepContext(self, bus, stage, ticket_id)
        name = type(step).__name__
        self.step_runs[stage] = self.step_runs.get(stage, 0) + 1
        log.info("step.start", extra={"fields": {"stage": stage, "step": name, "owner": getattr(step, "owner", "?"),
                                                 "ticket_id": ticket_id, "why": why}})
        try:
            if inspect.iscoroutinefunction(step.run):
                await step.run(ctx)
            else:
                tok = ticket_id_var.set(ticket_id)
                try:
                    res = await asyncio.to_thread(step.run, ctx)
                finally:
                    ticket_id_var.reset(tok)
                if inspect.isawaitable(res):
                    await res
        except Exception as e:  # noqa: BLE001 - a broken step leaves the ticket waiting, logged
            self.step_errors[stage] = f"{type(e).__name__}: {e}"
            log.exception("step.error", extra={"fields": {"stage": stage, "step": name, "ticket_id": ticket_id}})

    # ---- exit: derive the next status from rows and move
    def view(self, bus, ticket_id: int) -> TicketView | None:
        return TicketView.from_bus(bus, ticket_id)

    async def evaluate(self, bus, ticket_id: int, cause: str = "") -> Decision | None:
        self.ensure_attached(bus)
        v = self.view(bus, ticket_id)
        if v is None or v.status in TERMINAL:
            return None
        d = decide(v)
        tok = ticket_id_var.set(ticket_id)
        try:
            if d.to is None:
                self._log_wait(v, d, cause)
                if d.retry_step:
                    await self._retry(bus, v, d)
                if d.approve_plan is not None:
                    await self._maybe_auto_approve(bus, v, d.approve_plan)
                return d
            await self._transition(bus, v, d.to, d.reason, d.evidence, actor=self.actor, cause=cause)
            return d
        finally:
            ticket_id_var.reset(tok)

    def _log_wait(self, v: TicketView, d: Decision, cause: str) -> None:
        """TDM-70: log ``flywheel.wait`` when the ticket starts waiting in a stage (status or
        iteration changed), then at most every ``wait_log_s`` while only the reason text changes
        (collecting 1/3 -> 2/3, reviewing ...). Changes in between go to debug and are counted."""
        now = time.monotonic()
        w = self._last_wait.get(v.id)
        stage = (v.status, v.iteration)
        if w is not None and w.stage == stage:
            if w.reason == d.reason:
                return
            if now - w.at < self.wait_log_s:
                w.reason, w.suppressed = d.reason, w.suppressed + 1
                log.debug("flywheel.wait", extra={"fields": {"status": v.status, "iteration": v.iteration,
                                                             "reason": d.reason, "cause": cause}})
                return
        fields = {"status": v.status, "iteration": v.iteration, "reason": d.reason, "cause": cause}
        if w is not None and w.stage == stage and w.suppressed:
            fields["changes_since_last_log"] = w.suppressed
        log.info("flywheel.wait", extra={"fields": fields})
        self._last_wait[v.id] = _Wait(stage, d.reason, now)

    async def _retry(self, bus, v: TicketView, d: Decision) -> None:
        key = (v.id, v.status, v.iteration, "retry", len(v.training_runs) + len(v.eval_runs))
        if key in self._entered:
            return
        self._entered.add(key)
        await self.run_step(bus, v.id, v.status, why=f"retry: {d.reason}")

    async def _maybe_auto_approve(self, bus, v: TicketView, plan: dict) -> None:
        cap = self.auto_approve_max_cents
        est = int(plan.get("est_cost_cents") or 0)
        if cap <= 0 or plan.get("status") != "proposed":
            return
        if time.monotonic() - self._approving.get(plan["id"], -1e9) < self.pending_ttl_s:
            return
        approved_elsewhere = sum(int(p.get("approved_cents") or 0) for p in v.plans
                                 if p["id"] != plan["id"] and p.get("status") != "rejected")
        if est > cap or approved_elsewhere + est > v.budget_cents:
            log.info("flywheel.auto_approve_skip", extra={"fields": {"data_plan_id": plan["id"], "est_cost_cents": est,
                                                                    "cap_cents": cap, "budget_cents": v.budget_cents}})
            self._approving[plan["id"]] = time.monotonic()
            return
        self._approving[plan["id"]] = time.monotonic()
        log.info("flywheel.auto_approve", extra={"fields": {"data_plan_id": plan["id"], "est_cost_cents": est, "cap_cents": cap}})
        await bus.call_reducer("approve_spend", [v.id, plan["id"], 0, f"auto:flywheel(cap={cap}c)"])

    async def _transition(self, bus, v: TicketView, to: str, reason: str, evidence: dict, *, actor: str,
                          cause: str, manual: bool = False) -> bool:
        now = time.monotonic()
        p = self._pending.get(v.id)
        if p and p.frm == v.status and p.iteration == v.iteration and now - p.at < self.pending_ttl_s:
            return False  # already asked; waiting for the row to change
        if p and p.frm == v.status and p.iteration == v.iteration:
            log.warning("flywheel.transition_stalled", extra={"fields": {
                "from": p.frm, "to": p.to, "age_s": round(now - p.at, 1), "hint": "reducer refused? see /healthz last_error"}})
        self._pending[v.id] = _Pending(v.status, v.iteration, to, now)
        self._last_wait.pop(v.id, None)
        fields = {"from": v.status, "to": to, "iteration": v.iteration, "reason": reason, "cause": cause, "manual": manual}
        log.info("flywheel.transition", extra={"fields": {**fields, "evidence": evidence}})
        await bus.call_reducer("append_event", [actor, "flywheel.transition", json.dumps(
            {"ticket_id": v.id, **fields, "evidence": evidence}, default=str)])
        await bus.call_reducer("set_ticket_status", [v.id, to, reason[:500]])
        return True

    # ---- manual nudge (POST /tickets/{id}/advance)
    async def advance(self, bus, ticket_id: int, to: str | None = None, reason: str = "", by: str = "human") -> dict:
        v = self.view(bus, ticket_id)
        if v is None:
            raise LookupError(f"unknown ticket {ticket_id}")
        if v.status in TERMINAL:
            raise ValueError(f"ticket {ticket_id} is {v.status} (terminal)")
        d = decide(v)
        if to is None:
            to = d.to or FORWARD.get(v.status)
            if to is None:
                raise ValueError(f"from {v.status} a manual advance must name the target status "
                                 f"({'|'.join(TRANSITIONS.get(v.status, ()))}|failed|cancelled)")
        if not _allowed(v.status, to):
            raise ValueError(f"illegal transition {v.status} -> {to} "
                             f"(allowed: {'|'.join(TRANSITIONS.get(v.status, ()))}|failed|cancelled)")
        if v.status == "eval" and to in LOOP_TARGETS and v.iteration + 1 >= v.max_iterations:
            raise ValueError(f"max_iterations {v.max_iterations} reached; raise it or mark the ticket failed")
        who = by if by.startswith("human") else f"human:{by}"
        text = f"manual advance by {who}" + (f": {reason}" if reason else "")
        self._pending.pop(ticket_id, None)  # a human overrides any in-flight automatic ask
        await self._transition(bus, v, to, text, {"machine_decision": d.to, "machine_reason": d.reason},
                               actor=who, cause="manual", manual=True)
        return {"ticket_id": ticket_id, "from": v.status, "to": to, "iteration": v.iteration, "note": text,
                "machine_would": {"to": d.to, "reason": d.reason}}

    def timeline(self, bus, ticket_id: int, max_events: int = 500) -> dict | None:
        """TDM-70 ``GET /tickets/{id}/timeline``: the loop per iteration (eval before, plan,
        collection, training run, eval after, verdict), the ship record, and the ticket's
        event_log entries in order (type/actor/ts, plus the note on status changes)."""
        v = self.view(bus, ticket_id)
        if v is None:
            return None
        d = decide(v)
        ts = v.threshold_settings

        def cand(it: int):
            ids = {r["id"] for r in v.training_runs if int(r.get("iteration") or 0) == it and r.get("status") == "done"}
            return _latest([r for r in v.evals(status="done") if r.get("kind") != "baseline"
                            and r.get("training_run_id") in ids])

        last_it = max([v.iteration] + [int(r.get("iteration") or 0) for r in v.plans + v.training_runs])
        iterations = []
        for it in range(last_it + 1):
            before = v.baseline_eval if it == 0 else cand(it - 1)
            after = cand(it)
            plans = [p for p in v.plans if int(p.get("iteration") or 0) == it]
            plan = _latest([p for p in plans if p.get("status") != "rejected"]) or _latest(plans)
            runs = [r for r in v.training_runs if int(r.get("iteration") or 0) == it]
            run = _latest([r for r in runs if r.get("status") == "done"]) or _latest(runs)
            iterations.append({
                "iteration": it,
                "eval_before": _eval_ev(before) if before else None,
                "failure_clusters": [{"id": c["id"], "label": c.get("label"), "count": c.get("count")}
                                     for c in v.clusters if int(c.get("iteration") or 0) == it],
                "data_plan": None if plan is None else {
                    k: plan.get(k) for k in ("id", "data_type", "amount", "status", "est_cost_cents", "approved_cents")},
                "training_run": None if run is None else {
                    k: run.get(k) for k in ("id", "status", "checkpoint_uri", "gpu_cost_cents", "error")},
                "eval_after": _eval_ev(after) if after else None,
                "verdict": explain_threshold(after, ts) if after else None,
                "met_threshold": meets_threshold(after, ts) if after else None,
            })
        events = []
        for r in sorted(bus.rows("event_log"), key=lambda r: r.get("id") or 0):
            p = _payload(r)
            if p.get("ticket_id") != ticket_id:
                continue
            e = {"id": r.get("id"), "ts": r.get("ts"), "actor": r.get("actor"), "type": r.get("type")}
            if r.get("type") == "ticket.status_changed":
                e.update({k: p.get(k) for k in ("from", "to", "iteration", "note")})
            events.append(e)
        return {"ticket_id": ticket_id, "status": v.status, "iteration": v.iteration,
                "max_iterations": v.max_iterations, **ts,
                "decision": {"to": d.to, "reason": d.reason}, "money": v.money(),
                "deployed_checkpoint_uri": v.ticket.get("deployed_checkpoint_uri"),
                "iterations": iterations, "events": events[-max_events:], "events_total": len(events)}

    def describe(self, bus, ticket_id: int) -> dict | None:
        v = self.view(bus, ticket_id)
        if v is None:
            return None
        d = decide(v)
        return {"ticket_id": ticket_id, "status": v.status, "iteration": v.iteration,
                "max_iterations": v.max_iterations, "decision": {"to": d.to, "reason": d.reason, "evidence": d.evidence},
                "money": v.money(), "collection": v.collection() if v.plan else None,
                "step": self.step_table(bus)[STEP_STAGES.index(v.status)] if v.status in STEP_STAGES else None}

