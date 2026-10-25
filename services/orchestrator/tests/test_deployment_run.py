"""[SIM] POST /tickets/{id}/deployment/run: a shipped ticket's deployed policy on its sim robot, never a real one."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator.deployment_api import deployment_router
from rdf_orchestrator.robot_commands_api import get_operator_key, get_robot_commands_stdb

KEY = "k" * 32
H = {"x-rdf-operator-key": KEY}
SIM = json.dumps({"sim": True, "cameras": "mujoco"})
REAL_TELEOP = json.dumps({"sim": False, "teleop": True, "policy": False})
REAL_POLICY = json.dumps({"sim": False, "policy": True})
URI = "fake://stub-no-gpu/tickets/14/run-5"


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class FakeStdb:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "ticket": [{"id": 14, "robot_id": 3, "status": "shipped", "deployed_checkpoint_uri": URI}],
            "robot": [{"id": 3, "name": "sim-yam-mujoco", "status": "online", "camera_config_json": SIM},
                      {"id": 4, "name": "yam-01", "status": "online", "camera_config_json": REAL_TELEOP},
                      {"id": 6, "name": "yam-02", "status": "online", "camera_config_json": REAL_POLICY}],
            "robot_command": [],
        }
        self.calls: list[tuple] = []
        self.refuse: _Refused | None = None

    async def sql(self, q: str) -> list[dict]:
        m = re.fullmatch(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (\d+))?", q)
        assert m, q
        rows = self.tables[m[1]]
        return [dict(r) for r in rows if m[2] is None or r.get(m[2]) == int(m[3])]

    async def call_reducer(self, name: str, *args: Any) -> None:
        if name == "issue_robot_command" and self.refuse:
            raise self.refuse
        self.calls.append((name, *args))
        if name == "issue_robot_command":
            rid, typ, payload, tid = args
            self.tables["robot_command"].append({"id": 900 + len(self.tables["robot_command"]), "robot_id": rid,
                                                 "type": typ, "payload_json": payload, "ticket_id": tid,
                                                 "status": "issued"})


def _client(stdb: FakeStdb, key: str | None = KEY) -> TestClient:
    app = FastAPI()
    app.include_router(deployment_router)
    app.dependency_overrides[get_robot_commands_stdb] = lambda: stdb
    app.dependency_overrides[get_operator_key] = lambda: key
    return TestClient(app)


def _issued(stdb: FakeStdb) -> list[tuple]:
    return [c for c in stdb.calls if c[0] == "issue_robot_command"]


def test_runs_the_deployed_checkpoint_on_the_sim_robot_and_logs_it():
    s = FakeStdb()
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["command_id"] == 900 and body["robot_id"] == 3 and body["checkpoint_uri"] == URI and body["runs_on"] == "sim"
    (_, rid, typ, payload, tid), = _issued(s)
    assert (rid, typ, tid) == (3, "run_baseline", 14)
    p = json.loads(payload)
    assert p["purpose"] == "deployment_run" and p["checkpoint_uri"] == URI and "eval_run_id" not in p
    ev = [c for c in s.calls if c[0] == "append_event"]
    assert len(ev) == 1 and ev[0][2] == "deployment.run_started"
    assert json.loads(ev[0][3])["ticket_id"] == 14


def test_needs_operator_login():
    s = FakeStdb()
    assert _client(s).post("/tickets/14/deployment/run").status_code == 401
    assert _client(s).post("/tickets/14/deployment/run", headers={"x-rdf-operator-key": "nope"}).status_code == 401
    assert _client(s, key=None).post("/tickets/14/deployment/run", headers=H).status_code == 503
    assert not _issued(s)


def test_refuses_teleop_only_real_robot():
    s = FakeStdb()
    s.tables["ticket"][0]["robot_id"] = 4  # yam-01: teleop only
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "robot_not_sim"
    assert not _issued(s) and not s.calls


def test_refuses_policy_capable_real_robot_too():
    s = FakeStdb()
    s.tables["ticket"][0]["robot_id"] = 6  # a real arm that can eval: still never a deployment run
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "robot_not_sim"
    assert not _issued(s)


def test_refuses_unshipped_ticket():
    s = FakeStdb()
    s.tables["ticket"][0].update(status="eval", deployed_checkpoint_uri=None)
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_shipped"
    assert _client(s).post("/tickets/99/deployment/run", headers=H).status_code == 404
    assert not _issued(s)


def test_robot_busy_with_an_open_job():
    s = FakeStdb()
    s.tables["robot_command"].append({"id": 5, "robot_id": 3, "type": "run_eval", "status": "acked", "ticket_id": 15,
                                      "payload_json": "{}"})
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "robot_busy" and "Robot busy" in d["message"] and d["already_running"] is False
    assert not _issued(s)


def test_robot_busy_when_another_ticket_waits_to_eval():
    s = FakeStdb()
    s.tables["ticket"].append({"id": 15, "robot_id": 3, "status": "baseline"})
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "robot_busy"
    assert not _issued(s)


def test_robot_offline_and_reducer_busy():
    s = FakeStdb()
    s.tables["robot"][0]["status"] = "offline"
    assert _client(s).post("/tickets/14/deployment/run", headers=H).json()["detail"]["code"] == "robot_offline"
    s.tables["robot"][0]["status"] = "online"
    s.refuse = _Refused(530, "robot 3 is busy with home #7 (issued)")
    r = _client(s).post("/tickets/14/deployment/run", headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "robot_busy"
