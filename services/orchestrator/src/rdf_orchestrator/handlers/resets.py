"""TDM-62: bus wiring for reset escalation (logic in ``..reset_watch``).

  reset task enters open (boot snapshot too) -> arm escalation timers (RDF_RESET_ESCALATE_S)
  timer fires and the task is still open     -> page again + dispatch.reset_escalated {level}

The reset itself (task per eval trial / pause, pay + resume_policy on completion) stays the TDM-26
dispatcher's (handlers/dispatcher.py).
"""
from __future__ import annotations

from .. import reset_watch as rw
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..log import get_logger

log = get_logger("reset_watch")


def escalate_open_reset(ev: Event) -> None:
    delays = rw.get_watch(ev.bus).schedule(ev.row)
    if delays:
        log.info("reset.watch", extra={"fields": {"task_id": ev.row.get("id"), "escalate_in_s": [round(d) for d in delays],
                                                  "source": ev.source}})


def register(reg: Registry) -> Registry:
    reg.watch("task")
    reg.on_enter("task", where=lambda r: r.type == "reset" and r.status == "open", include_existing=True,
                 name="escalate_open_reset")(escalate_open_reset)
    return reg


register(default_registry)
