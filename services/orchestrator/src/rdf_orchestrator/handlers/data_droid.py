"""TDM-33: start the Data Droid (``rdf_factory.droids.data``) at the right flywheel moment.

Opt-in with ``RDF_DATA_DROID=1`` (every droid turn costs Factory credits; off for the TDM-28 harness).

  ticket entering ``review`` (boot snapshot too) -> if this ticket has no live data_droid
      agent_session, start one with ``target`` = accepted episodes from earlier iterations + this
      iteration's data_plan.amount (fallback run_settings.target_episodes). The droid validates what
      is accepted so far and parks with ``WAITING_FOR dataset:accepted_count>=<target>``.
  the TDM-27 resume bridge (handlers/resume_bridge.py) wakes it when the dataset row reaches the
      target; the droid assembles, verifies, writes the card, publishes ``dataset.lerobot_uri`` and
      parks on ``data_plan:iteration>=<next>``, so the next iteration's plan wakes it to rebuild.

A live session (starting/running/waiting_for/resuming/paused) is never started twice: the bridge owns
it. The first turn runs as a background task so no bus worker waits on Factory.

``droid_dataset_uri(view)`` is the hand-off to training: the dataset.lerobot_uri the droid published,
only when its ``dataset.assembled`` event lists exactly the ticket's accepted episodes and the uri is
on the rdf-datasets Volume (then ``hyperparams.dataset_uri`` lets TDM-21 skip its own convert).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("data_droid")

ROLE = "data_droid"
LIVE = ("starting", "running", "waiting_for", "resuming", "paused")
_starting: set[int] = set()

for _t in ("agent_session", "dataset", "data_plan", "episode", "run_settings", "event_log"):
    watch(_t)


def enabled() -> bool:
    return os.environ.get("RDF_DATA_DROID", "").strip() == "1"


def data_droid_target(view: Any) -> int:
    """Accepted-episode count the droid should build at: earlier iterations' accepted + this plan's amount."""
    from ..flywheel import DATASET_KINDS

    plan = view.plan
    if plan is not None and int(plan.get("amount") or 0) > 0:
        since = plan.get("created_at") or 0
        prior = sum(1 for e in view.episodes if e.get("kind") in DATASET_KINDS and e.get("review_status") == "accepted"
                    and (e.get("created_at") or 0) < since)
        return prior + int(plan["amount"])
    return int((view.settings or {}).get("target_episodes") or 0)


def live_sessions(bus: Any, ticket_id: int) -> list[dict]:
    return [dict(s) for s in bus.rows("agent_session")
            if s.get("role") == ROLE and int(s.get("ticket_id") or 0) == int(ticket_id) and s.get("status") in LIVE]


def droid_dataset_uri(view: Any, bus: Any) -> str | None:
    ds = view.dataset or {}
    uri = ds.get("lerobot_uri")
    if isinstance(uri, dict):  # option shapes
        uri = uri.get("some")
    if not uri or not str(uri).startswith("modal-vol://rdf-datasets/"):
        return None
    want = sorted(view.accepted_episode_ids)
    for r in sorted(bus.rows("event_log"), key=lambda r: r.get("id") or 0, reverse=True):
        if r.get("type") != "dataset.assembled":
            continue
        try:
            p = json.loads(r.get("payload_json") or "{}")
        except ValueError:
            continue
        if int(p.get("ticket_id") or 0) != view.id:
            continue
        return str(uri) if p.get("lerobot_uri") == uri and sorted(int(x) for x in p.get("episode_ids") or []) == want else None
    return None


async def _launch(ticket_id: int, target: int, iteration: int) -> None:
    try:
        from rdf_factory import FactoryClient
        from rdf_factory.droids.data import launch_data_droid

        async with FactoryClient() as fc:
            r = await launch_data_droid(fc, ticket_id, target=target, iteration=iteration,
                                        backend=os.environ.get("RDF_DATA_DROID_BACKEND", "local"),
                                        media_root=os.environ.get("RDF_DATA_DROID_MEDIA_ROOT", ""))
        log.info("data_droid.started", extra={"fields": {"ticket_id": ticket_id, "agent_session_id": r.agent_session_id,
                                                         "status": r.status, "credits": r.credits,
                                                         "waiting_for": (r.waiting_for or {}).get("raw")}})
    except Exception as e:  # noqa: BLE001 - a droid that cannot start must never stall the loop
        log.error("data_droid.start_failed", extra={"fields": {"ticket_id": ticket_id,
                                                               "error": f"{type(e).__name__}: {e}"[:500]}})
    finally:
        _starting.discard(ticket_id)


@on_enter("ticket", where=lambda r: r.status == "review", name="tdm33_data_droid_on_review")
async def data_droid_on_review(ev: Event) -> None:
    if not enabled():
        return
    from ..flywheel import TicketView

    tid = int(ev.row.id)
    live = live_sessions(ev.bus, tid)
    if live or tid in _starting:
        log.info("data_droid.skip", extra={"fields": {"ticket_id": tid, "reason": "live session" if live else "starting",
                                                      "agent_session_id": live[-1]["id"] if live else None}})
        return
    view = TicketView.from_bus(ev.bus, tid)
    if view is None:
        return
    target = data_droid_target(view)
    if target < 1:
        log.warning("data_droid.skip", extra={"fields": {"ticket_id": tid, "reason": "no plan amount or target_episodes"}})
        return
    _starting.add(tid)
    asyncio.get_running_loop().create_task(_launch(tid, target, int(view.ticket.get("iteration") or 0)),
                                           name=f"tdm33-data-droid-t{tid}")
