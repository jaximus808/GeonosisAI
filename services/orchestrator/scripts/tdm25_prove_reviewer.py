"""TDM-25 proof: the reviewer on the five labelled FIXTURE episodes, end to end through SpacetimeDB.

Boots a throwaway local SpacetimeDB (127.0.0.1:3025, db tdm25review, data under $TMPDIR/tdm-25,
never maincloud) with the TDM-28 ``LocalStack`` harness, publishes the module, then:

  contributor signs up (own identity) -> robot + ticket (budget $100) -> per fixture: a $10 task,
  claimed for the contributor, create_episode (file:// URIs to the fixture media), complete_task
  -> reviewer.review_episode (deterministic checks + REAL Claude unless --scripted)
  -> reviewer.record_review (qa_result rows + submit_review)

and checks: exactly one accept (clean_demo) and four rejects with the expected reasons, one
``payment`` row in status ``pending`` for the accepted episode only, ticket.spent_cents = 1000.

    cd services/orchestrator && uv run python scripts/tdm25_prove_reviewer.py [--scripted] [--bus] [--keep]

``--bus`` runs the real subscription path instead of calling the reviewer directly: a ``Bus`` with
only the ``tdm25_review_episode`` handler (no diagnose/planner/flywheel) subscribes to the local DB
and reviews the episodes as they are inserted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(HERE.parents[1] / "tests"))

import tdm25_fixtures as fx  # noqa: E402
from harness import LocalStack  # noqa: E402
from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402

from rdf_orchestrator import reviewer as rv  # noqa: E402

PORT, DB = 3025, "tdm25review"
PRICE = 1000


def opt_ts(v):
    return {"some": {"__timestamp_micros_since_unix_epoch__": int(v)}} if v is not None else {"none": []}


def start_bus(st: LocalStack, cfg: rv.ReviewConfig, make_judge):
    """A Bus running ONLY the TDM-25 handler, on its own thread + event loop."""
    import threading

    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.bus import registry as default_registry
    from rdf_orchestrator.handlers import reviewer as h
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    reg = Registry()
    reg.handlers = [x for x in default_registry.handlers if x.name == "tdm25_review_episode"]
    for t in ("episode", "ticket", "review"):
        reg.watch(t)
    h.make_config = lambda: cfg
    h.make_judge = lambda c: make_judge()
    h.make_stdb = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))

    def run():
        async def go():
            bus = Bus(reg, BusClient(host=st.host, db=st.db, token=st.token("orchestrator")), workers=4)
            await bus.start()
            while True:
                await asyncio.sleep(3600)
        asyncio.run(go())

    th = threading.Thread(target=run, name="tdm25-bus", daemon=True)
    th.start()
    time.sleep(2)
    return th


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true", help="use a scripted judge instead of calling Claude")
    ap.add_argument("--keep", action="store_true", help="leave the local SpacetimeDB running")
    ap.add_argument("--bus", action="store_true", help="review through the bus handler on a live subscription")
    args = ap.parse_args()

    out = REPO / "spikes" / "tdm-25-reviewer" / "tmp" / "proof"
    fixtures = fx.build_fixture_set(out)
    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-25")
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        orch = st.client("orchestrator")

        req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            contrib_token = json.loads(r.read())["token"]
        with SpacetimeClient(st.host, st.db, contrib_token) as c:
            c.call_reducer("register_contributor", "test.tdm25 alice", "", ["android_webxr"], "contributor", 0)
        contributor_id = int(st.newest("contributor")["id"])

        orch.call_reducer("register_robot", "test.tdm25-yam", "i2rt_yam", "v0", "{}")
        robot_id = int(st.newest("robot")["id"])
        orch.call_reducer("create_ticket", robot_id, f"test.tdm25 {fx.TASK}", 0.8, 10_000, "")
        ticket = st.newest("ticket")
        ticket_id = int(ticket["id"])

        cfg = rv.load_config(allow_fixtures=True)  # fixtures carry fixture:true; production rejects them
        if args.scripted:
            from test_reviewer import ScriptedJudge

            judge = ScriptedJudge()
        else:
            judge = rv.ClaudeJudge(cfg)
        if args.bus:
            start_bus(st, cfg, lambda: judge)

        by_episode: dict[int, fx.Fixture] = {}
        for f in fixtures:
            e = f.episode
            orch.call_reducer("create_task", ticket_id, e["kind"], PRICE, json.dumps({"fixture": f.name}))
            task_id = int(st.newest("task")["id"])
            orch.call_reducer("claim_task", task_id, contributor_id)
            if args.scripted and f.name == "off_task":  # before insert: the bus may review it immediately
                judge.off_task_ids.add(int((st.newest("episode") or {"id": 0})["id"]) + 1)
            orch.call_reducer("create_episode", ticket_id, task_id, 0, e["kind"], e["storage_uri"], e["actions_uri"],
                              e["cameras_json"], e["frame_count"], e["duration_ms"], opt_ts(e["takeover_ts"]),
                              opt_ts(e["handback_ts"]),
                              {"none": []} if e["success"] is None else {"some": e["success"]}, {"none": []})
            orch.call_reducer("complete_task", task_id, "{}")
            eid = int(st.newest("episode")["id"])
            by_episode[eid] = f

        print(f"\n{'fixture':22s} {'episode':>7s}  verdict   decided_by     reason")
        ok = True
        decisions = {}
        if args.bus:
            deadline = time.time() + 600
            while time.time() < deadline and len(st.sql(f"SELECT * FROM review WHERE ticket_id = {ticket_id}")) < len(by_episode):
                time.sleep(1)
            for r in st.sql(f"SELECT * FROM review WHERE ticket_id = {ticket_id}"):
                m = json.loads(r["metrics_json"])
                decisions[int(r["episode_id"])] = rv.ReviewDecision(int(r["episode_id"]), r["accepted"], r["reason"],
                                                                    m.get("decided_by", "?"), [], m)
        for eid, f in by_episode.items():
            if args.bus:
                d = decisions.get(eid) or rv.ReviewDecision(eid, False, "(no review written)", "missing", [], {})
            else:
                row = st.sql(f"SELECT * FROM episode WHERE id = {eid}")[0]
                d = rv.review_episode(row, ticket, config=cfg, judge=judge)
                rv.record_review(orch, d)
            verdict = "ACCEPT" if d.accepted else "reject"
            print(f"{f.name:22s} {eid:7d}  {verdict:8s}  {d.decided_by:13s}  {d.reason}")
            good = d.accepted == f.expect_accept and (f.expect_reason.lower() in d.reason.lower())
            if f.expect_check == "claude":
                good = good and d.decided_by == "claude"
            ok &= good

        reviews = st.sql(f"SELECT * FROM review WHERE ticket_id = {ticket_id}")
        payments = st.sql(f"SELECT * FROM payment WHERE ticket_id = {ticket_id}")
        eps = {int(r["id"]): r["review_status"] for r in st.sql(f"SELECT * FROM episode WHERE ticket_id = {ticket_id}")}
        qa = st.sql("SELECT * FROM qa_result")
        spent = int(st.sql(f"SELECT * FROM ticket WHERE id = {ticket_id}")[0]["spent_cents"])
        accepted = [int(r["episode_id"]) for r in reviews if r["accepted"]]
        clean_id = next(e for e, f in by_episode.items() if f.name == "clean_demo")
        print(f"\nreviews: {len(reviews)} ({len(accepted)} accepted)   episode review_status: {eps}")
        print(f"payments: {[(int(p['episode_id']), p['status'], int(p['amount_cents'])) for p in payments]}")
        print(f"ticket.spent_cents: {spent}   qa_result rows: {len(qa)}")
        ok &= accepted == [clean_id]
        ok &= [(int(p["episode_id"]), p["status"], int(p["amount_cents"])) for p in payments] == [(clean_id, "pending", PRICE)]
        ok &= spent == PRICE
        ok &= all(r["reason"] for r in reviews if not r["accepted"])
        print("\nOK" if ok else "\nFAILED")
        return 0 if ok else 1
    finally:
        if not args.keep:
            st.stop()


if __name__ == "__main__":
    sys.exit(main())
