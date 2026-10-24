"""Teleop integration proof: the real orchestrator app with only the dispatcher, the TDM-25 reviewer,
the payout handlers and the TDM-69 interventions on its bus (no flywheel: no Claude diagnose/planner,
no training). All HTTP routers are mounted (/stream/token, /uploads/sign-batch, /robots/{id}/interventions/flag).
Local throwaway SpacetimeDB only.

    uv run --project services/orchestrator python spikes/teleop-integration/orch_subset.py --port 8xxx
"""
from __future__ import annotations

import argparse
import os
import sys

KEEP_PREFIXES = ("dispatch_", "reset_after_eval_trial", "reset_on_robot_paused", "resume_after_reset",
                 "tdm25_review", "payments.", "intervention_", "event_log_tap")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    if not os.environ.get("SPACETIME_HOST", "").startswith("http://127."):
        print("refusing: this proof runner is for a local throwaway SpacetimeDB only", file=sys.stderr)
        return 2

    import uvicorn
    from rdf_orchestrator import handlers  # noqa: F401 - registers everything on the default registry
    from rdf_orchestrator.app import create_app
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.log import configure_logging

    registry.handlers[:] = [h for h in registry.handlers if h.name.startswith(KEEP_PREFIXES)]
    print("teleop-int orchestrator handlers:", sorted(h.name for h in registry.handlers), file=sys.stderr, flush=True)
    configure_logging()
    uvicorn.run(create_app(registry=registry), host=a.host, port=a.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
