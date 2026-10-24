"""TDM-23: cluster the failures of one finished eval run, outside the bus (manual / replay / proof).

    cd services/orchestrator
    uv run python scripts/tdm23_diagnose.py --eval-run-id 3            # dry run: print the diagnosis
    uv run python scripts/tdm23_diagnose.py --eval-run-id 3 --write    # + record_failure_cluster + event
    uv run python scripts/tdm23_diagnose.py --fixture tests/fixtures/tdm23_eval_run.json   # no DB

SpacetimeDB target comes from SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN (``.env``), or
``--host/--db``. The running orchestrator does the same thing automatically when an eval run is
finalized (``handlers/diagnose.py``); --write refuses an eval run that already has a diagnosis.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rdf_orchestrator import diagnose as dx


def _load_env() -> None:
    env = Path(__file__).resolve().parents[3] / ".env"
    if not env.exists():
        return
    import os

    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--eval-run-id", type=int)
    src.add_argument("--fixture", type=Path, help="JSON with ticket/eval_run/trials[/episodes]")
    ap.add_argument("--write", action="store_true", help="record the clusters + event_log row")
    ap.add_argument("--no-frames", action="store_true", help="text only (skip episode media)")
    ap.add_argument("--host")
    ap.add_argument("--db")
    a = ap.parse_args()
    _load_env()

    stdb = None
    if a.fixture:
        d = json.loads(a.fixture.read_text())
        inputs = dx.DiagnoseInputs(d["ticket"], d["eval_run"], d["trials"],
                                   {int(e["id"]): e for e in d.get("episodes", [])})
        if a.write:
            ap.error("--write needs --eval-run-id (fixtures are not in the database)")
    else:
        from rdf_common.spacetime_client import SpacetimeClient

        stdb = SpacetimeClient(host=a.host, db=a.db, timeout=20.0)
        inputs = dx.load_inputs(stdb, a.eval_run_id)
        if inputs.eval_run.get("status") != "done":
            print(f"eval run {a.eval_run_id} is {inputs.eval_run.get('status')!r}, not done", file=sys.stderr)
            return 2
        if a.write:
            from rdf_orchestrator.handlers.diagnose import already_diagnosed

            if already_diagnosed(stdb.sql("SELECT * FROM event_log WHERE actor = 'orchestrator.diagnose'"), a.eval_run_id):
                print(f"eval run {a.eval_run_id} already has a diagnosis; not writing again", file=sys.stderr)
                return 3

    print(f"eval run {inputs.eval_run.get('id')}: {len(inputs.trials)} trials, {len(inputs.failed)} failed",
          file=sys.stderr)
    diag = dx.diagnose_eval_run(inputs, claude=dx.ClaudeClusterer(),
                                frames=None if a.no_frames else dx.EpisodeFrameSource())
    payload = dx.record_diagnosis(diag, stdb) if (a.write and stdb is not None) else diag.payload()
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
