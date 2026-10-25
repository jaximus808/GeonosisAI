"""Hide today's test tickets before the demo by cancelling them. Nothing is deleted.

    cd services/orchestrator
    uv run python ../../scripts/demo/cancel_old_tickets.py --target maincloud              # dry run (default)
    uv run python ../../scripts/demo/cancel_old_tickets.py --target maincloud --yes        # write
    uv run python ../../scripts/demo/cancel_old_tickets.py --target local --host http://127.0.0.1:3191 --db samplesdb --yes

It lists every ticket that is not shipped / failed / cancelled and calls
``set_ticket_status(id, "cancelled", "Cleared before the demo")`` for each, except the ids in
``--keep 4,7``. ``[Sample]`` tickets are terminal already, so they are never touched.

Robot release (TDM-114): the live orchestrator's arbitration releases a cancelled ticket's robot
(``robot.released``: its paused / running rollout is stopped, the next queued eval kicked). The
reducer itself cannot stop a job, so after cancelling this script waits up to ``--release-wait`` s and
reports, per ticket, whether it held a robot job and whether that job was released. A job still open
at the end means no orchestrator is running; it says so rather than claiming a release.
Open / claimed marketplace tasks of the cancelled tickets are listed too (``--cancel-tasks`` withdraws
them with ``cancel_task``, which the dispatcher would otherwise do once it sees the cancel).

Auth: the owner SPACETIME_TOKEN from .env (never printed).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _target as T  # noqa: E402

TAG = "cancel"
NOTE = "Cleared before the demo"
TERMINAL = {"shipped", "failed", "cancelled"}
OPEN_JOB = {"issued", "acked"}
JOB_TYPES = {"run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint"}


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def ids(text: str | None) -> set[int]:
    out: set[int] = set()
    for part in (text or "").replace(" ", ",").split(","):
        if part.strip():
            out.add(int(part.strip().lstrip("#")))
    return out


def snapshot(c) -> dict:
    tickets = sorted(c.sql("SELECT * FROM ticket"), key=lambda r: int(r["id"]))
    cmds = [r for r in c.sql("SELECT * FROM robot_command") if r.get("status") in OPEN_JOB and r.get("type") in JOB_TYPES]
    tasks = [r for r in c.sql("SELECT * FROM task") if r.get("status") in ("open", "claimed")]
    robots = {int(r["id"]): r for r in c.sql("SELECT * FROM robot")}
    return {"tickets": tickets, "cmds": cmds, "tasks": tasks, "robots": robots}


def rows_for(s: dict, which: set[int] | None = None) -> list[dict]:
    out = []
    for t in s["tickets"]:
        tid = int(t["id"])
        if which is not None and tid not in which:
            continue
        held = [c for c in s["cmds"] if int(c.get("ticket_id") or 0) == tid]
        tasks = [k for k in s["tasks"] if int(k.get("ticket_id") or 0) == tid]
        robot = s["robots"].get(int(t.get("robot_id") or 0), {})
        out.append({"id": tid, "status": t["status"], "iter": t.get("iteration"),
                    "robot": f"{robot.get('name', '?')} ({robot.get('status', '?')})",
                    "robot_job": ",".join(f"#{c['id']} {c['type']}/{c['status']}" for c in held) or "-",
                    "open_tasks": len(tasks), "task": str(t.get("task_description") or "")[:48]})
    return out


COLS = ["id", "status", "iter", "robot", "robot_job", "open_tasks", "task"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    T.add_target_args(ap)
    ap.add_argument("--keep", help="ticket ids to leave alone, e.g. 4,7")
    ap.add_argument("--cancel-tasks", action="store_true", help="also cancel_task their open/claimed tasks")
    ap.add_argument("--release-wait", type=float, default=20.0, help="seconds to wait for robot.released")
    args = ap.parse_args()
    write = args.yes and not args.dry_run
    host, db, token = T.resolve(args, TAG)
    keep = ids(args.keep)
    log(f"target {args.target}: {host} / {db}  ({'WRITE' if write else 'dry run: pass --yes to write'})")

    with T.client(host, db, token) as c:
        before = snapshot(c)
        live = [t for t in before["tickets"] if t["status"] not in TERMINAL]
        todo = [t for t in live if int(t["id"]) not in keep]
        kept = [t for t in live if int(t["id"]) in keep]
        log(f"{len(before['tickets'])} tickets, {len(live)} not terminal; cancelling {len(todo)}"
            + (f", keeping {sorted(int(t['id']) for t in kept)}" if kept else ""))
        print("\nBEFORE (non-terminal tickets)")
        print(T.table(rows_for(before, {int(t["id"]) for t in live}), COLS))
        if missing := keep - {int(t["id"]) for t in before["tickets"]}:
            log(f"note: --keep ids not found: {sorted(missing)}")
        if not todo:
            log("nothing to cancel")
            return 0
        if not write:
            print()
            for t in todo:
                log(f"would call set_ticket_status({t['id']}, 'cancelled', {NOTE!r})")
            log("dry run: nothing written")
            return 0

        start_events = max([int(e["id"]) for e in c.sql("SELECT id FROM event_log")] or [0])
        done, failed = [], []
        for t in todo:
            tid = int(t["id"])
            try:
                c.call_reducer("set_ticket_status", tid, "cancelled", NOTE)
                done.append(tid)
            except Exception as e:  # noqa: BLE001 - report and keep going
                failed.append((tid, str(e)[:200]))
        log(f"cancelled {len(done)}" + (f"; {len(failed)} failed" if failed else ""))
        for tid, err in failed:
            log(f"  ticket {tid}: {err}")

        held = {int(cm.get("ticket_id") or 0): cm for cm in before["cmds"] if int(cm.get("ticket_id") or 0) in done}
        if args.cancel_tasks:
            for k in before["tasks"]:
                if int(k.get("ticket_id") or 0) in done:
                    try:
                        c.call_reducer("cancel_task", int(k["id"]), NOTE)
                        log(f"  cancel_task {k['id']} ({k['type']}, ticket {k['ticket_id']})")
                    except Exception as e:  # noqa: BLE001
                        log(f"  cancel_task {k['id']} failed: {str(e)[:160]}")

        # TDM-114: wait for the orchestrator's arbitration to release robots those tickets held.
        released: dict[int, dict] = {}
        deadline = time.monotonic() + (args.release_wait if held else 0)
        while True:
            for e in c.sql(f"SELECT * FROM event_log WHERE id > {start_events}"):
                if e.get("type") == "robot.released":
                    try:
                        import json
                        p = json.loads(e.get("payload_json") or "{}")
                    except Exception:  # noqa: BLE001
                        p = {}
                    tid = int(p.get("ticket_id") or 0)
                    if tid in held:
                        released[tid] = {"event_id": int(e["id"]), **p}
            still = {int(cm["id"]) for cm in c.sql("SELECT * FROM robot_command") if cm.get("status") in OPEN_JOB}
            pending = [tid for tid, cm in held.items() if tid not in released and int(cm["id"]) in still]
            if not pending or time.monotonic() > deadline:
                break
            time.sleep(1.0)

        after = snapshot(c)
        print("\nAFTER (the same tickets)")
        print(T.table(rows_for(after, {int(t["id"]) for t in live}), COLS))
        print("\nROBOT RELEASE (TDM-114)")
        if not held:
            log("none of the cancelled tickets held a robot job: nothing to release")
        open_now = {int(cm["id"]) for cm in after["cmds"]}
        for tid, cm in held.items():
            if tid in released:
                log(f"ticket {tid}: robot {cm['robot_id']} released (robot.released event {released[tid]['event_id']})")
            elif int(cm["id"]) not in open_now:
                log(f"ticket {tid}: robot {cm['robot_id']} job #{cm['id']} {cm['type']} closed (robot free)")
            else:
                log(f"ticket {tid}: robot {cm['robot_id']} STILL HELD by job #{cm['id']} {cm['type']} after "
                    f"{args.release_wait:.0f}s. No orchestrator released it: start one (or stop the job on the robot).")
        left = [k for k in after["tasks"] if int(k.get("ticket_id") or 0) in done]
        if left:
            log(f"{len(left)} open/claimed tasks remain on cancelled tickets: "
                + ", ".join(f"#{k['id']}" for k in left) + ("" if args.cancel_tasks else " (pass --cancel-tasks with --yes to withdraw them as they are cancelled)"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
