"""TDM-79 proof: a run-settings PATCH (the same body the web editor sends) is what the next training
run trains with.

    cd services/orchestrator && uv run python ../../spikes/tdm-79/settings_pickup_proof.py [--port 3080]

Own throwaway local SpacetimeDB (never maincloud) + fake robot. Only the TDM-20 run-settings router
runs (in-process, no flywheel handlers, no Anthropic calls). Steps: create a ticket, PATCH
eval_trials + hyperparams through the router, drive the ticket with the TDM-28 scripted flywheel
(which queues training with hyperparams_json "" = "use run_settings"), then read training_run back.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from conftest import FAKE_ROBOT_ARGS, new_ticket  # noqa: E402
from harness import LocalStack  # noqa: E402
from scripted_flywheel import ScriptedFlywheel  # noqa: E402
from stub_modal import StubTrainer  # noqa: E402

PATCH = {"eval_trials": 12, "hyperparams": {"lr": 0.0001, "steps": 1500, "prompt": "stack the red block on the blue block"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3080)
    a = ap.parse_args()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from rdf_common.spacetime_client import AsyncSpacetimeClient
    from rdf_orchestrator.run_settings_api import get_run_settings_stdb, run_settings_router

    st = LocalStack(port=a.port)
    trainer = None
    try:
        st.boot(fake_robot_args=FAKE_ROBOT_ARGS)
        robot_id = int(st.robot()["id"])
        trainer = StubTrainer(st.client(), mode="queued").start()
        tid = new_ticket(st, robot_id, "TDM-79 settings pickup proof",
                         '{"eval_trials": 10, "target_episodes": 3, "max_iterations": 3}')

        app = FastAPI()
        app.include_router(run_settings_router)
        stdb = AsyncSpacetimeClient(st.host, st.db, st.token("orchestrator"))
        app.dependency_overrides[get_run_settings_stdb] = lambda: stdb
        with TestClient(app) as http:
            bad = http.patch(f"/tickets/{tid}/settings", json={"eval_trials": 99, "hyperparams": {"lr": 5}})
            print(f"[tdm79] invalid PATCH -> {bad.status_code} {[d['loc'] for d in bad.json()['detail']]}")
            r = http.patch(f"/tickets/{tid}/settings", json=PATCH, headers={"X-Human-Id": "tdm79-proof"})
            print(f"[tdm79] PATCH -> {r.status_code} audit_logged={r.json().get('audit_logged')} "
                  f"hyperparams={r.json()['hyperparams']}")

        ScriptedFlywheel(st, robot_id).run(tid)
        runs = st.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}")
        evals = sorted(st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}"), key=lambda e: e["id"])
        hp = json.loads(runs[0]["hyperparams_json"])
        print(f"[tdm79] training_run {runs[0]['id']} hyperparams_json = {hp}")
        print(f"[tdm79] eval_run trials = {[(e['kind'], e['trials']) for e in evals]}")
        audit = [e for e in st.events() if e["type"] == "human.run_settings_changed"]
        print(f"[tdm79] audit events: {[(e['actor'], json.loads(e['payload_json'])['patch']) for e in audit]}")
        ok = (bad.status_code == 422 and r.status_code == 200
              and all(hp.get(k) == v for k, v in PATCH["hyperparams"].items())
              and all(e["trials"] == 12 for e in evals) and audit and audit[0]["actor"] == "human:tdm79-proof")
        print("[tdm79] PROOF", "OK" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        if trainer:
            trainer.stop()
        st.stop()


if __name__ == "__main__":
    raise SystemExit(main())
