"""TDM-113: text job offers wiring. Adds ``text_offers.page_listener`` to the dispatcher's page listeners (next
to TDM-65 push and TDM-108 drops), so every paging of new paid tasks also texts up to N opted-in contributors
(dry run by default). Task status changes expire unanswered offers once their tasks are no longer open.
"""
from __future__ import annotations

from .. import text_offers
from ..bus import Event, Registry
from ..bus import registry as default_registry


def tdm113_task_status_changed(ev: Event) -> None:
    if ev.source == "snapshot":
        return
    old = (ev.old or {}).get("status") if ev.old is not None else None
    new = ev.row.get("status")
    if new == "open" or (old is not None and old == new):
        return
    rows = ev.bus.rows if ev.bus is not None else (lambda t: [])
    text_offers.task_changed(int(ev.row["id"]), lambda t: [dict(r) for r in rows(t)])


def register(reg: Registry) -> Registry:
    text_offers.install()
    reg.on_update("task", name="tdm113_text_offer_lifecycle")(tdm113_task_status_changed)
    return reg


register(default_registry)
