"""TDM-74 live check: a local stack for the /tickets/new page (never maincloud).

    cd services/orchestrator && uv run python ../../spikes/tdm-74/stack.py [--port 3074] [--orch-port 8074]

Boots a throwaway TDM-28 SpacetimeDB + the fake robot (`fake-yam-1`, online), adds an online test robot
`test.tdm74-arm` (hidden on the page unless "Show test robots" is on), then serves ONLY the TDM-19/74
tickets router (POST /tickets/parse, POST /tickets, GET /tickets/{id}) on --orch-port. No flywheel
handlers run, so created tickets stay `created` and nothing else calls Claude. Each "Read the job"
on the page is one real Claude call (ANTHROPIC_API_KEY from .env); creates that send `parsed` back
make none.

Prints the env lines for `pnpm --filter @rdf/web dev`.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from conftest import FAKE_ROBOT_ARGS  # noqa: E402
from harness import LocalStack  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3074)
    ap.add_argument("--orch-port", type=int, default=8074)
    a = ap.parse_args()

    def _term(*_: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    st = LocalStack(port=a.port, db="tdm74new")
    try:
        st.boot(fake_robot_args=FAKE_ROBOT_ARGS)
        c = st.client()
        c.call_reducer("register_robot", "test.tdm74-arm", "i2rt_yam", "test", "{}")
        test_id = int(st.sql("SELECT * FROM robot WHERE name = 'test.tdm74-arm'")[0]["id"])
        c.call_reducer("robot_heartbeat", test_id, "online")

        os.environ.update(st.env("orchestrator"))
        import uvicorn
        from fastapi import FastAPI

        from rdf_orchestrator.tickets_api import _default_service, router

        app = FastAPI(title="tdm74 tickets-only")
        app.include_router(router)
        app.state.tickets_service = _default_service()
        print(f"\n# TDM-74 stack up. robots: fake-yam-1 (online), test.tdm74-arm #{test_id} (online)")
        print(f"NEXT_PUBLIC_SPACETIME_HOST={st.host} NEXT_PUBLIC_SPACETIME_DB_NAME={st.db} "
              f"RDF_ORCHESTRATOR_URL=http://127.0.0.1:{a.orch_port}")
        print(f"# logs: {st.logs}   Ctrl-C stops everything.\n", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=a.orch_port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
