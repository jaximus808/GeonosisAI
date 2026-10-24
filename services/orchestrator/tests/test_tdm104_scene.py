"""TDM-104: scene generation (fake LLM), event_log storage, deploy to the sim robot, the API, the handler."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_common import scene_spec as ss
from rdf_orchestrator import scene as sc
from rdf_orchestrator.robot_commands_api import get_operator_key
from rdf_orchestrator.scene_api import get_scene_llm, get_scene_stdb, scene_router

KEY = "k" * 32

TDM104_CUBE_ANSWER = {
    "name": "cube_plate", "task": "x", "notes": "cube and two plates", "table_color": None,
    "objects": [
        {"id": "cube", "label": "the cube", "primitive": "box", "size": [0.04, 0.04, 0.04], "radius": None, "height": None,
         "color": "#db1f1a", "rim_color": None, "mass": 0.05, "dynamic": True, "x": 0.31, "y": -0.14, "z": None,
         "yaw": 0.0, "spawn_region": "cube_spawn", "spawn_yaw": [-0.7, 0.7]},
        {"id": "plate", "label": "the empty plate", "primitive": "plate", "size": None, "radius": 0.075, "height": 0.012,
         "color": "#f0f0eb", "rim_color": "#336bcc", "mass": None, "dynamic": False, "x": 0.31, "y": 0.19, "z": None,
         "yaw": 0.0, "spawn_region": None, "spawn_yaw": None},
        {"id": "full_plate", "label": "the full plate", "primitive": "plate", "size": None, "radius": 0.075,
         "height": 0.012, "color": "#f0f0eb", "rim_color": "#bfbfc7", "mass": None, "dynamic": False, "x": 0.47,
         "y": -0.05, "z": None, "yaw": 0.0, "spawn_region": None, "spawn_yaw": None},
        {"id": "filler", "label": "the yellow block", "primitive": "cylinder", "size": None, "radius": 0.028,
         "height": 0.05, "color": "#f2c726", "rim_color": None, "mass": 0.08, "dynamic": True, "x": 0.47, "y": -0.05,
         "z": None, "yaw": 0.0, "spawn_region": None, "spawn_yaw": None}],
    "regions": [{"name": "cube_spawn", "x": [0.25, 0.37], "y": [-0.21, -0.07], "visible": False, "color": None}],
    "success": {"type": "object_in", "object": "cube", "target": "plate", "region": None},
}
TDM104_STACK_ANSWER = {
    "name": "stack_blocks", "task": "x", "notes": "two blocks", "table_color": "#8a6a4a",
    "objects": [
        {"id": "red_block", "label": "the red block", "primitive": "box", "size": [0.04, 0.04, 0.04], "radius": None,
         "height": None, "color": "#d42020", "rim_color": None, "mass": 0.05, "dynamic": True, "x": 0.30, "y": -0.12,
         "z": None, "yaw": 0.0, "spawn_region": "red_spawn", "spawn_yaw": [-0.5, 0.5]},
        {"id": "blue_block", "label": "the blue block", "primitive": "box", "size": [0.05, 0.05, 0.05], "radius": None,
         "height": None, "color": "#2040d0", "rim_color": None, "mass": 0.08, "dynamic": True, "x": 0.32, "y": 0.14,
         "z": None, "yaw": 0.3, "spawn_region": None, "spawn_yaw": None}],
    "regions": [{"name": "red_spawn", "x": [0.24, 0.36], "y": [-0.20, -0.06], "visible": False, "color": None}],
    "success": {"type": "object_on", "object": "red_block", "target": "blue_block", "region": None},
}


class Tdm104FakeLLM:
    """Answers by task keyword; ``bad_first`` makes the first answer invalid (cube out of reach)."""

    def __init__(self, bad_first: bool = False):
        self.calls: list[dict] = []
        self.bad_first = bad_first

    def complete_structured(self, **kw: Any):
        self.calls.append(kw)
        text = json.dumps(kw["messages"][0]["content"]).lower()
        ans = copy.deepcopy(TDM104_STACK_ANSWER if "stack" in text else TDM104_CUBE_ANSWER)
        if self.bad_first and len(self.calls) == 1:
            ans["objects"][0]["x"] = 0.9
        assert kw["schema"] is ss.LLM_SCHEMA and kw["step"] == "scene"
        return SimpleNamespace(parsed=ans, usage={"input_tokens": 10, "output_tokens": 20},
                               meta={"served_model": "fake", "backend": "fake"}, content=[])


class Tdm104FakeStdb:
    def __init__(self, robots: list[dict] | None = None, tickets: list[dict] | None = None):
        self.events: list[dict] = []
        self.robots = robots if robots is not None else [
            {"id": 1, "name": "yam-1", "status": "online", "embodiment": "i2rt_yam", "camera_config_json": "{}"},
            {"id": 2, "name": "sim-yam-mujoco", "status": "online", "embodiment": "sim_yam",
             "camera_config_json": json.dumps({"sim": True})}]
        self.tickets = tickets if tickets is not None else [
            {"id": 7, "robot_id": 1, "task_description": "Pick up the cube and place it in the empty plate"},
            {"id": 8, "robot_id": 2, "task_description": "stack the red block on the blue block"}]

    def call_reducer(self, name: str, *args: Any) -> None:
        assert name == "append_event"
        actor, type_, payload = args
        json.loads(payload)
        assert len(payload) < 64_000
        self.events.append({"id": len(self.events) + 1, "ts": 1_000 + len(self.events), "actor": actor, "type": type_,
                            "payload_json": payload})

    def sql(self, q: str) -> list[dict]:
        if q.startswith("SELECT * FROM event_log"):
            return [e for e in self.events if f"'{e['type']}'" in q]
        if q.startswith("SELECT * FROM robot"):
            return list(self.robots)
        if q.startswith("SELECT * FROM ticket WHERE id = "):
            tid = int(q.rsplit(" ", 1)[1])
            return [t for t in self.tickets if t["id"] == tid]
        raise AssertionError(q)

    def robot_loaded(self, deploy: dict, ok: bool = True) -> None:
        self.call_reducer("append_event", "robot-client:sim", sc.EV_LOADED, json.dumps(
            {"robot_id": deploy["robot_id"], "deploy_id": deploy["deploy_id"], "ok": ok, "scene_id": deploy["scene_id"]}))


def tdm104_client(stdb: Tdm104FakeStdb, llm: Any, key: str | None = KEY) -> TestClient:
    app = FastAPI()
    app.include_router(scene_router)
    app.dependency_overrides[get_scene_stdb] = lambda: stdb
    app.dependency_overrides[get_scene_llm] = lambda: llm
    app.dependency_overrides[get_operator_key] = lambda: key
    return TestClient(app)


H = {"x-rdf-operator-key": KEY}


def test_generate_two_tasks_gives_two_different_valid_scenes():
    stdb, llm = Tdm104FakeStdb(), Tdm104FakeLLM()
    store = sc.SceneStore(stdb.sql, stdb.call_reducer)
    a = store.generate(7, "Pick up the cube and place it in the empty plate", llm=llm)
    b = store.generate(8, "stack the red block on the blue block", llm=llm)
    assert ss.describe_success(a["spec"]) == "inside(cube, plate)"
    assert ss.describe_success(b["spec"]) == "on_top(red_block, blue_block)"
    assert a["spec"]["task"] == "Pick up the cube and place it in the empty plate"  # the ticket's words
    assert a["scene_id"] == "t7r1" and b["scene_id"] == "t8r1" and a["spec"]["meta"]["revision"] == 1
    assert b["spec"]["table"]["color"][:3] != a["spec"]["table"]["color"][:3]
    assert [e["type"] for e in stdb.events] == ["scene.created", "scene.created"]
    assert all(e["actor"] == sc.ACTOR for e in stdb.events)
    # stored spec re-validates unchanged (what the robot will load)
    assert ss.validate(a["spec"]) == a["spec"]


def test_invalid_answer_gets_one_corrective_retry():
    stdb, llm = Tdm104FakeStdb(), Tdm104FakeLLM(bad_first=True)
    p = sc.SceneStore(stdb.sql, stdb.call_reducer).generate(7, "Pick up the cube", llm=llm)
    assert len(llm.calls) == 2 and p["llm"]["attempts"] == 2
    feedback = llm.calls[1]["messages"][-1]["content"]
    assert "rejected by the validator" in feedback and "from the robot base" in feedback


def test_generation_failure_is_logged_not_stored():
    stdb = Tdm104FakeStdb()

    class Down:
        def complete_structured(self, **kw):
            raise RuntimeError("overloaded")

    with pytest.raises(sc.SceneGenerationError):
        sc.SceneStore(stdb.sql, stdb.call_reducer).generate(7, "Pick up the cube", llm=Down())
    assert [e["type"] for e in stdb.events] == [sc.EV_FAILED]
    assert sc.ticket_scene(stdb.events, 7)["last_error"]["error"].startswith("scene generation failed")


def test_api_generate_deploy_and_robot_view():
    stdb, llm = Tdm104FakeStdb(), Tdm104FakeLLM()
    c = tdm104_client(stdb, llm)
    assert c.get("/tickets/7/scene").json()["spec"] is None
    assert c.post("/tickets/7/scene/generate", json={}).status_code == 401
    r = c.post("/tickets/7/scene/generate", json={}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["success"] == "inside(cube, plate)"
    r2 = c.post("/tickets/7/scene/generate", json={"hint": "make it harder"}, headers=H).json()
    assert r2["revision"] == 2 and "make it harder" in llm.calls[-1]["messages"][0]["content"]
    g = c.get("/tickets/7/scene").json()
    assert g["revision"] == 2 and [x["revision"] for x in g["revisions"]] == [1, 2] and g["deployed"] is None
    assert c.get("/tickets/7/scene?revision=1").json()["revision"] == 1
    assert c.get("/tickets/7/scene?revision=9").status_code == 404

    # ticket 7 is on the REAL robot 1 -> deploy goes to the one online sim robot (2)
    d = c.post("/tickets/7/scene/deploy", json={"revision": 1}, headers=H)
    assert d.status_code == 200, d.text
    d = d.json()
    assert d["robot_id"] == 2 and d["revision"] == 1 and d["status"] == "pending"
    deployed = json.loads(stdb.events[-1]["payload_json"])
    assert stdb.events[-1]["type"] == "scene.deployed" and deployed["spec"]["meta"]["scene_id"] == "t7r1"
    rs = c.get("/robots/2/scene").json()
    assert rs["status"] == "pending" and rs["spec"]["name"] == "cube_plate"
    stdb.robot_loaded(deployed)
    assert c.get("/robots/2/scene").json()["status"] == "loaded"
    assert c.get("/tickets/7/scene").json()["deployed"]["status"] == "loaded"
    assert c.get("/robots/1/scene").json()["status"] == "none"


def test_deploy_refuses_real_robots_and_ambiguity():
    stdb, llm = Tdm104FakeStdb(), Tdm104FakeLLM()
    c = tdm104_client(stdb, llm)
    assert c.post("/tickets/7/scene/deploy", json={}, headers=H).status_code == 404  # no scene yet
    c.post("/tickets/7/scene/generate", json={}, headers=H)
    r = c.post("/tickets/7/scene/deploy", json={"robot_id": 1}, headers=H)
    assert r.status_code == 409 and "real robot" in r.json()["detail"]
    stdb.robots.append({"id": 3, "name": "sim-2", "status": "online", "embodiment": "sim_yam",
                        "camera_config_json": json.dumps({"sim": True})})
    r = c.post("/tickets/7/scene/deploy", json={}, headers=H)
    assert r.status_code == 409 and "several sim robots" in r.json()["detail"]
    assert c.post("/tickets/7/scene/deploy", json={"robot_id": 3}, headers=H).json()["robot_id"] == 3
    assert c.post("/tickets/99/scene/deploy", json={}, headers=H).status_code == 404
    assert tdm104_client(stdb, llm, key=None).post("/tickets/7/scene/deploy", json={}, headers=H).status_code == 503


def test_put_manual_spec_and_validate_route():
    stdb = Tdm104FakeStdb()
    c = tdm104_client(stdb, None)
    bad = copy.deepcopy(ss.CUBE_PLATE)
    bad["objects"][0]["pose"]["pos"] = [0.9, 0.0, 0.02]
    r = c.put("/tickets/8/scene", json={"spec": bad}, headers=H)
    assert r.status_code == 422 and any("from the robot base" in e for e in r.json()["detail"]["errors"])
    r = c.put("/tickets/8/scene", json={"spec": ss.CUBE_PLATE}, headers=H)
    assert r.status_code == 200 and r.json()["source"] == "manual"
    v = c.post("/scenes/validate", json={"spec": bad}).json()
    assert v["ok"] is False and v["errors"]
    assert c.post("/scenes/validate", json={"spec": ss.CUBE_PLATE}).json()["ok"] is True
    assert c.get("/scenes/default").json()["success"] == "inside(cube, plate)"
    # ticket 8's own robot (2) is a sim robot: deploy goes there
    assert c.post("/tickets/8/scene/deploy", json={}, headers=H).json()["robot_id"] == 2


def test_handler_generates_once_per_new_ticket(monkeypatch):
    from rdf_orchestrator.handlers import scene as hs

    stdb, llm = Tdm104FakeStdb(), Tdm104FakeLLM()
    monkeypatch.setattr(hs, "make_stdb", lambda: stdb)
    monkeypatch.setattr(hs, "make_llm", lambda: llm)
    hs._claimed.clear()
    row = SimpleNamespace(type="ticket.created", payload_json=json.dumps(
        {"ticket_id": 8, "robot_id": 2, "task_description": "stack the red block on the blue block"}))

    class Bus:
        def rows(self, table):
            return stdb.events

    ev = SimpleNamespace(row=row, bus=Bus())
    hs.scene_on_ticket_created(ev)
    hs._claimed.clear()  # a restart: the mirror still shows the scene
    hs.scene_on_ticket_created(ev)
    assert [e["type"] for e in stdb.events] == ["scene.created"] and len(llm.calls) == 1
    monkeypatch.setenv("RDF_SCENE_AUTOGEN", "0")
    hs._claimed.clear()
    stdb.events.clear()
    hs.scene_on_ticket_created(ev)
    assert stdb.events == []
