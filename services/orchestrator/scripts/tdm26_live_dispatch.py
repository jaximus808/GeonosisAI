"""TDM-26 live proof: the dispatcher against a throwaway local SpacetimeDB + the TDM-28 fake robot.

    cd services/orchestrator && uv run python scripts/tdm26_live_dispatch.py [--port 3126]

Boots tests/integration/harness.LocalStack on its own port (data under $TMPDIR/tdm-26, never
maincloud), runs an in-process Bus with ONLY the dispatcher handlers (orchestrator identity), then:

  A. ticket ($50) + data_plan 3 x clean_teleop, approve_spend -> 3 open tasks; time to visible.
     Then a second Bus (an orchestrator restart): the boot snapshot creates nothing more.
  B. ticket with a $1 budget: approve_spend(est) is refused by the reducer, approve_spend($1)
     passes -> 0 tasks and a dispatch.budget_exhausted row with the reason.
  C. run_eval (3 fake trials) on ticket A -> a $2 reset task (robot busy -> reset is "inline");
     a contributor claims + completes it -> reserve_task_payment row + resume_policy command.
  D. request_reset() while the robot is idle -> reset_request robot command (the fake completes it).

Every row it writes is tagged test.tdm26. Exit code 0 = all checks passed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for
from rdf_common.spacetime_client import SpacetimeClient, create_identity

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.handlers.dispatcher import register
from rdf_orchestrator.log import configure_logging
from rdf_orchestrator.stdb import SpacetimeClient as SubClient

PASSED: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if not cond:
        raise SystemExit(f"FAIL {name} {detail}")
    PASSED.append(name)
    print(f"ok   {name}" + (f"  ({detail})" if detail != "" else ""), flush=True)


class BusThread:
    """A Bus with only the dispatcher handlers, on its own event loop thread."""

    def __init__(self, st: LocalStack) -> None:
        self.st = st
        self.loop = asyncio.new_event_loop()
        self.bus = Bus(register(Registry()), SubClient(host=st.host, db=st.db, token=st.token("orchestrator"),
                                                       max_backoff=2.0), workers=4)
        self.t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.t.start()
        asyncio.run_coroutine_threadsafe(self.bus.start(), self.loop).result(10)
        wait_for(lambda: self.bus.state == "live" and self.bus.snapshot_at, 30, "dispatcher bus live")

    def settle(self, timeout: float = 10) -> None:
        wait_for(lambda: self.bus.health()["inflight"] == 0, timeout, "handlers idle")

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.bus.stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)


def events(st: LocalStack, type_: str, ticket_id: int) -> list[dict]:
    out = []
    for r in st.sql(f"SELECT * FROM event_log WHERE type = '{type_}'"):
        p = json.loads(r["payload_json"] or "{}")
        if int(p.get("ticket_id") or 0) == ticket_id:
            out.append(p)
    return out


def new_ticket(st: LocalStack, c: SpacetimeClient, robot_id: int, budget: int, label: str) -> int:
    c.call_reducer("create_ticket", robot_id, f"test.tdm26 {label}: put the cup in the bin", 0.8, budget, "")
    return int(st.newest("ticket")["id"])


def new_plan(st: LocalStack, c: SpacetimeClient, ticket_id: int, amount: int) -> int:
    c.call_reducer("record_data_plan", ticket_id, "clean_teleop", amount,
                   "test.tdm26: clean demos to cover missed grasps", "{}")
    return int(st.newest("data_plan", f"ticket_id = {ticket_id}")["id"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3126)
    ap.add_argument("--db", default="tdm26live")
    ap.add_argument("--keep", action="store_true", help="leave the stack running at the end (Ctrl-C to stop)")
    a = ap.parse_args()
    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))

    st = LocalStack(a.port, a.db, root=Path(tempfile.gettempdir()) / "tdm-26")
    bt = bt2 = None
    try:
        st.boot(fake_robot_args=("--time-scale", "400", "--seed", "26", "--heartbeat-seconds", "2",
                                 "--reset-ms", "2000:2000"))
        robot_id = int(st.robot()["id"])
        orch = st.client("orchestrator")
        dsp.make_writer = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))
        bt = BusThread(st)

        # ---------------------------------------------------------------- A
        ta = new_ticket(st, orch, robot_id, 5000, "A")
        pa = new_plan(st, orch, ta, 3)
        time.sleep(0.5)
        check("A: proposed plan creates no tasks", not st.sql(f"SELECT * FROM task WHERE ticket_id = {ta}"))
        t0 = time.monotonic()
        orch.call_reducer("approve_spend", ta, pa, 0, "human:test.tdm26")
        def open_tasks() -> list[dict] | None:
            rows = st.sql(f"SELECT * FROM task WHERE data_plan_id = {pa} AND status = 'open'")
            return rows if len(rows) >= 3 else None

        tasks = wait_for(open_tasks, 5, "3 open tasks")
        dt = time.monotonic() - t0
        check("A: 3 open clean_teleop tasks after approve_spend", len(tasks) == 3 and
              all(t["type"] == "clean_teleop" and int(t["price_cents"]) == 1000 for t in tasks),
              [int(t["id"]) for t in tasks])
        check("A: visible in the marketplace within 1 s", dt < 1.0, f"{dt * 1000:.0f} ms from approve_spend return")
        check("A: instructions carry ticket + price", all("put the cup in the bin" in t["instructions"]
                                                          and "$10.00" in t["instructions"] for t in tasks))
        bt.settle()
        ev = events(st, "dispatch.plan_dispatched", ta)
        check("A: dispatch.plan_dispatched logged", len(ev) == 1 and ev[0]["created"] == 3, ev[0]["created_task_ids"])
        check("A: dispatch.paged logged (no push hook yet)", len(events(st, "dispatch.paged", ta)) == 1)
        created = [e for e in st.sql("SELECT * FROM event_log WHERE type = 'task.created'")
                   if json.loads(e["payload_json"]).get("ticket_id") == ta]
        p = json.loads(created[0]["payload_json"])["payload"]
        check("A: task.created payload has policy prompt + data plan", p["data_plan_id"] == pa
              and p["policy_prompt"] and p["ticket_id"] == ta, {k: p[k] for k in ("data_type", "policy_prompt")})

        bt2 = BusThread(st)  # an orchestrator restart: boot snapshot replays the approved plan
        bt2.settle()
        time.sleep(0.5)
        check("A: restart snapshot is idempotent (still 3 tasks)",
              len(st.sql(f"SELECT * FROM task WHERE data_plan_id = {pa}")) == 3)
        bt2.stop()
        bt2 = None

        # ---------------------------------------------------------------- B
        tb = new_ticket(st, orch, robot_id, 100, "B ($1 budget)")
        pb = new_plan(st, orch, tb, 3)
        try:
            orch.call_reducer("approve_spend", tb, pb, 0, "human:test.tdm26")
            refused = ""
        except Exception as e:  # noqa: BLE001
            refused = str(e)
        check("B: approve_spend of the $30 estimate refused by the reducer", "budget_cents" in refused, refused[:120])
        orch.call_reducer("approve_spend", tb, pb, 100, "human:test.tdm26")
        ev = wait_for(lambda: events(st, "dispatch.budget_exhausted", tb), 5, "budget_exhausted event")
        time.sleep(0.5)
        check("B: $1 budget -> zero tasks", not st.sql(f"SELECT * FROM task WHERE ticket_id = {tb}"))
        check("B: dispatch.budget_exhausted logged with a reason", ev[0]["creating"] == 0 and ev[0]["reason"],
              ev[0]["reason"])

        # ---------------------------------------------------------------- C
        orch.call_reducer("start_eval_run", ta, "fake://test.tdm26/base", 3)
        run = st.newest("eval_run", f"ticket_id = {ta}")
        orch.call_reducer("issue_robot_command", robot_id, "run_eval",
                          json.dumps({"eval_run_id": int(run["id"]), "trials": 3}), ta)
        reset = wait_for(lambda: st.newest("task", f"ticket_id = {ta} AND type = 'reset'"), 20, "reset task")
        check("C: eval trial -> $2 reset task", reset["status"] == "open" and int(reset["price_cents"]) == 200,
              f"task {reset['id']}: {dsp.task_ref(reset)}")
        wait_for(lambda: len(st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {int(run['id'])}")) == 3, 30, "3 trials")
        wait_for(lambda: st.newest("robot_command", f"robot_id = {robot_id} AND type = 'run_eval'")["status"] == "done",
                 30, "run_eval done")
        bt.settle()
        resets = st.sql(f"SELECT * FROM task WHERE ticket_id = {ta} AND type = 'reset'")
        check("C: one live reset per robot (3 trials -> 1 reset task)", len(resets) == 1)
        rr = events(st, "dispatch.reset_requested", ta)
        check("C: reset_request held inline while run_eval holds the job lock", rr[0]["robot_command"] == "inline")

        _ident, tok = create_identity(st.host)
        with SpacetimeClient(st.host, st.db, tok) as alice:
            alice.call_reducer("register_contributor", "test.tdm26 alice", "", ["phone"], "contributor", ta)
        cid = int(st.newest("contributor")["id"])
        orch.call_reducer("claim_task", int(reset["id"]), cid)
        orch.call_reducer("complete_task", int(reset["id"]), json.dumps({"note": "test.tdm26 scene reset"}))
        resume = wait_for(lambda: [c for c in st.sql(f"SELECT * FROM robot_command WHERE robot_id = {robot_id}")
                                   if c["type"] == "resume_policy"
                                   and json.loads(c["payload_json"]).get("task_id") == int(reset["id"])], 5, "resume")
        check("C: completed reset -> resume_policy command", len(resume) == 1, f"command {resume[0]['id']}")
        pay = wait_for(lambda: st.sql(f"SELECT * FROM payment WHERE ticket_id = {ta}"), 5, "reset payment")
        check("C: reset payment reserved", pay[0]["batch_ref"] == f"task:{reset['id']}"
              and int(pay[0]["amount_cents"]) == 200, pay[0]["status"])

        # ---------------------------------------------------------------- D
        d = dsp.get_dispatcher(bt.bus)
        res = d.request_reset(robot_id, ta, reason="test.tdm26.manual")
        check("D: request_reset on an idle robot issues reset_request", res.created and res.robot_command == "issued",
              res)
        cmd = wait_for(lambda: st.newest("robot_command", f"robot_id = {robot_id} AND type = 'reset_request'"), 5, "cmd")
        wait_for(lambda: st.newest("robot_command", f"id = {int(cmd['id'])}")["status"] == "done", 20, "fake reset done")
        check("D: fake robot completed the reset_request", True, f"command {cmd['id']}")
        st_a = d.plan_status(pa)
        check("TDM-22 interface: plan_status", st_a.state == "collecting" and st_a.in_flight == 3 and not st_a.done,
              st_a.to_dict())
        h = bt.bus.health()
        check("no handler errors", h["handler_errors"] == 0, [x for x in h["handlers"] if x["errors"]])
        print(f"\nALL {len(PASSED)} CHECKS PASSED", flush=True)
        if a.keep:
            print(f"stack at {st.host} db {st.db}; Ctrl-C to stop", flush=True)
            while True:
                time.sleep(1)
    finally:
        for b in (bt2, bt):
            if b is not None:
                try:
                    b.stop()
                except Exception as e:  # noqa: BLE001
                    print(f"bus stop: {e}", file=sys.stderr)
        st.stop()


if __name__ == "__main__":
    main()
