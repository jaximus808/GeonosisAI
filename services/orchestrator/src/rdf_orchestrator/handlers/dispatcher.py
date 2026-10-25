"""TDM-26: bus wiring for the dispatcher (logic and the TDM-22 interface are in ``..dispatcher``).

  data_plan enters approved/executing (boot snapshot too)   -> dispatch_plan
  data_plan approved_cents changes                          -> dispatch_plan
  task of a plan changes status (cancel/expire/complete)    -> dispatch_plan (top up / stop)
  episode review_status changes on a task                   -> dispatch_plan (reject frees a slot)
  ticket budget_cents / status changes                      -> dispatch every plan of the ticket
  eval_trial inserted                                        -> request_reset (paid reset task)
  robot enters status paused (no takeover task on it)       -> request_reset
  reset task enters completed                               -> reserve_task_payment + resume_policy
  clean_teleop task claimed / no longer claimed             -> start_teleop / stop_teleop on its robot
  clean_teleop episode accepted, task still claimed         -> complete_task (frees the robot's lease)
  TDM-125: a robot's status changes / run_settings change   -> dispatch the split tickets collecting on it

Every path is idempotent: the dispatcher decides from rows, so a resync replaying an event is a no-op.
"""
from __future__ import annotations

from .. import dispatcher as dsp
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..log import get_logger

log = get_logger("dispatcher")


def _d(ev: Event) -> dsp.Dispatcher:
    return dsp.get_dispatcher(ev.bus)


def dispatch_on_plan_approved(ev: Event) -> None:
    st = _d(ev).dispatch_plan(int(ev.row["id"]))
    log.info("dispatch.plan", extra={"fields": {**st.to_dict(), "source": ev.source}})


def dispatch_on_plan_changed(ev: Event) -> None:
    if ev.old is not None and ev.old.get("approved_cents") != ev.row.get("approved_cents") \
            and ev.old.get("status") in dsp.DISPATCHABLE_PLAN:
        _d(ev).dispatch_plan(int(ev.row["id"]))


def dispatch_on_task_changed(ev: Event) -> None:
    if ev.old is None or ev.old.get("status") != ev.row.get("status"):
        _d(ev).dispatch_plan(int(ev.row["data_plan_id"]))


def dispatch_on_review(ev: Event) -> None:
    if ev.old is not None and ev.old.get("review_status") == ev.row.get("review_status"):
        return
    task = ev.bus.get("task", int(ev.row["task_id"]))
    if task is not None and int(task.get("data_plan_id") or 0):
        _d(ev).dispatch_plan(int(task["data_plan_id"]))
    _d(ev).complete_on_accept(ev.row)  # accepted clean demo: complete the still-claimed task


def dispatch_on_ticket_changed(ev: Event) -> None:
    o, n = ev.old or {}, ev.row
    if o.get("budget_cents") != n.get("budget_cents") or o.get("status") != n.get("status"):
        _d(ev).dispatch_ticket(int(n["id"]))


def reset_after_eval_trial(ev: Event) -> None:
    res = _d(ev).on_eval_trial(ev.row, snapshot=ev.source == "snapshot")
    if res is not None:
        log.info("dispatch.reset", extra={"fields": {"trial_id": ev.row.get("id"), **res.__dict__}})


def reset_on_robot_paused(ev: Event) -> None:
    _d(ev).on_robot_paused(ev.row)


def resume_after_reset(ev: Event) -> None:
    out = _d(ev).on_reset_completed(ev.row, snapshot=ev.source == "snapshot")
    if out:
        log.info("dispatch.resume", extra={"fields": {"task_id": ev.row.get("id"), "outcome": out}})


def dispatch_teleop_on_task(ev: Event) -> None:
    out = _d(ev).on_teleop_task(ev.row, ev.old)
    if out:
        log.info("dispatch.teleop", extra={"fields": {"task_id": ev.row.get("id"), "outcome": out}})


def dispatch_on_collect_robot(ev: Event) -> None:
    """TDM-125: a split ticket's collect robot came back (or went away), or its collect_robot_id changed:
    re-run the ticket's plans so waiting collection tasks get posted on it."""
    from ..robot_mode import collect_robot_id, is_split

    if ev.table == "robot":
        if ev.old is not None and ev.old.get("status") == ev.row.get("status"):
            return
        rid = int(ev.row["id"])
        tids = [int(t["id"]) for t in ev.bus.rows("ticket") if t.get("status") == "dispatch"
                and is_split(ev.bus.get("run_settings", int(t["id"])) or {}, t)
                and collect_robot_id(ev.bus.get("run_settings", int(t["id"])) or {}, t) == rid]
    else:
        if ev.old is not None and ev.old.get("hyperparams_json") == ev.row.get("hyperparams_json"):
            return
        t = ev.bus.get("ticket", int(ev.row["ticket_id"]))
        tids = [int(t["id"])] if t is not None and t.get("status") == "dispatch" else []
    for tid in tids:
        _d(ev).dispatch_ticket(tid)


def register(reg: Registry) -> Registry:
    """Put the dispatcher handlers on ``reg`` (the default registry at import; tests use their own)."""
    for t in dsp.TABLES:
        reg.watch(t)
    reg.on_enter("data_plan", where=lambda r: r.status in dsp.DISPATCHABLE_PLAN, name="dispatch_on_plan_approved")(dispatch_on_plan_approved)
    reg.on_update("data_plan", where=lambda r: r.status in dsp.DISPATCHABLE_PLAN, name="dispatch_on_plan_changed")(dispatch_on_plan_changed)
    reg.on_update("task", where=lambda r: int(r.data_plan_id or 0) > 0, name="dispatch_on_task_changed")(dispatch_on_task_changed)
    reg.on_update("episode", where=lambda r: int(r.task_id or 0) > 0, name="dispatch_on_review")(dispatch_on_review)
    reg.on_update("ticket", name="dispatch_on_ticket_changed")(dispatch_on_ticket_changed)
    reg.on_insert("eval_trial", include_existing=True, name="reset_after_eval_trial")(reset_after_eval_trial)
    reg.on_enter("robot", where=lambda r: r.status == "paused", include_existing=False, name="reset_on_robot_paused")(reset_on_robot_paused)
    reg.on_enter("task", where=lambda r: r.type == "reset" and r.status == "completed", name="resume_after_reset")(resume_after_reset)
    reg.on_update("task", where=lambda r: r.type == "clean_teleop", name="dispatch_teleop_on_task")(dispatch_teleop_on_task)
    reg.on_update("robot", name="dispatch_on_collect_robot")(dispatch_on_collect_robot)  # TDM-125
    reg.on_update("run_settings", name="dispatch_on_collect_robot_setting")(dispatch_on_collect_robot)  # TDM-125
    return reg


register(default_registry)
