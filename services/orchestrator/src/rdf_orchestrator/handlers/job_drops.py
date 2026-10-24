"""TDM-108: job drops wiring. Adds ``job_drops.page_listener`` to the dispatcher's page listeners, so
every paging of new paid tasks (plan tasks, resets, manual ``create_task`` rows paged by
``handlers/paging.py``) also builds a share card and, in ``RDF_DROP_MODE=live`` only, posts it to the
configured social channels. The listener returns at once; rendering and posting run on a worker thread.

Lifecycle: every ``task`` status change goes to ``job_drops.task_changed``: when a dropped task is claimed
its posts are marked "Claimed, no longer available" (Discord edit; deleted where a network can't edit),
when it is completed / cancelled / expired they are deleted, and a released task is posted again.
"""
from __future__ import annotations

from .. import job_drops
from ..bus import Event, Registry
from ..bus import registry as default_registry


def task_status_changed(ev: Event) -> None:
    if ev.source == "snapshot":
        return
    old = (ev.old or {}).get("status") if ev.old is not None else None
    if old is not None and old == ev.row.get("status"):
        return
    rows = ev.bus.rows if ev.bus is not None else (lambda t: [])
    job_drops.task_changed(int(ev.row["id"]), lambda t: [dict(r) for r in rows(t)])


def register(reg: Registry) -> Registry:
    job_drops.install()
    reg.on_update("task", name="tdm108_drop_lifecycle")(task_status_changed)
    return reg


register(default_registry)
