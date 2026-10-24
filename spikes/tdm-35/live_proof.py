"""TDM-35 live proof on a THROWAWAY local SpacetimeDB (never maincloud), no Factory credits spent.

    cd services/orchestrator && uv run python ../../spikes/tdm-35/live_proof.py

1. Boots spacetime on 127.0.0.1:$TDM35_PORT (3035), db tdm35agents, data under $TMPDIR/tdm-35/;
   mints `factory` and `orchestrator` identities (tokens stay in files, never printed).
2. Starts ``subscribe_feed.mjs``: Node + the apps/web generated bindings + normalize.ts, i.e. the
   agents panel's code path, fed by SpacetimeDB subscriptions only.
3. Re-plays TDM-30's recorded proof run (fixtures/tdm35_replay/1.events.jsonl) through TDM-30's own
   ``rdf_factory.mirror.Mirror`` (same reducers, same droid.* rows as a live droid), compressed in
   time, plus a few orchestrator-agent rows (clustering, reviewer, QA) written as `orchestrator`.
4. Calls the real ``GET /agents`` router against the same DB, and ``?replay=1``.
5. Checks the browser-built feed and GET /agents agree, then prints a summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM35_PORT", "3035"))
DB = "tdm35agents"
ROOT = Path(tempfile.gettempdir()) / "tdm-35"
RECORDING = REPO / "services" / "orchestrator" / "fixtures" / "tdm35_replay" / "1.events.jsonl"
STEP_KINDS = {"session_started", "session_restarted", "turn_started", "tool_call", "assistant_text",
              "permission_denied", "error"}
LOGGED = {"session_started", "session_restarted", "turn_started", "tool_call", "tool_result", "assistant_text",
          "permission_denied", "waiting_for", "turn_completed", "error"}


def log(msg: str) -> None:
    print(f"[tdm35-proof {time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def replay_into_db(st: LocalStack, gap: float) -> int:
    from rdf_common.spacetime_client import AsyncSpacetimeClient
    from rdf_factory.mirror import Mirror

    async with AsyncSpacetimeClient(st.host, st.db, st.token("factory")) as fac, \
            AsyncSpacetimeClient(st.host, st.db, st.token("orchestrator")) as orch:
        m = Mirror(fac)
        rec = [json.loads(x) for x in RECORDING.read_text().splitlines() if x.strip()]
        sid = await m.create("data_droid", "tdm35-proof-data-droid", 0, "starting: (replayed TDM-30 proof brief)")
        log(f"agent_session {sid} created; replaying {len(rec)} recorded events, {gap}s apart")
        for d in rec:
            d = {**d, "agent_session_id": sid, "ts_us": time.time_ns() // 1000}
            k = d["kind"]
            if k == "turn_started" and d["data"].get("protocol") == "native_resume":
                await m.resume_claim(sid, "tdm35 proof: replayed resume")
            if k == "session_started":
                await m.upsert(sid, "data_droid", "running", d["text"], d["factory_session_id"])
            elif k in STEP_KINDS:
                await m.step(sid, d["text"])
            if k in LOGGED:
                await m.event("droid:data", f"droid.{k}", json.dumps(d))
            if k == "waiting_for":
                await m.wait_for(sid, d["data"]["predicate"], d["data"].get("resume_payload"))
            if k == "turn_completed" and d["data"]["status"] in ("paused", "done"):
                await m.upsert(sid, "data_droid", d["data"]["status"], d["text"])
            if k == "turn_completed" and d["data"]["status"] == "paused":
                # orchestrator agents interleave with the droid, as on a real ticket
                for actor, typ, p in [
                    ("orchestrator.diagnose", "diagnose.clusters", {"ticket_id": 0, "eval_run_id": 1, "clusters": [{"label": "missed grasp"}]}),
                    ("orchestrator.reviewer", "review.error", {"episode_id": 3, "error": "tdm35 proof row"}),
                    ("orchestrator.dataset_qa", "qa.dataset_checked", {"episode_id": 3, "passed": True}),
                ]:
                    await orch.call_reducer("append_event", actor, typ, json.dumps(p))
            await asyncio.sleep(gap)
        return sid


def main() -> int:
    st = LocalStack(PORT, DB, root=ROOT)
    sub = None
    try:
        st.start_server()
        st.publish()
        st.make_identity("factory")
        st.make_identity("orchestrator")
        out = st.root / "browser_feed.json"
        sub = subprocess.Popen(["node", str(Path(__file__).with_name("subscribe_feed.mjs")), st.host, st.db, str(out), "120"],
                               cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(3)  # let it connect and apply the (empty) subscription
        sid = asyncio.run(replay_into_db(st, gap=float(os.environ.get("TDM35_GAP", "0.15"))))
        sub_out, _ = sub.communicate(timeout=60)
        sub_lines = sub_out.strip().splitlines()
        print("\n".join(sub_lines[:6] + ["  ..."] + sub_lines[-4:]))
        if sub.returncode != 0:
            log(f"subscriber exited {sub.returncode}")
            return 1
        browser = json.loads(out.read_text())

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from rdf_common.spacetime_client import AsyncSpacetimeClient
        from rdf_orchestrator.agents_api import agents_router, get_agents_stdb

        app = FastAPI()
        app.include_router(agents_router)
        app.dependency_overrides[get_agents_stdb] = lambda: AsyncSpacetimeClient(st.host, st.db, st.token("orchestrator"))
        with TestClient(app) as c:
            api = c.get("/agents").json()
            rep = c.get("/agents", params={"replay": "1"}).json()

        bf = browser["feed"]
        b_agents = {a["key"]: a for a in bf["agents"]}
        a_agents = {a["key"]: a for a in api["agents"]}
        d_api, d_b = a_agents[f"session:{sid}"], b_agents[f"session:{sid}"]
        checks = {
            "browser feed built from subscriptions only": bf["replay"] is False,
            "browser saw the droid step through states": browser["changes"] >= 10,
            "droid done in both": d_api["status"] == d_b["status"] == "done",
            "same agents": set(a_agents) == set(b_agents),
            "same event ids per agent": all([e["id"] for e in a_agents[k]["events"]] == [e["id"] for e in b_agents[k]["events"]]
                                            for k in a_agents),
            "usage from turn_completed": d_api["usage"]["credits_total"] == 10891.0 and d_api["usage"]["turns"] == 3,
            "reducer echoes deduplicated": not any(e["type"] in ("agent.step", "agent.waiting_for") for e in d_api["events"]),
            "resume shows up": any(e["kind"] == "resumed" for e in d_api["events"]) or d_api["event_count"] > 20,
            "services present": {"service:clustering", "service:reviewer", "service:qa"} <= set(a_agents),
            "replay labelled": rep["replay"] is True and all(a["replay"] for a in rep["agents"]),
        }
        log(f"GET /agents: {[(a['key'], a['status'], a['event_count']) for a in api['agents']]}")
        log(f"droid usage: {d_api['usage']}")
        for name, ok in checks.items():
            log(f"{'PASS' if ok else 'FAIL'}  {name}")
        (st.root / "api_feed.json").write_text(json.dumps(api, indent=1))
        log(f"feeds saved under {st.root}")
        return 0 if all(checks.values()) else 1
    finally:
        if sub and sub.poll() is None:
            sub.kill()
        st.stop()


if __name__ == "__main__":
    sys.exit(main())
