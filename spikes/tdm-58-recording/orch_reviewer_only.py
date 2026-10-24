"""TDM-58 proof: the real orchestrator app with ONLY the reviewer + payout handlers on its bus.

The full handler set would run the flywheel (baseline, Claude diagnose/planner, training) on the
throwaway ticket. Here the bus keeps tdm25_review_episode (TDM-25), payments.* (TDM-63: no Stripe
account -> awaiting_onboarding, no transfer) and event_log_tap; the HTTP routers are all there
(/uploads/sign-batch for the recorder's uploader). Local SpacetimeDB only.

    uv run --project services/orchestrator python spikes/tdm-58-recording/orch_reviewer_only.py --port 8058
"""
from __future__ import annotations

import argparse
import os
import sys

KEEP_PREFIXES = ("tdm25_review_episode", "payments.", "event_log_tap")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8058)
    a = ap.parse_args()
    if "maincloud" in os.environ.get("SPACETIME_HOST", "") or not os.environ.get("SPACETIME_HOST", "").startswith("http://127."):
        print("refusing: this proof runner is for a local throwaway SpacetimeDB only", file=sys.stderr)
        return 2

    import uvicorn
    from rdf_orchestrator import handlers  # noqa: F401 - registers everything on the default registry
    from rdf_orchestrator.app import create_app
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.log import configure_logging

    registry.handlers[:] = [h for h in registry.handlers if h.name.startswith(KEEP_PREFIXES)]
    print("tdm58 orchestrator handlers:", sorted(h.name for h in registry.handlers), file=sys.stderr, flush=True)
    configure_logging()
    uvicorn.run(create_app(registry=registry), host=a.host, port=a.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
