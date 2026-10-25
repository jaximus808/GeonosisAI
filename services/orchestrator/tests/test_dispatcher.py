"""TDM-26 dispatcher tests against an in-memory mirror + a fake reducer writer (no network).

The fake writer applies create_task / cancel_task / issue_robot_command / reserve_task_payment to
the same tables the dispatcher reads, the way a committed reducer + subscription echo would.
"""
from __future__ import annotations

import json
import time

import pytest

from rdf_orchestrator import dispatcher as dsp


@pytest.fixture(autouse=True)
def _no_page_hook():
    """Isolate the module-global page hook: importing rdf_orchestrator.handlers (TDM-65 paging) installs
    one, so without this the result depends on which tests ran first. Restored afterwards."""
    saved = dsp._page_hook
    dsp.set_page_hook(None)
    yield
    dsp.set_page_hook(saved)


class FakeDB:
    """Both the Mirror (rows/get) and the Writer (call_reducer)."""

    def __init__(self):
        self.t: dict[str, dict[int, dict]] = {name: {} for name in dsp.TABLES}
        self.calls: list[tuple] = []
        self.events: list[tuple[str, dict]] = []
        self.fail: dict[str, str] = {}

    # mirror
    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def put(self, table, **row):
        if "id" not in row:
            row["id"] = max(self.t[table], default=0) + 1
        key = row.get("ticket_id") if table == "run_settings" else row["id"]
        self.t[table][key] = row
        return row

    # writer
    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise RuntimeError(self.fail[name])
        if name == "append_event":
            self.events.append((args[1], json.loads(args[2])))
        elif name == "create_task":
            tid, typ, price, payload = args
            p = json.loads(payload)
            ticket = self.t["ticket"][tid]
            self.put("task", ticket_id=tid, robot_id=ticket["robot_id"], data_plan_id=p.get("data_plan_id") or 0,
                     type=typ, status="open", instructions=p.get("instructions", ""), price_cents=price,
                     assignee_id=0, completed_at=None)
        elif name == "cancel_task":
            self.t["task"][args[0]]["status"] = "cancelled"
        elif name == "issue_robot_command":
            rid, typ, payload, tid = args
            self.put("robot_command", robot_id=rid, ticket_id=tid, type=typ, payload_json=payload, status="issued")
        elif name == "reserve_task_payment":
            task = self.t["task"][args[0]]
            self.put("payment", ticket_id=task["ticket_id"], episode_id=0, batch_ref=f"task:{task['id']}",
                     amount_cents=task["price_cents"], status="pending")
            self.t["ticket"][task["ticket_id"]]["spent_cents"] += task["price_cents"]

    # helpers
    def ev(self, type_):
        return [p for t, p in self.events if t == type_]

    def tasks(self, **kw):
        return [t for t in self.rows("task") if all(t.get(k) == v for k, v in kw.items())]


def setup(budget=10_000, amount=3, data_type="clean_teleop", approved=None, plan_status="approved"):
    db = FakeDB()
    db.put("robot", id=1, status="online", last_heartbeat=1)
    db.put("ticket", id=7, robot_id=1, task_description="put the cup in the bin", budget_cents=budget,
           spent_cents=0, approved_cents=0, status="dispatch", iteration=0)
    db.put("run_settings", ticket_id=7, hyperparams_json=json.dumps({"prompt": "pick up the cup"}))
    unit = dsp.PRICES_CENTS[data_type]
    db.put("data_plan", id=3, ticket_id=7, iteration=0, data_type=data_type, amount=amount, unit_price_cents=unit,
           est_cost_cents=unit * amount, status=plan_status,
           approved_cents=unit * amount if approved is None else approved, failure_cluster_ids=[])
    d = dsp.Dispatcher(db, lambda: db, mirror_wait_s=0.2)
    return db, d


def test_plan_of_three_creates_three_open_tasks_with_payload():
    db, d = setup()
    t0 = time.time()
    st = d.dispatch_plan(3)
    assert time.time() - t0 < 1.0
    tasks = db.tasks(data_plan_id=3, status="open")
    assert len(tasks) == 3 and st.created_task_ids == sorted(t["id"] for t in tasks)
    assert all(t["type"] == "clean_teleop" and t["price_cents"] == 1000 for t in tasks)
    payload = json.loads(next(c for c in db.calls if c[0] == "create_task")[4])
    assert payload["policy_prompt"] == "pick up the cup" and payload["ticket_id"] == 7
    assert "put the cup in the bin" in payload["instructions"] and "$10.00" in payload["instructions"]
    (ev,) = db.ev("dispatch.plan_dispatched")
    assert ev["created"] == 3 and ev["created_task_ids"] == st.created_task_ids
    (page,) = db.ev("dispatch.paged")
    assert page["task_ids"] == st.created_task_ids and page["channel"] == "none"


def test_idempotent_redispatch_creates_nothing_more():
    db, d = setup()
    d.dispatch_plan(3)
    d.dispatch_plan(3)
    d.dispatch_plan(3)
    assert len(db.tasks(data_plan_id=3)) == 3


def test_unapproved_plan_dispatches_nothing():
    db, d = setup(plan_status="proposed", approved=0)
    st = d.dispatch_plan(3)
    assert st.state == "awaiting_approval" and not db.tasks()


def test_one_dollar_budget_creates_zero_tasks_and_logs_reason():
    db, d = setup(budget=100, approved=100)
    st = d.dispatch_plan(3)
    assert not db.tasks()
    # approve_spend capped at $1 -> the approval itself pays for 0 demos
    assert st.payable == 0 and st.state == "budget_exhausted" and "$1.00" in st.reason
    (ev,) = db.ev("dispatch.budget_exhausted")
    assert ev["limit"] == "approved_cents" and ev["payable"] == 0 and ev["creating"] == 0


def test_budget_minus_spent_caps_creation_and_logs_budget_exhausted():
    db, d = setup(budget=2500)  # approved 3000 > what is left; e.g. budget lowered after approval
    db.t["ticket"][7]["spent_cents"] = 400
    st = d.dispatch_plan(3)
    assert len(db.tasks(data_plan_id=3)) == 2  # 2100 left -> 2 x $10
    assert st.state == "budget_exhausted"
    (ev,) = db.ev("dispatch.budget_exhausted")
    assert ev["need"] == 3 and ev["creating"] == 2 and ev["budget_remaining_cents"] == 2100
    d.dispatch_plan(3)  # 1 still wanted, $1 left: a new state, logged once
    d.dispatch_plan(3)  # same state: no repeated event
    evs = db.ev("dispatch.budget_exhausted")
    assert len(evs) == 2 and evs[1]["need"] == 1 and evs[1]["creating"] == 0
    assert len(db.tasks(data_plan_id=3)) == 2


def test_zero_budget_left_logs_and_halts():
    db, d = setup(budget=100, approved=3000)
    st = d.dispatch_plan(3)
    assert not db.tasks() and st.state == "budget_exhausted"
    assert db.ev("dispatch.budget_exhausted")[0]["creating"] == 0


def test_approved_cents_cap_below_amount():
    db, d = setup(approved=2000)
    st = d.dispatch_plan(3)
    assert len(db.tasks()) == 2 and st.payable == 2


def test_rejection_tops_up_and_target_reached_cancels_leftovers():
    db, d = setup(amount=2, approved=3000)  # approval room for one retry
    d.dispatch_plan(3)
    t1, t2 = db.tasks(data_plan_id=3)
    # t1: accepted episode
    t1["status"], t1["assignee_id"] = "completed", 9
    db.put("episode", ticket_id=7, task_id=t1["id"], review_status="accepted")
    # t2: rejected episode -> its slot frees, one more task is created
    t2["status"], t2["assignee_id"] = "completed", 9
    db.put("episode", ticket_id=7, task_id=t2["id"], review_status="rejected")
    st = d.dispatch_plan(3)
    assert len(st.created_task_ids) == 1 and st.accepted == 1
    (t3,) = db.tasks(id=st.created_task_ids[0])
    # Another stray open task (e.g. created by hand) gets cancelled once the target is hit.
    t3["status"], t3["assignee_id"] = "completed", 9
    db.put("episode", ticket_id=7, task_id=t3["id"], review_status="accepted")
    db.call_reducer("create_task", 7, "clean_teleop", 1000, json.dumps({"data_plan_id": 3}))
    st = d.dispatch_plan(3)
    assert st.state == "target_reached" and st.done and len(st.cancelled_task_ids) == 1
    assert db.ev("dispatch.target_reached")[0]["accepted"] == 2
    assert d.collection_done(7)


def test_pending_review_holds_slot_and_budget():
    db, d = setup(amount=1, budget=1000)
    d.dispatch_plan(3)
    (t1,) = db.tasks()
    t1["status"], t1["completed_at"] = "completed", int(time.time() * 1e6)
    db.put("episode", ticket_id=7, task_id=t1["id"], review_status="pending")
    st = d.dispatch_plan(3)
    assert st.in_flight == 1 and not st.created_task_ids and d.budget_remaining(7) == 0


def test_terminal_ticket_and_stale_iteration_close_the_plan():
    db, d = setup()
    db.t["ticket"][7]["iteration"] = 1
    assert d.dispatch_plan(3).state == "closed" and not db.tasks()
    db.t["ticket"][7]["iteration"], db.t["ticket"][7]["status"] = 0, "cancelled"
    assert d.dispatch_plan(3).state == "closed" and not db.tasks()


def test_page_hook_called():
    db, d = setup(amount=1)
    db.put("contributor", id=5, online=True, kind="contributor")
    db.put("contributor", id=6, online=False, kind="contributor")
    got = []
    dsp.set_page_hook(lambda req: got.append(req) or {"sent": len(req.contributor_ids)})
    try:
        d.dispatch_plan(3)
    finally:
        dsp.set_page_hook(None)
    assert got[0].contributor_ids == [5] and got[0].task_type == "clean_teleop"
    assert db.ev("dispatch.paged")[0]["result"] == {"sent": 1}


# ---------------------------------------------------------------- resets

def eval_setup(job_open=True):
    db, d = setup()
    db.put("eval_run", id=11, ticket_id=7, robot_id=1, status="running", trials=10)
    if job_open:
        db.put("robot_command", robot_id=1, ticket_id=7, type="run_eval", payload_json="{}", status="acked")
    return db, d


def test_eval_trial_creates_reset_task_inline_while_eval_job_runs():
    db, d = eval_setup(job_open=True)
    trial = db.put("eval_trial", eval_run_id=11, trial_index=0, success=False, created_at=int(time.time() * 1e6))
    res = d.on_eval_trial(trial)
    (reset,) = db.tasks(type="reset")
    assert res.created and res.task_id == reset["id"] and reset["price_cents"] == 200
    assert res.robot_command == "inline"  # run_eval holds the job lock; it waits for resume_policy
    assert not [c for c in db.rows("robot_command") if c["type"] == "reset_request"]
    assert dsp.task_ref(reset) == f"eval_trial:{trial['id']}"
    # replay (resync) -> no second task
    assert d.on_eval_trial(trial).created is False and len(db.tasks(type="reset")) == 1


def test_reset_request_issued_when_robot_idle():
    db, d = eval_setup(job_open=False)
    trial = db.put("eval_trial", eval_run_id=11, trial_index=9, success=True, created_at=int(time.time() * 1e6))
    res = d.on_eval_trial(trial)
    assert res.robot_command == "issued"
    (cmd,) = [c for c in db.rows("robot_command") if c["type"] == "reset_request"]
    assert json.loads(cmd["payload_json"])["task_id"] == res.task_id


def test_one_live_reset_per_robot():
    db, d = eval_setup()
    a = db.put("eval_trial", eval_run_id=11, trial_index=0, success=False, created_at=0)
    b = db.put("eval_trial", eval_run_id=11, trial_index=1, success=False, created_at=0)
    d.on_eval_trial(a)
    assert d.on_eval_trial(b).created is False and len(db.tasks(type="reset")) == 1


def test_snapshot_ignores_old_trials():
    db, d = eval_setup()
    old = db.put("eval_trial", eval_run_id=11, trial_index=0, success=False, created_at=1)
    assert d.on_eval_trial(old, snapshot=True) is None and not db.tasks(type="reset")


def test_reset_budget_exhausted():
    db, d = eval_setup()
    db.t["ticket"][7]["budget_cents"] = 150
    trial = db.put("eval_trial", eval_run_id=11, trial_index=0, success=False, created_at=0)
    res = d.on_eval_trial(trial)
    assert res.task_id is None and not db.tasks(type="reset")
    assert db.ev("dispatch.budget_exhausted")[0]["task_type"] == "reset"


def test_reset_completed_pays_and_resumes_once():
    db, d = eval_setup()
    trial = db.put("eval_trial", eval_run_id=11, trial_index=0, success=False, created_at=0)
    res = d.on_eval_trial(trial)
    task = db.t["task"][res.task_id]
    task["status"], task["assignee_id"], task["completed_at"] = "completed", 5, int(time.time() * 1e6)
    assert d.on_reset_completed(task) == "issued"
    assert d.on_reset_completed(task) == "already_resumed"
    (cmd,) = [c for c in db.rows("robot_command") if c["type"] == "resume_policy"]
    assert json.loads(cmd["payload_json"])["task_id"] == task["id"]
    assert [c for c in db.calls if c[0] == "reserve_task_payment"] == [("reserve_task_payment", task["id"])] * 2
    assert db.ev("dispatch.resume_issued")


def test_robot_paused_gets_reset_unless_takeover_task_live():
    db, d = setup(data_type="recovery_takeover")
    robot = db.t["robot"][1]
    robot["status"] = "paused"
    d.dispatch_plan(3)  # open recovery_takeover tasks on robot 1
    assert d.on_robot_paused(robot) is None and not db.tasks(type="reset")
    for t in db.tasks(type="recovery_takeover"):
        t["status"] = "cancelled"
    res = d.on_robot_paused(robot)
    assert res.created and db.tasks(type="reset")


# ---------------------------------------------------------------- bus wiring

def test_handlers_registered_on_default_registry():
    from rdf_orchestrator import handlers  # noqa: F401
    from rdf_orchestrator.bus import registry

    names = {h.name for h in registry.handlers}
    assert {"dispatch_on_plan_approved", "reset_after_eval_trial", "resume_after_reset",
            "reset_on_robot_paused", "dispatch_on_task_changed", "dispatch_on_review"} <= names
    for t in ("data_plan", "task", "episode", "eval_trial", "robot_command", "payment"):
        assert t in registry.tables


@pytest.mark.parametrize("n", [3])
def test_bus_end_to_end_plan_approved_to_tasks(n):
    """Real Bus + FakeClient from test_bus: approving a plan on the bus yields n open tasks."""
    import asyncio

    from test_bus import FakeClient, _settle

    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.stdb import Row

    reg = Registry()
    reg.on_enter("data_plan", where=lambda r: r.status in dsp.DISPATCHABLE_PLAN)(
        lambda ev: dsp.get_dispatcher(ev.bus).dispatch_plan(int(ev.row["id"])))
    for t in dsp.TABLES:
        reg.watch(t)
    fc = FakeClient()
    bus = Bus(reg, fc, workers=2)
    created = []

    class Writer:
        def call_reducer(self, name, *args):
            if name == "create_task":
                created.append(args)
                tid, typ, price, payload = args
                row = Row(id=100 + len(created), ticket_id=tid, robot_id=1, type=typ, status="open",
                          data_plan_id=json.loads(payload)["data_plan_id"], price_cents=price,
                          instructions="", assignee_id=0, completed_at=None)
                bus._loop.call_soon_threadsafe(fc.cb["insert"], "task", row, {})

    old = dsp.make_writer
    dsp.make_writer = Writer
    try:
        async def go():
            await bus.start()
            fc.snapshot({"ticket": [Row(id=7, robot_id=1, task_description="cup", budget_cents=5000, spent_cents=0,
                                        status="dispatch", iteration=0)]})
            t0 = time.time()
            fc.cb["insert"]("data_plan", Row(id=3, ticket_id=7, iteration=0, data_type="clean_teleop", amount=n,
                                             unit_price_cents=1000, status="approved", approved_cents=n * 1000,
                                             failure_cluster_ids=[]), {})
            await _settle(bus, 200)
            dt = time.time() - t0
            open_tasks = [r for r in bus.rows("task") if r.status == "open"]
            await bus.stop()
            return dt, open_tasks

        dt, open_tasks = asyncio.run(go())
    finally:
        dsp.make_writer = old
    assert len(created) == n and len(open_tasks) == n and dt < 1.0


def test_claimed_clean_teleop_starts_and_stops_the_robot_teleop_session():
    """Teleop integration: claiming a clean_teleop task issues start_teleop {task_id} (the TDM-52 job
    the robot drives from /ws/control); leaving claimed issues stop_teleop. Idempotent; never while
    another job holds the robot; never for recovery takeovers (the paused rollout holds the job lock)."""
    db, d = setup(amount=1)
    d.dispatch_plan(3)
    (task,) = db.tasks(data_plan_id=3)
    open_ = dict(task)
    task.update(status="claimed", assignee_id=5)
    assert d.on_teleop_task(task, open_) == "issued"
    (cmd,) = [c for c in db.rows("robot_command") if c["type"] == "start_teleop"]
    p = json.loads(cmd["payload_json"])
    assert p["task_id"] == task["id"] and p["kind"] == "clean_teleop" and p["contributor_id"] == 5
    assert d.on_teleop_task(task, open_) == "already_started"
    assert d.on_teleop_task(task, dict(task)) is None  # no status change
    claimed = dict(task)
    task.update(status="completed")
    assert d.on_teleop_task(task, claimed) == "stopped"
    (stop,) = [c for c in db.rows("robot_command") if c["type"] == "stop_teleop"]
    assert json.loads(stop["payload_json"])["task_id"] == task["id"]
    assert d.on_teleop_task(task, claimed) == "already_stopping"
    # robot busy with another job: no session, logged
    db.put("robot_command", robot_id=1, ticket_id=7, type="run_baseline", payload_json="{}", status="acked")
    cmd["status"] = "done"
    t2 = db.put("task", ticket_id=7, robot_id=1, data_plan_id=0, type="clean_teleop", status="claimed",
                instructions="", price_cents=1000, assignee_id=5, completed_at=None)
    assert d.on_teleop_task(t2, {**t2, "status": "open"}) == "robot_busy"
    assert db.ev("dispatch.teleop_blocked")
    t3 = db.put("task", ticket_id=7, robot_id=1, data_plan_id=0, type="recovery_takeover", status="claimed",
                instructions="", price_cents=500, assignee_id=5, completed_at=None)
    assert d.on_teleop_task(t3, {**t3, "status": "open"}) is None


def test_accepted_clean_demo_completes_the_still_claimed_task():
    db, d = setup(amount=1)
    d.dispatch_plan(3)
    (task,) = db.tasks(data_plan_id=3)
    task.update(status="claimed", assignee_id=5)
    ep = db.put("episode", ticket_id=7, task_id=task["id"], kind="clean_teleop", review_status="pending")
    assert d.complete_on_accept(ep) is None
    ep["review_status"] = "accepted"
    assert d.complete_on_accept(ep) == "completed"
    (call,) = [c for c in db.calls if c[0] == "complete_task"]
    assert call[1] == task["id"] and json.loads(call[2])["reason"] == "episode_accepted"
    task["status"] = "completed"
    assert d.complete_on_accept(ep) is None


def test_tdm86_lease_sweeper_expires_lapsed_claims_then_starts_the_waiting_session():
    """TDM-86: an abandoned clean_teleop claim (tab closed, lease lapsed) used to keep its start_teleop
    job on the robot forever (expiry only ran when someone claimed that same task), so the next
    claimant's page got wrong_task for every command. The sweeper expires it, then starts the waiting
    claim's session once the robot is free."""
    db, d = setup(amount=1)
    now_us = int(time.time() * 1e6)
    d.clock = lambda: now_us / 1e6
    stale = db.put("task", ticket_id=7, robot_id=1, data_plan_id=0, type="clean_teleop", status="claimed",
                   instructions="", price_cents=1000, assignee_id=4, completed_at=None, lease_expires_at=now_us - 30_000_000)
    live = db.put("task", ticket_id=7, robot_id=1, data_plan_id=0, type="clean_teleop", status="claimed",
                  instructions="", price_cents=1000, assignee_id=5, completed_at=None, lease_expires_at=now_us + 50_000_000)
    job = db.put("robot_command", robot_id=1, ticket_id=7, type="start_teleop", status="acked",
                 payload_json=json.dumps({"task_id": stale["id"]}))
    assert d.sweep_leases() == {"expired": True, "started": []}
    assert ("expire_task_leases",) in db.calls
    # the reducer reopens the stale claim; the dispatcher stops its session; the robot finishes the job
    stale.update(status="open", lease_expires_at=None)
    job["status"] = "done"
    assert d.sweep_leases() == {"expired": False, "started": [live["id"]]}
    started = [json.loads(c["payload_json"])["task_id"] for c in db.rows("robot_command") if c["type"] == "start_teleop" and c["status"] == "issued"]
    assert started == [live["id"]]
    assert d.sweep_leases() == {"expired": False, "started": []}  # idempotent: its job is open now
