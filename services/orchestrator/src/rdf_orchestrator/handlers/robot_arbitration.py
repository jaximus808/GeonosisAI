"""TDM-114: bus wiring for robot arbitration (logic in ``..robot_arbitration``).

  ticket enters cancelled / failed  -> release its robot (stop its paused or running rollout) and kick
                                       the next queued eval (``robot.released``)
  robot row updates                 -> start the arbiter's watchdog (auto-yield after
                                       RDF_PAUSED_TAKEOVER_TIMEOUT_S) on first sight
"""
from __future__ import annotations

from .. import robot_arbitration as arb
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..log import get_logger

log = get_logger("arbitration")


def arbitration_on_ticket_end(ev: Event) -> None:
    out = arb.get_arbiter(ev.bus).release_ticket(int(ev.row["id"]), why=f"is {ev.row.get('status')}")
    if out:
        log.info("arbitration.released", extra={"fields": out})


def arbitration_on_robot(ev: Event) -> None:
    arb.get_arbiter(ev.bus)  # starts the watchdog once


def register(reg: Registry) -> Registry:
    for t in ("ticket", "robot", "robot_command", "event_log", "task", "eval_run"):
        reg.watch(t)
    reg.on_enter("ticket", where=lambda r: r.status in ("cancelled", "failed"), include_existing=True,
                 name="arbitration_on_ticket_end")(arbitration_on_ticket_end)
    reg.on_update("robot", name="arbitration_on_robot")(arbitration_on_robot)
    return reg


register(default_registry)
