"""Who holds a robot right now, and give it back. Dry run by default.

    cd services/orchestrator
    uv run python ../../scripts/demo/release_robot_hold.py --target maincloud --robot yam-01           # who holds it
    uv run python ../../scripts/demo/release_robot_hold.py --target maincloud --robot yam-01 --yes     # release
    uv run python ../../scripts/demo/release_robot_hold.py --target local --host http://127.0.0.1:3191 --db x --robot 4

It lists every claimed task on the robot (arm control: clean_teleop / recovery_takeover; resets too) with its
ticket, the contributor holding it, how long the lease has left and the contributor's job-text number, masked to
the last 4 digits (from the private text store, ``RDF_TEXT_STORE`` or ``~/.local/state/rdf/tdm113_texts.sqlite3``,
opened read-only). Open robot jobs (start_teleop, run_eval, ...) on the robot are shown too.

``--yes`` calls ``release_task(task_id)`` for each claimed task (``--cancel`` calls ``cancel_task`` instead, which
withdraws it). The task goes back to open, so the teleop page of whoever held it loses control and the robot's
/control guard holds the arm within 2 s. A text-offer hold (the orchestrator heartbeating a texted YES claim for
up to 3 min) stops on its own: its next tick sees the task is no longer claimed and drops it, so it never comes
back. ``--task 12`` limits it to one task.

Auth: the owner SPACETIME_TOKEN from .env (never printed). Robot jobs are only listed, never changed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _target as T  # noqa: E402

TAG = "release"
NOTE = "Released by release_robot_hold.py"
OPEN_JOB = {"issued", "acked"}
DEFAULT_STORE = Path.home() / ".local" / "state" / "rdf" / "tdm113_texts.sqlite3"


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def mask(phone: str | None) -> str:
    return f"***{phone[-4:]}" if phone else "-"


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _micros(v) -> int:
    from rdf_common.spacetime_client import ts_micros

    try:
        return int(ts_micros(v)) if v not in (None, {}, []) else 0
    except Exception:  # noqa: BLE001 - an option none / odd value
        return 0


def phones_by_contributor(path: str | Path | None) -> dict[int, str]:
    """contributor_id -> E.164 number from the private text store, read-only. {} when there is no store."""
    p = Path(path or os.environ.get("RDF_TEXT_STORE") or DEFAULT_STORE)
    if not p.exists():
        return {}
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {int(c): str(ph) for ph, c in db.execute("SELECT phone, contributor_id FROM optin")}
        finally:
            db.close()
    except sqlite3.Error:
        return {}


def find_robot(robots: list[dict], arg: str) -> dict | None:
    a = arg.strip()
    for r in robots:
        if a.isdigit() and _i(r.get("id")) == int(a):
            return r
    for r in robots:
        if str(r.get("name") or "").lower() == a.lower():
            return r
    return None


def holds(snap: dict, robot_id: int, phones: dict[int, str], now_us: int, only_task: int | None = None) -> list[dict]:
    tickets = {_i(t.get("id")): t for t in snap["tickets"]}
    people = {_i(c.get("id")): c for c in snap["contributors"]}
    out = []
    for k in sorted(snap["tasks"], key=lambda r: _i(r.get("id"))):
        if _i(k.get("robot_id")) != robot_id or k.get("status") != "claimed":
            continue
        if only_task and _i(k.get("id")) != only_task:
            continue
        cid = _i(k.get("assignee_id"))
        exp = _micros(k.get("lease_expires_at"))
        left = (exp - now_us) / 1e6 if exp else 0.0
        tk = tickets.get(_i(k.get("ticket_id")), {})
        who = people.get(cid, {})
        out.append({"task": _i(k.get("id")), "type": k.get("type"), "ticket": _i(k.get("ticket_id")),
                    "ticket_status": tk.get("status", "?"), "contributor": cid,
                    "name": str(who.get("name") or "?")[:28], "phone": mask(phones.get(cid)),
                    "lease": f"{left:.0f}s left" if left > 0 else "lapsed",
                    "task_text": str(tk.get("task_description") or "")[:40]})
    return out


def snapshot(c) -> dict:
    return {"robots": c.sql("SELECT * FROM robot"), "tasks": c.sql("SELECT * FROM task"),
            "tickets": c.sql("SELECT * FROM ticket"), "contributors": c.sql("SELECT * FROM contributor"),
            "cmds": [r for r in c.sql("SELECT * FROM robot_command") if r.get("status") in OPEN_JOB]}


def release(c, rows: list[dict], cancel: bool) -> tuple[list[int], list[tuple[int, str]]]:
    done, failed = [], []
    for r in rows:
        try:
            if cancel:
                c.call_reducer("cancel_task", r["task"], NOTE)
            else:
                c.call_reducer("release_task", r["task"])
            done.append(r["task"])
        except Exception as e:  # noqa: BLE001 - report and keep going
            failed.append((r["task"], str(e)[:200]))
    return done, failed


COLS = ["task", "type", "ticket", "ticket_status", "contributor", "name", "phone", "lease", "task_text"]


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    T.add_target_args(ap)
    ap.add_argument("--robot", required=True, help="robot name (yam-01) or id (4)")
    ap.add_argument("--task", type=int, help="only this task id")
    ap.add_argument("--cancel", action="store_true", help="cancel_task (withdraw) instead of release_task (reopen)")
    ap.add_argument("--text-store", help="private text store (default RDF_TEXT_STORE or ~/.local/state/rdf/...)")
    args = ap.parse_args(argv)
    write = args.yes and not args.dry_run
    if client is None:
        host, db, token = T.resolve(args, TAG)
        log(f"target {args.target}: {host} / {db}  ({'WRITE' if write else 'dry run: pass --yes to release'})")
        client = T.client(host, db, token)
    phones = phones_by_contributor(args.text_store)
    with client as c:
        snap = snapshot(c)
        robot = find_robot(snap["robots"], args.robot)
        if robot is None:
            log(f"no robot {args.robot!r}; robots: " + ", ".join(f"{r.get('name')} (#{r.get('id')})" for r in snap["robots"]))
            return 2
        rid = _i(robot.get("id"))
        rows = holds(snap, rid, phones, int(time.time() * 1e6), args.task)
        log(f"robot {robot.get('name')} (#{rid}, {robot.get('status')})")
        print("\nCLAIMED TASKS (who holds it)")
        print(T.table(rows, COLS))
        jobs = [j for j in snap["cmds"] if _i(j.get("robot_id")) == rid]
        if jobs:
            print("\nOPEN ROBOT JOBS (listed only)")
            print(T.table([{"job": j.get("id"), "type": j.get("type"), "status": j.get("status"),
                            "ticket": j.get("ticket_id")} for j in jobs], ["job", "type", "status", "ticket"]))
        if not rows:
            log("nobody holds this robot: nothing to release")
            return 0
        verb = "cancel_task" if args.cancel else "release_task"
        if not write:
            print()
            for r in rows:
                log(f"would call {verb}({r['task']}) (held by contributor {r['contributor']} {r['name']}, {r['phone']})")
            log("dry run: nothing written")
            return 0
        done, failed = release(c, rows, args.cancel)
        for tid in done:
            log(f"{verb}({tid}) ok")
        for tid, err in failed:
            log(f"{verb}({tid}) FAILED: {err}")
        after = holds(snapshot(c), rid, phones, int(time.time() * 1e6), args.task)
        print("\nAFTER")
        print(T.table(after, COLS))
        if done:
            log("a text-offer hold on these tasks stops on the orchestrator's next tick (<= 20 s): the task is not claimed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
