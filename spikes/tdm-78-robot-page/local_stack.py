"""TDM-78: throwaway local stack for the robot page (never touches maincloud).

    cd services/orchestrator && uv run python ../../spikes/tdm-78-robot-page/local_stack.py [--port 3078]

Boots SpacetimeDB on 127.0.0.1:<port> (db tdm78robot, data under $TMPDIR/tdm-78/), publishes the
module from a clean worktree of HEAD (not the shared working tree), mints orchestrator /
robot_client / web identities, and starts the sim robot-client (TDM-49) on it. Tokens land in
$TMPDIR/tdm-78/run-<port>/tokens/ (0600). Ctrl-C stops everything.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
import harness  # noqa: E402

ROOT = Path(tempfile.gettempdir()) / "tdm-78"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=3078)
    p.add_argument("--db", default="tdm78robot")
    a = p.parse_args()
    wt = ROOT / "module-worktree"
    if wt.exists():
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], check=False)
    ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), "HEAD"], check=True, capture_output=True)
    nm = wt / "spacetime" / "spacetimedb" / "node_modules"
    if not nm.exists():
        nm.symlink_to(REPO / "spacetime" / "spacetimedb" / "node_modules")
    harness.MODULE_SRC = wt / "spacetime" / "spacetimedb"

    st = harness.LocalStack(a.port, a.db, root=ROOT)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        st.start_server()
        st.publish()
        for role in ("orchestrator", "robot_client", "web"):
            st.make_identity(role)
        rc = st._spawn("robot-client", ["uv", "run", "--quiet", "rdf-robot-client", "--sim", "--host", st.host, "--db", st.db,
                                         "--token-file", str(st.tokens / "robot_client.token"), "--heartbeat-s", "5"],
                       cwd=REPO / "services" / "robot-client")
        print(f"\n# TDM-78 stack up: {st.host} db {st.db}; tokens {st.tokens}; logs {st.logs}", flush=True)
        while rc.poll() is None and all(x.poll() is None for x in st.procs.values()):
            time.sleep(1)
        print(f"# a process exited; see {st.logs}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], check=False)


if __name__ == "__main__":
    main()
