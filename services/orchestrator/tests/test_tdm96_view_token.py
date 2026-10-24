"""TDM-96: POST /stream/token scope "view" (read-only robot state + cameras) is gated by operator login
(X-RDF-Operator-Key, added by the web server after requireOperator()) or the live claimant.
The control scope keeps the TDM-55 rules (tests/test_tdm55_stream_api.py)."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_tdm55_stream_api import GOOD_TOKEN, ME, SECRET, FakeStdb, req

from rdf_common.stream_token import verify
from rdf_orchestrator.stream_api import (get_identity_verifier, get_stream_operator_key, get_stream_secret,
                                         get_stream_stdb, stream_router)

OPKEY = "tdm96-operator-key-0123456789"


def _app(fake, opkey):
    async def verifier(identity: str, token: str) -> bool:
        return identity == ME and token == GOOD_TOKEN

    app = FastAPI()
    app.include_router(stream_router)
    app.dependency_overrides[get_stream_stdb] = lambda: fake
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_stream_secret] = lambda: SECRET
    app.dependency_overrides[get_stream_operator_key] = lambda: opkey
    return TestClient(app)


@pytest.fixture
def client(monkeypatch):
    for k in ("RDF_STREAM_URL", "RDF_STATE_URL"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeStdb()
    return _app(fake, OPKEY), fake


def test_operator_gets_view_token_not_bound_to_a_task(client):
    c, _ = client
    r = c.post("/stream/token", json={"scope": "view", "robot_id": 1}, headers={"X-RDF-Operator-Key": OPKEY})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["scope"] == "view" and b["task_id"] == 0
    assert b["state_url"] == "wss://x.trycloudflare.com/ws/state"  # derived from the camera URL
    cl = verify(SECRET, b["token"])
    assert (cl.scope, cl.robot_id, cl.task_id, cl.sub, cl.can_control) == ("view", 1, 0, "operator:view", False)
    assert cl.exp - time.time() <= 60.5


def test_view_without_operator_login_is_refused(client):
    c, _ = client
    assert c.post("/stream/token", json={"scope": "view", "robot_id": 1}).status_code == 401
    assert c.post("/stream/token", json={"scope": "view", "robot_id": 1},
                  headers={"X-RDF-Operator-Key": "wrong"}).status_code == 401
    # robot_id alone never gets a CONTROL token, operator key or not
    assert c.post("/stream/token", json={"robot_id": 1}, headers={"X-RDF-Operator-Key": OPKEY}).status_code == 422
    assert c.post("/stream/token", json={"scope": "view"}, headers={"X-RDF-Operator-Key": OPKEY}).status_code == 422
    assert c.post("/stream/token", json={"scope": "admin", "robot_id": 1}).status_code == 422


def test_operator_view_off_without_key_configured():
    c = _app(FakeStdb(), None)
    assert c.post("/stream/token", json={"scope": "view", "robot_id": 1},
                  headers={"X-RDF-Operator-Key": "anything"}).status_code == 503


def test_operator_view_unknown_robot_and_no_stream(client):
    c, fake = client
    h = {"X-RDF-Operator-Key": OPKEY}
    assert c.post("/stream/token", json={"scope": "view", "robot_id": 5}, headers=h).status_code == 404
    fake.tables["robot"][1]["camera_config_json"] = "{}"
    assert c.post("/stream/token", json={"scope": "view", "robot_id": 1}, headers=h).status_code == 503


def test_claimant_view_token_and_others_refused(client):
    c, _ = client
    r = c.post("/stream/token", json={**req(7), "scope": "view"})
    assert r.status_code == 200, r.text
    cl = verify(SECRET, r.json()["token"])
    assert (cl.scope, cl.task_id, cl.sub) == ("view", 7, "contributor:3")
    assert c.post("/stream/token", json={**req(10), "scope": "view"}).status_code == 403  # not their claim
    assert c.post("/stream/token", json={**req(7, token="nope-nope-nope"), "scope": "view"}).status_code == 401


def test_control_default_unchanged_and_published_state_url(client):
    c, fake = client
    import json
    cc = json.loads(fake.tables["robot"][1]["camera_config_json"])
    cc["stream"]["state_url"] = "wss://y.trycloudflare.com/ws/state"
    fake.tables["robot"][1]["camera_config_json"] = json.dumps(cc)
    b = c.post("/stream/token", json=req(7)).json()
    assert b["scope"] == "control" and verify(SECRET, b["token"]).can_control
    assert b["state_url"] == "wss://y.trycloudflare.com/ws/state"
