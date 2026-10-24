"""TDM-31 "done when" proof with a REAL local Factory droid (droid-sdk, 2 turns of Factory credits).

    cd services/orchestrator && uv run python scripts/tdm31_wait_proof.py

Throwaway local SpacetimeDB on 127.0.0.1:$TDM31_PORT (3131); never maincloud. Droid state and workdir
live under $TMPDIR/tdm-31-droid.

  turn 1: a trivial droid writes resume_payload.json (with a "next" value it is told to pick) and parks
          with ``WAITING_FOR dataset:accepted_count>=1 timeout=1h``; TDM-30 stores it with agent_wait_for
          (ticket-scoped, timeout_s 3600).
  manual: ``submit_review(episode, accepted)`` bumps dataset.accepted_count to 1.
  bridge: the TDM-27 resume bridge (real FactoryClient as its resumer) claims with agent_resume and
          sends the message: predicate, what changed, the saved resume_payload and the ticket snapshot.
  turn 2: the droid must quote the payload's "next" value and the snapshot's accepted_count, then DONE.
Prints a JSON summary and exits non-zero if a check fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM31_PORT", "3131"))
ROOT = Path(tempfile.gettempdir()) / "tdm-31-droid"
NEXT = "assemble-dataset-31"
checks: list[tuple[str, bool, str]] = []

BRIEF = f"""This is a short test of the wait-for protocol. Do exactly this and nothing else:
1. Write resume_payload.json in your workdir containing {{"done": "parked", "next": "{NEXT}"}}, print the
   RESUME_PAYLOAD line, and end your reply with exactly this final line:
WAITING_FOR dataset:accepted_count>=1 timeout=1h
2. Later you will get a message starting with [resume-bridge]. Do not read any file. Reply with one line,
   taking both values ONLY from that message:
   PAYLOAD_NEXT=<the "next" value of the resume_payload in the message> ACCEPTED=<the dataset accepted_count in the ticket snapshot>
   and then end with DONE."""


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


async def wait(pred, timeout: float, what: str, every: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


async def main() -> int:
    st = LocalStack(PORT, "tdm31wait", root=ROOT / "stack")
    assert "maincloud" not in st.host
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        return await run(st)
    finally:
        st.stop()


async def run(st: LocalStack) -> int:
    tok = st.token("orchestrator")
    os.environ.update({"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "SPACETIME_TOKEN": tok,
                       "FACTORY_MODE": "local"})
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient
    from rdf_factory import FactoryClient, FactoryConfig

    from rdf_orchestrator import resume_bridge as rb
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.handlers import resume_bridge as rb_handlers
    from rdf_orchestrator.log import configure_logging
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    configure_logging("WARNING")
    c = st.client()
    q = st.sql

    c.call_reducer("register_robot", "test.tdm31-wait-robot", "yam", "fake", "{}")
    rid = int(q("SELECT * FROM robot WHERE name = 'test.tdm31-wait-robot'")[0]["id"])
    c.call_reducer("create_ticket", rid, "[FAKE] tdm31 wait-for proof", 0.8, 5000, "")
    tid = int(st.newest("ticket")["id"])
    c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", "modal-vol://fake/tdm31/ep", "", "{}", 10, 1000,
                   None, None, None, None)
    ep = int(st.newest("episode")["id"])

    cfg = FactoryConfig.from_env("local")
    cfg.state_dir, cfg.work_root = ROOT / "state", ROOT / "sessions"
    starter = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, tok))
    bridge_client = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, tok))
    sent: list[tuple[int, str, float]] = []

    class Timed:
        async def resume(self, agent_session_id, text, *, reason="", claim=True, finish_on_idle=False):
            sent.append((agent_session_id, text, time.time()))
            return await bridge_client.resume(agent_session_id, text, reason=reason, claim=claim,
                                              finish_on_idle=finish_on_idle)

    reg = Registry()
    rb_handlers.register(reg)
    bus = Bus(reg, BusClient(host=st.host, db=st.db, token=tok, max_backoff=2.0), workers=4)
    br = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, tok),
                         resumer_factory=lambda: Timed(), poll_s=5, bridge_id="bridge-tdm31")
    rb._instances[id(bus)] = br
    await bus.start()
    await wait(lambda: bus.state == "live", 30, "bridge live")
    out: dict = {"ticket_id": tid, "episode_id": ep}

    # ---- turn 1: the droid parks itself
    r1 = await starter.start_session("data_droid", BRIEF, ticket_id=tid, name="tdm31-wait-droid")
    sid = r1.agent_session_id
    out["turn1"] = {"status": r1.status, "seconds": r1.seconds, "credits": r1.credits, "waiting_for": r1.waiting_for,
                    "resume_payload": r1.resume_payload, "factory_session_id": r1.factory_session_id}
    row = q(f"SELECT * FROM agent_session WHERE id = {sid}")[0]
    wf = row.get("waiting_for_json")
    wf = wf.get("some") if isinstance(wf, dict) else (wf[1] if isinstance(wf, list) and len(wf) == 2 else wf)
    pred = json.loads(wf) if isinstance(wf, str) and wf else {}
    check("turn 1: the row is waiting_for on dataset.accepted_count >= 1, ticket-scoped, timeout 3600 s",
          row["status"] == "waiting_for" and pred.get("table") == "dataset" and pred.get("ticket_id") == tid
          and pred.get("timeout_s") == 3600, json.dumps(pred))
    if row["status"] != "waiting_for":
        print(json.dumps(out, indent=2, default=str))
        return 1
    await wait(lambda: sid in br.waiting, 10, "bridge tracks the droid's wait")
    await asyncio.sleep(1.0)
    check("bridge does not wake the droid before the review", not sent)
    check("bridge holds a 1 h deadline for it", br.waiting[sid].timeout_s() == 3600.0, str(br.waiting[sid].timeout_s()))

    # ---- the manual review
    t0 = time.time()
    c.call_reducer("submit_review", ep, True, "[FAKE] tdm31 manual accept", "{}")
    await wait(lambda: sent, 10, "bridge calls FactoryClient.resume")
    lat = sent[0][2] - t0
    check("submit_review(accepted) -> bridge resume call", lat < 2.0, f"{lat * 1000:.0f} ms")
    msg = sent[0][1]
    check("the bridge message carries the predicate, the payload and the ticket snapshot",
          "dataset.accepted_count >= 1" in msg and NEXT in msg and f"snapshot for ticket {tid}" in msg
          and '"accepted_count":1' in msg, msg[:600])
    d = await wait(lambda: [json.loads(e["payload_json"]) for e in st.events() if e["type"] == rb.EV_DELIVERED], 600,
                   "turn 2 finished (agent.resume_delivered)")
    row = q(f"SELECT * FROM agent_session WHERE id = {sid}")[0]
    replies = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "droid.assistant_text"]
    last = (replies[-1].get("data", {}).get("full_text") or replies[-1].get("text", "")) if replies else ""
    m_next = re.search(r"PAYLOAD_NEXT=(\S+)", last)
    m_acc = re.search(r"ACCEPTED=(\d+)", last)
    check("turn 2: native resume on the same Factory session, ended DONE",
          d[0]["mode"] == "native" and d[0]["factory_session_id"] == r1.factory_session_id and row["status"] == "done",
          f"turn_status={d[0].get('turn_status')} row={row['status']}")
    check("turn 2 step text proves the droid read the payload and the snapshot",
          bool(m_next and m_next.group(1).strip(".,") == NEXT and m_acc and m_acc.group(1) == "1"), last[:300])
    check("exactly one agent.resumed claim", len([e for e in st.events() if e["type"] == "agent.resumed"]) == 1)
    out["resume"] = {"latency_ms": round(lat * 1000, 1), "delivered": {k: d[0].get(k) for k in (
        "predicate_text", "changed", "elapsed_s", "mode", "turn_status", "timed_out")}, "bridge_message": msg}
    out["droid_turn2_reply"] = last
    out["credits_total"] = row and (await bridge_client._spec(sid)).credits_total
    await bus.stop()
    br.stop()
    await starter.aclose()
    await bridge_client.aclose()
    out["checks"] = {"passed": sum(ok for _, ok, _ in checks), "failed": [n for n, ok, _ in checks if not ok]}
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["checks"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
