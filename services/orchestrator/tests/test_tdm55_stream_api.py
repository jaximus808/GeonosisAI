"""TDM-55: POST /stream/token only mints for the verified current claimant."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_common.stream_token import verify
from rdf_orchestrator.stream_api import (get_identity_verifier, get_stream_secret, get_stream_stdb,
                                         stream_router)

SECRET = "tdm55-test-secret-0123456789abcdef"
ME = "c2" + "ab" * 31
OTHER = "c2" + "cd" * 31
GOOD_TOKEN = "browser-token-for-me"


class FakeStdb:
    def __init__(self) -> None:
        now_us = int(time.time() * 1e6)
        self.tables = {
            "task": {7: {"id": 7, "status": "claimed", "assignee_id": 3, "robot_id": 1,
                         "lease_expires_at": now_us + 45_000_000},
                     8: {"id": 8, "status": "open", "assignee_id": 0, "robot_id": 1, "lease_expires_at": None},
                     9: {"id": 9, "status": "claimed", "assignee_id": 3, "robot_id": 1,
                         "lease_expires_at": now_us - 1_000_000},
                     10: {"id": 10, "status": "claimed", "assignee_id": 4, "robot_id": 1,
                          "lease_expires_at": now_us + 45_000_000}},
            "contributor": {3: {"id": 3, "identity": "0x" + ME}, 4: {"id": 4, "identity": "0x" + OTHER}},
            "robot": {1: {"id": 1, "name": "yam-office-1", "camera_config_json": json.dumps(
                {"cameras": "config", "stream": {"url": "wss://x.trycloudflare.com/ws/cameras",
                                                 "http": "https://x.trycloudflare.com", "cams": ["top", "wrist"],
                                                 "transport": "jpeg-ws/1"}})}},
        }

    async def sql(self, q: str) -> list[dict]:
        table = q.split("FROM ")[1].split()[0]
        rid = int(q.rsplit("=", 1)[1])
        row = self.tables[table].get(rid)
        return [row] if row else []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("RDF_STREAM_URL", raising=False)
    fake = FakeStdb()

    async def verifier(identity: str, token: str) -> bool:
        return identity == ME and token == GOOD_TOKEN

    app = FastAPI()
    app.include_router(stream_router)
    app.dependency_overrides[get_stream_stdb] = lambda: fake
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_stream_secret] = lambda: SECRET
    return TestClient(app), fake


def req(task_id: int, identity: str = ME, token: str = GOOD_TOKEN) -> dict:
    return {"task_id": task_id, "identity": identity, "spacetime_token": token}


def test_claimant_gets_scoped_short_token(client):
    c, _ = client
    r = c.post("/stream/token", json=req(7))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["stream_url"] == "wss://x.trycloudflare.com/ws/cameras" and b["cams"] == ["top", "wrist"]
    claims = verify(SECRET, b["token"])
    assert (claims.robot_id, claims.robot_name, claims.task_id, claims.sub) == (1, "yam-office-1", 7, "contributor:3")
    assert claims.exp - time.time() <= 60.5


def test_token_never_outlives_the_lease(client):
    c, fake = client
    fake.tables["task"][7]["lease_expires_at"] = int((time.time() + 5) * 1e6)
    claims = verify(SECRET, c.post("/stream/token", json=req(7)).json()["token"])
    assert claims.exp <= time.time() + 5 + 10 + 0.5  # lease + 10 s grace


def test_wrong_browser_token_is_401(client):
    c, _ = client
    assert c.post("/stream/token", json=req(7, token="someone-elses-token")).status_code == 401


def test_other_contributor_cannot_watch(client):
    c, _ = client
    r = c.post("/stream/token", json=req(10))
    assert r.status_code == 403 and "claimed" in r.json()["detail"]


def test_unclaimed_expired_and_missing(client):
    c, _ = client
    assert c.post("/stream/token", json=req(8)).status_code == 403
    assert c.post("/stream/token", json=req(9)).status_code == 403
    assert c.post("/stream/token", json=req(99)).status_code == 404


def test_no_stream_url_is_503_and_env_override(client, monkeypatch):
    c, fake = client
    fake.tables["robot"][1]["camera_config_json"] = "{}"
    assert c.post("/stream/token", json=req(7)).status_code == 503
    monkeypatch.setenv("RDF_STREAM_URL", "ws://jetson.local:8091/ws/cameras")
    assert c.post("/stream/token", json=req(7)).json()["stream_url"] == "ws://jetson.local:8091/ws/cameras"


def test_bad_identity_format(client):
    c, _ = client
    assert c.post("/stream/token", json=req(7, identity="nope")).status_code == 422
