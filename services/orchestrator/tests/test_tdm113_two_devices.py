"""One person, two devices (laptop + phone): "Give back control", takeover of a text claim by the same
person's second device, merged accounts, and phone sign-in codes. Inkbox is mocked (dry-run transport)."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import text_offers as tx
from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb
from rdf_orchestrator.text_offers_api import get_text_offers, text_offers_router
from test_tdm113_text_offers import Tdm113Market, make, no_numbers, opt, req, task

PHONE = "4155550144"
E164 = "+14155550144"


def released_market(m: Tdm113Market):
    out: list[int] = []

    def release(tid: int) -> None:
        t = m.tasks[tid]
        if t["status"] != "claimed":
            raise RuntimeError(f"task {tid} is {t['status']}, not claimed")
        t.update(status="open", assignee_id=0)
        out.append(tid)
    return out, release


def setup(**kw):
    m = Tdm113Market([task(10)])
    released, release = released_market(m)
    o, m, tr, ev, clock = make(m, **kw)
    o._release = release
    return o, m, tr, ev, clock, released


# --------------------------------------------------------------------------- give back / hold

def test_give_back_releases_and_stops_the_text_hold():
    o, m, tr, ev, clock, released = setup()
    opt(o, 4, PHONE)
    o.on_page(req([10]), m.rows)
    assert o.on_inbound(E164, "YES")["action"] == "claimed"
    assert o.holding(10)
    out = o.give_back(4, m.tasks[10])
    assert out["ok"] and out["released"] and out["hold_dropped"]
    assert released == [10] and m.tasks[10]["status"] == "open" and not o.holding(10)
    assert o.tick_holds() == 0  # nothing heartbeats the released task again
    assert any(t == tx.EV_RELEASED for t, _ in ev)
    no_numbers(ev)


def test_give_back_refuses_someone_else_but_allows_a_merged_account():
    o, m, tr, ev, clock, released = setup()
    m.tasks[10].update(status="claimed", assignee_id=4)
    assert o.give_back(1, m.tasks[10])["code"] == "not_holder"
    assert released == []
    o.store.merge(4, 1, clock(), move_devices=False)  # 4 is the same person as 1
    assert o.give_back(1, m.tasks[10])["released"] is True
    assert released == [10]


def test_give_back_on_a_free_task_only_drops_the_hold():
    o, m, *_ , released = setup()
    o.hold(10, 4)
    out = o.give_back(4, m.tasks[10])
    assert out["ok"] and not out["released"] and out["hold_dropped"] and released == []


# --------------------------------------------------------------------------- takeover

def test_second_device_of_the_same_person_takes_over():
    o, m, tr, ev, clock, released = setup()
    m.tasks[10].update(status="claimed", assignee_id=4)
    o.hold(10, 4)
    assert o.take_over(2, m.tasks[10])["code"] == "taken"  # someone else: refused
    o.store.merge(4, 1, clock(), move_devices=True)
    out = o.take_over(1, m.tasks[10])
    assert out["ok"] and out["previous_holder_id"] == 4
    assert released == [10] and m.tasks[10]["assignee_id"] == 1 and not o.holding(10)
    assert any(t == tx.EV_TAKEOVER for t, _ in ev)


def test_takeover_same_contributor_new_generation():
    o, m, *_, released = setup()
    m.tasks[10].update(status="claimed", assignee_id=4)
    assert o.take_over(4, m.tasks[10])["ok"]
    assert released == [10] and m.claims[-1] == (10, 4)


# --------------------------------------------------------------------------- merge in the store

def test_store_merge_repoints_optin_offers_devices():
    o, m, tr, ev, clock, _ = setup()
    opt(o, 4, PHONE)
    o.store.add_offer([10], E164, 4, "recovery_takeover", 4, clock())
    o.store.remember_device("aa" * 32, 4, "tok-4", clock())
    out = o.store.merge(4, 1, clock(), move_devices=True)
    assert out == {"optin": 1, "offers": 1, "devices_moved": 1, "devices_dropped": 0}
    assert o.store.optin(E164).contributor_id == 1
    assert o.store.devices_for(1) == [("aa" * 32, "tok-4")]
    assert o.store.canonical(4) == 1 and o.store.canonical(1) == 1
    assert o.store.merge(4, 1, clock(), move_devices=True)["optin"] == 0  # idempotent


# --------------------------------------------------------------------------- phone sign-in

def sent_code(tr) -> str:
    body = [t for _, t in tr.sent if "sign-in code" in t][-1]
    return body.split("code is ")[1][:6]


def test_login_code_only_for_opted_in_numbers_and_rate_limited():
    o, m, tr, ev, clock, _ = setup()
    tr.sent.clear()
    assert o.login_start("4155550199")["ok"]  # unknown: same answer, nothing sent
    assert tr.sent == []
    opt(o, 4, PHONE)
    tr.sent.clear()
    assert o.login_start(PHONE)["ok"]
    assert len(tr.sent) == 1 and tr.sent[0][0] == E164
    assert o.login_start(PHONE)["code"] == "rate_limited"  # 30 s gap
    for _ in range(4):
        clock.t += 31
        assert o.login_start(PHONE)["ok"]
    clock.t += 31
    assert o.login_start(PHONE)["code"] == "rate_limited"  # 5 / hour
    o.store.set_status(E164, "stopped", clock())
    clock.t += 3600
    tr.sent.clear()
    assert o.login_start(PHONE)["ok"] and tr.sent == []  # STOPped numbers get no code
    no_numbers(ev)


def test_login_code_expires_single_use_and_attempts():
    o, m, tr, ev, clock, _ = setup()
    opt(o, 4, PHONE)
    o.login_start(PHONE)
    code = sent_code(tr)
    wrong = "000000" if code != "000000" else "111111"
    assert o.login_check(PHONE, wrong)["code"] == "bad_code"
    assert o.login_check(PHONE, code) == {"ok": True, "contributor_id": 4}
    assert o.login_check(PHONE, code)["code"] == "bad_code"  # single use
    clock.t += 60
    o.login_start(PHONE)
    code = sent_code(tr)
    clock.t += 601
    assert o.login_check(PHONE, code)["code"] == "bad_code"  # 10 min
    clock.t += 60
    o.login_start(PHONE)
    code = sent_code(tr)
    for _ in range(5):
        o.login_check(PHONE, wrong)
    assert o.login_check(PHONE, code)["code"] == "bad_code"  # burned after 5 wrong tries


def test_login_resolves_merged_owner():
    o, m, tr, ev, clock, _ = setup()
    opt(o, 4, PHONE)
    o.store.merge(4, 1, clock(), move_devices=True)
    o.login_start(PHONE)
    assert o.login_check(PHONE, sent_code(tr))["contributor_id"] == 1


def test_login_code_kept_out_of_the_live_outbox():
    o, m, tr, ev, clock, _ = setup(mode="live")
    opt(o, 4, PHONE)
    o.login_start(PHONE)
    code = sent_code(tr)
    assert code not in json.dumps(o.outbox.tail(50))


# --------------------------------------------------------------------------- API

LAPTOP = "aa" * 32
PHONE_ID = "bb" * 32
TOKENS = {LAPTOP: "laptop-token-xx", PHONE_ID: "phone-token-xxx"}


class TwoDeviceStdb:
    def __init__(self, m: Tdm113Market) -> None:
        self.m = m
        self.contributors = [{"id": 1, "identity": "0x" + LAPTOP, "kind": "contributor", "name": "Jaxon"},
                             {"id": 4, "identity": "0x" + PHONE_ID, "kind": "contributor", "name": "Jaxon"}]

    async def sql(self, q: str) -> list[dict]:
        if "FROM task" in q:
            tid = int(q.rsplit("=", 1)[1])
            return [dict(self.m.tasks[tid])] if tid in self.m.tasks else []
        return [dict(c) for c in self.contributors]


@pytest.fixture
def api():
    o, m, tr, ev, clock, released = setup()
    db = TwoDeviceStdb(m)

    async def verifier(identity: str, token: str) -> bool:
        return TOKENS.get(identity) == token

    app = FastAPI()
    app.include_router(text_offers_router)
    app.dependency_overrides[get_stream_stdb] = lambda: db
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_text_offers] = lambda: o
    return TestClient(app), o, m, tr, db, released


def me(identity: str) -> dict:
    return {"identity": identity, "spacetime_token": TOKENS[identity]}


def test_api_release_and_takeover(api):
    c, o, m, tr, db, released = api
    m.tasks[10].update(status="claimed", assignee_id=4)
    o.hold(10, 4)
    assert c.post("/texts/release", json={**me(LAPTOP), "task_id": 10}).status_code == 403
    assert c.post("/texts/takeover", json={**me(LAPTOP), "task_id": 10}).status_code == 409
    o.store.merge(4, 1, o.clock(), move_devices=False)
    r = c.post("/texts/takeover", json={**me(LAPTOP), "task_id": 10})
    assert r.status_code == 200, r.text
    assert m.tasks[10]["assignee_id"] == 1 and not o.holding(10)
    r = c.post("/texts/release", json={**me(LAPTOP), "task_id": 10})
    assert r.status_code == 200 and r.json()["released"] and m.tasks[10]["status"] == "open"
    assert c.post("/texts/release", json={**me(LAPTOP), "task_id": 99}).status_code == 404


def test_api_phone_login_hands_over_the_device_token(api):
    c, o, m, tr, db, _ = api
    r = c.post("/texts/optin", json={**me(LAPTOP), "phone": PHONE, "consent": True, "consent_text": tx.CONSENT_TEXT})
    assert r.status_code == 200  # the laptop (contributor 1) is remembered as a device
    assert c.post("/texts/login_code", json={"phone": PHONE}).status_code == 200
    code = sent_code(tr)
    assert c.post("/texts/login", json={**me(PHONE_ID), "phone": PHONE, "code": "12345x"}).status_code == 401
    r = c.post("/texts/login", json={**me(PHONE_ID), "phone": PHONE, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["switch"] is True and r.json()["token"] == TOKENS[LAPTOP] and r.json()["contributor_id"] == 1
    assert E164 not in r.text and "+1415" not in r.text
    # the laptop itself signing in: nothing to switch
    o.clock.t += 31
    c.post("/texts/login_code", json={"phone": PHONE})
    r = c.post("/texts/login", json={**me(LAPTOP), "phone": PHONE, "code": sent_code(tr)})
    assert r.json() == {"ok": True, "contributor_id": 1, "switch": False, "name": "Jaxon"}


def test_api_phone_login_without_a_known_device(api):
    c, o, m, tr, db, _ = api
    opt(o, 1, PHONE)  # opted in, but the laptop never made a verified call to this orchestrator
    c.post("/texts/login_code", json={"phone": PHONE})
    r = c.post("/texts/login", json={**me(PHONE_ID), "phone": PHONE, "code": sent_code(tr)})
    assert r.status_code == 409 and "device you signed up with" in r.json()["detail"]


def test_api_merged_identity_device_is_not_handed_out(api):
    c, o, m, tr, db, _ = api
    opt(o, 4, PHONE)
    o.store.remember_device(PHONE_ID, 4, TOKENS[PHONE_ID], o.clock())
    o.store.merge(4, 1, o.clock(), move_devices=True)  # but the module merge kept identities: 4's id != 1's
    c.post("/texts/login_code", json={"phone": PHONE})
    r = c.post("/texts/login", json={**me(PHONE_ID), "phone": PHONE, "code": sent_code(tr)})
    assert r.status_code == 409
    db.contributors[0]["identity"] = "0x" + PHONE_ID  # merge_contributor(take_identity) moved 4's identity onto 1
    db.contributors[1]["identity"] = None
    o.clock.t += 31
    c.post("/texts/login_code", json={"phone": PHONE})
    r = c.post("/texts/login", json={**me(LAPTOP), "phone": PHONE, "code": sent_code(tr)})
    assert r.status_code == 200 and r.json()["token"] == TOKENS[PHONE_ID]
