"""The ticket's new scene is deployed to its eval robot when that robot is a sim robot."""
import json
from types import SimpleNamespace

from rdf_orchestrator.handlers import scene as hs


class _Bus:
    def __init__(self, robot):
        self._rows = {"ticket": [{"id": 10, "robot_id": 3}], "robot": [robot]}

    def rows(self, table):
        return self._rows.get(table, [])


class _Store:
    def __init__(self):
        self.deployed = []

    def deploy(self, tid, rid, **kw):
        self.deployed.append((tid, rid, kw.get("by")))


def test_new_scene_is_deployed_to_a_sim_eval_robot():
    store = _Store()
    hs._autodeploy(store, SimpleNamespace(bus=_Bus({"id": 3, "camera_config_json": json.dumps({"sim": True})})), 10)
    assert store.deployed == [(10, 3, "orchestrator:autodeploy")]


def test_never_deployed_to_a_real_robot(monkeypatch):
    store = _Store()
    hs._autodeploy(store, SimpleNamespace(bus=_Bus({"id": 3, "camera_config_json": json.dumps({"sim": False})})), 10)
    assert store.deployed == []


def test_autodeploy_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("RDF_SCENE_AUTODEPLOY", "0")
    store = _Store()
    hs._autodeploy(store, SimpleNamespace(bus=_Bus({"id": 3, "camera_config_json": json.dumps({"sim": True})})), 10)
    assert store.deployed == []
