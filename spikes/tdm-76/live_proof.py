"""TDM-76 live proof: a real local droid session on a THROWAWAY SpacetimeDB, woken by the real
TDM-27 resume bridge, while the /agents page is open. Never touches maincloud.

    cd services/orchestrator && uv run python ../../spikes/tdm-76/live_proof.py

  1. Boots spacetime on 127.0.0.1:$TDM76_PORT (3076), db tdm76agents, data under $TMPDIR/tdm-76/.
     Mints `orchestrator` and `factory` identities (tokens stay in files, never printed).
  2. Creates a [FAKE] robot + ticket (dataset row starts at accepted_count 0) and 3 pending episodes.
  3. Starts the resume bridge in this process with the real TDM-30 FactoryClient as its resumer.
  4. Waits for command files in $TMPDIR/tdm-76/cmd/ so the human (or agent) driving the browser
     controls the pace:
       start   -> `uv run rdf-factory start --role onboarding_droid ...` (real droid, costs credits;
                  the brief ends the turn with WAITING_FOR dataset:accepted_count>=3)
       accept  -> submit_review(accepted) on the next pending episode (the 3rd one wakes the droid)
       stop    -> shut everything down
Point the web app at it: NEXT_PUBLIC_SPACETIME_HOST=http://127.0.0.1:3076 NEXT_PUBLIC_SPACETIME_DB_NAME=tdm76agents
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM76_PORT", "3076"))
ROOT = Path(tempfile.gettempdir()) / "tdm-76"
CMD = ROOT / "cmd"
FACTORY_STATE = ROOT / "factory" / "state"
FACTORY_WORK = ROOT / "factory" / "sessions"

BRIEF = (
    "Real repo task (small). Read services/robot-client/src/rdf_robot_client/adapters/base.py and write "
    "adapter_checklist.md in your working directory: one line per method a new arm adapter must implement, "
    "with what it must do in a few words. Keep it short. Then park until 3 episodes for this ticket are "
    "accepted: write resume_payload.json, print RESUME_PAYLOAD, and end with the line "
    "WAITING_FOR dataset:accepted_count>=3"
)


def log(msg: str) -> None:
    print(f"[tdm76-proof {time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> int:
    st = LocalStack(PORT, "tdm76agents", root=ROOT)
    assert "maincloud" not in st.host
    st.start_server()
    procs: list[subprocess.Popen] = []
    try:
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("factory")
        c = st.client("orchestrator")
        c.call_reducer("register_robot", "test.tdm76-robot", "yam", "fake", "{}")
        rid = int(st.newest("robot")["id"])
        c.call_reducer("create_ticket", rid, "[FAKE] TDM-76 agents panel proof: pick up the cup", 0.8, 5000, "")
        tid = int(st.newest("ticket")["id"])
        eps = []
        for i in range(3):
            c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", f"modal-vol://fake/tdm76/{i}", "", "{}", 10, 1000,
                           None, None, None, None)
            eps.append(int(st.newest("episode")["id"]))
        log(f"ticket {tid}, episodes {eps} pending; dataset accepted_count 0")

        from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient
        from rdf_factory import FactoryClient, FactoryConfig
        from rdf_orchestrator import resume_bridge as rb
        from rdf_orchestrator.bus import Bus, Registry
        from rdf_orchestrator.handlers import resume_bridge as rb_handlers
        from rdf_orchestrator.log import configure_logging
        from rdf_orchestrator.stdb import SpacetimeClient as BusClient

        configure_logging("INFO")
        otok, ftok = st.token("orchestrator"), st.token("factory")
        FACTORY_STATE.mkdir(parents=True, exist_ok=True)
        FACTORY_WORK.mkdir(parents=True, exist_ok=True)

        def make_client():
            cfg = FactoryConfig.from_env("local")
            cfg.state_dir, cfg.work_root = FACTORY_STATE, FACTORY_WORK
            return FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, ftok))

        reg = Registry()
        rb_handlers.register(reg)
        bus = Bus(reg, BusClient(host=st.host, db=st.db, token=otok, max_backoff=2.0), workers=4)
        bridge = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, otok),
                                 resumer_factory=make_client, poll_s=10, bridge_id="tdm76-proof")
        rb._instances[id(bus)] = bridge
        await bus.start()
        log("resume bridge live (real FactoryClient resumer)")

        CMD.mkdir(parents=True, exist_ok=True)
        (ROOT / "ready").write_text(str(tid))
        env = {**os.environ, "SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "SPACETIME_TOKEN": ftok,
               "FACTORY_MODE": "local", "FACTORY_STATE_DIR": str(FACTORY_STATE), "FACTORY_WORK_ROOT": str(FACTORY_WORK)}
        next_ep = 0
        while True:
            for f in sorted(CMD.iterdir()):
                name = f.name
                f.unlink()
                if name == "stop":
                    log("stop")
                    return 0
                if name == "start":
                    log("starting a real onboarding_droid via `uv run rdf-factory start`")
                    out = open(ROOT / "droid-start.log", "ab")
                    procs.append(subprocess.Popen(
                        ["uv", "run", "rdf-factory", "start", "--role", "onboarding_droid", "--brief", BRIEF,
                         "--ticket", str(tid), "--name", "onboarding droid (TDM-76 proof)"],
                        cwd=REPO / "services" / "factory", env=env, stdout=out, stderr=subprocess.STDOUT))
                if name == "accept" and next_ep < len(eps):
                    c.call_reducer("submit_review", eps[next_ep], True, "[FAKE] reviewer stub: ok", "{}")
                    next_ep += 1
                    log(f"accepted episode {eps[next_ep - 1]} -> accepted_count {next_ep}")
            await asyncio.sleep(0.3)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        try:
            bridge.stop()  # type: ignore[possibly-undefined]
            await bus.stop()  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass
        st.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
