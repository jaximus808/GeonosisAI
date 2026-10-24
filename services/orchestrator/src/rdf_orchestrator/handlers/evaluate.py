"""TDM-67: bus wiring for eval orchestration (logic in ``..evaluate``).

  ticket enters baseline / eval            -> flywheel step EvalStep -> ensure_eval (start + run_eval)
  eval_trial inserted (boot snapshot too)  -> check_run: finalize once every planned trial row exists
  run_eval command done/failed/preempted   -> check_run: finalize, or abort an eval that can't finish
  any job command on a robot closes        -> kick_robot: start evals that waited for the job lock
  robot goes offline / estop               -> check_run on its running evals (abort)
  robot comes back online                  -> kick_robot (re-run an aborted eval)
  event_log eval.rerun_requested           -> ensure_eval for that ticket (human OK after an e-stop)
  reset task opens (EVAL_SOLO_MODE=1)      -> solo_reset after EVAL_SOLO_RESET_DELAY_S
  watchdog thread (EVAL_WATCHDOG_S)        -> check_all: stale heartbeats, timeouts, missed events

Resets between trials themselves are the TDM-26 dispatcher's (handlers/dispatcher.py): a paid reset
task per eval_trial, and resume_policy when it completes.
"""
from __future__ import annotations

from .. import evaluate as evl
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..dispatcher import JOB_TYPES, OPEN_JOB
from .. import flywheel as _fw
from ..flywheel import Flywheel, NotImplementedStep
from ..log import get_logger

log = get_logger("evaluate")

# Steps TDM-67 takes over: the stub, and TDM-22's interim RobotEvalStep (its docstring hands
# baseline/eval to TDM-67). A simulated step (FLYWHEEL_SIM) is kept.
_REPLACEABLE = tuple(c for c in (NotImplementedStep, getattr(_fw, "RobotEvalStep", None), evl.EvalStep) if c)


def _e(ev: Event) -> evl.EvalOrchestrator:
    return evl.get_evaluator(ev.bus)


def eval_on_trial(ev: Event) -> None:
    run = ev.bus.get("eval_run", int(ev.row["eval_run_id"]))
    if run is not None and run.get("status") == "running":
        out = _e(ev).check_run(run)
        if out == "finalized":
            log.info("eval.finalize_fired", extra={"fields": {"eval_run_id": run["id"], "trial_id": ev.row.get("id")}})


def eval_on_command(ev: Event) -> None:
    old = (ev.old or {}).get("status")
    new = ev.row.get("status")
    if old == new:
        return
    e = _e(ev)
    if ev.row.get("type") in evl.EVAL_COMMANDS:
        rid = evl._i(evl._json(ev.row.get("payload_json")).get("eval_run_id"))
        run = ev.bus.get("eval_run", rid) if rid else None
        if run is not None:
            out = e.check_run(run)
            log.info("eval.command_changed", extra={"fields": {"command_id": ev.row["id"], "from": old, "to": new,
                                                               "eval_run_id": rid, "outcome": out}})
    if ev.row.get("type") in JOB_TYPES and old in OPEN_JOB and new not in OPEN_JOB:
        e.kick_robot(int(ev.row["robot_id"]), why=f"job command {ev.row['id']} {new}")


def eval_on_robot(ev: Event) -> None:
    old = (ev.old or {}).get("status")
    new = ev.row.get("status")
    if old == new:
        return
    e = _e(ev)
    rid = int(ev.row["id"])
    if new not in evl.USABLE_ROBOT:
        for run in ev.bus.rows("eval_run"):
            if int(run.get("robot_id") or 0) == rid and run.get("status") == "running":
                e.check_run(run)
    elif old not in evl.USABLE_ROBOT:
        e.kick_robot(rid, why=f"robot {rid} {old} -> {new}")


def eval_on_rerun_request(ev: Event) -> None:
    tid = evl._i(evl._json(ev.row.get("payload_json")).get("ticket_id"))
    if tid:
        out = _e(ev).ensure_eval(tid, why=f"eval.rerun_requested (event {ev.row.get('id')})")
        log.info("eval.rerun", extra={"fields": {"ticket_id": tid, "outcome": out}})


def eval_solo_reset(ev: Event) -> None:
    _e(ev).solo_reset(ev.row)


def register(reg: Registry, fw: Flywheel | None = None) -> Registry:
    """Put the eval handlers on ``reg`` and the EvalStep on ``fw``'s baseline/eval stages."""
    for t in evl.TABLES:
        reg.watch(t)
    reg.on_insert("eval_trial", include_existing=True, name="eval_on_trial")(eval_on_trial)
    reg.on_update("robot_command", name="eval_on_command")(eval_on_command)
    reg.on_update("robot", name="eval_on_robot")(eval_on_robot)
    reg.on_insert("event_log", where=lambda r: r.type == "eval.rerun_requested", name="eval_on_rerun_request")(
        eval_on_rerun_request)
    reg.on_enter("task", where=lambda r: r.type == "reset" and r.status == "open", include_existing=True,
                 name="eval_solo_reset")(eval_solo_reset)
    if fw is not None:
        for stage in evl.EVAL_STAGES:
            cur = fw.steps.get(stage)
            if cur is not None and not isinstance(cur, _REPLACEABLE):
                log.warning("eval.step_kept", extra={"fields": {"stage": stage, "step": type(cur).__name__,
                                                               "note": "another step (e.g. FLYWHEEL_SIM) owns it"}})
                continue
            fw.register_step(stage, evl.EvalStep(stage))
    return reg


def _default_flywheel() -> Flywheel:
    from .flywheel import FLYWHEEL

    return FLYWHEEL


register(default_registry, _default_flywheel())
