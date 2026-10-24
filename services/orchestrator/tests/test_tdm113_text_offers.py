"""TDM-113: text job offers (opt-in, STOP/START/HELP, fan-out, first YES wins, in-person resets, webhook
signatures, /texts/* routes). Inkbox is mocked: every send goes to a recording transport, nothing leaves."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import job_drops as jd
from rdf_orchestrator import text_offers as tx
from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb
from rdf_orchestrator.text_offers_api import get_text_offers, text_offers_router

TDM113_SECRET = "whsec_tdm113_test_key"
TDM113_ME = "c3" + "ab" * 31
TDM113_TOKEN = "browser-token-tdm113"


class Tdm113Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class Tdm113Transport:
    """Mocked Inkbox: records (phone, text); can fail chosen numbers."""

    def __init__(self, state: str = "sent", fail: dict[str, str] | None = None) -> None:
        self.state, self.fail = state, fail or {}
        self.sent: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, phone: str, text: str) -> tx.SendResult:
        with self.lock:
            self.sent.append((phone, text))
        return tx.SendResult(self.fail.get(phone, self.state), channel="imessage")

    def to(self, phone: str) -> list[str]:
        return [t for p, t in self.sent if p == phone]


class Tdm113Market:
    """claim_task with SpacetimeDB's semantics: atomic, the first claim of an open task wins."""

    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = {t["id"]: dict(t) for t in tasks}
        self.lock = threading.Lock()
        self.claims: list[tuple[int, int]] = []
        self.heartbeats: list[int] = []

    def rows(self, table: str) -> list[dict]:
        if table == "task":
            return [dict(t) for t in self.tasks.values()]
        if table == "robot":
            return [{"id": 1, "name": "sim-yam-mujoco", "camera_config_json": json.dumps({"sim": True})}]
        if table == "ticket":
            return [{"id": 4, "task_description": "Pick up the cube"}]
        return []

    def claim(self, task_id: int, cid: int) -> None:
        time.sleep(0.01)  # widen the race window
        with self.lock:
            t = self.tasks[task_id]
            if t["status"] != "open":
                raise RuntimeError(f"task {task_id} is {t['status']}")
            t.update(status="claimed", assignee_id=cid)
            self.claims.append((task_id, cid))

    def heartbeat(self, task_id: int) -> None:
        self.heartbeats.append(task_id)


def task(i: int, type_: str = "recovery_takeover", price: int = 350, **kw) -> dict:
    return {"id": i, "ticket_id": 4, "robot_id": 1, "type": type_, "price_cents": price, "status": "open",
            "assignee_id": 0, "instructions": "Finish the drop.", **kw}


def req(ids: list[int], type_: str = "recovery_takeover", price: int = 350) -> dsp.PageRequest:
    return dsp.PageRequest(ticket_id=4, task_ids=ids, task_type=type_, price_cents=price, contributor_ids=[])


def make(market: Tdm113Market | None = None, transport: Tdm113Transport | None = None, **kw):
    market = market or Tdm113Market([task(10)])
    transport = transport or Tdm113Transport()
    events: list[tuple[str, dict]] = []
    clock = Tdm113Clock()
    s = tx.TextSettings(_env_file=None, rdf_text_mode=kw.pop("mode", "dry_run"), rdf_text_store=":memory:",
                        rdf_text_webhook_secret=TDM113_SECRET, **kw)
    ds = jd.DropSettings(_env_file=None, rdf_web_public_url="https://geonosis.example.com", rdf_edge_secret="edge-s3cret")
    o = tx.TextOffers(s, store=tx.TextStore(":memory:"), transport=transport, outbox=tx.Outbox(None),
                      link=tx.InkboxLink(s, client=object(), state_path="/nonexistent/tdm113.json"),
                      append=lambda t, p: events.append((t, p)), claim=market.claim, heartbeat=market.heartbeat,
                      rows=market.rows, clock=clock, inline=True, drop_settings=ds,
                      record_claim=lambda t, c, src: events.append(("job_drop.claimed", {"task_id": t, "src": src})))
    return o, market, transport, events, clock


def opt(o: tx.TextOffers, cid: int, phone: str, in_person: bool = False) -> dict:
    return o.opt_in(cid, phone, in_person, tx.CONSENT_TEXT)


def no_numbers(events: list[tuple[str, dict]]) -> None:
    blob = json.dumps(events)
    assert "555" not in blob, "a phone number reached the (public) event log"


# --------------------------------------------------------------------------- opt-in

def test_opt_in_stores_and_sends_welcome():
    o, _, tr, ev, _ = make()
    out = opt(o, 3, "(415) 555-0101", in_person=True)
    assert out["ok"] and out["opted_in"] and out["last4"] == "0101" and out["in_person"] is True
    assert tr.sent == [("+14155550101", tx.WELCOME)]
    assert ev[0][0] == tx.EV_OPT_IN and ev[0][1]["contributor_id"] == 3
    assert o.outbox.tail()[-1]["to"] == "***0101" and o.outbox.tail()[-1]["text"] == tx.WELCOME
    no_numbers(ev)


def test_opt_in_requires_consent_and_a_real_number():
    o, _, tr, _, _ = make()
    assert opt(o, 3, "555")["code"] == "bad_phone"
    assert o.opt_in(3, "4155550101", False, "something else")["code"] == "consent_required"
    assert tr.sent == []


def test_dry_run_default_never_uses_inkbox():
    s = tx.TextSettings(_env_file=None, rdf_text_store=":memory:")
    assert s.rdf_text_mode == "dry_run"
    o = tx.TextOffers(s, store=tx.TextStore(":memory:"), outbox=tx.Outbox(None), append=lambda t, p: None,
                      link=tx.InkboxLink(s, client=object(), state_path="/nonexistent/x.json"))
    assert o.transport() is tx.dry_run_transport
    assert o.opt_in(3, "4155550101", False, tx.CONSENT_TEXT)["welcome"]["state"] == "dry_run"


def test_never_texts_a_number_that_did_not_opt_in():
    o, _, tr, _, _ = make()
    assert o._text("+14155550199", "hi", "offer").state == "blocked"
    assert o.on_inbound("+14155550199", "YES")["action"] == "ignored"
    assert o.on_inbound("+14155550199", "HELP")["action"] == "ignored"
    assert tr.sent == []


# --------------------------------------------------------------------------- STOP / START / HELP

def test_stop_start_help():
    o, m, tr, ev, _ = make()
    opt(o, 3, "4155550101")
    tr.sent.clear()
    assert o.on_inbound("+14155550101", "help")["action"] == "help"
    assert tr.to("+14155550101") == [tx.HELP]
    assert o.on_inbound("+14155550101", "Stop.")["action"] == "stopped"
    assert tr.to("+14155550101")[-1] == tx.STOP_CONFIRM
    assert any(t == tx.EV_STOP for t, _ in ev)
    n = len(tr.sent)
    o.on_inbound("+14155550101", "HELP")  # stopped: no reply
    o.on_page(req([10]), m.rows)  # stopped: no offer
    assert len(tr.sent) == n
    assert o.on_inbound("+14155550101", "START")["action"] == "started"
    assert tr.to("+14155550101")[-1] == tx.WELCOME
    no_numbers(ev)


def test_web_opt_out_stops_offers():
    o, m, tr, _, _ = make()
    opt(o, 3, "4155550101")
    o.opt_out(3)
    tr.sent.clear()
    o.on_page(req([10]), m.rows)
    assert tr.sent == []


# --------------------------------------------------------------------------- offers

def test_offer_fan_out_text_and_rate_limits():
    m = Tdm113Market([task(i) for i in (10, 11, 12)])
    o, m, tr, ev, clock = make(m)
    for cid in range(1, 6):
        opt(o, cid, f"41555501{cid:02d}")
    tr.sent.clear()
    out = o.on_page(req([10]), m.rows)
    assert out["count"] == 3 and out["contributors"] == [1, 2, 3]
    body = tr.sent[0][1]
    assert body == "$3.50 robot job, about 2 min: recovery takeover on yam sim. Reply YES to claim. (Stripe test mode)"
    assert [t for t, _ in ev].count(tx.EV_OFFER) == 1
    clock.t += 30  # inside the 2 min window: 1-3 are rate-limited, 4-5 get the next one
    out = o.on_page(req([11]), m.rows)
    assert out["contributors"] == [4, 5] and out["skipped"]["rate_limited"] == 3
    clock.t += 200  # past the window: least recently offered first
    out = o.on_page(req([12]), m.rows)
    assert out["contributors"] == [1, 2, 3]
    no_numbers(ev)


def test_daily_cap_and_already_offered():
    m = Tdm113Market([task(i) for i in range(100, 125)])
    o, m, tr, _, clock = make(m, rdf_text_daily_max=2)
    opt(o, 1, "4155550101")
    for i in range(100, 104):
        o.on_page(req([i]), m.rows)
        clock.t += 130
    assert len(tr.to("+14155550101")) == 1 + 2  # welcome + 2 offers
    assert o.on_page(req([100]), m.rows)["reason"] == "already offered"


def test_resets_only_go_to_in_person_and_busy_is_skipped():
    m = Tdm113Market([task(20, "reset", 200), task(21), task(9, status="claimed", assignee_id=2)])
    o, m, tr, _, _ = make(m)
    opt(o, 1, "4155550101", in_person=False)
    opt(o, 2, "4155550102", in_person=True)  # in person but busy (holds task 9)
    opt(o, 3, "4155550103", in_person=True)
    tr.sent.clear()
    out = o.on_page(req([20], "reset", 200), m.rows)
    assert out["contributors"] == [3]
    assert out["skipped"] == {"not_in_person": 1, "busy": 1, "rate_limited": 0, "daily_cap": 0}
    assert "reset on yam sim" in tr.sent[0][1]


def test_operator_tasks_are_never_texted():
    m = Tdm113Market([task(30, instructions="Internal check [operator]")])
    o, m, tr, ev, _ = make(m)
    opt(o, 1, "4155550101")
    tr.sent.clear()
    assert o.on_page(req([30]), m.rows)["text"] == "skipped"
    assert tr.sent == []


# --------------------------------------------------------------------------- first YES wins

def test_first_yes_wins_the_race():
    m = Tdm113Market([task(10)])
    o, m, tr, ev, _ = make(m)
    opt(o, 1, "4155550101")
    opt(o, 2, "4155550102")
    o.on_page(req([10]), m.rows)
    results: dict[str, dict] = {}
    go = threading.Barrier(2)

    def yes(phone: str) -> None:
        go.wait()
        results[phone] = o.on_inbound(phone, "YES")

    ts = [threading.Thread(target=yes, args=(p,)) for p in ("+14155550101", "+14155550102")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(m.claims) == 1
    winner_cid = m.claims[0][1]
    win = f"+141555501{winner_cid:02d}"
    lose = "+14155550102" if winner_cid == 1 else "+14155550101"
    assert results[win]["action"] == "claimed" and results[lose]["action"] == "taken"
    link = jd.invite_link(o.drop_settings(), "https://geonosis.example.com", 10, "text")
    assert link.startswith("https://geonosis.example.com/j/10/text?k=")
    assert link in tr.to(win)[-1]
    assert tr.to(lose)[-1] == tx.TAKEN
    claimed = [p for t, p in ev if t == tx.EV_CLAIMED]
    assert claimed == [{**claimed[0], "task_id": 10, "contributor_id": winner_cid, "src": "text"}]
    assert ("job_drop.claimed", {"task_id": 10, "src": "text"}) in ev  # /ops/drops attribution
    assert "k=" not in json.dumps(ev)  # the invite never reaches the public event log
    # a second YES from the winner resends the link; from the loser still "Taken"
    assert o.on_inbound(win, "yes")["repeat"] is True
    assert o.on_inbound(lose, "yes")["action"] == "taken"
    no_numbers(ev)


def test_group_offer_gives_each_yes_its_own_task_then_taken():
    m = Tdm113Market([task(10), task(11)])
    o, m, tr, _, _ = make(m)
    for cid in (1, 2, 3):
        opt(o, cid, f"41555501{cid:02d}")
    o.on_page(req([10, 11]), m.rows)
    assert "$3.50 robot job" in tr.to("+14155550103")[-1]
    acts = [o.on_inbound(f"+141555501{c:02d}", "YES") for c in (1, 2, 3)]
    assert [a["action"] for a in acts] == ["claimed", "claimed", "taken"]
    assert sorted(t for t, _ in m.claims) == [10, 11]


def test_offer_expires_when_task_closes_and_hold_heartbeats():
    m = Tdm113Market([task(10)])
    o, m, tr, _, clock = make(m)
    opt(o, 1, "4155550101")
    opt(o, 2, "4155550102")
    o.on_page(req([10]), m.rows)
    assert o.on_inbound("+14155550101", "YES")["action"] == "claimed"
    o.task_changed(10, m.rows)  # claimed: the other offer expires
    assert o.on_inbound("+14155550102", "YES")["action"] == "taken"
    assert o.tick_holds(m.rows) == 1 and m.heartbeats == [10]  # arm-control claim kept alive while the link opens
    clock.t += 200
    assert o.tick_holds(m.rows) == 0  # past RDF_TEXT_HOLD_S: the teleop page heartbeats from here


def test_no_offer_reply():
    o, _, tr, _, _ = make()
    opt(o, 1, "4155550101")
    assert o.on_inbound("+14155550101", "YES")["action"] == "no_offer"
    assert tr.to("+14155550101")[-1] == tx.NO_OFFER


def test_page_listener_off_under_pytest_without_install():
    tx.set_offers(None)
    assert tx.page_listener(req([1]), lambda t: []) is None


# --------------------------------------------------------------------------- webhook

def inbound_body(phone: str, text: str, eid: str = "evt_1") -> bytes:
    return json.dumps({"id": eid, "event_type": "imessage.received", "timestamp": "2026-10-24T00:00:00Z",
                       "data": {"message": {"id": "m1", "conversation_id": "c1", "direction": "inbound",
                                            "remote_number": phone, "content": text, "is_group": False},
                                "reaction": None, "contacts": [], "agent_identities": []}}).encode()


def test_webhook_signature_checks_and_replay():
    o, m, tr, _, clock = make()
    opt(o, 1, "4155550101")
    tr.sent.clear()
    raw = inbound_body("+14155550101", "HELP")
    good = tx.sign_webhook(TDM113_SECRET, raw, request_id="r1", ts=int(clock.t))
    assert o.handle_webhook(raw, good)[0] == 200
    assert tr.to("+14155550101") == [tx.HELP]
    assert o.handle_webhook(raw, good)[1].get("duplicate") is True  # replayed event id: handled once
    forged = tx.sign_webhook("whsec_other", raw, request_id="r2", ts=int(clock.t))
    assert o.handle_webhook(raw, forged) == (401, {"ok": False, "reason": "signature mismatch"})
    tampered = raw.replace(b"HELP", b"YES!")
    assert o.handle_webhook(tampered, good)[0] == 401
    stale = tx.sign_webhook(TDM113_SECRET, raw, request_id="r3", ts=int(clock.t) - 301)
    assert o.handle_webhook(raw, stale)[1]["reason"] == "timestamp outside 300s window"
    assert o.handle_webhook(raw, {})[1]["reason"].startswith("missing")
    assert len(tr.sent) == 1


def test_tunnel_asgi_app_only_serves_the_webhook():
    o, _, _, _, clock = make()
    app = o.asgi()

    def call(path: str, method: str = "POST", body: bytes = b"{}", headers: dict | None = None) -> int:
        out: dict = {}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]

        scope = {"type": "http", "path": path, "method": method,
                 "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()]}
        asyncio.run(app(scope, receive, send))
        return out["status"]

    assert call("/healthz", "GET") == 404
    assert call("/tickets") == 404
    assert call(tx.WEBHOOK_PATH, "GET") == 405
    assert call(tx.WEBHOOK_PATH) == 401
    body = json.dumps({"id": "evt_s", "event_type": "imessage.sent", "data": {"message": None}}).encode()
    assert call(tx.WEBHOOK_PATH, body=body, headers=tx.sign_webhook(TDM113_SECRET, body, request_id="r", ts=int(clock.t))) == 200


def test_keyword_and_phone_parsing():
    assert tx.keyword("  yes please") == "YES" and tx.keyword("Y") == "Y" and tx.keyword("") == ""
    assert tx.normalize_phone("+1 (415) 555-0101") == "+14155550101"
    assert tx.normalize_phone("14155550101") == "+14155550101"
    assert tx.normalize_phone("+12345") is None and tx.normalize_phone("abc") is None


def test_attribution_counts_text_offers_and_claims():
    rows = [{"type": tx.EV_OFFER, "payload_json": json.dumps({"src": "text", "task_id": 1})},
            {"type": jd.EV_CLAIMED, "payload_json": json.dumps({"src": "text", "task_id": 1, "contributor_id": 2})},
            {"type": jd.EV_POSTED, "payload_json": json.dumps({"channel": "discord"})}]
    assert jd.attribution(rows)["by_src"] == {"text": {"claims": 1, "posts": 1}, "discord": {"claims": 0, "posts": 1}}


# --------------------------------------------------------------------------- API

class Tdm113FakeStdb:
    async def sql(self, q: str) -> list[dict]:
        return [{"id": 3, "identity": "0x" + TDM113_ME, "kind": "contributor"}]


@pytest.fixture
def tdm113_api():
    o, m, tr, ev, clock = make()

    async def verifier(identity: str, token: str) -> bool:
        return token == TDM113_TOKEN and identity == TDM113_ME

    app = FastAPI()
    app.include_router(text_offers_router)
    app.dependency_overrides[get_stream_stdb] = lambda: Tdm113FakeStdb()
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_text_offers] = lambda: o
    return TestClient(app), o, tr, clock


def test_api_optin_state_optout(tdm113_api):
    c, o, tr, _ = tdm113_api
    me = {"identity": TDM113_ME, "spacetime_token": TDM113_TOKEN}
    assert c.post("/texts/state", json=me).json()["opted_in"] is False
    r = c.post("/texts/optin", json={**me, "phone": "415-555-0101", "in_person": True, "consent": True,
                                     "consent_text": tx.CONSENT_TEXT})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["opted_in"] and j["last4"] == "0101" and "+1415" not in r.text and j["welcome"]["state"] == "sent"
    assert c.post("/texts/optin", json={**me, "phone": "415-555-0101", "consent": False,
                                        "consent_text": tx.CONSENT_TEXT}).status_code == 422
    assert c.post("/texts/optin", json={**me, "token": "x", "spacetime_token": "wrong-token-xx", "phone": "4155550101",
                                        "consent": True, "consent_text": tx.CONSENT_TEXT}).status_code == 401
    assert c.post("/texts/optout", json=me).json()["opted_in"] is False
    assert c.post("/texts/state", json=me).json()["stopped"] is True


def test_api_webhook_route(tdm113_api):
    c, o, tr, clock = tdm113_api
    raw = inbound_body("+14155550101", "HELP", "evt_api")
    h = tx.sign_webhook(TDM113_SECRET, raw, request_id="r", ts=int(clock.t))
    assert c.post("/texts/webhook", content=raw, headers=h).status_code == 200  # unknown number: verified, ignored
    assert tr.sent == []
    assert c.post("/texts/webhook", content=raw, headers={**h, "x-inkbox-signature": "sha256=00"}).status_code == 401
