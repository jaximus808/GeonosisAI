"""TDM-65: web-push paging (Pager, the dispatcher hook, the manual-task handler, /push/* routes)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import paging
from rdf_orchestrator.paging_api import get_pager, paging_router
from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb

FCM = "https://fcm.googleapis.com/fcm/send/tdm65-"
ME = "c2" + "ab" * 31
SELLER = "c2" + "cd" * 31
TOKEN = "browser-token-for-me"


def settings(**kw) -> paging.PushSettings:
    base = dict(vapid_public_key="BPUB", vapid_private_key="cHJpdg", vapid_subject="mailto:t@example.invalid",
                rdf_push_store=":memory:", rdf_web_public_url=None, rdf_push_rate_s=10.0)
    base.update(kw)
    return paging.PushSettings(_env_file=None, **base)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Sender:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.codes = codes or {}
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, info: dict, data: str, key: str, claims: dict, ttl: int) -> int:
        assert key == "cHJpdg" and claims == {"sub": "mailto:t@example.invalid"} and ttl == paging.PUSH_TTL_S
        self.sent.append((info["endpoint"], json.loads(data)))
        return self.codes.get(info["endpoint"], 201)


def make_pager(sender: Sender | None = None, **kw) -> tuple[paging.Pager, Sender, Clock]:
    s, c = sender or Sender(), Clock()
    p = paging.Pager(settings(**kw), paging.PushStore(":memory:"), sender=s, clock=c)
    return p, s, c


def sub(n: int, cid: int) -> paging.Subscription:
    return paging.Subscription(f"{FCM}{n}", cid, "p256dh-key-xx", "auth-key-xx")


def req(task_ids, task_type="reset", price=200, online=()):
    return dsp.PageRequest(ticket_id=4, task_ids=list(task_ids), task_type=task_type, price_cents=price,
                           contributor_ids=list(online))


def test_pages_every_subscribed_contributor_with_deep_link():
    p, s, _ = make_pager(rdf_web_public_url="https://demo.trycloudflare.com/")
    p.store.upsert(sub(1, 3))
    p.store.upsert(sub(2, 3))  # second device of the same contributor
    p.store.upsert(sub(3, 5))
    out = p.page(req([11], online=[3, 9]))
    assert out["push"] == "sent" and out["sent"] == 3 and out["contributors"] == [3, 5]
    assert out["online_without_push"] == [9]  # online, no subscription: live marketplace highlight only
    assert "endpoint" not in json.dumps(out)  # endpoints are sensitive; the result is logged in event_log
    payload = s.sent[0][1]
    assert payload["url"] == "https://demo.trycloudflare.com/marketplace?task=11&src=push"
    assert payload["title"] == "Paid reset · $2" and payload["tag"] == "rdf-task-11"


def test_rate_limit_one_push_per_contributor_per_10s_and_task_paged_once():
    p, s, clock = make_pager()
    p.store.upsert(sub(1, 3))
    assert p.page(req([11]))["sent"] == 1
    assert p.page(req([11]))["push"] == "skipped"  # same task id never pages twice
    clock.t += 5
    out = p.page(req([12]))
    assert out["sent"] == 0 and out["rate_limited"] == [3]
    clock.t += 5.1
    assert p.page(req([13], "recovery_takeover", 500))["sent"] == 1
    assert s.sent[-1][1]["title"] == "Paid takeover · $5"
    assert len(s.sent) == 2


def test_batch_of_tasks_is_one_push():
    p, s, _ = make_pager()
    p.store.upsert(sub(1, 3))
    out = p.page(req([21, 22, 23], "clean_teleop", 1000))
    assert out["sent"] == 1 and out["task_ids"] == [21, 22, 23]
    assert s.sent[0][1]["title"] == "3 paid teleop demo tasks · $10 each"
    assert s.sent[0][1]["url"] == "/marketplace?task=21&src=push"


def test_gone_subscription_is_deleted():
    p, s, _ = make_pager(Sender({f"{FCM}1": 410}))
    p.store.upsert(sub(1, 3))
    p.store.upsert(sub(2, 4))
    out = p.page(req([11]))
    assert out["gone"] == 1 and out["sent"] == 1
    assert [x.endpoint for x in p.store.all()] == [f"{FCM}2"]


def test_disabled_without_vapid_keys():
    p, s, _ = make_pager(vapid_private_key=None)
    p.store.upsert(sub(1, 3))
    out = p.page(req([11]))
    assert out["push"] == "disabled" and not s.sent


def test_endpoint_allow_list():
    assert paging.endpoint_allowed("https://fcm.googleapis.com/fcm/send/abc")
    assert paging.endpoint_allowed("https://web.push.apple.com/QM_abc")
    assert paging.endpoint_allowed("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert not paging.endpoint_allowed("http://fcm.googleapis.com/fcm/send/abc")
    assert not paging.endpoint_allowed("https://localhost:8000/push")
    assert not paging.endpoint_allowed("https://evil-fcm.googleapis.com.attacker.io/x")


def test_store_file_is_private(tmp_path):
    path = tmp_path / "sub" / "push.sqlite3"
    st = paging.PushStore(path)
    st.upsert(sub(1, 3))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert paging.PushStore(path).stats() == {"subscriptions": 1, "contributors": 1}


# --------------------------------------------------------------------------- dispatcher + handler

class FakeMirror:
    def __init__(self, rows: dict[str, list[dict]]) -> None:
        self._rows = rows

    def rows(self, table: str) -> list[dict]:
        return self._rows.get(table, [])

    def get(self, table: str, rid: int):
        return next((r for r in self.rows(table) if r.get("id") == rid), None)


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call_reducer(self, name: str, *args):
        self.calls.append((name, *args))


def test_dispatcher_page_calls_hook_and_records_result(monkeypatch):
    p, s, _ = make_pager()
    p.store.upsert(sub(1, 3))
    paging.set_pager(p)
    paging.install()
    try:
        w = FakeWriter()
        m = FakeMirror({"contributor": [{"id": 3, "online": False, "kind": "contributor"},
                                        {"id": 9, "online": True, "kind": "contributor"}]})
        d = dsp.Dispatcher(m, lambda: w)
        d.page(4, [31], "reset", 200)
        (name, actor, type_, body), = [c for c in w.calls if c[0] == "append_event"]
        ev = json.loads(body)
        assert type_ == "dispatch.paged" and ev["channel"] == "push_hook"
        assert ev["result"]["sent"] == 1 and ev["result"]["online_without_push"] == [9]
        assert s.sent[0][1]["task_ids"] == [31]
    finally:
        dsp.set_page_hook(None)
        paging.set_pager(None)


def test_handler_pages_only_live_manual_tasks(monkeypatch):
    from rdf_orchestrator.handlers import paging as hp

    calls = []
    monkeypatch.setattr(dsp, "get_dispatcher", lambda bus: SimpleNamespace(page=lambda *a: calls.append(a)))
    manual = {"id": 40, "ticket_id": 4, "type": "reset", "price_cents": 200, "status": "open",
              "data_plan_id": 0, "instructions": "Put the cup back."}
    hp.page_manual_task(SimpleNamespace(row=manual, source="live", bus=None))
    hp.page_manual_task(SimpleNamespace(row={**manual, "id": 41}, source="snapshot", bus=None))
    hp.page_manual_task(SimpleNamespace(row={**manual, "id": 42, "data_plan_id": 7}, source="live", bus=None))
    hp.page_manual_task(SimpleNamespace(row={**manual, "id": 43, "instructions": "Reset. [ref trial:9]"},
                                        source="live", bus=None))
    assert calls == [(4, [40], "reset", 200)]
    dsp.set_page_hook(None)


# --------------------------------------------------------------------------- API

class FakeStdb:
    async def sql(self, q: str) -> list[dict]:
        assert q == "SELECT * FROM contributor"
        return [{"id": 3, "identity": "0x" + ME, "kind": "contributor"},
                {"id": 4, "identity": "0x" + SELLER, "kind": "dataset_seller"}]


@pytest.fixture
def api():
    p, s, _ = make_pager()

    async def verifier(identity: str, token: str) -> bool:
        return token == TOKEN and identity in (ME, SELLER)

    app = FastAPI()
    app.include_router(paging_router)
    app.dependency_overrides[get_stream_stdb] = lambda: FakeStdb()
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_pager] = lambda: p
    return TestClient(app), p, s


def body(identity=ME, token=TOKEN, endpoint=f"{FCM}abc", **extra):
    return {"identity": identity, "spacetime_token": token,
            "subscription": {"endpoint": endpoint, "keys": {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM", "auth": "tBHItJI5svbpez7KI4CCXg"}},
            **extra}


def test_config_exposes_public_key_only(api):
    c, _, _ = api
    assert c.get("/push/config").json() == {"enabled": True, "public_key": "BPUB", "rate_limit_s": 10.0}


def test_subscribe_state_test_unsubscribe(api):
    c, p, s = api
    r = c.post("/push/subscribe", json=body(user_agent="Chrome Android"))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "contributor_id": 3, "devices": 1}
    st = {"identity": ME, "spacetime_token": TOKEN, "endpoint": f"{FCM}abc"}
    assert c.post("/push/state", json=st).json()["subscribed"] is True
    t = c.post("/push/test", json={"identity": ME, "spacetime_token": TOKEN}).json()
    assert t["push"] == "sent" and s.sent[0][1]["test"] is True
    assert c.post("/push/test", json={"identity": ME, "spacetime_token": TOKEN}).json()["push"] == "rate_limited"
    assert c.post("/push/unsubscribe", json=st).json() == {"ok": True, "removed": True}
    assert c.post("/push/state", json=st).json()["subscribed"] is False


def test_subscribe_refusals(api):
    c, p, _ = api
    assert c.post("/push/subscribe", json=body(token="wrong-token-123")).status_code == 401
    assert c.post("/push/subscribe", json=body(identity=SELLER)).status_code == 403
    assert c.post("/push/subscribe", json=body(endpoint="https://127.0.0.1:8000/x")).status_code == 422
    other = "c2" + "ee" * 31
    assert c.post("/push/subscribe", json={**body(), "identity": other}).status_code == 401
    assert p.store.stats()["subscriptions"] == 0
