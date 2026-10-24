"""TDM-27 proof with a REAL local Factory droid (droid-sdk, ~2 turns of Factory credits).

    cd services/orchestrator && uv run python scripts/tdm27_real_droid_proof.py

Throwaway local SpacetimeDB on 127.0.0.1:$TDM27_PORT (3127); never maincloud. The droid runs locally
through TDM-30's ``FactoryClient`` (FACTORY_MODE=local); its workdir and state live under
$TMPDIR/tdm-27-droid.

  turn 1: start_session -> the droid writes note.txt + resume_payload.json and ends with
          ``WAITING_FOR training_run:status==done``; TDM-30 parks the row with agent_wait_for.
  bridge: the resume bridge (real FactoryClient as its resumer) sees the wait, we set the
          training_run to done, and it claims (agent_resume) and calls FactoryClient.resume.
  turn 2: the droid gets the [resume-bridge] message, appends to note.txt and ends with DONE.
Prints a JSON summary (latency from set_training_status to the resume call, credits) and exits
non-zero if a check fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM27_PORT", "3127"))
ROOT = Path(tempfile.gettempdir()) / "tdm-27-droid"
checks: list[tuple[str, bool, str]] = []

BRIEF = """This is a short pause/resume test of our resume bridge. Do exactly this and nothing else:
1. In your workdir, create note.txt containing the single line: step1 waiting
2. You now have to wait until the training run finishes. Write resume_payload.json in your workdir with
   {"done": "wrote note.txt", "next": "append resumed to note.txt"}, print the RESUME_PAYLOAD line, and end
   your reply with exactly this final line:
WAITING_FOR training_run:status==done
3. Later you will get a message starting with [resume-bridge]. Then append the line `step2 resumed` to
   note.txt, reply with one sentence saying what the bridge told you changed, and end with DONE."""


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
    st = LocalStack(PORT, "tdm27droid", root=ROOT / "stack")
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
    # Point every env-driven client at the throwaway DB before rdf_factory loads .env (setdefault).
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

    # fixtures: robot, ticket, one accepted episode, a running training run
    c.call_reducer("register_robot", "test.tdm27-droid-robot", "yam", "fake", "{}")
    rid = int(q("SELECT * FROM robot WHERE name = 'test.tdm27-droid-robot'")[0]["id"])
    c.call_reducer("create_ticket", rid, "[FAKE] tdm27 real-droid resume proof", 0.8, 5000, "")
    tid = int(st.newest("ticket")["id"])
    c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", "modal-vol://fake/tdm27/droid", "", "{}", 10, 1000,
                   None, None, None, None)
    ep = int(st.newest("episode")["id"])
    c.call_reducer("submit_review", ep, True, "[FAKE] ok", "{}")
    c.call_reducer("queue_training_run", tid, json.dumps([ep]), "")
    run_id = int(st.newest("training_run")["id"])
    c.call_reducer("set_training_status", run_id, "running", "fake-call", None, None, None)

    cfg = FactoryConfig.from_env("local")
    cfg.state_dir, cfg.work_root = ROOT / "state", ROOT / "sessions"
    starter = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, tok))
    bridge_client = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, tok))

    class Timed:
        """Records when the bridge calls the real client (the resume call itself is untouched)."""

        def __init__(self):
            self.calls = []

        async def resume(self, agent_session_id, text, *, reason="", claim=True, finish_on_idle=False):
            self.calls.append((agent_session_id, text, time.time()))
            return await bridge_client.resume(agent_session_id, text, reason=reason, claim=claim,
                                              finish_on_idle=finish_on_idle)

    timed = Timed()
    reg = Registry()
    rb_handlers.register(reg)
    bus = Bus(reg, BusClient(host=st.host, db=st.db, token=tok, max_backoff=2.0), workers=4)
    br = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, tok),
                         resumer_factory=lambda: timed, poll_s=5, bridge_id="bridge-real-droid")
    rb._instances[id(bus)] = br
    await bus.start()
    await wait(lambda: bus.state == "live", 30, "bridge live")
    out: dict = {"ticket_id": tid, "training_run_id": run_id}

    # ---- turn 1: the droid parks itself
    t_turn1 = time.time()
    r1 = await starter.start_session("training_droid", BRIEF, ticket_id=tid, name="tdm27-real-droid")
    sid = r1.agent_session_id
    out["turn1"] = {"status": r1.status, "seconds": round(time.time() - t_turn1, 1), "credits": r1.credits,
                    "waiting_for": r1.waiting_for, "factory_session_id": r1.factory_session_id}
    row = q(f"SELECT * FROM agent_session WHERE id = {sid}")[0]
    check("turn 1: droid ended with WAITING_FOR and the row is waiting_for", r1.status == "waiting_for"
          and row["status"] == "waiting_for", f"{row.get('waiting_for_json')}")
    if r1.status != "waiting_for":
        print(json.dumps(out, indent=2, default=str))
        return 1
    await wait(lambda: sid in br.waiting, 10, "bridge tracks the droid's wait")
    await asyncio.sleep(1.0)
    check("bridge does not wake the droid while the run is still running", not timed.calls)

    # ---- the real-time update
    t0 = time.time()
    c.call_reducer("set_training_status", run_id, "done", None, "modal-vol://fake/tdm27/ckpt", None, None)
    await wait(lambda: timed.calls, 10, "bridge calls FactoryClient.resume")
    lat = timed.calls[0][2] - t0
    check("bridge called FactoryClient.resume within ~1 s of the training_run update", lat < 1.5, f"{lat * 1000:.0f} ms")
    d = await wait(lambda: [json.loads(e["payload_json"]) for e in st.events() if e["type"] == rb.EV_DELIVERED], 600,
                   "turn 2 finished (agent.resume_delivered)")
    row = q(f"SELECT * FROM agent_session WHERE id = {sid}")[0]
    note = Path(cfg.workdir_for(sid, "local")) / "note.txt"
    note_text = note.read_text() if note.exists() else ""
    check("turn 2: droid resumed natively on the same Factory session and finished DONE",
          d[0]["mode"] == "native" and d[0]["factory_session_id"] == r1.factory_session_id and row["status"] == "done",
          f"turn_status={d[0].get('turn_status')} row={row['status']}")
    check("the droid's workdir shows both steps", "step1" in note_text and "step2" in note_text, note_text.strip())
    resumed = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "agent.resumed"]
    check("exactly one agent.resumed claim (the bridge's)", len(resumed) == 1, resumed[0].get("reason", "")[:160] if resumed else "")
    msgs = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "droid.assistant_text"]
    out["resume"] = {"latency_ms": round(lat * 1000, 1), "turn2_seconds": round(d[0]["deliver_ms"] / 1000, 1),
                     "delivered": {k: d[0].get(k) for k in ("predicate_text", "changed", "elapsed_s", "mode", "turn_status")}}
    out["droid_last_reply"] = (msgs[-1].get("text") if msgs else None)
    await bus.stop()
    br.stop()
    await starter.aclose()
    await bridge_client.aclose()
    out["checks"] = {"passed": sum(ok for _, ok, _ in checks), "failed": [n for n, ok, _ in checks if not ok]}
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["checks"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
