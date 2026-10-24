"""A second local SpacetimeDB that plays the 'remote' (maincloud) for testing `run.py up --stdb-url ...`.

    cd services/orchestrator && uv run python ../../spikes/mujoco-sim/fake_remote.py --port 3091 --db fakemain
It publishes the module once (the CLI login = owner) and stays up until killed. Never maincloud.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
os.environ.setdefault("TDM28_ROOT", str(Path(tempfile.gettempdir()) / "rdf-fake-remote"))
from harness import LocalStack  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=3091)
ap.add_argument("--db", default="fakemain")
a = ap.parse_args()
st = LocalStack(a.port, a.db)
signal.signal(signal.SIGTERM, lambda *_: (st.stop(), sys.exit(0)))
st.start_server()
st.publish()
print(f"fake remote up: {st.host} db {a.db}", flush=True)
try:
    while True:
        time.sleep(1)
finally:
    st.stop()
