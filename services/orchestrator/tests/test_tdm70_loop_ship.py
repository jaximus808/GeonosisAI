"""TDM-70: the loop controller's ship step and wait logging, on the TDM-22 in-memory fake DB.

Ship = set ticket.deployed_checkpoint_uri (mark_checkpoint_deployed), point the policy server at
it (/policy/activate) and save the TDM-71 report; each part idempotent across restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging

from tdm22_fake_stdb import FakeClient, FakeDB
from test_tdm22_flywheel import seed, statuses, ticket

from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.flywheel import Decision, Flywheel, ShipStep, TicketView
from rdf_orchestrator.flywheel_sim import SimScript, install_sim
from rdf_orchestrator.handlers.flywheel import register_flywheel


class Recorder:
    def __init__(self, fail_activate: bool = False):
        self.activations: list[tuple[str, dict]] = []
        self.reports: list[int] = []
        self.fail_activate = fail_activate

    def activate(self, url: str, body: dict, timeout: float) -> None:
        self.activations.append((url, body))
        if self.fail_activate:
            raise RuntimeError("HTTP 503: cold start")

    async def report(self, bus, ticket_id: int) -> dict:
        self.reports.append(ticket_id)  # like report_api.save_report: it logs report.saved
        await bus.call_reducer("append_event", ["orchestrator:report", "report.saved", json.dumps({"ticket_id": ticket_id})])
        return {"uri": f"modal-vol://rdf-media/reports/test/ticket-{ticket_id}/report.md"}


def ship_step(rec: Recorder, url: str | None = "https://policy.test") -> ShipStep:
    return ShipStep(activate=rec.activate, reporter=rec.report, policy_server_url=url if url else "", confirm_s=2.0)


def servable(db: FakeDB) -> None:
    """Make the sim's checkpoints look real (modal-vol://) so the /policy/activate path runs."""
    orig = db.r_set_training_status

    def patched(run_id, status, modal_call_id, checkpoint_uri, metrics_json, gpu_cost_cents):
        if checkpoint_uri and checkpoint_uri.startswith("sim://"):
            checkpoint_uri = "modal-vol://rdf-checkpoints/runs/" + checkpoint_uri.removeprefix("sim://")
        orig(run_id, status, modal_call_id, checkpoint_uri, metrics_json, gpu_cost_cents)
    db.r_set_training_status = patched


async def run_until(db: FakeDB, step: ShipStep, until, script: SimScript | None = None, timeout=10.0) -> None:
    reg = Registry()
    fw = install_sim(Flywheel({"shipped": step}, reconcile_s=0.2), script or SimScript(), replace_external=True,
                     host="fake")
    register_flywheel(reg, fw)
    bus = Bus(reg, FakeClient(db), workers=2)
    await bus.start()
    end = asyncio.get_running_loop().time() + timeout
    try:
        while not until():
            assert asyncio.get_running_loop().time() < end, f"timed out; statuses={statuses(db)} errors={db.errors}"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
    finally:
        fw.detach()
        await bus.stop()


def _deployed_and_reported(db, rec):
    return lambda: bool(ticket(db).get("deployed_checkpoint_uri")) and rec.reports


def test_two_iterations_ship_sets_deployed_checkpoint_activates_policy_and_saves_report():
    db = FakeDB()
    servable(db)
    seed(db)
    rec = Recorder()
    asyncio.run(run_until(db, ship_step(rec), _deployed_and_reported(db, rec),
                          SimScript(baseline_successes=2, candidate_successes=(5, 9))))
    t = ticket(db)
    runs = sorted(db.t["training_run"].values(), key=lambda r: r["id"])
    assert t["status"] == "shipped" and t["iteration"] == 1
    # the loop went back through diagnose (re-cluster the new checkpoint's failures), not straight to dispatch
    assert statuses(db)[statuses(db).index("eval") + 1] == "diagnose"
    assert t["deployed_checkpoint_uri"] == runs[1]["checkpoint_uri"]
    dep = db.events("ticket.checkpoint_deployed")
    cand = [r for r in db.t["eval_run"].values() if r["kind"] == "candidate" and r["training_run_id"] == runs[1]["id"]]
    assert len(dep) == 1 and dep[0]["payload"]["eval_run_id"] == cand[0]["id"]
    assert rec.activations == [("https://policy.test/policy/activate",
                                {"checkpoint_uri": runs[1]["checkpoint_uri"], "ticket_id": 1, "robot_id": 1,
                                 "kind": "deployed"})]
    assert len(db.events("flywheel.policy_activated")) == 1
    assert rec.reports == [1]
    assert not [e for e in db.errors if e[0] == "mark_checkpoint_deployed"], db.errors

    # restart: the shipped on_enter fires again for the existing row; nothing is repeated
    asyncio.run(run_until(db, ship_step(rec), lambda: True))
    assert len(db.events("ticket.checkpoint_deployed")) == 1 and len(db.events("flywheel.shipped")) == 1
    assert len(rec.activations) == 1 and rec.reports == [1]


def test_fake_checkpoint_is_not_activated_and_failed_activation_is_logged():
    db = FakeDB()
    seed(db)
    rec = Recorder()
    asyncio.run(run_until(db, ship_step(rec), _deployed_and_reported(db, rec),
                          SimScript(baseline_successes=2, candidate_successes=(9,))))
    skipped = db.events("flywheel.policy_activate_skipped")  # URL set, but sim:// is never sent to a GPU
    assert rec.activations == [] and len(skipped) == 1 and "simulated" in skipped[0]["payload"]["note"]

    db2 = FakeDB()
    servable(db2)
    seed(db2)
    rec2 = Recorder(fail_activate=True)
    asyncio.run(run_until(db2, ship_step(rec2), _deployed_and_reported(db2, rec2),
                          SimScript(baseline_successes=2, candidate_successes=(9,))))
    failed = db2.events("flywheel.policy_activate_failed")
    assert len(failed) == 1 and "503" in failed[0]["payload"]["error"]
    assert ticket(db2)["deployed_checkpoint_uri"]  # the deployment record does not depend on the server


def test_mark_checkpoint_deployed_refuses_unshipped_ticket():
    db = FakeDB()
    seed(db)
    db.call("mark_checkpoint_deployed", [1, 1, 1, "too early"])
    assert db.errors and "only a shipped ticket" in db.errors[-1][2]


def _view(status="dispatch", iteration=0, reason="collecting: 0/3"):
    return TicketView(ticket={"id": 1, "status": status, "iteration": iteration}), Decision(None, reason)


def test_wait_log_is_once_per_stage_then_rate_limited(caplog):
    fw = Flywheel(wait_log_s=60.0)
    caplog.set_level(logging.INFO)

    def infos():
        return [r for r in caplog.records if r.getMessage() == "flywheel.wait" and r.levelno == logging.INFO]

    for n in range(3):  # count changes within one stage: logged once
        v, d = _view(reason=f"collecting: {n}/3")
        fw._log_wait(v, d, "episode")
    assert len(infos()) == 1
    v, d = _view(status="review", reason="reviewing: 0/3 accepted")  # new stage: logged
    fw._log_wait(v, d, "ticket")
    assert len(infos()) == 2
    fw.wait_log_s = 0.0  # interval elapsed: the next reason change is logged, with the suppressed count
    v, d = _view(status="review", reason="reviewing: 1/3 accepted")
    fw._log_wait(v, d, "episode")
    assert len(infos()) == 3
    fw._log_wait(v, d, "tick")  # same reason: never logged again
    assert len(infos()) == 3


class DbMirror:
    """bus.rows/bus.get over a FakeDB (what Flywheel.timeline and gather_inputs read)."""

    def __init__(self, db: FakeDB):
        self.db = db

    def rows(self, table):
        return list(self.db.t.get(table, {}).values())

    def get(self, table, key):
        return self.db.t.get(table, {}).get(key)


def test_timeline_and_planner_previous_plan_effect_after_two_iterations():
    from rdf_orchestrator.planner import gather_inputs

    db = FakeDB()
    seed(db)
    rec = Recorder()
    asyncio.run(run_until(db, ship_step(rec), _deployed_and_reported(db, rec),
                          SimScript(baseline_successes=2, candidate_successes=(5, 9))))
    m = DbMirror(db)
    tl = Flywheel().timeline(m, 1)
    assert tl["status"] == "shipped" and tl["deployed_checkpoint_uri"] == "sim://ckpt/ticket1/iter1"
    it0, it1 = tl["iterations"]
    assert (it0["eval_before"]["successes"], it0["eval_after"]["successes"], it0["met_threshold"]) == (2, 5, False)
    assert (it1["eval_before"]["successes"], it1["eval_after"]["successes"], it1["met_threshold"]) == (5, 9, True)
    assert it0["data_plan"]["data_type"] == "recovery_takeover" and it1["training_run"]["status"] == "done"
    types = [e["type"] for e in tl["events"]]
    assert "ticket.checkpoint_deployed" in types and types.index("ticket.checkpoint_deployed") > types.index("flywheel.shipped")
    assert [e["to"] for e in tl["events"] if e["type"] == "ticket.status_changed"][-1] == "shipped"

    # what the planner would have seen for iteration 1: plan 0 and its effect (2/10 -> 5/10)
    t = dict(ticket(db))
    inp = gather_inputs(m.rows, {**t, "iteration": 1})
    p0 = next(p for p in inp.previous_plans if p["iteration"] == 0)
    assert p0["effect"]["before"]["successes"] == 2 and p0["effect"]["after"]["successes"] == 5
    assert p0["effect"]["success_rate_change"] == 0.3
