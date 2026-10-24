"""TDM-62 reset flow: routine storage, the /resets API (claimant-only scene photo) and escalation.

No network: an in-memory media store, a fake SpacetimeDB SQL client, and a local HTTP server that
plays the robot's TDM-55 /snapshot endpoint.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_common.stream_token import verify as verify_stream_token
from rdf_orchestrator import reset_routine as rr
from rdf_orchestrator import reset_api
from rdf_orchestrator import reset_watch as rw
from rdf_orchestrator.reset_api import get_storage, http_base_of, pick_cam, reset_router
from rdf_orchestrator.storage import ObjectInfo, SignedUrl, make_uri
from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb

SECRET = "tdm62-test-secret-0123456789abcdef"
ME = "c2" + "ab" * 31
OTHER = "c2" + "cd" * 31
GOOD = "browser-token-for-me"
JPEG = b"\xff\xd8\xff\xe0" + b"tdm62-fake-jpeg" * 10


class MemStore:
    scheme, bucket = "modal-vol", "rdf-media"

    def __init__(self) -> None:
        self.objs: dict[str, tuple[bytes, str]] = {}

    def put(self, key, data, content_type=None):
        self.objs[key] = (bytes(data), content_type or "")
        return make_uri(self.scheme, self.bucket, key)

    def list(self, prefix="", *, limit=1000):
        return [ObjectInfo(k, make_uri(self.scheme, self.bucket, k), len(v[0]), int(time.time()))
                for k, v in sorted(self.objs.items()) if k.startswith(prefix)][:limit]

    def signed_get(self, uri_or_key, *, ttl_s=0):
        key = uri_or_key.split("rdf-media/", 1)[-1]
        return SignedUrl(f"mem://{key}", "GET", make_uri(self.scheme, self.bucket, key), key, 0)


class MemHttp:
    """httpx.get stand-in for reset_routine.load: serves mem:// URLs from the MemStore."""

    def __init__(self, store: MemStore) -> None:
        self.store = store

    def get(self, url, timeout=None):
        body = self.store.objs[url[len("mem://"):]][0]

        class R:
            def raise_for_status(self):
                return None

            def json(self):
                return json.loads(body)

        return R()


# ------------------------------------------------------------------ routine

def test_default_steps_when_nothing_is_stored():
    r = rr.load(5, "put the cup in the bin", backend=MemStore())
    assert r.source == "default" and r.reference_uri is None
    assert any("start spot" in s for s in r.steps)
    assert r.steps[-1].startswith("Tap Done")


def test_save_and_load_routine_with_photo():
    st = MemStore()
    saved = rr.save(5, ["  Cup on the X  ", "", "Bin empty"], JPEG, backend=st)
    assert saved.steps == ["Cup on the X", "Bin empty"]
    assert rr.reference_key(5) in st.objs and rr.routine_key(5) in st.objs
    got = rr.load(5, "ignored", backend=st, http=MemHttp(st))
    assert got.source == "stored" and got.steps == ["Cup on the X", "Bin empty"]
    assert got.reference_uri == make_uri("modal-vol", "rdf-media", rr.reference_key(5))
    with pytest.raises(ValueError):
        rr.save(5, ["x"], b"not an image", backend=st)
    with pytest.raises(ValueError):
        rr.save(5, ["   "], None, backend=st)


ONB = {"onboarding": {"reset_routine": {"human_steps": ["Cup upright on the taped X", "Bin back in its outline"],
                                        "operator_prompt": "Put the cup back on the X.", "est_seconds": 20,
                                        "requires_human": True}}}


def test_onboarding_routine_beats_default_and_stored_beats_onboarding():
    st = MemStore()
    onb = rr.onboarding_routine(json.dumps(ONB))
    r = rr.load(5, "x", onboarding=onb, backend=st)
    assert r.source == "onboarding" and r.prompt == "Put the cup back on the X." and r.est_seconds == 20
    assert r.steps[1:3] == ["Cup upright on the taped X", "Bin back in its outline"]
    assert r.steps[0].startswith("Check the banner") and r.steps[-1].startswith("Tap Done")
    rr.save(5, ["Operator step"], None, backend=st)
    r2 = rr.load(5, "x", onboarding=onb, backend=st, http=MemHttp(st))
    assert r2.source == "stored" and r2.steps == ["Operator step"]
    assert rr.onboarding_routine("not json") == {} and rr.onboarding_routine("{}") == {}
    assert rr.load(5, "x", onboarding={"human_steps": []}, backend=MemStore()).source == "default"


def test_load_never_raises_when_storage_is_down():
    class Down(MemStore):
        def list(self, *a, **k):
            raise RuntimeError("media store down")

    assert rr.load(1, "x", backend=Down()).source == "default"


# ------------------------------------------------------------------ API

class FakeStdb:
    def __init__(self, robot_cfg: dict) -> None:
        now_us = int(time.time() * 1e6)
        self.tables = {
            "task": {7: {"id": 7, "type": "reset", "status": "claimed", "assignee_id": 3, "robot_id": 1,
                         "ticket_id": 4, "lease_expires_at": now_us + 300_000_000, "claimed_at": now_us - 5_000_000},
                     8: {"id": 8, "type": "reset", "status": "open", "assignee_id": 0, "robot_id": 1, "ticket_id": 4,
                         "lease_expires_at": None},
                     9: {"id": 9, "type": "clean_teleop", "status": "claimed", "assignee_id": 3, "robot_id": 1,
                         "ticket_id": 4, "lease_expires_at": now_us + 30_000_000},
                     10: {"id": 10, "type": "reset", "status": "claimed", "assignee_id": 4, "robot_id": 1,
                          "ticket_id": 4, "lease_expires_at": now_us + 300_000_000}},
            "contributor": {3: {"id": 3, "identity": "0x" + ME}, 4: {"id": 4, "identity": "0x" + OTHER}},
            "robot": {1: {"id": 1, "name": "yam-office-1", "camera_config_json": json.dumps(robot_cfg)}},
            "ticket": {4: {"id": 4, "task_description": "put the cup in the bin"}},
        }

    async def sql(self, q: str) -> list[dict]:
        table = q.split("FROM ")[1].split()[0]
        row = self.tables[table].get(int(q.rsplit("=", 1)[1]))
        return [row] if row else []


@pytest.fixture
def snapshot_server():
    seen: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlsplit(self.path)
            tok = (parse_qs(u.query).get("token") or [""])[0]
            try:
                claims = verify_stream_token(SECRET, tok)
            except Exception:  # noqa: BLE001
                self.send_response(403)
                self.end_headers()
                return
            seen.append({"path": u.path, "task_id": claims.task_id, "robot_id": claims.robot_id})
            if u.path != "/snapshot/top.jpg":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(JPEG)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


@pytest.fixture
def api(monkeypatch, snapshot_server):
    url, seen = snapshot_server
    monkeypatch.delenv("RDF_STREAM_URL", raising=False)
    cfg = {"stream": {"url": url.replace("http", "ws") + "/ws/cameras", "http": url, "cams": ["wrist", "top"]}}
    fake, store = FakeStdb(cfg), MemStore()

    async def verifier(identity: str, token: str) -> bool:
        return (identity, token) in ((ME, GOOD), (OTHER, GOOD))

    app = FastAPI()
    app.include_router(reset_router)
    app.dependency_overrides[get_stream_stdb] = lambda: fake
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    monkeypatch.setattr(reset_api, "get_stream_secret_dep", lambda: SECRET)
    app.dependency_overrides[get_storage] = lambda: store
    return TestClient(app), fake, store, seen


def body(**kw) -> dict:
    return {"identity": ME, "spacetime_token": GOOD, **kw}


def test_camera_photo_for_the_claimant(api):
    c, _, store, seen = api
    r = c.post("/resets/7/photo", json=body(source="camera"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source"] == "camera" and out["cam"] == "top"  # wrist 404s, top answers
    key = out["storage_uri"].split("rdf-media/", 1)[1]
    assert key.startswith("tickets/4/resets/7/camera-") and store.objs[key][0] == JPEG
    assert all(s["task_id"] == 7 and s["robot_id"] == 1 for s in seen)  # token scoped to this task + robot
    info = c.get("/resets/7").json()
    assert [e["source"] for e in info["evidence"]] == ["camera"]
    assert info["routine"]["source"] == "default" and info["routine"]["steps"]


def test_reset_info_uses_the_onboarding_routine(api):
    c, fake, _, _ = api
    cfg = json.loads(fake.tables["robot"][1]["camera_config_json"])
    fake.tables["robot"][1]["camera_config_json"] = json.dumps({**cfg, **ONB})
    info = c.get("/resets/8").json()["routine"]
    assert info["source"] == "onboarding" and info["prompt"] == "Put the cup back on the X."
    assert "Cup upright on the taped X" in info["steps"]


def test_phone_upload_photo(api):
    c, _, store, _ = api
    r = c.post("/resets/7/photo", json=body(source="upload", image_b64="data:image/jpeg;base64," + base64.b64encode(JPEG).decode()))
    assert r.status_code == 200, r.text
    assert r.json()["storage_uri"].split("rdf-media/", 1)[1].startswith("tickets/4/resets/7/phone-")
    bad = c.post("/resets/7/photo", json=body(source="upload", image_b64=base64.b64encode(b"hello").decode()))
    assert bad.status_code == 422


def test_only_the_claimant_of_a_live_reset(api):
    c, fake, _, _ = api
    assert c.post("/resets/7/photo", json=body(spacetime_token="wrong-token-xx")).status_code == 401
    assert c.post("/resets/10/photo", json=body()).status_code == 403  # someone else's claim
    assert c.post("/resets/8/photo", json=body()).status_code == 403  # open, not claimed
    assert c.post("/resets/9/photo", json=body()).status_code == 409  # not a reset task
    assert c.post("/resets/99/photo", json=body()).status_code == 404
    fake.tables["task"][7]["lease_expires_at"] = int((time.time() - 1) * 1e6)
    assert c.post("/resets/7/photo", json=body()).status_code == 403
    assert c.get("/resets/9").status_code == 409


def test_no_stream_published_is_503(api):
    c, fake, _, _ = api
    fake.tables["robot"][1]["camera_config_json"] = "{}"
    assert c.post("/resets/7/photo", json=body()).status_code == 503


def test_stream_url_helpers():
    assert http_base_of("wss://x.trycloudflare.com/ws/cameras", {}) == "https://x.trycloudflare.com"
    assert http_base_of("ws://h:1/ws/cameras", {"http": "http://h:1/"}) == "http://h:1"
    assert pick_cam({"cams": ["wrist", "exo"]}) == ["exo", "wrist"]
    assert pick_cam({})[0] == "exo"


# ------------------------------------------------------------------ escalation

class Mirror:
    def __init__(self) -> None:
        self.t: dict[str, dict[int, dict]] = {"task": {}, "event_log": {}}

    def rows(self, table):
        return list(self.t[table].values())

    def get(self, table, key):
        return self.t[table].get(key)


class FakeDispatcher:
    def __init__(self, m: Mirror) -> None:
        self.m, self.pages, self.events = m, [], []

    def event(self, type_, payload, dedup=None):
        self.events.append((type_, payload))
        eid = len(self.m.t["event_log"]) + 1
        self.m.t["event_log"][eid] = {"id": eid, "type": type_, "payload_json": json.dumps(payload)}

    def page(self, ticket_id, task_ids, task_type, price):
        self.pages.append((ticket_id, tuple(task_ids), task_type, price))


def test_unclaimed_reset_escalates_twice_then_stops():
    m = Mirror()
    now = [1_000.0]
    d = FakeDispatcher(m)
    w = rw.ResetWatch(m, lambda: d, escalate_s=90, levels=2, clock=lambda: now[0])
    m.t["task"][5] = {"id": 5, "type": "reset", "status": "open", "ticket_id": 2, "robot_id": 1, "price_cents": 200,
                      "created_at": int(1_000 * 1e6)}
    assert w.check(5) is None  # too early
    now[0] = 1_095.0
    assert w.check(5) == 1
    assert w.check(5) is None  # same level once
    now[0] = 1_185.0
    assert w.check(5) == 2
    now[0] = 1_400.0
    assert w.check(5) is None  # capped at levels
    levels = [p["level"] for t, p in d.events if t == rw.EVENT]
    assert levels == [1, 2] and d.events[-1][1]["final"] is True and "/reset/5" in d.events[-1][1]["page"]
    assert d.pages == [(2, (5,), "reset", 200)] * 2


def test_claimed_reset_is_not_escalated_and_restart_reads_the_log():
    m = Mirror()
    d = FakeDispatcher(m)
    m.t["task"][5] = {"id": 5, "type": "reset", "status": "open", "ticket_id": 2, "robot_id": 1, "price_cents": 200,
                      "created_at": int(1_000 * 1e6)}
    w = rw.ResetWatch(m, lambda: d, escalate_s=90, levels=2, clock=lambda: 1_100.0)
    assert w.check(5) == 1
    w2 = rw.ResetWatch(m, lambda: d, escalate_s=90, levels=2, clock=lambda: 1_100.0)  # orchestrator restart
    assert w2.check(5) is None
    m.t["task"][5]["status"] = "claimed"
    w3 = rw.ResetWatch(m, lambda: d, escalate_s=90, levels=2, clock=lambda: 1_300.0)
    assert w3.check(5) is None
    assert rw.ResetWatch(m, lambda: d, escalate_s=0).schedule(m.t["task"][5]) == []


def test_schedule_arms_timers_that_escalate():
    m = Mirror()
    d = FakeDispatcher(m)
    m.t["task"][6] = {"id": 6, "type": "reset", "status": "open", "ticket_id": 2, "robot_id": 1, "price_cents": 200,
                      "created_at": int(time.time() * 1e6)}
    w = rw.ResetWatch(m, lambda: d, escalate_s=0.2, levels=2)
    assert len(w.schedule(m.t["task"][6])) == 2
    time.sleep(0.7)
    w.stop()
    assert [p["level"] for t, p in d.events if t == rw.EVENT] == [1, 2]
