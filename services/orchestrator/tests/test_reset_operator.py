"""Real-robot reset asks: the operator's "Done, scene reset" button (POST /resets/{id}/done), the reset-ask text
to RDF_RESET_TEXT_CONTRIBUTOR and the DONE reply. No network, nothing texted (a recording transport)."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import reset_api
from rdf_orchestrator import reset_done as rdone
from rdf_orchestrator import text_offers as tx
from rdf_orchestrator.robot_commands_api import get_operator_key, get_robot_commands_stdb

from test_tdm113_text_offers import Tdm113Market, make, opt, req, task

KEY = "op-key-test"
PHONE = "4155550177"
E164 = "+14155550177"


def reset_task(i: int = 30, status: str = "open", trial_id: int = 7, **kw) -> dict:
    return task(i, "reset", 200, status=status,
                instructions=f"Reset the scene for: Pick up the cube. Pays $2.00. [ref eval_trial:{trial_id}]", **kw)


class EvalMarket(Tdm113Market):
    """A real arm (sim=False) with one eval run of 3 trials; trial 7 is index 1 (the 2nd)."""

    def __init__(self, tasks: list[dict]) -> None:
        super().__init__(tasks, sim=False)
        self.calls: list[tuple] = []

    def rows(self, table: str) -> list[dict]:
        if table == "eval_trial":
            return [{"id": 7, "eval_run_id": 3, "trial_index": 1}]
        if table == "eval_run":
            return [{"id": 3, "trials": 3}]
        return super().rows(table)

    def call(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name == "complete_task":
            self.tasks[args[0]]["status"] = "completed"


# --------------------------------------------------------------------------- plan

def test_plan_open_claimed_done_and_no_contributor():
    assert [s[0] for s in rdone.plan(reset_task(), 1, via="t")] == ["claim_task", "complete_task"]
    assert rdone.plan(reset_task(), 1, via="t")[0] == ("claim_task", 30, 1)
    assert [s[0] for s in rdone.plan(reset_task(status="claimed"), 1, via="t")] == ["complete_task"]
    assert rdone.plan(reset_task(status="completed"), 1, via="t") == []
    fb = rdone.plan(reset_task(), 0, via="t")
    assert [s[0] for s in fb] == ["cancel_task", "issue_robot_command"]
    assert fb[1][2] == "resume_policy" and json.loads(fb[1][3])["ref"] == "eval_trial:7"
    try:
        rdone.plan(task(9), 1, via="t")
        raise AssertionError("a non-reset task must be refused")
    except ValueError:
        pass


def test_refused_claim_falls_back_to_cancel_and_resume():
    calls: list[tuple] = []

    def call(name, *a):
        if name == "claim_task":
            raise RuntimeError("contributor 1 already holds a claimed task")
        calls.append((name, *a))

    out = rdone.run_sync(call, reset_task(), 1, via="t")
    assert out["mode"] == "cancelled_and_resumed"
    assert [c[0] for c in calls] == ["cancel_task", "issue_robot_command"]


def test_trial_words():
    m = EvalMarket([reset_task()])
    assert rdone.trial_words(rdone.trial_of(reset_task(), m.rows)) == "after trial 2/3"
    assert rdone.trial_words(None) == ""
    assert rdone.tkt(42) == "TKT-0042"


# --------------------------------------------------------------------------- the cockpit button

class FakeStdb:
    def __init__(self, tasks: dict[int, dict]) -> None:
        self.tasks = tasks
        self.calls: list[tuple] = []

    async def sql(self, q: str) -> list[dict]:
        tid = int(q.rsplit("=", 1)[1])
        return [dict(self.tasks[tid])] if tid in self.tasks else []

    async def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))


def client(stdb: FakeStdb, cid: int = 1, key: str | None = KEY) -> TestClient:
    app = FastAPI()
    app.include_router(reset_api.reset_router)
    app.dependency_overrides[get_robot_commands_stdb] = lambda: stdb
    app.dependency_overrides[get_operator_key] = lambda: key
    app.dependency_overrides[reset_api.get_reset_contributor] = lambda: cid
    return TestClient(app)


def test_done_route_claims_and_completes_as_the_operator():
    st = FakeStdb({30: reset_task()})
    r = client(st).post("/resets/30/done", headers={"x-rdf-operator-key": KEY})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "completed_as_contributor"
    names = [c[0] for c in st.calls]
    assert names == ["claim_task", "complete_task", "append_event"]
    assert st.calls[0] == ("claim_task", 30, 1)
    assert st.calls[2][2] == rdone.EV_DONE and json.loads(st.calls[2][3])["via"] == "cockpit"


def test_done_route_needs_the_operator_key_and_a_reset():
    st = FakeStdb({30: reset_task(), 9: task(9)})
    c = client(st)
    assert c.post("/resets/30/done").status_code == 401
    assert c.post("/resets/30/done", headers={"x-rdf-operator-key": "nope"}).status_code == 401
    assert c.post("/resets/9/done", headers={"x-rdf-operator-key": KEY}).status_code == 409
    assert c.post("/resets/99/done", headers={"x-rdf-operator-key": KEY}).status_code == 404
    assert client(st, key=None).post("/resets/30/done", headers={"x-rdf-operator-key": KEY}).status_code == 503
    assert st.calls == []


def test_done_route_on_a_finished_reset_is_a_no_op():
    st = FakeStdb({30: reset_task(status="completed")})
    r = client(st).post("/resets/30/done", headers={"x-rdf-operator-key": KEY})
    assert r.status_code == 200 and r.json()["already"] == "completed"
    assert st.calls == []


# --------------------------------------------------------------------------- the text

def make_ops(market: EvalMarket, cid: int = 1):
    o, m, tr, ev, clock = make(market, rdf_reset_text_contributor=cid)
    o._reset_call = market.call
    return o, m, tr, ev, clock


def test_real_reset_texts_the_operator_with_ticket_trial_and_link():
    m = EvalMarket([reset_task()])
    o, _, tr, ev, _ = make_ops(m)
    opt(o, 1, PHONE, in_person=False)  # not marked in person: the operator still gets the ask
    tr.sent.clear()
    out = o.on_page(req([30], "reset", 200), m.rows)
    assert out["text"] == "reset_ask" and out["trial"] == "after trial 2/3"
    (to, body), = tr.sent
    assert to == E164
    assert body.startswith("Reset the scene for TKT-0004 after trial 2/3: put the cube back, then reply DONE or tap https://")
    assert "/j/30/text" in body
    assert any(t == tx.EV_RESET_ASK for t, _ in ev)
    assert "0177" not in json.dumps(ev), "a phone number reached the event log"


def test_done_reply_claims_and_completes_the_reset():
    m = EvalMarket([reset_task()])
    o, _, tr, ev, _ = make_ops(m)
    opt(o, 1, PHONE)
    o.on_page(req([30], "reset", 200), m.rows)
    tr.sent.clear()
    out = o.on_inbound(E164, "Done!")
    assert out == {"action": "reset_done", "task_id": 30, "mode": "completed_as_contributor"}
    assert m.claims == [(30, 1)] and m.calls[0][0] == "complete_task"
    assert tr.to(E164) == [tx.RESET_THANKS]
    assert any(t == tx.EV_RESET_DONE for t, _ in ev)
    # a second DONE: nothing left to reset
    assert o.on_inbound(E164, "done")["action"] == "no_reset"


def test_no_operator_setting_keeps_the_old_in_person_rule():
    m = EvalMarket([reset_task()])
    o, _, tr, _, _ = make_ops(m, cid=0)
    opt(o, 1, PHONE, in_person=False)
    tr.sent.clear()
    out = o.on_page(req([30], "reset", 200), m.rows)
    assert out["text"] == "none" and tr.sent == []


def test_sim_resets_are_never_asked():
    m = Tdm113Market([reset_task()], sim=True)
    o, _, tr, _, _ = make(m, rdf_reset_text_contributor=1)
    opt(o, 1, PHONE)
    tr.sent.clear()
    o.on_page(req([30], "reset", 200), m.rows)
    assert tr.sent == []


def test_other_in_person_contributors_still_get_the_normal_offer():
    m = EvalMarket([reset_task()])
    o, _, tr, _, _ = make_ops(m)
    opt(o, 1, PHONE)
    opt(o, 3, "4155550103", in_person=True)
    tr.sent.clear()
    o.on_page(req([30], "reset", 200), m.rows)
    assert len(tr.to(E164)) == 1 and tr.to(E164)[0].startswith("Reset the scene")
    assert len(tr.to("+14155550103")) == 1 and "Reply YES" in tr.to("+14155550103")[0]
