"""TDM-67 eval orchestration against an in-memory mirror + fake reducer writer (no network).

The fake writer applies the reducers ``evaluate.py`` calls to the same tables it reads, with the
rules that matter here (job lock, finalize needs every trial row, abort only a live run). Trial
outcomes are written by the test, standing in for the robot; nothing here is an eval result.
"""
from __future__ import annotations

import json
import time

import pytest

from rdf_orchestrator import evaluate as evl
from rdf_orchestrator.flywheel import PI05_BASE_CHECKPOINT
from rdf_orchestrator.stats import wilson_interval

NOW = 1_800_000_000.0


class FakeDB:
    def __init__(self):
        self.t: dict[str, dict] = {name: {} for name in evl.TABLES}
        self.calls: list[tuple] = []
        self.events: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}
        self.now = NOW

    def us(self) -> int:
        return int(self.now * 1e6)

    # mirror
    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def put(self, table, **row):
        if "id" not in row:
            row["id"] = max(self.t[table], default=0) + 1
        key = row["ticket_id"] if table == "run_settings" else row["id"]
        self.t[table][key] = row
        return row

    # writer
    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(self.fail[name])
        getattr(self, f"r_{name}")(*args)

    def r_append_event(self, actor, type_, payload):
        p = json.loads(payload)
        self.events.append((type_, p))
        self.put("event_log", actor=actor, type=type_, payload_json=payload)

    def r_start_eval_run(self, ticket_id, ckpt, trials):
        tk = self.t["ticket"][ticket_id]
        n = trials or self.t["run_settings"][ticket_id]["eval_trials"]
        src = next((r for r in self.rows("training_run") if r["ticket_id"] == ticket_id and r["status"] == "done"
                    and r["checkpoint_uri"] == ckpt), None)
        self.put("eval_run", ticket_id=ticket_id, robot_id=tk["robot_id"], training_run_id=src["id"] if src else 0,
                 kind="candidate" if src else "baseline", checkpoint_uri=ckpt, status="running", trials=n,
                 completed=0, successes=0, ci_low=0.0, ci_high=0.0, created_at=self.us())

    def r_issue_robot_command(self, robot_id, type_, payload, ticket_id):
        if type_ in evl.JOB_TYPES and any(c["robot_id"] == robot_id and c["status"] in ("issued", "acked")
                                          and c["type"] in evl.JOB_TYPES for c in self.rows("robot_command")):
            raise RuntimeError("robot busy")
        self.put("robot_command", robot_id=robot_id, ticket_id=ticket_id, type=type_, payload_json=payload,
                 status="issued", result_json=None, issued_at=self.us())

    def r_record_eval_trial(self, run_id, index, success, duration_ms, episode_id, note):
        run = self.t["eval_run"][run_id]
        assert run["status"] == "running"
        assert all(t["trial_index"] != index for t in self.rows("eval_trial") if t["eval_run_id"] == run_id)
        self.put("eval_trial", eval_run_id=run_id, trial_index=index, success=success, duration_ms=duration_ms,
                 episode_id=episode_id, failure_note=note, created_at=self.us())
        rows = [t for t in self.rows("eval_trial") if t["eval_run_id"] == run_id]
        run.update(completed=len(rows), successes=sum(t["success"] for t in rows))

    def r_finalize_eval_run(self, run_id, successes, trials, low, high, summary):
        run = self.t["eval_run"][run_id]
        rows = [t for t in self.rows("eval_trial") if t["eval_run_id"] == run_id]
        if run["status"] != "running":
            raise RuntimeError(f"eval run {run_id} is {run['status']}")
        if len(rows) < run["trials"] or trials != run["trials"]:
            raise RuntimeError("cannot finalize: missing trial rows")
        assert successes == sum(t["success"] for t in rows)
        run.update(status="done", ci_low=low, ci_high=high)

    def r_abort_eval_run(self, run_id, status, reason):
        run = self.t["eval_run"][run_id]
        if run["status"] not in ("queued", "running"):
            raise RuntimeError(f"eval run {run_id} is {run['status']}")
        run.update(status=status)
        self.put("event_log", actor="orchestrator", type="eval.aborted",
                 payload_json=json.dumps({"eval_run_id": run_id, "ticket_id": run["ticket_id"], "reason": reason}))

    def r_cancel_task(self, task_id, reason):
        self.t["task"][task_id]["status"] = "cancelled"

    def r_claim_task(self, task_id, cid):
        self.t["task"][task_id].update(status="claimed", assignee_id=cid)

    def r_complete_task(self, task_id, result):
        self.t["task"][task_id]["status"] = "completed"

    # helpers
    def names(self):
        return [c[0] for c in self.calls if c[0] != "append_event"]

    def event_types(self):
        return [e[0] for e in self.events]


@pytest.fixture
def db():
    d = FakeDB()
    d.put("robot", id=1, name="yam-1", status="online", last_heartbeat=d.us())
    d.put("ticket", id=1, robot_id=1, status="baseline", iteration=0, task_description="cup in bin")
    d.put("run_settings", ticket_id=1, eval_trials=10)
    return d


def make(db, **cfg) -> evl.EvalOrchestrator:
    c = evl.EvalConfig(watchdog_s=0, **cfg)
    return evl.EvalOrchestrator(db, lambda: db, c, clock=lambda: db.now, mirror_wait_s=0.0)


def run_eval_cmds(db):
    return [c for c in db.rows("robot_command") if c["type"] == "run_eval"]


def add_trials(db, run_id, outcomes):
    have = len([t for t in db.rows("eval_trial") if t["eval_run_id"] == run_id])
    for i, ok in enumerate(outcomes, start=have):
        db.r_record_eval_trial(run_id, i, ok, 1000, 0, "")


def test_baseline_starts_eval_and_commands_robot_once(db):
    e = make(db)
    assert e.ensure_eval(1, "test") == "dispatched"
    runs = db.rows("eval_run")
    assert len(runs) == 1 and runs[0]["checkpoint_uri"] == PI05_BASE_CHECKPOINT and runs[0]["trials"] == 10
    (cmd,) = run_eval_cmds(db)
    p = json.loads(cmd["payload_json"])
    assert p["eval_run_id"] == runs[0]["id"] and p["trials"] == 10 and p["checkpoint_uri"] == PI05_BASE_CHECKPOINT
    assert p["timeout_s"] == 120.0
    assert "eval.policy_activate_skipped" in db.event_types() and "eval.dispatched" in db.event_types()
    assert e.ensure_eval(1, "again") == "in_progress"
    assert len(db.rows("eval_run")) == 1 and len(run_eval_cmds(db)) == 1


def test_waits_for_offline_stale_or_busy_robot(db):
    e = make(db)
    db.t["robot"][1]["status"] = "offline"
    assert e.ensure_eval(1) == "waiting_for_robot"
    db.t["robot"][1].update(status="online", last_heartbeat=db.us() - 120 * 10**6)
    assert e.ensure_eval(1) == "waiting_for_robot"
    db.t["robot"][1]["last_heartbeat"] = db.us()
    db.put("robot_command", robot_id=1, ticket_id=0, type="start_teleop", payload_json="{}", status="acked", issued_at=db.us())
    assert e.ensure_eval(1) == "robot_busy"
    assert db.names() == []


def test_policy_activate_called_and_failure_blocks_start(db):
    seen = []
    e = make(db, policy_server_url="http://policy:9000")
    e.activate = lambda url, body, timeout: (_ for _ in ()).throw(RuntimeError("503"))
    assert e.ensure_eval(1) == "policy_activate_failed"
    assert db.rows("eval_run") == [] and "eval.policy_activate_failed" in db.event_types()
    assert e.ensure_eval(1) == "backoff"
    e._backoff.clear()
    e.activate = lambda url, body, timeout: seen.append((url, body))
    assert e.ensure_eval(1) == "dispatched"
    assert seen == [("http://policy:9000/policy/activate", {"checkpoint_uri": PI05_BASE_CHECKPOINT, "ticket_id": 1,
                                                            "robot_id": 1, "kind": "baseline"})]


def test_candidate_uses_latest_finished_training_run(db):
    db.t["ticket"][1].update(status="eval", iteration=1)
    db.put("training_run", ticket_id=1, iteration=1, status="done", checkpoint_uri="modal-vol://ck/1", gpu_cost_cents=0)
    db.put("training_run", ticket_id=1, iteration=1, status="done", checkpoint_uri="modal-vol://ck/2", gpu_cost_cents=0)
    e = make(db)
    assert e.ensure_eval(1) == "dispatched"
    (run,) = db.rows("eval_run")
    assert run["kind"] == "candidate" and run["checkpoint_uri"] == "modal-vol://ck/2" and run["training_run_id"] == 2


def test_no_training_run_waits(db):
    db.t["ticket"][1]["status"] = "eval"
    assert make(db).ensure_eval(1) == "no_checkpoint"


def test_finalizes_only_with_every_trial_row(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    add_trials(db, run["id"], [True, False, True, True, False, True, True, False, True])
    assert e.check_run(run) == "running"
    assert "finalize_eval_run" not in db.names()
    add_trials(db, run["id"], [True])
    assert e.check_run(run) == "finalized"
    (call,) = [c for c in db.calls if c[0] == "finalize_eval_run"]
    lo, hi = wilson_interval(7, 10)
    assert call[1:6] == (run["id"], 7, 10, lo, hi) and call[6].startswith("7/10, 95% CI")
    assert db.t["eval_run"][run["id"]]["status"] == "done"
    assert e.check_run(run) == "not_running"
    assert e.ensure_eval(1) == "already_done"


def test_robot_offline_mid_eval_aborts_then_reruns(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    db.t["robot_command"][1]["status"] = "acked"
    add_trials(db, run["id"], [True, False, True])
    db.t["robot"][1]["status"] = "offline"
    assert e.check_run(run) == "aborted"
    assert db.t["eval_run"][run["id"]]["status"] == "cancelled"
    (call,) = [c for c in db.calls if c[0] == "abort_eval_run"]
    assert call[2] == "cancelled" and "robot lost mid-eval" in call[3] and "3/10" in call[3]
    # The robot client comes back: it fails the stale acked command, heartbeats online.
    db.t["robot_command"][1]["status"] = "failed"
    db.t["robot"][1].update(status="online", last_heartbeat=db.us())
    e.kick_robot(1, "robot back")
    runs = db.rows("eval_run")
    assert len(runs) == 2 and runs[1]["status"] == "running"
    assert json.loads(run_eval_cmds(db)[-1]["payload_json"])["eval_run_id"] == runs[1]["id"]


def test_stale_heartbeat_aborts(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    db.now += 31
    assert e.check_run(run) == "aborted"
    assert "has not heartbeated" in [c for c in db.calls if c[0] == "abort_eval_run"][0][3]


def test_command_failed_marks_failed(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    db.t["robot_command"][1].update(status="failed", result_json=json.dumps({"error": "camera unplugged"}))
    assert e.check_run(run) == "aborted"
    assert db.t["eval_run"][run["id"]]["status"] == "failed"


def test_estop_abort_waits_for_rerun_request(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    db.t["robot_command"][1].update(status="preempted", result_json=json.dumps({"preempted_by": 9}))
    assert e.check_run(run) == "aborted"
    assert e.ensure_eval(1) == "awaiting_rerun_request"
    db.r_append_event("human:web", "eval.rerun_requested", json.dumps({"ticket_id": 1}))
    assert e.ensure_eval(1) == "dispatched"
    assert len(db.rows("eval_run")) == 2


def test_too_many_aborts_wait_for_human(db):
    e = make(db, max_aborts=2)
    for _ in range(2):
        assert e.ensure_eval(1) == "dispatched"
        run = [r for r in db.rows("eval_run") if r["status"] == "running"][0]
        cmd = run_eval_cmds(db)[-1]
        cmd["status"] = "done"  # robot finished without writing trial rows
        e._guard.clear()
        assert e.check_run(run) == "aborted"
    assert e.ensure_eval(1) == "awaiting_rerun_request"


def test_command_refused_cancels_the_new_run(db):
    e = make(db)
    db.fail["issue_robot_command"] = "robot 1 is in estop"
    assert e.ensure_eval(1) == "command_refused"
    assert db.rows("eval_run")[0]["status"] == "cancelled"


def test_records_outcomes_reported_only_in_result_json(db):
    e = make(db)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    outcomes = [{"index": i, "success": i % 2 == 0, "duration_ms": 900, "note": "x"} for i in range(10)]
    db.t["robot_command"][1].update(status="done", result_json=json.dumps({"outcomes": outcomes}))
    assert e.check_run(run) == "running"  # rows written; next check finalizes
    assert len([t for t in db.rows("eval_trial") if t["eval_run_id"] == run["id"]]) == 10
    e._guard.clear()
    assert e.check_run(run) == "finalized"
    assert db.t["eval_run"][run["id"]]["successes"] == 5


def test_timeout_issues_stop_and_cancels(db):
    e = make(db, trial_timeout_s=1, reset_wait_s=1)
    e.ensure_eval(1)
    run = db.rows("eval_run")[0]
    db.t["robot_command"][1]["status"] = "acked"
    db.now += 25  # > 10 trials * 2 s
    db.t["robot"][1]["last_heartbeat"] = db.us()
    assert e.check_run(run) == "aborted"
    assert [c for c in db.rows("robot_command") if c["type"] == "stop"]


def _reset_task(db):
    return db.put("task", ticket_id=1, robot_id=1, type="reset", status="open", assignee_id=0,
                  instructions="Reset the scene [ref eval_trial:3]")


def test_solo_reset_without_contributor_cancels_and_resumes(db):
    e = make(db, solo_mode=True, solo_reset_delay_s=0.01)
    task = _reset_task(db)
    e.solo_reset(task)
    e.solo_reset(task)  # once per task
    deadline = time.time() + 2
    while db.t["task"][task["id"]]["status"] == "open" and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert db.t["task"][task["id"]]["status"] == "cancelled"
    resumes = [c for c in db.rows("robot_command") if c["type"] == "resume_policy"]
    assert len(resumes) == 1 and json.loads(resumes[0]["payload_json"])["task_id"] == task["id"]


def test_solo_reset_as_contributor_completes(db):
    e = make(db, solo_mode=True, solo_reset_delay_s=0.01, solo_contributor_id=7)
    task = _reset_task(db)
    e.solo_reset(task)
    deadline = time.time() + 2
    while db.t["task"][task["id"]]["status"] != "completed" and time.time() < deadline:
        time.sleep(0.01)
    assert db.t["task"][task["id"]]["assignee_id"] == 7 and db.t["task"][task["id"]]["status"] == "completed"


def test_solo_off_does_nothing(db):
    e = make(db)
    task = _reset_task(db)
    e.solo_reset(task)
    time.sleep(0.05)
    assert db.t["task"][task["id"]]["status"] == "open"


def test_sim_robot_reset_is_automatic_without_solo_mode(db):
    db.t["robot"][1]["camera_config_json"] = json.dumps({"sim": True})
    e = make(db, solo_reset_delay_s=0.01)  # solo_mode off; sim_auto_reset defaults on
    task = _reset_task(db)
    e.solo_reset(task)
    deadline = time.time() + 2
    while db.t["task"][task["id"]]["status"] == "open" and time.time() < deadline:
        time.sleep(0.01)
    assert db.t["task"][task["id"]]["status"] == "cancelled"
    assert [c for c in db.rows("robot_command") if c["type"] == "resume_policy"]


def test_sim_auto_reset_off_leaves_sim_reset_open(db):
    db.t["robot"][1]["camera_config_json"] = json.dumps({"sim": True})
    e = make(db, solo_reset_delay_s=0.01, sim_auto_reset=False)
    task = _reset_task(db)
    e.solo_reset(task)
    time.sleep(0.05)
    assert db.t["task"][task["id"]]["status"] == "open"


# --------------------------------------------------------------------------- TDM-126: the real YAM in the eval stage

YAM_POLICY = json.dumps({"sim": False, "teleop": True, "policy": True})  # tdm119_teleop with TDM119_ALLOW_POLICY=1


def test_real_yam_baseline_is_the_yam_smoke_checkpoint_capped_and_slow(db):
    from rdf_orchestrator.robot_mode import REAL_YAM_BASELINE_CHECKPOINT

    db.t["robot"][1]["camera_config_json"] = YAM_POLICY
    assert make(db).ensure_eval(1, "test") == "dispatched"
    (run,) = db.rows("eval_run")
    assert run["checkpoint_uri"] == REAL_YAM_BASELINE_CHECKPOINT != PI05_BASE_CHECKPOINT
    p = json.loads(run_eval_cmds(db)[0]["payload_json"])
    assert p["checkpoint_uri"] == REAL_YAM_BASELINE_CHECKPOINT and p["robot_mode"] == "real"
    assert p["timeout_s"] <= 90 and p["speed_scale"] <= 0.5 and p["outcome_timeout_s"] >= 120


def test_real_yam_never_gets_a_fake_or_base_checkpoint(db):
    db.t["robot"][1]["camera_config_json"] = YAM_POLICY
    db.t["ticket"][1]["status"] = "eval"
    db.put("training_run", id=3, ticket_id=1, status="done", iteration=0, checkpoint_uri="fake://stub-no-gpu/t1/r3")
    assert make(db).ensure_eval(1, "test") == "checkpoint_not_real"
    assert not run_eval_cmds(db)


def test_real_yam_reset_goes_to_a_person(db):
    db.t["robot"][1]["camera_config_json"] = YAM_POLICY
    e = make(db, solo_reset_delay_s=0.01)  # sim_auto_reset on (default) applies to sim robots only
    task = _reset_task(db)
    e.solo_reset(task)
    time.sleep(0.05)
    assert db.t["task"][task["id"]]["status"] == "open"
    assert not [c for c in db.rows("robot_command") if c["type"] == "resume_policy"]
