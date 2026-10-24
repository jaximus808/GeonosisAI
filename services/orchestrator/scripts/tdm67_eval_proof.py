"""TDM-67 proof: eval orchestration end to end against a THROWAWAY local SpacetimeDB + the TDM-28
fake robot. Every trial here is FAKE (failure_note starts with [FAKE]); never quote these numbers.

    cd services/orchestrator && uv run python scripts/tdm67_eval_proof.py

What it does (refuses maincloud: the harness only boots 127.0.0.1):
  1. Boots spacetime on 127.0.0.1:3067 (DB tdm67eval, fresh data dir under $TMPDIR/tdm-67), publishes
     the working-tree module, mints orchestrator + robot_client identities, starts the fake robot.
  2. Runs, in this process, a Bus with the real flywheel (TDM-22), dispatcher (TDM-26) and TDM-67
     eval handlers, with EVAL_SOLO_MODE=1 so reset tasks complete themselves.
  3. Ticket A: created -> baseline -> 10-trial eval on the fake robot -> finalized -> diagnose.
  4. Ticket B: baseline eval starts; after 3 trials the fake robot is SIGKILLed. The watchdog sees the
     heartbeat go stale and aborts the eval (cancelled). The fake robot is restarted; the eval is
     re-run from scratch on a new eval_run, finalizes, and the ticket moves on (not wedged).
Prints a JSON summary at the end and exits non-zero if any check fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM67_PORT", "3067"))
FAKE_ARGS = ("--time-scale", "25", "--seed", "67", "--heartbeat-seconds", "1", "--estop-clear-seconds", "1",
             "--success-prob", "0.3", "--candidate-success-prob", "0.9")
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


async def wait(pred, timeout: float, what: str, every: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


async def main() -> int:
    st = LocalStack(PORT, "tdm67eval", root=Path(tempfile.gettempdir()) / "tdm-67")
    assert "maincloud" not in st.host
    os.environ.update({"EVAL_SOLO_MODE": "1", "EVAL_SOLO_RESET_DELAY_S": "0.3", "EVAL_ROBOT_STALE_S": "5",
                       "EVAL_WATCHDOG_S": "1", "POLICY_SERVER_URL": ""})
    st.boot(fake_robot_args=FAKE_ARGS)
    try:
        return await run(st)
    finally:
        st.stop()


async def run(st: LocalStack) -> int:
    from rdf_common.spacetime_client import SpacetimeClient

    from rdf_orchestrator import dispatcher as dsp
    from rdf_orchestrator import evaluate as evl
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.flywheel import Flywheel
    from rdf_orchestrator.handlers import dispatcher as dsp_handlers
    from rdf_orchestrator.handlers import evaluate as eval_handlers
    from rdf_orchestrator.handlers.flywheel import register_flywheel
    from rdf_orchestrator.log import configure_logging
    from rdf_orchestrator.stdb import SpacetimeClient as BusSpacetimeClient

    configure_logging()
    tok = st.token("orchestrator")
    dsp.make_writer = evl.make_writer = lambda: SpacetimeClient(st.host, st.db, tok)
    reg = Registry()
    fw = Flywheel(reconcile_s=1.0)
    register_flywheel(reg, fw)
    dsp_handlers.register(reg)
    eval_handlers.register(reg, fw)
    steps = {s["stage"]: s["step"] for s in fw.step_table()}
    check("flywheel baseline/eval steps are TDM-67 EvalStep", steps["baseline"] == steps["eval"] == "EvalStep", str(steps))
    bus = Bus(reg, BusSpacetimeClient(host=st.host, db=st.db, token=tok, max_backoff=2.0), workers=8)
    await bus.start()
    await wait(lambda: bus.state == "live", 30, "bus live")
    robot = st.robot()
    rid = int(robot["id"])
    c = st.client()
    out: dict = {"robot_id": rid}
    try:
        # ---------------------------------------------------------------- ticket A: clean 10-trial baseline
        c.call_reducer("create_ticket", rid, "[FAKE] tdm67 A: put the cup in the bin", 0.8, 5000, "")
        ta = int(st.newest("ticket")["id"])
        run_a = await wait(lambda: next((r for r in bus.rows("eval_run") if r.get("ticket_id") == ta), None),
                           60, "ticket A eval_run")
        done_a = await wait(lambda: (bus.get("eval_run", run_a["id"]) or {}).get("status") == "done"
                            and bus.get("eval_run", run_a["id"]), 120, "ticket A eval finalized")
        trials_a = st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run_a['id']}")
        check("A: eval_run finalized with all planned trials", done_a["trials"] == 10 and len(trials_a) == 10
              and done_a["completed"] == 10, f"{done_a['successes']}/{done_a['trials']} ci=[{done_a['ci_low']:.3f}, {done_a['ci_high']:.3f}]")
        check("A: every trial is tagged [FAKE]", all((t.get("failure_note") or "").startswith("[FAKE]") for t in trials_a))
        cmds = [r for r in st.sql(f"SELECT * FROM robot_command WHERE ticket_id = {ta}")]
        run_cmds = [r for r in cmds if r["type"] == "run_eval"]
        check("A: exactly one run_eval command, payload names the eval_run", len(run_cmds) == 1
              and json.loads(run_cmds[0]["payload_json"])["eval_run_id"] == run_a["id"] and run_cmds[0]["status"] == "done",
              run_cmds[0]["payload_json"] if run_cmds else "none")
        await asyncio.sleep(2.0)  # let the last reset task resolve
        resets = st.sql(f"SELECT * FROM task WHERE ticket_id = {ta} AND type = 'reset'")
        resumes = [r for r in st.sql(f"SELECT * FROM robot_command WHERE ticket_id = {ta}") if r["type"] == "resume_policy"]
        check("A: dispatcher created reset tasks between trials; solo mode resolved them; robot resumed",
              len(resets) >= 1 and all(r["status"] == "cancelled" for r in resets) and len(resumes) == len(resets),
              f"{len(resets)} reset task(s) {sorted({r['status'] for r in resets})}, {len(resumes)} resume_policy")
        tick_a = await wait(lambda: (bus.get("ticket", ta) or {}).get("status") == "diagnose" and bus.get("ticket", ta),
                            30, "ticket A -> diagnose")
        check("A: flywheel moved the ticket baseline -> diagnose on the finalized eval", tick_a["status"] == "diagnose")
        ev = st.events()
        fin = [e for e in ev if e["type"] == "eval.finalized" and json.loads(e["payload_json"])["eval_run_id"] == run_a["id"]]
        check("A: eval.finalized event logged once", len(fin) == 1, fin[0]["payload_json"][:200] if fin else "")
        out["A"] = {"ticket_id": ta, "eval_run": {k: done_a[k] for k in ("id", "kind", "status", "trials", "successes", "ci_low", "ci_high")},
                    "reset_tasks": len(resets), "resume_policy": len(resumes)}

        # ---------------------------------------------------------------- ticket B: kill the robot mid-eval
        c.call_reducer("create_ticket", rid, "[FAKE] tdm67 B: kill the robot client mid-eval", 0.8, 5000, "")
        tb = int(st.newest("ticket")["id"])
        run_b = await wait(lambda: next((r for r in bus.rows("eval_run") if r.get("ticket_id") == tb), None),
                           60, "ticket B eval_run")
        await wait(lambda: len([t for t in bus.rows("eval_trial") if t.get("eval_run_id") == run_b["id"]]) >= 3,
                   60, "3 trials on ticket B")
        p = st.procs["fake-robot"]
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=10)
        killed_at = time.monotonic()
        n_before = len([t for t in bus.rows("eval_trial") if t.get("eval_run_id") == run_b["id"]])
        print(f"killed the fake robot (SIGKILL) after {n_before} trials of eval_run {run_b['id']}", flush=True)
        aborted = await wait(lambda: (bus.get("eval_run", run_b["id"]) or {}).get("status") in ("cancelled", "failed")
                             and bus.get("eval_run", run_b["id"]), 30, "ticket B eval aborted")
        ab = [e for e in st.events() if e["type"] == "eval.aborted" and json.loads(e["payload_json"])["eval_run_id"] == run_b["id"]]
        check("B: eval marked aborted (cancelled) after the robot was killed",
              aborted["status"] == "cancelled" and len(ab) == 1,
              f"{time.monotonic() - killed_at:.1f}s after kill: {json.loads(ab[0]['payload_json'])['reason'] if ab else ''}")
        tb_row = bus.get("ticket", tb)
        check("B: ticket still in baseline (not failed, not wedged in a running eval)", tb_row["status"] == "baseline")
        fin_try = [e for e in st.events() if e["type"] == "eval.finalized" and json.loads(e["payload_json"])["eval_run_id"] == run_b["id"]]
        check("B: aborted run was never finalized", not fin_try)

        st.spawn_fake_robot(*FAKE_ARGS)
        print("restarted the fake robot", flush=True)
        run_b2 = await wait(lambda: next((r for r in bus.rows("eval_run") if r.get("ticket_id") == tb and r["id"] != run_b["id"]), None),
                            90, "ticket B re-run eval_run")
        done_b2 = await wait(lambda: (bus.get("eval_run", run_b2["id"]) or {}).get("status") == "done"
                             and bus.get("eval_run", run_b2["id"]), 120, "ticket B re-run finalized")
        old_cmd = [r for r in st.sql(f"SELECT * FROM robot_command WHERE ticket_id = {tb}") if r["type"] == "run_eval"
                   and json.loads(r["payload_json"])["eval_run_id"] == run_b["id"]]
        check("B: the killed run's command was failed by the restarted robot client",
              old_cmd and old_cmd[0]["status"] == "failed", old_cmd[0].get("result_json") if old_cmd else "")
        check("B: re-run on a fresh eval_run finalized with 10 trials", done_b2["trials"] == 10 and done_b2["completed"] == 10,
              f"{done_b2['successes']}/{done_b2['trials']}")
        tick_b = await wait(lambda: (bus.get("ticket", tb) or {}).get("status") == "diagnose" and bus.get("ticket", tb),
                            30, "ticket B -> diagnose")
        check("B: ticket moved on to diagnose after the re-run", tick_b["status"] == "diagnose")
        out["B"] = {"ticket_id": tb, "aborted_run": {k: aborted[k] for k in ("id", "status", "completed", "trials")},
                    "abort_reason": json.loads(ab[0]["payload_json"])["reason"] if ab else None,
                    "rerun": {k: done_b2[k] for k in ("id", "status", "trials", "successes", "ci_low", "ci_high")}}
        types: dict[str, int] = {}
        for e in st.events():
            if e["type"].startswith(("eval.", "dispatch.reset", "dispatch.resume")):
                types[e["type"]] = types.get(e["type"], 0) + 1
        out["event_counts"] = types
        out["handler_errors"] = {h.name: h.last_error for h in reg.handlers if h.errors}
        check("no handler errors", not out["handler_errors"], json.dumps(out["handler_errors"])[:300])
    finally:
        evl.get_evaluator(bus).stop()
        fw.detach() if hasattr(fw, "detach") else None
        await bus.stop()
    out["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]
    print(json.dumps(out, indent=2, default=str))
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
