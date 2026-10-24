"""TDM-20 run-settings API against a REAL SpacetimeDB (skipped unless TDM20_STDB_HOST is set).

Writes rows, so point it at a throwaway local server:

    spacetime start --listen-addr 127.0.0.1:3920 --data-dir $TMPDIR/tdm20-stdb/data --non-interactive
    (cd spacetime && spacetime publish -s http://127.0.0.1:3920 tdm20test --yes)
    set -a; source ../../.env; set +a      # SPACETIME_TOKEN = publisher = owner on the local server
    TDM20_STDB_HOST=http://127.0.0.1:3920 TDM20_STDB_DB=tdm20test uv run pytest tests/test_run_settings_api_local.py -v

Against maincloud it also needs TDM20_ALLOW_MAINCLOUD=1; it then tags its robot/ticket ``test.tdm20``,
cancels the ticket at the end, and skips the training-run step (a queued training_run on maincloud
could be picked up by a live orchestrator and launch a real Modal job).
"""
from __future__ import annotations

import json
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HOST = os.environ.get("TDM20_STDB_HOST")
DB = os.environ.get("TDM20_STDB_DB", "tdm20test")
MAINCLOUD = bool(HOST and "maincloud" in HOST)

pytestmark = [
    pytest.mark.skipif(not HOST, reason="set TDM20_STDB_HOST to a throwaway SpacetimeDB"),
    pytest.mark.skipif(MAINCLOUD and os.environ.get("TDM20_ALLOW_MAINCLOUD") != "1",
                       reason="maincloud writes rows: set TDM20_ALLOW_MAINCLOUD=1"),
]


@pytest.fixture(scope="module")
def env():
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient

    from rdf_orchestrator.run_settings_api import get_run_settings_stdb, run_settings_router

    sync = SpacetimeClient(HOST, DB)
    tag = "test.tdm20"
    sync.call_reducer("register_robot", f"{tag}-bot", "yam", "tdm20", "{}")
    robot_id = max(r["id"] for r in sync.sql("SELECT * FROM robot") if r["name"] == f"{tag}-bot")
    sync.call_reducer("create_ticket", robot_id, f"{tag}: put the cup in the bin", 0.8, 5000, "")
    ticket_id = max(r["id"] for r in sync.sql(f"SELECT * FROM ticket WHERE robot_id = {robot_id}"))

    app = FastAPI()
    app.include_router(run_settings_router)
    holder: dict = {}

    def dep():  # built inside TestClient's event loop
        if "c" not in holder:
            holder["c"] = AsyncSpacetimeClient(HOST, DB)
        return holder["c"]

    app.dependency_overrides[get_run_settings_stdb] = dep
    with TestClient(app) as client:
        yield {"client": client, "sync": sync, "ticket_id": ticket_id, "tag": tag}
    if MAINCLOUD:
        sync.call_reducer("set_ticket_status", ticket_id, "cancelled", f"{tag} cleanup")
    sync.close()


def test_eval_trials_zero_rejected_field_level(env):
    r = env["client"].patch(f"/tickets/{env['ticket_id']}/settings", json={"eval_trials": 0})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"] == ["body", "eval_trials"]


def test_valid_patch_visible_within_1s_and_logged(env):
    tid, sync = env["ticket_id"], env["sync"]
    body = {"eval_trials": 12, "success_threshold": 0.7, "budget_cents": 7500, "target_episodes": 6,
            "max_iterations": 4, "data_type_override": "recovery_takeover",
            "hyperparams": {"lr": 1e-4, "steps": 1500, "prompt": "put the cup in the bin"}}
    t0 = time.monotonic()
    r = env["client"].patch(f"/tickets/{tid}/settings", json=body, headers={"X-Human-Id": env["tag"]})
    assert r.status_code == 200, r.text
    # visible straight from the run_settings table (not just the API's echo)
    row = sync.sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")[0]
    elapsed = time.monotonic() - t0
    assert row["eval_trials"] == 12 and row["data_type_override"] == "recovery_takeover"
    hp = json.loads(row["hyperparams_json"])
    assert hp["lr"] == 1e-4 and hp["steps"] == 1500 and hp["batch_size"] == 32  # merged
    assert elapsed < 1.0 or MAINCLOUD, f"patch took {elapsed:.2f}s to become visible"
    print(f"patch -> visible in run_settings: {elapsed * 1000:.0f} ms")
    assert sync.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["budget_cents"] == 7500

    g = env["client"].get(f"/tickets/{tid}/settings").json()
    assert g["eval_trials"] == 12 and g["data_type_override"] == "recovery_takeover"

    ev = [e for e in sync.sql("SELECT * FROM event_log WHERE type = 'human.run_settings_changed'")
          if json.loads(e["payload_json"]).get("ticket_id") == tid]
    assert ev and ev[-1]["actor"] == f"human:{env['tag']}"
    assert json.loads(ev[-1]["payload_json"])["patch"] == body
    assert any(json.loads(e["payload_json"]).get("ticket_id") == tid
               for e in sync.sql("SELECT * FROM event_log WHERE type = 'ticket.run_settings_updated'"))


def test_clear_override_and_reducer_side_rules(env):
    tid = env["ticket_id"]
    r = env["client"].patch(f"/tickets/{tid}/settings", json={"data_type_override": None})
    assert r.status_code == 200 and r.json()["data_type_override"] is None
    r = env["client"].patch(f"/tickets/{tid}/settings", json={"hyperparams": {"dropout": 0.5}})
    assert r.status_code == 422
    assert env["client"].get("/tickets/999999/settings").status_code == 404


@pytest.mark.skipif(MAINCLOUD, reason="would queue a real training run on maincloud")
def test_next_training_run_uses_new_hyperparams(env):
    tid, sync = env["ticket_id"], env["sync"]
    r = env["client"].patch(f"/tickets/{tid}/settings", json={"hyperparams": {"lr": 3e-4, "lora_rank": 8}})
    assert r.status_code == 200, r.text
    sync.call_reducer("create_episode", tid, 0, 0, "clean_teleop", "s3://tdm20/ep0", "", "{}", 10, 1000,
                      None, None, True, None)
    ep = max(e["id"] for e in sync.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}"))
    sync.call_reducer("submit_review", ep, True, "", "{}")
    sync.call_reducer("queue_training_run", tid, json.dumps([ep]), "")  # "" = take run_settings
    run = max(sync.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}"), key=lambda x: x["id"])
    hp = json.loads(run["hyperparams_json"])
    assert hp["lr"] == 3e-4 and hp["lora_rank"] == 8 and hp["steps"] == 1500
