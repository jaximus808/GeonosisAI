"""TDM-27 proof: the resume bridge against a THROWAWAY local SpacetimeDB (never maincloud).

    cd services/orchestrator && uv run python scripts/tdm27_resume_proof.py

The Factory client is a FAKE resumer that records when it is called (TDM-30's real client plugs in
through ``resume_bridge.make_resumer``). Everything else is real: the TDM-13 reducers, the bus
subscription over WebSocket, and HTTP reducer calls.

  1. Boots spacetime on 127.0.0.1:$TDM27_PORT (3127), publishes the working-tree module, mints an
     orchestrator identity. Two bridges (A and B) run in this process, each on its own socket, its
     own HTTP writer and its own fake resumer, so they really race through agent_resume.
  2. S1 waits for training_run.status == 'done'. We set it with set_training_status. Exactly one
     bridge resumes it; the script measures the time from the reducer call to the resume.
  3. S2 waits for dataset.accepted_count >= 3 (the ticket's done condition). Two episodes get accepted
     with submit_review; the second one resumes it.
  4. S3's predicate is already true when it waits: it resumes at once.
  5. S4: the resumer reports a Protocol B restart; the new Factory session id lands in
     factory_session_ids_json.
  6. S5 waits on a ticket that does not exist: agent.wait_error is logged; nothing crashes.
  7. S6 waits on contributor_presence (not on the main bus): the side subscription picks it up.
  8. Restart: both bridges stop. S7's predicate becomes true while no bridge runs. A fresh bridge C
     boots and resumes it from the snapshot.
Prints a JSON summary and exits non-zero if any check fails.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM27_PORT", "3127"))
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


async def wait(pred, timeout: float, what: str, every: float = 0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


class FakeResumer:
    """[FAKE] Factory client: records (agent_session_id, text, wall time). Optionally reports a restart."""

    def __init__(self, name: str, restart_for: set[int] | None = None):
        self.name, self.calls, self.restart_for = name, [], restart_for or set()

    def resume(self, agent_session_id: int, text: str):
        self.calls.append((agent_session_id, text, time.time()))
        if agent_session_id in self.restart_for:
            return {"mode": "restart", "new_factory_session_id": f"fake-fs-restart-{agent_session_id}"}
        return {"mode": "native"}


async def real_client_leg(st, tok, tid, run2, session, wait_for, srow, events, new_bridge_with) -> dict:
    try:
        from rdf_factory import FactoryClient, FactoryConfig
        from rdf_factory.backends import RawTurn
    except ImportError as e:
        print(f"[SKIP] S8 real TDM-30 client: rdf_factory not importable ({e}); run with "
              "`uv run --with-editable ../factory python scripts/tdm27_resume_proof.py`", flush=True)
        return {"skipped": str(e)}
    from rdf_common.spacetime_client import AsyncSpacetimeClient

    class FakeDroidBackend:
        """[FAKE] droid: records the turn it is given and answers DONE. No Factory credits are used."""

        mode = "local"

        def __init__(self):
            self.turns = []

        async def run_turn(self, spec, factory_session_id, text, emit, on_session_id):
            self.turns.append((spec.agent_session_id, factory_session_id, text, time.time()))
            emit("assistant_text", "[FAKE] droid resumed by the bridge; finishing")
            return RawTurn(factory_session_id or "", "success", True, "[FAKE] resumed.\nDONE", seconds=0.01)

        async def interrupt(self, spec, factory_session_id):
            return None

        async def aclose(self):
            return None

    root = Path(tempfile.gettempdir()) / "tdm-27" / "factory"
    backend = FakeDroidBackend()
    cfg = FactoryConfig(mode="local", state_dir=root / "state", work_root=root / "sessions")
    client = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, tok), backends={"local": backend})
    bus_d, br_d = new_bridge_with("D-real-client", client)
    await bus_d.start()
    await wait(lambda: bus_d.state == "live", 30, "bridge D live")
    s8 = session("s8", tid)
    wait_for(s8, {"table": "training_run", "id": run2, "field": "status", "value": "done"})
    await wait(lambda: s8 in br_d.waiting, 10, "S8 tracked")
    st.client().call_reducer("set_training_status", run2, "done", None, "modal-vol://fake/tdm27/ckpt2", None, None)
    t0 = time.time()
    await wait(lambda: backend.turns, 10, "S8 droid turn")
    lat = backend.turns[0][3] - t0
    await wait(lambda: srow(s8)["status"] == "done", 10, "S8 done")
    d8 = await wait(lambda: events("agent.resume_delivered", s8), 10, "S8 delivered")
    sid, fsid, text, _ = backend.turns[0]
    check("S8 real FactoryClient.resume: native turn on the same Factory session, [resume-bridge] prefix, "
          "row done after the turn", fsid == "fake-fs-s8" and text.startswith("[resume-bridge] The condition")
          and srow(s8)["status"] == "done" and d8[0]["turn_status"] == "done",
          f"{lat * 1000:.0f} ms from set_training_status to the droid turn")
    await bus_d.stop()
    br_d.stop()
    await client.aclose()
    return {"latency_ms": round(lat * 1000, 1), "turns": len(backend.turns), "turn_status": d8[0]["turn_status"]}


async def main() -> int:
    st = LocalStack(PORT, "tdm27resume", root=Path(tempfile.gettempdir()) / "tdm-27")
    assert "maincloud" not in st.host
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        return await run(st)
    finally:
        st.stop()


async def run(st: LocalStack) -> int:
    from rdf_common.spacetime_client import SpacetimeClient

    from rdf_orchestrator import resume_bridge as rb
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.handlers import resume_bridge as rb_handlers
    from rdf_orchestrator.log import configure_logging
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    configure_logging("WARNING")
    tok = st.token("orchestrator")
    c = st.client()
    q = st.sql

    def new_bridge(name: str, restart_for: set[int] | None = None):
        reg = Registry()
        rb_handlers.register(reg)
        bus = Bus(reg, BusClient(host=st.host, db=st.db, token=tok, max_backoff=2.0), workers=4)
        res = FakeResumer(name, restart_for)
        b = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, tok), resumer_factory=lambda: res,
                            poll_s=5, bridge_id=f"bridge-{name}")
        rb._instances[id(bus)] = b
        return bus, b, res

    def new_bridge_with(name: str, resumer):
        reg = Registry()
        rb_handlers.register(reg)
        bus = Bus(reg, BusClient(host=st.host, db=st.db, token=tok, max_backoff=2.0), workers=4)
        b = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, tok),
                            resumer_factory=lambda: resumer, poll_s=5, bridge_id=f"bridge-{name}")
        rb._instances[id(bus)] = b
        return bus, b

    def session(name: str, ticket: int) -> int:
        c.call_reducer("upsert_agent_session", 0, "training_droid", f"fake-fs-{name}", "running", "working", name, ticket)
        return int(q(f"SELECT * FROM agent_session WHERE name = '{name}'")[0]["id"])

    def wait_for(sid: int, pred: dict, payload: dict | None = None) -> None:
        c.call_reducer("agent_wait_for", sid, json.dumps(pred), json.dumps(payload or {"next": "continue"}))

    def srow(sid: int) -> dict:
        return q(f"SELECT * FROM agent_session WHERE id = {sid}")[0]

    def events(type_: str, sid: int | None = None) -> list[dict]:
        out = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == type_]
        return [p for p in out if sid is None or int(p.get("session_id") or 0) == sid]

    # ---- fixtures: robot, ticket, three pending episodes (one accepted), a running training run
    c.call_reducer("register_robot", "test.tdm27-robot", "yam", "fake", "{}")
    rid = int(q("SELECT * FROM robot WHERE name = 'test.tdm27-robot'")[0]["id"])
    c.call_reducer("create_ticket", rid, "[FAKE] tdm27 resume bridge proof", 0.8, 5000, "")
    tid = int(st.newest("ticket")["id"])
    eps = []
    for i in range(4):
        c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", f"modal-vol://fake/tdm27/{i}", "", "{}", 10, 1000,
                       None, None, None, None)
        eps.append(int(st.newest("episode")["id"]))
    c.call_reducer("submit_review", eps[0], True, "[FAKE] ok", "{}")
    c.call_reducer("queue_training_run", tid, json.dumps([eps[0]]), "")
    run1 = int(st.newest("training_run")["id"])
    c.call_reducer("set_training_status", run1, "running", "fake-call", None, None, None)

    bus_a, br_a, res_a = new_bridge("A", restart_for=set())
    bus_b, br_b, res_b = new_bridge("B", restart_for=set())
    await bus_a.start()
    await bus_b.start()
    await wait(lambda: bus_a.state == "live" and bus_b.state == "live", 30, "both bridges live")
    out: dict = {"ticket_id": tid, "training_run_id": run1}

    def resumed(sid: int):
        return [(n, call) for n, r in (("A", res_a), ("B", res_b)) for call in r.calls if call[0] == sid]

    # ---- S1: training_run.status == 'done'
    s1 = session("s1", tid)
    wait_for(s1, {"table": "training_run", "id": run1, "field": "status", "op": "==", "value": "done"})
    await wait(lambda: s1 in br_a.waiting and s1 in br_b.waiting, 10, "both bridges track S1")
    await asyncio.sleep(0.3)
    check("S1 not resumed while the run is still running", not resumed(s1))
    t0 = time.time()
    c.call_reducer("set_training_status", run1, "done", None, "modal-vol://fake/tdm27/ckpt", None, None)
    t_call = time.time()
    await wait(lambda: resumed(s1), 5, "S1 resumed")
    await asyncio.sleep(1.0)  # let the losing bridge finish its claim attempt
    hits = resumed(s1)
    lat1 = hits[0][1][2] - t0
    check("S1 resumed exactly once across two racing bridges", len(hits) == 1,
          f"by bridge {hits[0][0]}; lost: A={br_a.stats['lost']} B={br_b.stats['lost']}")
    check("S1 resumed within ~1 s of set_training_status", lat1 < 1.0,
          f"{lat1 * 1000:.0f} ms from the call (reducer HTTP round trip {1000 * (t_call - t0):.0f} ms)")
    check("S1 message says what changed", "status: 'running' -> 'done'" in hits[0][1][1], hits[0][1][1].splitlines()[2])
    r1 = srow(s1)
    check("S1 row is resuming and its waiting_for_json is cleared", r1["status"] == "resuming")
    d1 = events(rb.EV_DELIVERED, s1)
    a1 = events("agent.resumed", s1)
    check("event_log: one agent.resumed (claim) and one agent.resume_delivered with predicate + elapsed",
          len(a1) == 1 and len(d1) == 1 and d1[0]["predicate"]["table"] == "training_run" and d1[0]["elapsed_s"] > 0,
          json.dumps({k: d1[0].get(k) for k in ("predicate_text", "elapsed_s", "detect_ms", "mode", "bridge")}) if d1 else "")
    out["S1"] = {"latency_ms": round(lat1 * 1000, 1), "winner": hits[0][0], "delivered": d1[0] if d1 else None}

    # ---- S2: dataset.accepted_count >= 3 (accept two more episodes)
    s2 = session("s2", tid)
    wait_for(s2, {"table": "dataset", "ticket_id": tid, "field": "accepted_count", "op": ">=", "value": 3})
    await wait(lambda: s2 in br_a.waiting, 10, "S2 tracked")
    c.call_reducer("submit_review", eps[1], True, "[FAKE] ok", "{}")
    await asyncio.sleep(0.5)
    check("S2 not resumed at accepted_count 2", not resumed(s2))
    t0 = time.time()
    c.call_reducer("submit_review", eps[2], True, "[FAKE] ok", "{}")
    await wait(lambda: resumed(s2), 5, "S2 resumed")
    await asyncio.sleep(0.5)
    hits = resumed(s2)
    lat2 = hits[0][1][2] - t0
    check("S2 resumed once when the 3rd episode was accepted", len(hits) == 1, f"{lat2 * 1000:.0f} ms after submit_review")
    out["S2"] = {"latency_ms": round(lat2 * 1000, 1)}

    # ---- S3: already true when it starts waiting
    s3 = session("s3", tid)
    t0 = time.time()
    wait_for(s3, {"all": [{"table": "training_run", "id": run1, "field": "status", "value": "done"},
                          {"table": "episode", "ticket_id": tid, "review_status": "accepted", "agg": "count", "op": ">=", "value": 3}]})
    await wait(lambda: resumed(s3), 5, "S3 resumed")
    lat3 = resumed(s3)[0][1][2] - t0
    check("S3 (compound predicate already true at wait time) resumed at once",
          len(resumed(s3)) == 1 and "already true" in resumed(s3)[0][1][1], f"{lat3 * 1000:.0f} ms after agent_wait_for")
    out["S3"] = {"latency_ms": round(lat3 * 1000, 1)}

    # ---- S4: Protocol B restart reported by the resumer
    s4 = session("s4", tid)
    res_a.restart_for.add(s4)
    res_b.restart_for.add(s4)
    wait_for(s4, {"table": "dataset", "ticket_id": tid, "field": "accepted_count", "op": ">=", "value": 1})
    await wait(lambda: resumed(s4), 5, "S4 resumed")
    await wait(lambda: "fake-fs-restart" in (srow(s4)["factory_session_ids_json"] or ""), 5, "S4 new session id recorded")
    r4 = srow(s4)
    d4 = await wait(lambda: events(rb.EV_DELIVERED, s4), 5, "S4 delivered event")
    check("S4 Protocol B: new Factory session appended under the same agent_session.id",
          json.loads(r4["factory_session_ids_json"]) == ["fake-fs-s4", f"fake-fs-restart-{s4}"] and d4[0]["mode"] == "restart",
          r4["factory_session_ids_json"])

    # ---- S5: ticket that does not exist
    s5 = session("s5", tid)
    wait_for(s5, {"table": "dataset", "ticket_id": 999999, "field": "accepted_count", "op": ">=", "value": 2})
    e5 = await wait(lambda: events(rb.EV_WAIT_ERROR, s5), 5, "S5 wait_error")
    check("S5 non-existent ticket: agent.wait_error logged, session still waiting, bridges alive",
          "999999 does not exist" in e5[0]["error"] and srow(s5)["status"] == "waiting_for"
          and bus_a.state == bus_b.state == "live", e5[0]["error"])

    # ---- S6: a table the main bus does not subscribe to (contributor_presence)
    req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
    ctok = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
    cc = SpacetimeClient(st.host, st.db, ctok)
    cc.call_reducer("register_contributor", "test.tdm27-contrib", "", ["laptop"], "contributor", 0)
    cid = int(q("SELECT * FROM contributor WHERE name = 'test.tdm27-contrib'")[0]["id"])
    cc.call_reducer("contributor_heartbeat", "join")
    s6 = session("s6", tid)
    wait_for(s6, {"table": "contributor_presence", "contributor_id": cid, "field": "page", "value": "marketplace"})
    await wait(lambda: "contributor_presence" in br_a.side.ready and "contributor_presence" in br_b.side.ready, 10,
               "side subscription ready")
    await asyncio.sleep(0.3)
    check("S6 not resumed while the contributor is on 'join'", not resumed(s6))
    t0 = time.time()
    cc.call_reducer("contributor_heartbeat", "marketplace")
    await wait(lambda: resumed(s6), 5, "S6 resumed")
    await asyncio.sleep(0.5)
    lat6 = resumed(s6)[0][1][2] - t0
    check("S6 side-subscription table resumed once", len(resumed(s6)) == 1, f"{lat6 * 1000:.0f} ms after the heartbeat")
    cc.close()

    # ---- restart: no bridge running while S7's predicate becomes true
    await bus_a.stop()
    await bus_b.stop()
    br_a.stop()
    br_b.stop()
    c.call_reducer("submit_review", eps[3], True, "[FAKE] ok", "{}")
    c.call_reducer("queue_training_run", tid, json.dumps([eps[0], eps[3]]), "")
    run2 = int(st.newest("training_run")["id"])
    s7 = session("s7", tid)
    wait_for(s7, {"table": "training_run", "id": run2, "field": "status", "value": "running"})
    c.call_reducer("set_training_status", run2, "running", "fake-call-2", None, None, None)
    await asyncio.sleep(0.5)
    check("S7 still waiting while no bridge runs", srow(s7)["status"] == "waiting_for")
    bus_c, br_c, res_c = new_bridge("C")
    t0 = time.time()
    await bus_c.start()
    await wait(lambda: any(call[0] == s7 for call in res_c.calls), 15, "S7 resumed after restart")
    lat7 = next(call for call in res_c.calls if call[0] == s7)[2] - t0
    check("S7 resumed by a freshly booted bridge from the snapshot", srow(s7)["status"] == "resuming"
          and "orchestrator boot" in next(call for call in res_c.calls if call[0] == s7)[1], f"{lat7 * 1000:.0f} ms after boot")
    others = [call[0] for call in res_c.calls if call[0] != s7]
    check("restart did not re-resume anything already resumed", not others, str(others))
    await bus_c.stop()
    br_c.stop()

    # ---- S8: the real TDM-30 FactoryClient (rdf_factory) behind the bridge, with a FAKE droid backend
    out["S8"] = await real_client_leg(st, tok, tid, run2, session, wait_for, srow, events, new_bridge_with)

    out["latency_ms"] = {"S1_training_done": out["S1"]["latency_ms"], "S2_accept": out["S2"]["latency_ms"],
                         "S3_already_true": round(lat3 * 1000, 1), "S6_side_table": round(lat6 * 1000, 1),
                         "S7_after_boot": round(lat7 * 1000, 1)}
    out["latency_median_ms"] = statistics.median([lat1 * 1000, lat2 * 1000, lat6 * 1000])
    out["checks"] = {"passed": sum(ok for _, ok, _ in checks), "failed": [n for n, ok, _ in checks if not ok]}
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["checks"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
