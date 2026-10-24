"""TDM-79 live check: a TDM-28 local stack that stays up for the web settings editor + report page.

    cd services/orchestrator && uv run python ../../spikes/tdm-79/stack.py [--port 3079] [--orch-port 8079]

Boots a throwaway local SpacetimeDB (never maincloud) + the fake robot, drives one ticket through the
scripted flywheel (no Anthropic calls) so it ships with a real report, creates a second ticket that
stays open (for editing run settings), then starts the real orchestrator (RDF_MODAL_STUB=1, with
RDF_WEB_ORIGINS for the dev server) and keeps everything running until Ctrl-C / SIGTERM.

Prints the env lines for `pnpm --filter @rdf/web dev`.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from conftest import FAKE_ROBOT_ARGS, new_ticket  # noqa: E402
from harness import LocalStack  # noqa: E402
from scripted_flywheel import ScriptedFlywheel  # noqa: E402
from stub_modal import StubTrainer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3079)
    ap.add_argument("--orch-port", type=int, default=8079)
    ap.add_argument("--web-origin", default="http://localhost:3779,http://127.0.0.1:3779")
    a = ap.parse_args()

    def _term(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    st = LocalStack(port=a.port)
    trainer = None
    try:
        st.boot(fake_robot_args=FAKE_ROBOT_ARGS)
        robot_id = int(st.robot()["id"])
        trainer = StubTrainer(st.client(), mode="queued").start()
        shipped = new_ticket(st, robot_id, "put the cup in the bin (TDM-79 report check)",
                             '{"eval_trials": 10, "target_episodes": 3, "max_iterations": 3}')
        ScriptedFlywheel(st, robot_id).run(shipped)
        trainer.stop()
        trainer = None
        open_tid = new_ticket(st, robot_id, "stack the red block on the blue one (TDM-79 settings check)",
                              '{"eval_trials": 10, "target_episodes": 5, "max_iterations": 3}')
        st.spawn_orchestrator(port=a.orch_port, extra_env={"RDF_MODAL_STUB": "1", "RDF_WEB_ORIGINS": a.web_origin})
        status = st.sql(f"SELECT * FROM ticket WHERE id = {shipped}")[0]["status"]
        print(f"\n# TDM-79 stack up. shipped ticket {shipped} ({status}), open ticket {open_tid}")
        print(f"NEXT_PUBLIC_SPACETIME_HOST={st.host} NEXT_PUBLIC_SPACETIME_DB_NAME={st.db} "
              f"NEXT_PUBLIC_ORCHESTRATOR_URL=http://127.0.0.1:{a.orch_port} "
              f"RDF_ORCHESTRATOR_URL=http://127.0.0.1:{a.orch_port}")
        print(f"# logs: {st.logs}   Ctrl-C stops everything.\n", flush=True)
        while all(p.poll() is None for p in st.procs.values()):
            time.sleep(1)
        print("[tdm79] a child process exited:", [n for n, p in st.procs.items() if p.poll() is not None], flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        if trainer:
            trainer.stop()
        st.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
