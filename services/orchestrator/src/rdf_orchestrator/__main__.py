"""Orchestrator entry point: FastAPI + the SpacetimeDB subscription runtime (TDM-18).

    uv run rdf-orchestrator [--host 0.0.0.0] [--port 8000]      (PORT env var also honoured)
"""
import argparse
import os

from rdf_common.settings import OrchestratorSettings, load_settings


def main() -> None:
    load_settings(OrchestratorSettings, "orchestrator")  # exits non-zero, naming any missing env var
    p = argparse.ArgumentParser(prog="rdf-orchestrator")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    a = p.parse_args()
    from .app import main as serve

    serve(a.host, a.port)


if __name__ == "__main__":
    main()
