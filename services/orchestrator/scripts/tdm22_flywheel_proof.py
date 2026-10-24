"""TDM-22 proof: the flywheel drives a ticket to ``shipped`` on a REAL (throwaway, local) SpacetimeDB,
with an orchestrator restart in the middle of training.

The robot and the agents are SIMULATED (``rdf_orchestrator.flywheel_sim``): fake eval trials,
clusters, plan, episodes, reviews and a fake Modal. The state machine, the bus, the reducers and
their validation are real. Refuses maincloud.

    spacetime start -l 127.0.0.1:3922 --data-dir $TMPDIR/tdm22-stdb/data --non-interactive
    cd spacetime && spacetime publish -s http://127.0.0.1:3922 tdm22test --yes
    set -a; source ../.env; set +a        # SPACETIME_TOKEN = publisher = owner (never printed)
    cd services/orchestrator && uv run python scripts/tdm22_flywheel_proof.py [--host ...] [--db ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.flywheel import TERMINAL, Flywheel
from rdf_orchestrator.flywheel_sim import SimScript, install_sim
from rdf_orchestrator.handlers.flywheel import register_flywheel
from rdf_orchestrator.log import configure_logging
from rdf_orchestrator.stdb import SpacetimeClient, call_reducer_http, sql

P = argparse.ArgumentParser()
P.add_argument("--host", default="http://127.0.0.1:3922")
P.add_argument("--db", default="tdm22test")
P.add_argument("--timeout", type=float, default=180)
A = P.parse_args()
if "maincloud" in A.host:
    sys.exit("refusing: this script writes SIMULATED rows; use a throwaway local server")
TOKEN = os.environ.get("SPACETIME_TOKEN") or sys.exit("SPACETIME_TOKEN not set (source .env)")
KW = {"host": A.host, "db": A.db, "token": TOKEN}
MARK = f"tdm22 proof {int(time.time())}"


def q(query: str) -> list[dict]:
    return sql(query, **KW)


def ticket_row(tid: int) -> dict:
    return q(f"SELECT * FROM ticket WHERE id = {tid}")[0]


async def run_process(label: str, script: SimScript, until) -> None:
    reg = Registry()
    fw = install_sim(Flywheel(reconcile_s=2.0), script, replace_external=True, host=A.host)
    register_flywheel(reg, fw)
    bus = Bus(reg, SpacetimeClient(host=A.host, db=A.db, token=TOKEN, max_backoff=5.0), workers=4)
    print(f"--- orchestrator process {label}: start", flush=True)
    await bus.start()
    end = time.monotonic() + A.timeout
    try:
        while not until(bus):
            if time.monotonic() > end:
                raise SystemExit(f"timeout in process {label}")
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)
    finally:
        fw.detach()
        await bus.stop()
    print(f"--- orchestrator process {label}: stopped", flush=True)


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))  # INFO shows every flywheel.* line
    call_reducer_http("register_robot", ["tdm22-sim-yam", "yam", "", "{}"], **KW)
    robot = max(q("SELECT * FROM robot WHERE name = 'tdm22-sim-yam'"), key=lambda r: r["id"])
    call_reducer_http("create_ticket", [robot["id"], MARK, 0.8, 10_000,
                                        json.dumps({"eval_trials": 10, "target_episodes": 3, "max_iterations": 3})], **KW)
    tid = max(r["id"] for r in q("SELECT * FROM ticket") if r["task_description"] == MARK)
    print(f"ticket {tid} created on {A.host}/{A.db} (robot {robot['id']})", flush=True)

    def training_running(bus) -> bool:
        return any(r.get("ticket_id") == tid and r.get("status") == "running" and r.get("iteration") == 0
                   for r in bus.rows("training_run"))

    def finished(bus) -> bool:
        t = bus.get("ticket", tid)
        return bool(t and t.get("status") in TERMINAL)

    script = dict(baseline_successes=2, candidate_successes=(5, 9))
    # process A: dies while iteration 0's fine-tune is running (the fake Modal never reports done)
    asyncio.run(run_process("A", SimScript(**script, complete_training=False),
                            lambda bus: training_running(bus) or finished(bus)))
    print(f"after A: ticket status={ticket_row(tid)['status']} iteration={ticket_row(tid)['iteration']}", flush=True)
    # process B: fresh memory; derives everything from rows
    asyncio.run(run_process("B", SimScript(**script), finished))

    t = ticket_row(tid)
    ev = [r for r in q("SELECT * FROM event_log") if f'"ticket_id":{tid}' in r["payload_json"].replace(" ", "")]
    ev.sort(key=lambda r: r["id"])
    print(f"\nticket {tid}: status={t['status']} iteration={t['iteration']}\n")
    for r in ev:
        p = json.loads(r["payload_json"])
        if r["type"] == "ticket.status_changed":
            print(f"  [{r['id']:>4}] {p['from']:>10} -> {p['to']:<10} it={p['iteration']}  {p['note']}")
        elif r["type"] in ("training.queued", "flywheel.shipped"):
            print(f"  [{r['id']:>4}] {r['type']}  {json.dumps({k: p.get(k) for k in ('iteration', 'episode_ids', 'checkpoint_uri') if k in p})}")
    runs = [r for r in q("SELECT * FROM training_run") if r["ticket_id"] == tid]
    print(f"\ntraining runs: {[(r['id'], r['iteration'], r['status']) for r in runs]}")
    ok = t["status"] == "shipped" and t["iteration"] == 1 and len(runs) == 2
    print("OK" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
