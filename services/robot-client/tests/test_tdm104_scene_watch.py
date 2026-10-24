"""TDM-104: the sim robot applies the newest scene.deployed for itself, once, never under a running job."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

from rdf_robot_client import scene_watch as sw


class Tdm104FakeSim:
    def __init__(self, fail: bool = False):
        self.loaded: list[dict] = []
        self.fail = fail

    def load_scene(self, spec: dict) -> dict:
        if self.fail:
            raise ValueError("bad spec")
        self.loaded.append(spec)
        return {"scene": spec.get("name"), "objects": [o["id"] for o in spec.get("objects", [])]}


class Tdm104FakeClient:
    def __init__(self, robot_id: int = 2):
        self.robot_id = robot_id
        self.events: list[tuple[str, dict]] = []
        self.job: Any = None
        self.arm = None
        self.services: dict = {}

    def current_job(self):
        return self.job

    def event(self, type_: str, **payload: Any) -> None:
        self.events.append((type_, payload))


def tdm104_row(eid: int, robot_id: int, deploy_id: str, name: str = "s") -> dict:
    return {"id": eid, "type": "scene.deployed", "payload_json": json.dumps(
        {"robot_id": robot_id, "ticket_id": 7, "revision": eid, "scene_id": f"t7r{eid}", "deploy_id": deploy_id,
         "spec": {"name": name, "objects": [{"id": "cube"}]}})}


def tdm104_watcher(sim: Tdm104FakeSim, client: Tdm104FakeClient) -> sw.SceneWatcher:
    w = sw.SceneWatcher.__new__(sw.SceneWatcher)
    w.client, w.poll_s, w.sim, w.sub = client, 0.02, sim, None
    w.applied, w.current, w._pending = None, None, None
    w._cv, w._stop = threading.Condition(), threading.Event()
    w._thread = threading.Thread(target=w._apply_loop, daemon=True)
    w._thread.start()
    return w


def _wait(cond, timeout: float = 2.0) -> bool:
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_snapshot_applies_only_the_newest_deploy_for_this_robot():
    sim, client = Tdm104FakeSim(), Tdm104FakeClient()
    w = tdm104_watcher(sim, client)
    w._on_applied({"event_log": [tdm104_row(1, 2, "a", "old"), tdm104_row(2, 9, "b", "other"), tdm104_row(3, 2, "c", "new")]})
    assert _wait(lambda: client.events)
    assert [s["name"] for s in sim.loaded] == ["new"]
    t, p = client.events[0]
    assert t == "scene.loaded" and p["ok"] and p["deploy_id"] == "c" and p["objects"] == ["cube"]
    # a resync replays the same rows: not applied twice
    w._on_applied({"event_log": [tdm104_row(3, 2, "c", "new")]})
    time.sleep(0.1)
    assert len(sim.loaded) == 1
    w.stop()


def test_waits_for_the_running_job():
    sim, client = Tdm104FakeSim(), Tdm104FakeClient()
    client.job = SimpleNamespace(command=SimpleNamespace(label=lambda: "run_eval #5"))
    w = tdm104_watcher(sim, client)
    w._on_insert("event_log", tdm104_row(4, 2, "d"), {})
    time.sleep(0.15)
    assert sim.loaded == []
    client.job = None
    assert _wait(lambda: sim.loaded)
    w.stop()


def test_bad_spec_reports_failure_and_keeps_going():
    sim, client = Tdm104FakeSim(fail=True), Tdm104FakeClient()
    w = tdm104_watcher(sim, client)
    w._on_insert("event_log", tdm104_row(5, 2, "e"), {})
    assert _wait(lambda: client.events)
    t, p = client.events[0]
    assert t == "scene.loaded" and p["ok"] is False and "bad spec" in p["error"]
    w.stop()


def test_real_arm_does_not_watch():
    client = Tdm104FakeClient()
    client.arm = object()
    assert sw.start_scene_watch(client) is None
