"""TDM-91 offers: verdict maths (mirrors TDM-14), settle readiness, LeRobot unpacking, the settle handler.
The end-to-end proof against a real local SpacetimeDB is spikes/tdm-91-offers/prove_offers.py."""
from __future__ import annotations

import pytest

from rdf_orchestrator import offers as of
from rdf_orchestrator.handlers import offers as oh

OFFER = {"id": 7, "ticket_id": 3, "seller_id": 9, "needed_count": 3, "status": "qa", "price_per_episode_cents": 400,
         "accepted_count": 0, "ingested_count": 3}


def ep(i, status="pending"):
    return {"id": i, "ticket_id": 3, "dataset_offer_id": 7, "kind": "purchased", "review_status": status,
            "storage_uri": f"local://b/tickets/3/purchased/7/e{i}"}


def qa(rid, eid, name, passed=True):
    return {"id": rid, "episode_id": eid, "check_name": name, "passed": passed, "detail_json": "{}"}


def rev(rid, eid, accepted):
    return {"id": rid, "episode_id": eid, "accepted": accepted, "reason": "r"}


def test_verdict_mirrors_reducer():
    assert of.episode_verdict(ep(1), [], []) == "pending"
    assert of.episode_verdict(ep(1), [qa(1, 1, "jerk")], []) == "pending"
    assert of.episode_verdict(ep(1), [qa(1, 1, "jerk", False)], []) == "rejected"
    # latest row per check wins
    assert of.episode_verdict(ep(1), [qa(1, 1, "jerk", False), qa(2, 1, "jerk", True)], [rev(1, 1, True)]) == "accepted"
    assert of.episode_verdict(ep(1, "rejected"), [], []) == "rejected"
    assert of.episode_verdict(ep(1), [qa(1, 1, "jerk")], [rev(1, 1, False)]) == "rejected"
    # billable needs >= 1 QA check AND an accepted review
    assert not of.episode_billable(ep(1, "accepted"), [], [rev(1, 1, True)])
    assert of.episode_billable(ep(1, "accepted"), [qa(1, 1, "jerk")], [rev(1, 1, True)])


def _state(qa_rows, reviews, required=frozenset({"deterministic_qa"}), offer=OFFER):
    eps = [ep(1), ep(2), ep(3), {**ep(4), "dataset_offer_id": 8}]
    qa_by, rv_by = {}, {}
    for r in qa_rows:
        qa_by.setdefault(r["episode_id"], []).append(r)
    for r in reviews:
        rv_by.setdefault(r["episode_id"], []).append(r)
    return of.offer_state(offer, eps, qa_by, rv_by, set(required))


def test_readiness():
    passed = [qa(10 + i, i, "deterministic_qa") for i in (1, 2, 3)]
    st = _state(passed, [rev(1, 1, True), rev(2, 2, True)])
    assert len(st.episodes) == 3  # episode 4 belongs to another offer
    assert of.finalize_readiness(st, False) == (False, "1 episode(s) still under QA")
    st = _state(passed, [rev(1, 1, True), rev(2, 2, True), rev(3, 3, True)])
    assert of.finalize_readiness(st, False)[0] and st.billable == 3
    # one rejected: wait for a replacement unless the seller is done
    st = _state(passed[:2] + [qa(20, 3, "jerk", False)], [rev(1, 1, True), rev(2, 2, True)])
    assert (st.billable, st.rejected, st.open_slots) == (2, 1, 1)
    assert not of.finalize_readiness(st, False)[0]
    assert of.finalize_readiness(st, True)[0]
    # an accepted review without TDM-92's summary row is held back
    st = _state([qa(1, 1, "jerk")], [rev(1, 1, True), rev(2, 2, False), rev(3, 3, False)])
    assert of.finalize_readiness(st, True) == (False, "waiting for QA checks {1: ['deterministic_qa']}")
    assert not of.finalize_readiness(_state([], [], offer={**OFFER, "status": "done"}), True)[0]


def test_required_checks_env(monkeypatch):
    assert of.required_checks() == {"deterministic_qa"}
    monkeypatch.setenv("RDF_OFFER_REQUIRED_QA", "none")
    assert of.required_checks() == set()
    monkeypatch.setenv("RDF_OFFER_REQUIRED_QA", "a, b")
    assert of.required_checks() == {"a", "b"}


def test_keys():
    assert of.purchased_key(3, 7, "ep_1", "exo.mp4") == "tickets/3/purchased/7/ep_1/exo.mp4"
    assert of.lerobot_key(3, 7, "meta/info.json") == "tickets/3/purchased/7/lerobot/meta/info.json"
    with pytest.raises(ValueError):
        of.purchased_key(3, 7, "../x", "exo.mp4")
    with pytest.raises(ValueError):
        of.lerobot_key(3, 7, "data/../../../secret")


def test_unpack_lerobot():
    info = {"fps": 30, "chunks_size": 1000, "features": {
        "observation.images.wrist": {"dtype": "video"}, "observation.images.exo": {"dtype": "video"},
        "observation.state": {"dtype": "float32"}}}
    out = of.unpack_lerobot("local://b/tickets/3/purchased/7/lerobot/", info,
                            [{"episode_index": 1, "length": 60}, {"episode_index": 0, "length": 300}])
    assert [d.ref for d in out] == ["lerobot-000000", "lerobot-000001"]
    d = out[0]
    assert d.actions_uri == "local://b/tickets/3/purchased/7/lerobot/data/chunk-000/episode_000000.parquet"
    assert d.cameras == {
        "exo": "local://b/tickets/3/purchased/7/lerobot/videos/chunk-000/observation.images.exo/episode_000000.mp4",
        "wrist": "local://b/tickets/3/purchased/7/lerobot/videos/chunk-000/observation.images.wrist/episode_000000.mp4"}
    assert (d.frame_count, d.duration_ms) == (300, 10_000)
    assert out[1].storage_uri.endswith("/lerobot/episode_000001")


class FakeBus:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, t):
        return list(self.tables.get(t, []))

    def get(self, t, key):
        return next((r for r in self.tables.get(t, []) if r["id"] == key), None)


class FakeStdb:
    def __init__(self):
        self.calls = []

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))


def test_handler_settles_and_reserves(monkeypatch):
    stdb = FakeStdb()
    monkeypatch.setattr(oh, "make_stdb", lambda: stdb)
    tables = {"dataset_offer": [dict(OFFER)], "episode": [ep(1), ep(2), ep(3)],
              "qa_result": [qa(1, 1, "deterministic_qa"), qa(2, 2, "deterministic_qa"), qa(3, 3, "jerk", False)],
              "review": [rev(1, 1, True), rev(2, 2, True)], "event_log": [], "payment": []}
    bus = FakeBus(tables)
    assert not oh.try_settle(bus, 7, "test") and stdb.calls == []  # 2/3, seller not done
    tables["event_log"].append({"id": 1, "type": of.EVENT_DELIVERY_COMPLETE, "payload_json": '{"offer_id": 7}'})
    assert oh.try_settle(bus, 7, "test") and stdb.calls == [("finalize_offer", 7)]
    done = {**OFFER, "status": "done", "accepted_count": 2}
    assert oh.reserve_payout(bus, done) and stdb.calls[-1] == ("reserve_dataset_payment", 7, "settle")
    tables["payment"].append({"id": 1, "dataset_offer_id": 7, "batch_ref": "settle", "status": "pending"})
    assert not oh.reserve_payout(bus, done)  # already reserved
    assert not oh.reserve_payout(bus, {**done, "accepted_count": 0})
