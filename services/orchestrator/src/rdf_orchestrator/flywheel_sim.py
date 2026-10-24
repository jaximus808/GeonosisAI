"""SIMULATED fake robot + fake agents for the flywheel (TDM-22), until the real steps land.

EVERYTHING THIS MODULE WRITES IS FAKE: eval trials, failure clusters, data plans, episodes,
reviews and training runs. Rows are tagged (``sim://`` URIs, "SIMULATED" in every reasoning,
note and reason) and ``install_sim`` refuses to run against maincloud. It exists to prove the
state machine end to end (pytest's fake SpacetimeDB, or a throwaway local server) and stands in
for the TDM-28 fake-robot harness. Never use its numbers as results.

    fw = Flywheel(); install_sim(fw, SimScript(baseline_successes=2, candidate_successes=(5, 9)))

Every sim step is idempotent and derives what to do from rows, like a real step must.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from .flywheel import (BusHandlerStep, DATASET_KINDS, Flywheel, NotImplementedStep, PI05_BASE_CHECKPOINT,
                       QueueTrainingStep, StepContext)
from .stats import format_ci, wilson_interval

TAG = "SIMULATED"
NONE = {"none": []}


def some(v):
    return {"some": v}


@dataclass
class SimScript:
    baseline_successes: int = 2
    candidate_successes: tuple[int, ...] = (5, 9)  # per iteration; the last value repeats
    data_type: str = "recovery_takeover"
    amount: int = 0  # 0 -> run_settings.target_episodes
    reject_first_episode: bool = True  # iteration 0: one rejection exercises review -> dispatch
    gpu_cost_cents: int = 150
    complete_training: bool = True  # False: leave the run 'running' (to test a restart mid-train)
    adapter_version: str = "sim-adapter-1"


def _step(stage: str):
    def deco(cls):
        cls.stage, cls.owner = stage, f"SIM (stands in for {cls.owner})"
        return cls
    return deco


class _Sim:
    owner = "?"

    def __init__(self, script: SimScript) -> None:
        self.s = script


@_step("onboarding")
class SimOnboard(_Sim):
    owner = "E04 onboarding droid"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        r = v.robot
        if r is None:
            ctx.log("sim.onboard_skip", reason=f"robot {v.ticket.get('robot_id')} not in mirror")
            return
        if (r.get("adapter_version") or "").strip():
            return
        await ctx.call("register_robot", [r["name"], r.get("embodiment") or "yam", self.s.adapter_version,
                                          r.get("camera_config_json") or "{}"])


class _SimEval(_Sim):
    owner = "TDM-67"

    def checkpoint(self, ctx: StepContext) -> str | None:
        raise NotImplementedError

    def successes(self, ctx: StepContext, trials: int) -> int:
        raise NotImplementedError

    async def run(self, ctx: StepContext) -> None:
        ckpt = self.checkpoint(ctx)
        if not ckpt:
            ctx.log("sim.eval_skip", reason="no checkpoint")
            return

        def current():
            runs = [r for r in ctx.view.eval_runs if r.get("checkpoint_uri") == ckpt and r.get("status") in ("running", "done")]
            return max(runs, key=lambda r: r["id"]) if runs else None

        run = current()
        if run is None:
            await ctx.call("start_eval_run", [ctx.ticket_id, ckpt, 0])
            run = await ctx.wait_for(current)
        if run["status"] == "done":
            return
        rid, n = run["id"], int(run["trials"])
        k = min(self.successes(ctx, n), n)
        have = {t.get("trial_index") for t in ctx.bus.rows("eval_trial") if t.get("eval_run_id") == rid}
        for i in range(n):
            if i not in have:
                ok = i < k
                await ctx.call("record_eval_trial", [rid, i, ok, 1000, 0, "" if ok else f"{TAG}: missed the cup"])
        run = await ctx.wait_for(lambda: (r := ctx.bus.get("eval_run", rid)) and int(r["completed"]) >= n and r)
        succ = int(run["successes"])
        lo, hi = wilson_interval(succ, n)
        await ctx.call("finalize_eval_run", [rid, succ, n, lo, hi, f"{TAG} {format_ci(succ, n)}"])


@_step("baseline")
class SimBaseline(_SimEval):
    def checkpoint(self, ctx):
        return PI05_BASE_CHECKPOINT

    def successes(self, ctx, trials):
        return self.s.baseline_successes


@_step("eval")
class SimCandidateEval(_SimEval):
    def checkpoint(self, ctx):
        run = ctx.view.done_training_run
        return run.get("checkpoint_uri") if run else None

    def successes(self, ctx, trials):
        seq = self.s.candidate_successes
        return seq[min(ctx.view.iteration, len(seq) - 1)]


@_step("diagnose")
class SimDiagnose(_Sim):
    owner = "TDM-23"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        if v.iteration_clusters:
            return
        src = v.last_done_eval
        if src is None:
            return
        failures = int(src["trials"]) - int(src["successes"])
        if failures > 0:
            await ctx.call("record_failure_cluster", [ctx.ticket_id, f"{TAG}: misses the cup on approach", failures, "[]"])


@_step("plan_data")
class SimPlan(_Sim):
    owner = "TDM-24"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        plan = v.plan
        if plan is None:
            amount = self.s.amount or int(v.setting("target_episodes", 3))
            evidence = {"failure_cluster_ids": [c["id"] for c in v.iteration_clusters], "simulated": True}
            await ctx.call("record_data_plan", [ctx.ticket_id, self.s.data_type, amount,
                                                f"{TAG} planner (TDM-24 not wired in this run): {amount} x "
                                                f"{self.s.data_type} for iteration {v.iteration}", json.dumps(evidence)])
            plan = await ctx.wait_for(lambda: ctx.view.plan)
        if plan.get("status") == "proposed":
            await ctx.call("approve_spend", [ctx.ticket_id, plan["id"], 0, "sim:auto"])


@_step("dispatch")
class SimDispatch(_Sim):
    owner = "TDM-26"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        if v.plan is None:
            return
        c = v.collection()
        for n in range(max(c["amount"] - c["non_rejected"], 0)):
            uri = f"sim://ticket{ctx.ticket_id}/plan{v.plan['id']}/ep{c['collected'] + n}"
            kind = v.plan["data_type"] if v.plan["data_type"] in DATASET_KINDS[:2] else "clean_teleop"
            await ctx.call("create_episode", [ctx.ticket_id, 0, 0, kind, uri, uri + "/actions", "{}", 300, 10000,
                                              NONE, NONE, some(True), NONE])


@_step("review")
class SimReview(_Sim):
    owner = "TDM-25"

    async def run(self, ctx: StepContext) -> None:
        v = ctx.view
        eps = v.collected()
        first = min((e["id"] for e in eps), default=None)
        any_rejected = any(e.get("review_status") == "rejected" for e in eps)
        for e in eps:
            if e.get("review_status") != "pending":
                continue
            reject = self.s.reject_first_episode and v.iteration == 0 and e["id"] == first and not any_rejected
            await ctx.call("submit_review", [e["id"], not reject,
                                             f"{TAG} reviewer: jerk above threshold" if reject else f"{TAG} reviewer: ok",
                                             json.dumps({"simulated": True})])


@_step("train")
class SimTrain(_Sim):
    """The real QueueTrainingStep, then a fake Modal: queued -> running -> done (sim:// checkpoint)."""

    owner = "TDM-21"

    async def run(self, ctx: StepContext) -> None:
        await QueueTrainingStep().run(ctx)
        run = await ctx.wait_for(lambda: (r := ctx.view.training_run) and r.get("status") in ("queued", "running", "done") and r)
        rid = run["id"]
        if run["status"] == "queued":
            await ctx.call("set_training_status", [rid, "running", some(f"sim-modal-{rid}"), NONE, NONE, NONE])
            await ctx.wait_for(lambda: (r := ctx.bus.get("training_run", rid)) and r.get("status") != "queued")
        if not self.s.complete_training or (ctx.bus.get("training_run", rid) or {}).get("status") != "running":
            return
        it = int(run.get("iteration") or 0)
        await ctx.call("set_training_status", [rid, "done", NONE, some(f"sim://ckpt/ticket{ctx.ticket_id}/iter{it}"),
                                               some(json.dumps({"simulated": True, "loss": 0.1})),
                                               some(self.s.gpu_cost_cents)])


SIM_STEPS = (SimOnboard, SimBaseline, SimDiagnose, SimPlan, SimDispatch, SimReview, SimTrain, SimCandidateEval)


def install_sim(fw: Flywheel, script: SimScript | None = None, *, replace_external: bool = False,
                host: str | None = None, registry=None, stages: set[str] | None = None) -> Flywheel:
    """Swap stages with no implementation for the simulation.

    * ``stages=None`` (pytest / the proof script): every NotImplementedStep and ``train``'s Modal
      launch. With ``replace_external`` also every BusHandlerStep (only safe
      on a bus whose registry does not carry those handlers).
    * ``stages={...}`` (env ``FLYWHEEL_SIM=review,...`` in a real orchestrator): only those stages,
      and only if nothing implements them: a NotImplementedStep, or a BusHandlerStep whose
      handler is not registered on ``registry``."""
    h = host if host is not None else os.environ.get("SPACETIME_HOST", "")
    if "maincloud" in h:
        raise RuntimeError("flywheel_sim writes fake eval/episode rows; refusing to run against maincloud")
    script = script or SimScript()
    names = {x.name for x in getattr(registry, "handlers", [])}
    for cls in SIM_STEPS:
        cur = fw.steps.get(cls.stage)
        if stages is not None:
            absent = isinstance(cur, NotImplementedStep) or (
                isinstance(cur, BusHandlerStep) and not any(n in names for n in cur.handlers))
            if cls.stage in stages and absent:
                fw.register_step(cls.stage, cls(script))
            continue
        if (isinstance(cur, NotImplementedStep) or (replace_external and isinstance(cur, BusHandlerStep))
                or (cls.stage == "train" and isinstance(cur, QueueTrainingStep))):
            fw.register_step(cls.stage, cls(script))
    fw.sim_installed_at = time.time()  # type: ignore[attr-defined]
    return fw
