"""TDM-27: bus wiring for the resume bridge (logic in ``..resume_bridge``).

  agent_session inserted / updated (boot snapshot too) -> track or drop the wait, evaluate now
  agent_session deleted                                 -> drop the wait
  any row change on a predicate table (BUS_TABLES)      -> re-evaluate the waits that name that table
  other tables named by a predicate                     -> side subscription inside the bridge
  safety poll thread (RESUME_BRIDGE_POLL_S, 30 s)       -> resync from the mirror + orphan recovery

The handlers are async and only do a dict lookup plus a cheap predicate evaluation on the loop; the
claim (agent_resume) and the Factory call run on the bridge's own thread pool, so a slow Factory turn
never holds a bus worker.
"""
from __future__ import annotations

from .. import resume_bridge as rb
from ..bus import Event, Registry
from ..bus import registry as default_registry


def _change(ev: Event) -> rb.Change:
    kind = "boot" if ev.source == "snapshot" else ev.kind
    return rb.Change(ev.table, kind, ev.row, ev.old, ev.server_ts_micros)


async def resume_bridge_on_session(ev: Event) -> None:
    b = rb.get_bridge(ev.bus)
    if ev.kind == "delete":
        b.session_deleted(ev.row)
    else:
        b.session_changed(ev.row, _change(ev))
    b.table_changed("agent_session", _change(ev))  # predicates may wait on another agent's status


async def resume_bridge_on_table(ev: Event) -> None:
    b = rb._instances.get(id(ev.bus))  # no bridge yet means no session has waited: nothing to do
    if b is not None and ev.table in b.by_table:
        b.table_changed(ev.table, _change(ev))


def register(reg: Registry) -> Registry:
    for t in rb.BUS_TABLES:
        reg.watch(t)
    reg.on_insert("agent_session", include_existing=True, name="resume_bridge_session_insert")(resume_bridge_on_session)
    reg.on_update("agent_session", name="resume_bridge_session_update")(resume_bridge_on_session)
    reg.on_delete("agent_session", name="resume_bridge_session_delete")(resume_bridge_on_session)
    for t in rb.BUS_TABLES:
        if t == "agent_session":
            continue
        for kind in ("insert", "update", "delete"):
            getattr(reg, f"on_{kind}")(t, name=f"resume_bridge_{t}_{kind}")(resume_bridge_on_table)
    return reg


register(default_registry)
