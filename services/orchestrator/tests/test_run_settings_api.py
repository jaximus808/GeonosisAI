"""TDM-20 run-settings API: validation and reducer wiring, with an in-memory fake SpacetimeDB.

The live check against a real (local) SpacetimeDB is tests/test_run_settings_api_local.py.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator.run_settings_api import get_run_settings_stdb, run_settings_router


class Tdm20FakeError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Tdm20FakeStdb:
    """Holds one ticket + run_settings row; applies patches the way the reducer would (roughly)."""

    def __init__(self, *, status: str = "baseline", iteration: int = 0, approved: int = 0, reject: str | None = None):
        self.calls: list[tuple] = []
        self.reject = reject
        self.ticket = {"id": 7, "status": status, "iteration": iteration, "budget_cents": 5000,
                       "approved_cents": approved, "spent_cents": 0}
        self.settings = {"ticket_id": 7, "eval_trials": 10, "success_threshold": 0.8, "require_ci_low": False,
                         "budget_cents": 5000, "target_episodes": 5, "max_iterations": 3,
                         "hyperparams_json": json.dumps({"lr": 2.5e-5, "steps": 2000, "batch_size": 32,
                                                          "lora_rank": 16, "prompt": ""}),
                         "updated_at": 1_700_000_000_000_000, "data_type_override": ""}

    async def sql(self, query: str) -> list[dict]:
        if query.endswith("= 7"):
            return [dict(self.ticket)] if "FROM ticket " in query else [dict(self.settings)]
        return []

    async def call_reducer(self, name: str, /, *args):
        self.calls.append((name, *args))
        if name == "update_run_settings":
            if self.reject:
                raise Tdm20FakeError(530, self.reject)
            patch = json.loads(args[1])
            hp = json.loads(self.settings["hyperparams_json"])
            hp.update(patch.pop("hyperparams", {}))
            self.settings["hyperparams_json"] = json.dumps(hp)
            if "data_type_override" in patch:
                patch["data_type_override"] = patch["data_type_override"] or ""
            self.settings.update(patch)


@pytest.fixture
def fake():
    return Tdm20FakeStdb()


def _client(fake: Tdm20FakeStdb) -> TestClient:
    app = FastAPI()
    app.include_router(run_settings_router)
    app.dependency_overrides[get_run_settings_stdb] = lambda: fake
    return TestClient(app)


def test_get_returns_effective_settings(fake):
    r = _client(fake).get("/tickets/7/settings")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["eval_trials"] == 10 and d["data_type_override"] is None
    assert d["hyperparams"]["steps"] == 2000 and d["ticket"]["status"] == "baseline"


def test_get_unknown_ticket_404(fake):
    assert _client(fake).get("/tickets/99/settings").status_code == 404


@pytest.mark.parametrize("body,field", [
    ({"eval_trials": 0}, "eval_trials"),
    ({"eval_trials": 51}, "eval_trials"),
    ({"eval_trials": None}, "eval_trials"),
    ({"eval_trials": "10"}, "eval_trials"),
    ({"success_threshold": 1.5}, "success_threshold"),
    ({"require_ci_low": "yes"}, "require_ci_low"),
    ({"budget_cents": -1}, "budget_cents"),
    ({"target_episodes": 0}, "target_episodes"),
    ({"max_iterations": 21}, "max_iterations"),
    ({"data_type_override": "purchased"}, "data_type_override"),
    ({"bogus": 1}, "bogus"),
])
def test_field_level_rejections(fake, body, field):
    r = _client(fake).patch("/tickets/7/settings", json=body)
    assert r.status_code == 422, r.text
    locs = [e["loc"] for e in r.json()["detail"]]
    assert any(loc[:2] == ["body", field] for loc in locs), locs
    assert fake.calls == []  # never reached the reducer


@pytest.mark.parametrize("body,loc", [
    ({"hyperparams": {"lr": 0}}, ["body", "hyperparams", "lr"]),
    ({"hyperparams": {"batch_size": 5000}}, ["body", "hyperparams", "batch_size"]),
    ({"hyperparams": {"dropout": 0.1}}, ["body", "hyperparams", "dropout"]),
])
def test_hyperparam_rejections(fake, body, loc):
    r = _client(fake).patch("/tickets/7/settings", json=body)
    assert r.status_code == 422
    assert loc in [e["loc"] for e in r.json()["detail"]]


def test_empty_patch_rejected(fake):
    assert _client(fake).patch("/tickets/7/settings", json={}).status_code == 422
    assert _client(fake).patch("/tickets/7/settings", json={"hyperparams": {}}).status_code == 422


def test_valid_patch_calls_reducer_and_logs_human(fake):
    body = {"eval_trials": 12, "success_threshold": 1, "data_type_override": "recovery_takeover",
            "hyperparams": {"lr": 1e-4, "prompt": "put the cup in the bin"}}
    r = _client(fake).patch("/tickets/7/settings", json=body, headers={"X-Human-Id": "jaxon"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["eval_trials"] == 12 and d["success_threshold"] == 1.0
    assert d["data_type_override"] == "recovery_takeover"
    assert d["hyperparams"]["lr"] == 1e-4 and d["hyperparams"]["steps"] == 2000  # merged, not replaced
    assert d["audit_logged"] is True
    (name, tid, patch_json), audit = fake.calls
    assert (name, tid) == ("update_run_settings", 7)
    assert json.loads(patch_json) == body  # only the sent fields
    assert audit[0:3] == ("append_event", "human:jaxon", "human.run_settings_changed")
    assert json.loads(audit[3])["patch"] == body


def test_null_clears_override(fake):
    fake.settings["data_type_override"] = "clean_teleop"
    r = _client(fake).patch("/tickets/7/settings", json={"data_type_override": None})
    assert r.status_code == 200 and r.json()["data_type_override"] is None
    assert json.loads(fake.calls[0][2]) == {"data_type_override": None}
    assert fake.calls[1][1] == "human:anonymous"


def test_bad_human_id_rejected(fake):
    r = _client(fake).patch("/tickets/7/settings", json={"eval_trials": 5}, headers={"X-Human-Id": "a b"})
    assert r.status_code == 422


def test_frozen_ticket_409():
    fake = Tdm20FakeStdb(status="shipped")
    r = _client(fake).patch("/tickets/7/settings", json={"eval_trials": 5})
    assert r.status_code == 409 and fake.calls == []


def test_ticket_dependent_rules_mirrored():
    fake = Tdm20FakeStdb(iteration=2, approved=3000)
    r = _client(fake).patch("/tickets/7/settings", json={"budget_cents": 2000, "max_iterations": 2})
    assert r.status_code == 422
    assert sorted(e["loc"][1] for e in r.json()["detail"]) == ["budget_cents", "max_iterations"]
    assert fake.calls == []


def test_reducer_rejection_passed_through():
    fake = Tdm20FakeStdb(reject="something the API did not foresee")
    r = _client(fake).patch("/tickets/7/settings", json={"eval_trials": 5})
    assert r.status_code == 422
    assert r.json()["detail"][0]["type"] == "reducer_rejected"
    assert "did not foresee" in r.json()["detail"][0]["msg"]
