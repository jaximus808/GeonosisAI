"""TDM-22 flywheel state machine against an in-memory fake SpacetimeDB (no network).

The fake robot / agents are ``flywheel_sim`` (SIMULATED rows); the machine under test is the real
``Flywheel`` + its bus handlers + the real ``Bus``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient
from tdm22_fake_stdb import FakeClient, FakeDB

from rdf_orchestrator.app import create_app
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.flywheel import Decision, Flywheel, TicketView, decide
from rdf_orchestrator.flywheel_sim import SimScript, install_sim
from rdf_orchestrator.handlers.flywheel import register_flywheel


def seed(db: FakeDB, *, budget=10_000, adapter="", **overrides) -> int:
    db.call("register_robot", ["yam-1", "yam", adapter, "{}"])
    s = {"eval_trials": 10, "target_episodes": 3, "max_iterations": 3, **overrides}
    db.call("create_ticket", [1, "put the cup in the bin", 0.8, budget, json.dumps(s)])
    assert not db.errors, db.errors
    return 1


def statuses(db: FakeDB) -> list[str]:
    return [e["payload"]["to"] for e in db.events("ticket.status_changed")]


async def run_flywheel(db: FakeDB, script: SimScript, until, timeout=10.0) -> Flywheel:
    reg = Registry()
    fw = install_sim(Flywheel(reconcile_s=0.2), script, replace_external=True, host="fake")
    register_flywheel(reg, fw)
    bus = Bus(reg, FakeClient(db), workers=2)
    await bus.start()
    end = time.monotonic() + timeout
    try:
        while not until():
            assert time.monotonic() < end, f"timed out; statuses={statuses(db)} errors={db.errors}"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)  # let the step for the final state (ShipStep) land
    finally:
        fw.detach()
        await bus.stop()
    return fw


def ticket(db, tid=1):
    return db.t["ticket"][tid]


def test_two_iterations_to_shipped():
    db = FakeDB()
    tid = seed(db)
    asyncio.run(run_flywheel(db, SimScript(baseline_successes=2, candidate_successes=(5, 9)),
                             lambda: ticket(db)["status"] in ("shipped", "failed")))
    t = ticket(db)
    assert t["status"] == "shipped" and t["iteration"] == 1, statuses(db)
    assert statuses(db) == [
        "onboarding", "baseline", "diagnose", "plan_data", "dispatch", "review",
        "dispatch", "review",  # iteration 0: the sim reviewer rejected one episode -> collect one more
        "train", "eval",
        "diagnose", "plan_data", "dispatch", "review", "train", "eval",  # iteration 1
        "shipped",
    ]
    # every transition carries its reason, and a flywheel.transition event with the evidence
    notes = [e["payload"]["note"] for e in db.events("ticket.status_changed")]
    assert all(notes) and "looping to iteration 1" in notes[10] and "9/10" in notes[-1]
    tr = db.events("flywheel.transition")
    assert len(tr) == len(notes) and tr[-1]["payload"]["evidence"]["successes"] == 9
    runs = sorted(db.t["training_run"].values(), key=lambda r: r["id"])
    assert [(r["iteration"], r["status"]) for r in runs] == [(0, "done"), (1, "done")]
    assert len(runs[1]["episode_ids"]) == 6  # the per-ticket dataset: 3 accepted per iteration
    shipped = db.events("flywheel.shipped")
    assert len(shipped) == 1 and shipped[0]["payload"]["final"]["successes"] == 9
    assert shipped[0]["payload"]["baseline"]["successes"] == 2
    assert not db.errors, db.errors


def test_restart_mid_training_resumes_without_requeue():
    db = FakeDB()
    seed(db)
    # first process: stops while the fine-tune is running (fake Modal never reports done)
    asyncio.run(run_flywheel(db, SimScript(candidate_successes=(9,), complete_training=False),
                             lambda: any(r["status"] == "running" for r in db.t["training_run"].values())))
    assert ticket(db)["status"] == "train"
    calls_before = [c for c in db.calls if c[0] == "queue_training_run"]
    assert len(calls_before) == 1

    # second process: fresh memory, state derived from the rows in the snapshot
    asyncio.run(run_flywheel(db, SimScript(candidate_successes=(9,)), lambda: ticket(db)["status"] in ("shipped", "failed")))
    assert ticket(db)["status"] == "shipped" and ticket(db)["iteration"] == 0
    assert [c for c in db.calls if c[0] == "queue_training_run"] == calls_before  # never re-queued
    assert len(db.t["training_run"]) == 1
    assert statuses(db)[-3:] == ["train", "eval", "shipped"]
    assert not db.errors, db.errors


def test_max_iterations_stops_with_logged_reason():
    db = FakeDB()
    seed(db, max_iterations=2)
    asyncio.run(run_flywheel(db, SimScript(candidate_successes=(5, 6)), lambda: ticket(db)["status"] in ("shipped", "failed")))
    t = ticket(db)
    assert t["status"] == "failed" and t["iteration"] == 1
    last = db.events("ticket.status_changed")[-1]["payload"]
    assert last["from"] == "eval" and "max_iterations 2 reached" in last["note"]
    assert db.events("flywheel.transition")[-1]["payload"]["evidence"]["stop"] == "max_iterations"
    assert not db.errors, db.errors  # the machine never asked the reducer for the refused loop-back


def test_budget_stops_with_logged_reason():
    db = FakeDB()
    seed(db, budget=2000)  # plan 3 x 500c = 1500c approved; GPU 2500c blows the cap
    asyncio.run(run_flywheel(db, SimScript(candidate_successes=(5,), gpu_cost_cents=2500),
                             lambda: ticket(db)["status"] in ("shipped", "failed")))
    t = ticket(db)
    assert t["status"] == "failed" and t["iteration"] == 0
    last = db.events("ticket.status_changed")[-1]["payload"]
    assert last["from"] == "eval" and "budget exhausted" in last["note"]
    ev = db.events("flywheel.transition")[-1]["payload"]["evidence"]
    assert ev["stop"] == "budget" and ev["remaining_cents"] == -500


def test_stub_steps_wait_and_manual_advance_is_logged_as_human():
    db = FakeDB()
    seed(db)
    reg, fw = Registry(), Flywheel(reconcile_s=60)  # no sim: onboarding etc. are NotImplementedStep
    register_flywheel(reg, fw)
    fc = FakeClient(db)
    app = create_app(client=fc, registry=reg, workers=1)
    app.state.flywheel = fw
    with TestClient(app) as c:
        for _ in range(200):
            if ticket(db)["status"] == "onboarding":
                break
            threading.Event().wait(0.01)
        assert ticket(db)["status"] == "onboarding"  # created -> onboarding automatically, then waits
        d = c.get("/flywheel/tickets/1").json()
        assert d["decision"]["to"] is None and "onboarding" in d["decision"]["reason"]
        steps = {s["stage"]: s for s in c.get("/flywheel/steps").json()}
        assert steps["onboarding"]["wired"] is False and steps["train"]["wired"] is True

        r = c.post("/tickets/1/advance", json={"reason": "adapter checked by hand", "by": "jaxon"})
        assert r.status_code == 202, r.text
        assert r.json()["to"] == "baseline" and r.json()["observed_status"] == "baseline"
        assert c.post("/tickets/1/advance", json={"to": "shipped"}).status_code == 409
        assert c.post("/tickets/99/advance", json={}).status_code == 404
        fw.detach()
    tr = db.events("flywheel.transition")[-1]
    assert tr["actor"] == "human:jaxon" and tr["payload"]["manual"] is True
    assert "manual advance by human:jaxon: adapter checked by hand" in db.events("ticket.status_changed")[-1]["payload"]["note"]


def _view(**kw) -> TicketView:
    t = {"id": 1, "robot_id": 1, "status": "review", "iteration": 0, "budget_cents": 5000, "spent_cents": 0,
         "target_success": 0.8, **kw.pop("ticket", {})}
    return TicketView(ticket=t, settings={"max_iterations": 3, "success_threshold": 0.8}, **kw)


def test_decide_review_rules():
    plan = {"id": 7, "iteration": 0, "status": "approved", "amount": 2, "created_at": 10, "data_type": "clean_teleop"}
    ep = lambda i, s: {"id": i, "kind": "clean_teleop", "review_status": s, "created_at": 20}  # noqa: E731
    assert decide(_view(plans=[plan], episodes=[ep(1, "accepted"), ep(2, "pending")])).to is None
    assert decide(_view(plans=[plan], episodes=[ep(1, "accepted"), ep(2, "rejected")])).to == "dispatch"
    assert decide(_view(plans=[plan], episodes=[ep(1, "accepted"), ep(2, "rejected"), ep(3, "accepted")])).to == "train"
    # episodes from before this iteration's plan don't count
    old = {"id": 9, "kind": "clean_teleop", "review_status": "accepted", "created_at": 5}
    assert decide(_view(plans=[plan], episodes=[old, ep(1, "accepted")])).to is None


def test_decide_eval_ship_loop_and_ci_rule():
    run = {"id": 3, "iteration": 0, "status": "done", "checkpoint_uri": "ck", "gpu_cost_cents": 0}
    ev = lambda s: {"id": 4, "kind": "candidate", "status": "done", "training_run_id": 3, "successes": s, "trials": 10}  # noqa: E731
    d: Decision = decide(_view(ticket={"status": "eval"}, training_runs=[run], eval_runs=[ev(8)]))
    assert d.to == "shipped" and "point estimate: met" in d.reason
    assert decide(_view(ticket={"status": "eval"}, training_runs=[run], eval_runs=[ev(7)])).to == "diagnose"
    v = _view(ticket={"status": "eval"}, training_runs=[run], eval_runs=[ev(10)])
    v.settings["require_ci_low"] = True  # 10/10 has ci_low ~0.72 < 0.8
    assert decide(v).to == "diagnose"
