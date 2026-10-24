"""TDM-69: bus wiring for DAgger-style interventions (logic in ``..interventions``).

  event_log intervention.failure_detected / .flagged  -> start: route/create a recovery_takeover
                                                          task, pause_for_takeover, page
  event_log intervention.handed_back                   -> handback over the TDM-52 control socket
  task (recovery_takeover) changes status              -> claimed / claim lapsed / completed
                                                          (handback for the claimant) / cancelled
  robot_command changes status                         -> handback done; the paused rollout ended
                                                          (close, complete the task); collection
                                                          rollout done -> paid reset; any job
                                                          closed -> next collection rollout
  episode inserted / review_status changes             -> link the recovery episode, log the review
  ticket enters dispatch                               -> start collection rollouts (if enabled)
  reset task completed                                 -> next collection rollout
  watchdog thread (INTERVENTION_WATCHDOG_S)            -> claim / takeover timeouts, robot lost,
                                                          orchestrator-side rollout timeout

Every path decides from rows + the replayed ``intervention.*`` trail, so a resync is a no-op.
"""
from __future__ import annotations

from .. import interventions as ivm
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..log import get_logger

log = get_logger("interventions")


def _m(ev: Event) -> ivm.InterventionManager:
    return ivm.get_manager(ev.bus)


def intervention_on_signal(ev: Event) -> None:
    out = _m(ev).on_signal(ev.row)
    log.info("intervention.signal", extra={"fields": {"event_id": ev.row.get("id"), "type": ev.row.get("type"),
                                                      "outcome": out, "source": ev.source}})


def intervention_on_task(ev: Event) -> None:
    if ev.old is not None and ev.old.get("status") == ev.row.get("status"):
        return
    _m(ev).on_task(ev.row, ev.old)


def intervention_on_command(ev: Event) -> None:
    _m(ev).on_command(ev.row, ev.old)


def intervention_on_episode(ev: Event) -> None:
    if ev.old is not None and ev.old.get("review_status") == ev.row.get("review_status"):
        return
    _m(ev).on_episode(ev.row, ev.old)


def intervention_collect_on_dispatch(ev: Event) -> None:
    _m(ev).ensure_collection(int(ev.row["id"]), why=f"ticket entered dispatch ({ev.source})")


def intervention_collect_after_reset(ev: Event) -> None:
    tid = int(ev.row.get("ticket_id") or 0)
    if tid:
        _m(ev).ensure_collection(tid, why=f"reset task {ev.row.get('id')} completed")


def register(reg: Registry) -> Registry:
    for t in ivm.TABLES:
        reg.watch(t)
    reg.on_insert("event_log", where=lambda r: r.type in (ivm.FAILURE_EVENT, ivm.FLAG_EVENT, ivm.HANDBACK_EVENT),
                  include_existing=False, name="intervention_on_signal")(intervention_on_signal)
    reg.on_update("task", where=lambda r: r.type == ivm.TASK_TYPE, name="intervention_on_task")(intervention_on_task)
    reg.on_update("robot_command", name="intervention_on_command")(intervention_on_command)
    reg.on_insert("episode", where=lambda r: int(r.task_id or 0) > 0, include_existing=False,
                  name="intervention_on_episode_insert")(intervention_on_episode)
    reg.on_update("episode", where=lambda r: int(r.task_id or 0) > 0, name="intervention_on_episode")(intervention_on_episode)
    reg.on_enter("ticket", where=lambda r: r.status == "dispatch", name="intervention_collect_on_dispatch")(
        intervention_collect_on_dispatch)
    reg.on_enter("task", where=lambda r: r.type == "reset" and r.status == "completed", include_existing=False,
                 name="intervention_collect_after_reset")(intervention_collect_after_reset)
    return reg


register(default_registry)
