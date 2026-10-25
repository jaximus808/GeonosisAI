"""TDM-104: a new ticket gets a generated 3D task scene (``scene.created``), ready to deploy to the sim robot.

``ticket.created`` in event_log -> ``SceneStore.generate`` (Claude through ``rdf_common.llm``; one corrective
retry when the spec does not validate). A failure writes ``scene.generation_failed`` and nothing else: the
ticket carries on, and an operator can regenerate from the ticket page (``POST /tickets/{id}/scene/generate``).
Deploying stays a human step (the operator's Deploy scene button).

Idempotent: a ticket that already has a ``scene.created`` row (bus mirror) is skipped, an in-process claim set
stops two pool workers generating for the same ticket, and old tickets are not backfilled at boot
(``include_existing`` is off: that would be one Claude call per historical ticket).
``RDF_SCENE_AUTOGEN=0`` turns it off.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

from .. import scene as sc
from ..bus import Event, on_insert, watch
from ..log import get_logger

log = get_logger("scene")

watch("event_log")
watch("ticket")
watch("robot")

_lock = threading.Lock()
_claimed: set[int] = set()


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


# Swappable for tests.
make_stdb: Callable[[], Any] = _default_stdb
make_llm: Callable[[], Any] = lambda: None  # noqa: E731 - None = the shared switchable rdf_common.llm client


def enabled() -> bool:
    return os.environ.get("RDF_SCENE_AUTOGEN", "1").strip().lower() not in ("0", "false", "off", "no")


def autodeploy_enabled() -> bool:
    return os.environ.get("RDF_SCENE_AUTODEPLOY", "1").strip().lower() not in ("0", "false", "off", "no")


def _autodeploy(store: Any, ev: Event, tid: int) -> None:
    """Deploy the new scene to the ticket's eval robot when that is a sim robot, so its baseline runs in the
    ticket's own scene (the sim robot loads it before trial 0) instead of whatever scene it had last."""
    if not autodeploy_enabled():
        return
    ticket = next((r for r in ev.bus.rows("ticket") if int(r.get("id") or 0) == tid), None)
    rid = int((ticket or {}).get("robot_id") or 0)
    robot = next((r for r in ev.bus.rows("robot") if int(r.get("id") or 0) == rid), None)
    if not robot or not sc.is_sim_robot(robot):
        return
    store.deploy(tid, rid, by="orchestrator:autodeploy")


@on_insert("event_log", where=lambda r: r.type == "ticket.created", name="tdm104_scene_on_ticket_created")
def scene_on_ticket_created(ev: Event) -> None:
    if not enabled():
        return
    p = sc._payload(ev.row)
    tid = int(p.get("ticket_id") or 0)
    task = str(p.get("task_description") or "").strip()
    if not tid or not task:
        return
    with _lock:
        if tid in _claimed:
            return
        if sc.ticket_revisions(ev.bus.rows("event_log"), tid):
            log.info("scene.skip", extra={"fields": {"ticket_id": tid, "reason": "already has a scene"}})
            return
        _claimed.add(tid)
    stdb = make_stdb()
    try:
        store = sc.SceneStore(stdb.sql, stdb.call_reducer)
        try:
            store.generate(tid, task, llm=make_llm())
            _autodeploy(store, ev, tid)
        except sc.SceneGenerationError as e:
            log.warning("scene.generation_failed", extra={"fields": {"ticket_id": tid, "error": str(e)[:300],
                                                                     "errors": e.errors[:5]}})
        except Exception as e:  # noqa: BLE001 - never take the bus down for a scene
            log.warning("scene.error", extra={"fields": {"ticket_id": tid, "error": repr(e)[:300]}})
    finally:
        close = getattr(stdb, "close", None)
        if callable(close):
            close()
