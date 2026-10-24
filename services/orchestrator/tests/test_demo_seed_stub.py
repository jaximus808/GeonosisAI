"""Demo switches: RDF_SEED_EVALS (seeded evals, SIM robots only) and RDF_MODAL_STUB on maincloud (allowed when
explicit, logged once as modal.stub_on_maincloud). In-memory mirror + fake writer from the TDM-67 tests."""
from __future__ import annotations

import json

import pytest

from rdf_orchestrator import evaluate as evl
from rdf_orchestrator import modal_client
from rdf_orchestrator.flywheel import PI05_BASE_CHECKPOINT

from test_tdm67_evaluate import FakeDB, make


def _db(sim: bool) -> FakeDB:
    d = FakeDB()
    d.put("robot", id=1, name="sim-yam-mujoco" if sim else "yam-1", status="online", last_heartbeat=d.us(),
          embodiment="sim_yam" if sim else "i2rt_yam", camera_config_json=json.dumps({"sim": True} if sim else {}))
    d.put("ticket", id=7, robot_id=1, status="baseline", iteration=0, task_description="cube in plate")
    d.put("run_settings", ticket_id=7, eval_trials=10)
    return d


def test_seeded_eval_on_sim_robot_finishes_without_a_robot_command():
    db = _db(sim=True)
    e = make(db, seed_evals=True)
    assert e.ensure_eval(7, why="test") == "seeded"
    run = db.rows("eval_run")[0]
    trials = [t for t in db.rows("eval_trial") if t["eval_run_id"] == run["id"]]
    assert len(trials) == 10 and all(t["failure_note"].startswith("[SEEDED] not run") for t in trials)
    assert not [c for c in db.rows("robot_command") if c["type"] == "run_eval"]  # nothing moved
    assert "eval.seeded" in db.event_types()
    e.check_run(run)
    assert db.t["eval_run"][run["id"]]["status"] == "done"
    # the same ticket draws the same outcomes (fixed seed per ticket + kind + checkpoint family)
    db2 = _db(sim=True)
    make(db2, seed_evals=True).ensure_eval(7, why="again")
    assert [t["success"] for t in db2.rows("eval_trial")] == [t["success"] for t in trials]
    # re-dispatch of a seeded run writes nothing new and logs nothing new
    n_ev = len(db.events)
    assert e.seed_eval(db.t["ticket"][7], {**run, "status": "running"}, db.t["robot"][1]) == "seeded"
    assert len(db.events) == n_ev


def test_seeded_rates_follow_the_checkpoint_family():
    db = _db(sim=True)
    db.put("run_settings", ticket_id=7, eval_trials=400)
    e = make(db, seed_evals=True, seed_base_rate=0.6, seed_ft_rate=0.95)
    run = db.put("eval_run", ticket_id=7, robot_id=1, training_run_id=0, kind="baseline",
                 checkpoint_uri=PI05_BASE_CHECKPOINT, status="running", trials=400, completed=0, successes=0)
    e.seed_eval(db.t["ticket"][7], run, db.t["robot"][1])
    base_rate = sum(t["success"] for t in db.rows("eval_trial")) / 400
    run2 = db.put("eval_run", ticket_id=7, robot_id=1, training_run_id=3, kind="candidate",
                  checkpoint_uri="fake://stub-no-gpu/tickets/7/run-3", status="running", trials=400, completed=0, successes=0)
    e.seed_eval(db.t["ticket"][7], run2, db.t["robot"][1])
    ft = [t for t in db.rows("eval_trial") if t["eval_run_id"] == run2["id"]]
    assert abs(base_rate - 0.6) < 0.08 and abs(sum(t["success"] for t in ft) / 400 - 0.95) < 0.05
    assert "fine-tuned rate 95%" in ft[0]["failure_note"]


def test_seed_evals_never_applies_to_a_real_robot():
    db = _db(sim=False)
    e = make(db, seed_evals=True)
    assert e.ensure_eval(7, why="test") == "dispatched"
    assert [c for c in db.rows("robot_command") if c["type"] == "run_eval"]
    assert not db.rows("eval_trial")
    assert "eval.seed_ignored" in db.event_types()


def test_stub_on_maincloud_is_allowed_and_logged_once(monkeypatch):
    monkeypatch.setenv("RDF_MODAL_STUB", "1")
    monkeypatch.setenv("SPACETIME_HOST", "https://maincloud.spacetimedb.com")
    monkeypatch.setattr(modal_client, "_stub_notice_sent", False)
    calls: list[tuple] = []

    class W:
        def call_reducer(self, *a):
            calls.append(a)

    import threading

    started: list[threading.Thread] = []
    real_start = threading.Thread.start

    def start(self):  # run the notice thread inline-ish: start and remember it
        started.append(self)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    modal_client.note_stub_on_shared_db(W())
    modal_client.note_stub_on_shared_db(W())  # once per process
    for t in started:
        t.join(5)
    assert [c[2] for c in calls] == ["modal.stub_on_maincloud"]
    assert json.loads(calls[0][3])["label"] == "[STUB] no GPU"
    from rdf_orchestrator.handlers import training

    training._check_stub_target()  # no longer raises on maincloud


def test_report_marks_seeded_and_stub():
    from rdf_orchestrator.report import fake_markers

    assert fake_markers({"failure_note": "[SEEDED] not run: success"}) == ["failure_note: [SEEDED] not run"]
    assert fake_markers({"checkpoint_uri": "fake://stub-no-gpu/tickets/7/run-3"}) == ["checkpoint_uri: [STUB] no GPU"]


def test_profile_for_stub_checkpoint_is_finetuned():
    pytest.importorskip("numpy")
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "robot-client" / "src"))
    try:
        from rdf_robot_client.sim_policy import profile_for
    except ImportError:
        pytest.skip("robot-client not importable from the orchestrator venv")
    assert profile_for("fake://stub-no-gpu/tickets/7/run-3").name == "finetuned"
