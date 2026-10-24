"""TDM-33: the orchestrator side of the Data Droid (handlers/data_droid.py). No Factory calls."""
from __future__ import annotations

import asyncio
import json

from rdf_orchestrator.bus import Event
from rdf_orchestrator.flywheel import TicketView
from rdf_orchestrator.handlers import data_droid as dd
from rdf_orchestrator.stdb import Row


class FakeBus:
    def __init__(self, tables):
        self.tables = {t: [Row(r) for r in rows] for t, rows in tables.items()}

    def rows(self, table):
        return list(self.tables.get(table, []))

    def get(self, table, key):
        k = "ticket_id" if table == "run_settings" else "id"
        return next((r for r in self.tables.get(table, []) if r.get(k) == key), None)


def _ep(i, status, created, kind="clean_teleop"):
    return {"id": i, "ticket_id": 4, "kind": kind, "review_status": status, "created_at": created}


def _bus(sessions=(), plan_amount=3, lerobot_uri=None, events=()):
    return FakeBus({
        "ticket": [{"id": 4, "status": "review", "iteration": 1, "robot_id": 0}],
        "run_settings": [{"ticket_id": 4, "target_episodes": 9}],
        "dataset": [{"id": 2, "ticket_id": 4, "accepted_count": 3, "lerobot_uri": lerobot_uri}],
        "data_plan": [{"id": 1, "ticket_id": 4, "iteration": 0, "amount": 2, "status": "approved", "created_at": 10},
                      {"id": 5, "ticket_id": 4, "iteration": 1, "amount": plan_amount, "status": "approved",
                       "created_at": 100}],
        "episode": [_ep(1, "accepted", 20), _ep(2, "accepted", 30), _ep(3, "rejected", 40),
                    _ep(6, "accepted", 120), _ep(7, "pending", 130), _ep(8, "accepted", 5, kind="baseline")],
        "agent_session": list(sessions),
        "event_log": list(events),
    })


def test_target_is_prior_accepted_plus_plan_amount():
    v = TicketView.from_bus(_bus(), 4)
    assert dd.data_droid_target(v) == 2 + 3  # episodes 1, 2 before plan 5; baseline rollouts never count
    v0 = TicketView.from_bus(_bus(plan_amount=0), 4)
    assert dd.data_droid_target(v0) == 9  # no plan amount: run_settings.target_episodes


def test_handler_starts_once_and_never_beside_a_live_session(monkeypatch):
    started = []

    async def fake_launch(tid, target, iteration):
        started.append((tid, target, iteration))
        dd._starting.discard(tid)

    monkeypatch.setattr(dd, "_launch", fake_launch)

    async def fire(bus):
        await dd.data_droid_on_review(Event("ticket", "update", bus.get("ticket", 4), bus=bus))
        await asyncio.sleep(0)

    monkeypatch.delenv("RDF_DATA_DROID", raising=False)
    asyncio.run(fire(_bus()))
    assert started == []  # opt-in only
    monkeypatch.setenv("RDF_DATA_DROID", "1")
    asyncio.run(fire(_bus(sessions=[{"id": 9, "ticket_id": 4, "role": "data_droid", "status": "waiting_for"}])))
    assert started == []  # the resume bridge owns a parked droid
    asyncio.run(fire(_bus(sessions=[{"id": 9, "ticket_id": 4, "role": "data_droid", "status": "done"},
                                    {"id": 10, "ticket_id": 4, "role": "training_droid", "status": "running"}])))
    assert started == [(4, 5, 1)]


def test_droid_dataset_uri_requires_matching_accepted_ids():
    uri = "modal-vol://rdf-datasets/lerobot/rdf/ticket-4"
    ok = {"id": 50, "type": "dataset.assembled", "payload_json": json.dumps(
        {"ticket_id": 4, "lerobot_uri": uri, "episode_ids": [6, 2, 1]})}
    stale = {"id": 40, "type": "dataset.assembled", "payload_json": json.dumps(
        {"ticket_id": 4, "lerobot_uri": uri, "episode_ids": [1, 2]})}
    b = _bus(lerobot_uri=uri, events=[stale, ok])
    assert dd.droid_dataset_uri(TicketView.from_bus(b, 4), b) == uri
    b = _bus(lerobot_uri=uri, events=[stale])
    assert dd.droid_dataset_uri(TicketView.from_bus(b, 4), b) is None  # built before episode 6 was accepted
    b = _bus(lerobot_uri="file:///tmp/x", events=[ok])
    assert dd.droid_dataset_uri(TicketView.from_bus(b, 4), b) is None  # a laptop path is not trainable on Modal
