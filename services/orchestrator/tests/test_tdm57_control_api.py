"""TDM-57: POST /tasks/{id}/revoke (operator key; release_task or cancel_task as the orchestrator)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator.tdm57_control_api import get_tdm57_operator_key, get_tdm57_stdb, tdm57_control_router

KEY = "tdm57-operator-key"


class Refused(Exception):
    status = 530


class FakeStdb:
    def __init__(self) -> None:
        self.tasks = {7: {"id": 7, "status": "claimed", "assignee_id": 3, "robot_id": 1},
                      8: {"id": 8, "status": "open", "assignee_id": 0, "robot_id": 1},
                      9: {"id": 9, "status": "completed", "assignee_id": 3, "robot_id": 1}}
        self.calls: list[tuple] = []

    async def sql(self, q: str) -> list[dict]:
        row = self.tasks.get(int(q.rsplit("=", 1)[1]))
        return [row] if row else []

    async def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))


def make(key=KEY):
    fake = FakeStdb()
    app = FastAPI()
    app.include_router(tdm57_control_router)
    app.dependency_overrides[get_tdm57_stdb] = lambda: fake
    app.dependency_overrides[get_tdm57_operator_key] = lambda: key
    return TestClient(app), fake


def test_auth_fails_closed():
    c, fake = make(key=None)
    assert c.post("/tasks/7/revoke", json={}, headers={"X-RDF-Operator-Key": "x"}).status_code == 503
    c, fake = make()
    assert c.post("/tasks/7/revoke", json={}).status_code == 401
    assert c.post("/tasks/7/revoke", json={}, headers={"X-RDF-Operator-Key": "nope"}).status_code == 401
    assert fake.calls == []


def test_release_and_cancel():
    c, fake = make()
    h = {"X-RDF-Operator-Key": KEY, "X-Human-Id": "jaxon"}
    r = c.post("/tasks/7/revoke", json={"reason": "unsafe"}, headers=h)
    assert r.status_code == 200 and r.json()["action"] == "released" and r.json()["previous_assignee_id"] == 3
    assert fake.calls[0] == ("release_task", 7)
    assert fake.calls[1][0:3] == ("append_event", "human:operator", "operator.control_revoked")
    r = c.post("/tasks/7/revoke", json={"reason": "done for today", "cancel": True}, headers=h)
    assert r.status_code == 200 and fake.calls[2] == ("cancel_task", 7, "done for today")


def test_nothing_to_revoke():
    c, fake = make()
    h = {"X-RDF-Operator-Key": KEY}
    assert c.post("/tasks/8/revoke", json={}, headers=h).status_code == 409
    assert c.post("/tasks/9/revoke", json={"cancel": True}, headers=h).status_code == 409
    assert c.post("/tasks/99/revoke", json={}, headers=h).status_code == 404
    assert fake.calls == []
