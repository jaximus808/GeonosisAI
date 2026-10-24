"""TDM-78: POST /robots/{id}/commands (operator key; soft_estop never blocked)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator.robot_commands_api import get_operator_key, get_robot_commands_stdb, robot_commands_router


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class FakeStdb:
    def __init__(self, refuse: _Refused | None = None):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.rows: list[dict[str, Any]] = []
        self.refuse = refuse

    async def call_reducer(self, name: str, *args: Any) -> None:
        if name == "issue_robot_command" and self.refuse:
            raise self.refuse
        self.calls.append((name, args))
        if name == "issue_robot_command":
            self.rows.append({"id": 100 + len(self.rows), "seq": len(self.rows) + 1, "type": args[1],
                              "status": "issued", "issued_by": "orchestrator"})

    async def sql(self, query: str) -> list[dict]:
        return list(self.rows)


def _client(stdb: FakeStdb, key: str | None) -> TestClient:
    app = FastAPI()
    app.include_router(robot_commands_router)
    app.dependency_overrides[get_robot_commands_stdb] = lambda: stdb
    app.dependency_overrides[get_operator_key] = lambda: key
    return TestClient(app)


def test_home_needs_the_key():
    stdb = FakeStdb()
    c = _client(stdb, "k" * 32)
    assert c.post("/robots/1/commands", json={"type": "home"}).status_code == 401
    assert c.post("/robots/1/commands", json={"type": "home"}, headers={"x-rdf-operator-key": "nope"}).status_code == 401
    r = c.post("/robots/1/commands", json={"type": "home"}, headers={"x-rdf-operator-key": "k" * 32})
    assert r.status_code == 200, r.text
    assert r.json()["command_id"] == 100 and r.json()["type"] == "home"
    assert stdb.calls[0] == ("issue_robot_command", (1, "home", "{}", 0))
    assert stdb.calls[1][0] == "append_event"  # operator.robot_command audit row


def test_motion_is_off_without_a_configured_key():
    c = _client(FakeStdb(), None)
    assert c.post("/robots/1/commands", json={"type": "home"}).status_code == 503


@pytest.mark.parametrize("key", [None, "k" * 32])
def test_soft_estop_is_never_blocked_by_the_key(key):
    stdb = FakeStdb()
    r = _client(stdb, key).post("/robots/7/commands", json={"type": "soft_estop", "payload": {"reason": "web"}})
    assert r.status_code == 200, r.text
    assert stdb.calls[0] == ("issue_robot_command", (7, "soft_estop", '{"reason": "web"}', 0))


def test_unknown_type_and_reducer_refusal():
    c = _client(FakeStdb(), "k")
    assert c.post("/robots/1/commands", json={"type": "run_eval"}, headers={"x-rdf-operator-key": "k"}).status_code == 400
    busy = _client(FakeStdb(_Refused(530, "robot 1 is in estop")), "k")
    r = busy.post("/robots/1/commands", json={"type": "home"}, headers={"x-rdf-operator-key": "k"})
    assert r.status_code == 409 and "estop" in r.json()["detail"]
    down = _client(FakeStdb(ConnectionError("refused")), "k")  # type: ignore[arg-type]
    assert down.post("/robots/1/commands", json={"type": "home"}, headers={"x-rdf-operator-key": "k"}).status_code == 502
