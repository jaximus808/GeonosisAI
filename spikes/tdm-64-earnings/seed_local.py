"""TDM-64: throwaway local SpacetimeDB seeded with payments, for the earnings page and spend panel.

    cd services/orchestrator && uv run python ../../spikes/tdm-64-earnings/seed_local.py [--with-orchestrator]

Never touches maincloud. Boots spacetime on 127.0.0.1:3064 (db tdm64earn, data under
$TMPDIR/tdm-64/), seeds one ticket with:
  - contributor A ("test.avery", no payout account): one ACCEPTED clean demo (payment pending ->
    awaiting_onboarding once the orchestrator payout handler runs), one REJECTED demo (reason
    shown, no payment), one paid reset task (pending -> awaiting_onboarding)
  - contributor B ("test.sam", fake acct id): one accepted demo marked paid with a fake
    tr_ ref (seed only, no Stripe call) and one marked failed
  - one done training run with gpu_cost_cents 340
Contributor A's browser token is written to $TMPDIR/tdm-64/run-3064/tokens/contributor_a.token
(put it in localStorage key rdf.spacetime.token.tdm64earn to view /earnings as A).
With --with-orchestrator the real orchestrator runs on :8064 against this DB (payout handler,
dispatcher, /payments/* and /tickets/{id}/settings).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
os.environ.setdefault("TDM28_ROOT", str(Path(os.environ.get("TMPDIR", "/tmp")) / "tdm-64"))
from harness import LocalStack  # noqa: E402

from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402

NONE = {"none": []}


def mint(st: LocalStack, name: str) -> SpacetimeClient:
    req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    p = st.tokens / f"{name}.token"
    p.write_text(d["token"])
    os.chmod(p, 0o600)
    return SpacetimeClient(st.host, st.db, d["token"])


def newest(st: LocalStack, table: str, where: str = "") -> dict:
    row = st.newest(table, where)
    assert row is not None, f"no {table} row ({where})"
    return row


def episode(st, o, ticket, task, cid) -> int:
    o.call_reducer("create_episode", ticket, task, cid, "clean_teleop", f"fake://tdm64/ep-task{task}", "fake://a",
                   "{}", 300, 10000, NONE, NONE, {"some": True}, NONE)
    return int(newest(st, "episode", f"task_id = {task}")["id"])


def main() -> None:
    with_orch = "--with-orchestrator" in sys.argv
    st = LocalStack(3064, "tdm64earn")
    st.start_server()
    st.publish()
    st.make_identity("orchestrator")
    o = st.client("orchestrator")

    o.call_reducer("register_robot", "test.tdm64-yam", "yam", "fake-0", "{}")
    robot = int(newest(st, "robot")["id"])
    o.call_reducer("create_ticket", robot, "Put the red cup in the bin [test.tdm64 seed]", 0.8, 5000, "")
    ticket = int(newest(st, "ticket")["id"])
    for s in ("baseline", "diagnose", "plan_data", "dispatch"):
        o.call_reducer("set_ticket_status", ticket, s, "test.tdm64 seed")
    o.call_reducer("record_data_plan", ticket, "clean_teleop", 4, "test.tdm64 seed: 4 clean demos", "{}")
    plan = int(newest(st, "data_plan")["id"])
    o.call_reducer("approve_spend", ticket, plan, 0, "human:tdm64-seed")

    a = mint(st, "contributor_a")
    a.call_reducer("register_contributor", "test.avery", "", ["phone"], "contributor", ticket)
    b = mint(st, "contributor_b")
    b.call_reducer("register_contributor", "test.sam", "", ["laptop"], "contributor", ticket)
    cs = {c["name"]: int(c["id"]) for c in st.sql("SELECT * FROM contributor")}
    ca, cb = cs["test.avery"], cs["test.sam"]
    o.call_reducer("set_contributor_payout_account", cb, "acct_1TdmSixtyFourFake", True)

    def task(kind: str, price: int) -> int:
        o.call_reducer("create_task", ticket, kind, price,
                       json.dumps({"instructions": f"test.tdm64 seed {kind}", "data_plan_id": plan if kind != "reset" else 0}))
        return int(newest(st, "task")["id"])

    def do(client, cid, kind, price) -> int:
        t = task(kind, price)
        client.call_reducer("claim_task", t, cid)
        client.call_reducer("complete_task", t, json.dumps({"seed": "test.tdm64"}))
        return t

    t1 = do(a, ca, "clean_teleop", 1000)
    o.call_reducer("submit_review", episode(st, o, ticket, t1, ca), True, "Clean grasp and place; cup inside the bin.", "{}")
    t2 = do(a, ca, "clean_teleop", 1000)
    o.call_reducer("submit_review", episode(st, o, ticket, t2, ca), False,
                   "Cup left on the bin rim at handback: the gripper opened 4 cm too early.", "{}")
    t3 = do(b, cb, "clean_teleop", 1000)
    o.call_reducer("submit_review", episode(st, o, ticket, t3, cb), True, "Good demo.", "{}")
    p3 = int(newest(st, "payment", f"contributor_id = {cb}")["id"])
    o.call_reducer("mark_payment_paid", p3, "stripe_test", "tr_test_tdm64_seed_not_real")
    t4 = do(b, cb, "clean_teleop", 1000)
    o.call_reducer("submit_review", episode(st, o, ticket, t4, cb), True, "Good demo.", "{}")
    p4 = int(newest(st, "payment", f"contributor_id = {cb}")["id"])
    o.call_reducer("mark_payment_failed", p4, "test.tdm64 seed: Stripe refused the test transfer (account not transfer-capable)")
    t5 = do(a, ca, "reset", 200)
    o.call_reducer("reserve_task_payment", t5)

    accepted = [int(e["id"]) for e in st.sql("SELECT * FROM episode") if e["review_status"] == "accepted"]
    o.call_reducer("queue_training_run", ticket, json.dumps(accepted), "{}")
    run = int(newest(st, "training_run")["id"])
    o.call_reducer("set_training_status", run, "running", {"some": "test-tdm64"}, NONE, NONE, {"some": 120})
    o.call_reducer("set_training_status", run, "done", NONE, {"some": "fake://tdm64/ckpt"}, NONE, {"some": 340})

    print(json.dumps({"ticket": ticket, "plan": plan, "contributor_a": ca, "contributor_b": cb,
                      "payments": st.sql("SELECT * FROM payment"),
                      "ticket_row": st.sql(f"SELECT * FROM ticket WHERE id = {ticket}")}, default=str, indent=1), flush=True)
    if with_orch:
        st.spawn_orchestrator(port=8064)
    print(f"# up: SPACETIME_HOST={st.host} DB={st.db}; A token at {st.tokens / 'contributor_a.token'}", flush=True)
    try:
        while all(p.poll() is None for p in st.procs.values()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()


if __name__ == "__main__":
    main()
