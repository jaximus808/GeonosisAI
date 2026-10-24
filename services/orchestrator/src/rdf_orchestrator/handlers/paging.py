"""TDM-65: web-push paging wiring.

* Installs ``paging.page_hook`` as the dispatcher's page hook, so every task the dispatcher creates
  (plan tasks and reset requests) pages contributors' phones.
* A ``task`` inserted live with ``status=open`` that the dispatcher did NOT create (no data plan and
  no ``[ref ...]`` tag: a manual ``create_task`` from a script or the dashboard) is paged through the
  same ``Dispatcher.page``, so it is recorded as a ``dispatch.paged`` event too.

Snapshot and resync inserts are never paged (a restart must not re-buzz every phone), and the
Pager pages a task id at most once.
"""
from __future__ import annotations

from .. import dispatcher as dsp
from .. import paging
from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..log import get_logger

log = get_logger("paging")


def page_manual_task(ev: Event) -> None:
    if ev.source != "live":
        return
    row = ev.row
    if int(row.get("data_plan_id") or 0) > 0 or dsp.task_ref(row) is not None:
        return  # the dispatcher made it and pages it itself
    dsp.get_dispatcher(ev.bus).page(int(row.get("ticket_id") or 0), [int(row["id"])], str(row.get("type") or ""),
                                    int(row.get("price_cents") or 0))


def register(reg: Registry) -> Registry:
    paging.install()
    reg.watch("contributor")
    reg.on_insert("task", where=lambda r: r.status == "open", name="tdm65_page_manual_task")(page_manual_task)
    return reg


register(default_registry)
