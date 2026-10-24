"""TDM-42: /checkpoints routes over a fake Volume + fake SpacetimeDB SQL; GPU spend in the budget
(dispatcher budget_remaining and the flywheel's TicketView agree)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import checkpoints_api
from rdf_orchestrator.checkpoints_api import Tdm42Source, checkpoints_router
from rdf_training.registry import BASE_URI, Entry

T0 = 1792828000.0
MID = "modal-vol://rdf-checkpoints/runs/ticket-42/20261024-080000-bbbbbb/199"
NEW = "modal-vol://rdf-checkpoints/runs/ticket-42/20261024-090000-cccccc/199"
OLD = "modal-vol://rdf-checkpoints/runs/ticket-42/20261024-070000-aaaaaa/199"


class Tdm42Vol:
    def __init__(self) -> None:
        self.files: dict[str, int] = {}
        self.json: dict[str, dict] = {}
        for i, run in enumerate(("20261024-070000-aaaaaa", "20261024-080000-bbbbbb", "20261024-090000-cccccc")):
            b = f"runs/ticket-42/{run}"
            self.files.update({f"{b}/199/params/p": 100, f"{b}/199/_CHECKPOINT_METADATA": 1, f"{b}/rdf_run.json": 1})
            self.json[f"{b}/rdf_run.json"] = {"created_at": T0 + i * 3600, "settings": {"steps": 200}}
            self.json[f"metrics/ticket-42/{run}.json"] = {"wall_s": 480.0, "gpu": "NVIDIA H100 80GB HBM3"}
        self.removed: list[str] = []

    def listdir(self, path, recursive=False):
        return [Entry(f, False, s, T0) for f, s in self.files.items() if f.startswith(path.strip("/") + "/")]

    def read_json(self, path):
        return self.json.get(path)

    def remove(self, path):  # the API must never call this
        self.removed.append(path)


class Tdm42Sql:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.t = {
            "training_run": [{"id": 3, "ticket_id": 42, "status": "done", "checkpoint_uri": MID, "gpu_cost_cents": 73,
                              "episode_ids": [1], "metrics_json": json.dumps({"cost": {"source": "estimate"}}),
                              "hyperparams_json": "{}", "base_checkpoint_uri": BASE_URI}],
            "eval_run": [{"id": 9, "checkpoint_uri": MID, "kind": "candidate", "status": "done", "trials": 10,
                          "successes": 8, "completed": 10, "ci_low": 0.49, "ci_high": 0.94}],
            "ticket": [{"id": 42, "deployed_checkpoint_uri": MID}],
        }

    async def sql(self, q: str):
        if self.fail:
            raise ConnectionError("maincloud down")
        return self.t[q.split("FROM ")[1].split()[0]]


def client(sql=None, vol=None):
    vol = vol or Tdm42Vol()
    app = FastAPI()
    app.include_router(checkpoints_router)
    src = Tdm42Source(volume_factory=lambda: vol, policy_state=lambda: {"active": NEW}, cache_s=0)
    app.dependency_overrides[checkpoints_api.get_checkpoints_stdb] = lambda: sql or Tdm42Sql()
    app.dependency_overrides[checkpoints_api.get_checkpoints_source] = lambda: src
    return TestClient(app), vol


def test_list_for_ticket_has_latest_deployed_base_and_spend():
    c, _ = client()
    r = c.get("/checkpoints", params={"ticket_id": "42"}).json()
    assert r["latest"] == NEW and r["deployed"] == MID and r["base"] == BASE_URI and r["state_ok"]
    assert r["gpu_spend"] == {"gpu_cost_cents": 73, "by_source_cents": {"estimate": 73}}
    mid = next(x for x in r["checkpoints"] if x["uri"] == MID)
    assert mid["deployed"] and mid["training_run"]["id"] == 3 and mid["evals"][0]["successes"] == 8
    assert next(x for x in r["checkpoints"] if x["uri"] == NEW)["policy_server"] == "active"
    assert any(x["kind"] == "base" for x in r["checkpoints"])


def test_resolve_and_404():
    c, _ = client()
    assert c.get("/checkpoints/resolve", params={"ticket_id": 42, "which": "deployed"}).json()["uri"] == MID
    assert c.get("/checkpoints/resolve", params={"ticket_id": 42}).json()["uri"] == NEW
    assert c.get("/checkpoints/resolve", params={"ticket_id": 42, "which": "base"}).json()["uri"] == BASE_URI
    assert c.get("/checkpoints/resolve", params={"ticket_id": 5, "which": "deployed"}).status_code == 404
    assert c.get("/checkpoints/resolve", params={"ticket_id": 42, "which": "best"}).status_code == 422


def test_cleanup_is_dry_run_and_fails_closed_when_stdb_is_down():
    c, vol = client()
    p = c.get("/checkpoints/cleanup", params={"keep_latest": 1}).json()
    assert p["dry_run"] and [g["run_id"] for g in p["delete"]] == ["20261024-070000-aaaaaa"] and not vol.removed
    c2, vol2 = client(sql=Tdm42Sql(fail=True))
    p2 = c2.get("/checkpoints/cleanup", params={"keep_latest": 1}).json()
    assert p2["refused"] and p2["delete"] == [] and not vol2.removed
    r = c2.get("/checkpoints", params={"ticket_id": "42"}).json()
    assert not r["state_ok"] and r["latest"] == NEW  # listing still works without the join


def test_dispatcher_budget_counts_gpu_like_the_flywheel():
    from rdf_orchestrator.dispatcher import Dispatcher
    from rdf_orchestrator.flywheel import TicketView

    class M:
        t = {"ticket": {42: {"id": 42, "budget_cents": 5000, "spent_cents": 1000}},
             "training_run": {3: {"id": 3, "ticket_id": 42, "gpu_cost_cents": 730}, 4: {"id": 4, "ticket_id": 7,
                                                                                        "gpu_cost_cents": 99}},
             "task": {}, "episode": {}, "payment": {}}

        def rows(self, table):
            return list(self.t.get(table, {}).values())

        def get(self, table, key):
            return self.t.get(table, {}).get(key)

    d = Dispatcher.__new__(Dispatcher)
    d.m = M()
    assert d.budget_remaining(42) == 5000 - 1000 - 730
    v = TicketView.__new__(TicketView)
    v.ticket, v.training_runs = M.t["ticket"][42], [M.t["training_run"][3]]
    assert v.remaining_cents == d.budget_remaining(42)
