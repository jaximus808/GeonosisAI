"""TDM-71 report: pure assembler, markdown, event_log seal and the HTTP routes (no network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import report
from rdf_orchestrator.report_api import get_report_stdb, get_report_storage, report_router

S = 1_000_000  # micros
REPO = Path(__file__).resolve().parents[3]


def _ev(id_, ts, typ, payload, actor="orchestrator"):
    return {"id": id_, "ts": ts * S, "actor": actor, "type": typ, "payload_json": json.dumps(payload)}


def tables(fake: bool = False) -> dict:
    tag = "[FAKE] " if fake else ""
    trials_base = [{"id": i + 1, "eval_run_id": 1, "trial_index": i, "success": i < 3, "duration_ms": 1000,
                    "episode_id": 100 + i, "failure_note": f"{tag}missed the cup" if i >= 3 else tag or None}
                   for i in range(10)]
    trials_cand = [{"id": 20 + i, "eval_run_id": 2, "trial_index": i, "success": i < 9, "duration_ms": 1000,
                    "episode_id": 0, "failure_note": tag or None} for i in range(10)]
    return {
        "ticket": [{"id": 7, "robot_id": 1, "task_description": f"{tag}Put the cup in the bin", "target_success": 0.8,
                    "budget_cents": 5000, "approved_cents": 3000, "spent_cents": 1500, "status": "shipped",
                    "iteration": 0, "deployed_checkpoint_uri": None, "created_by": "web", "created_at": 1 * S}],
        "run_settings": [{"ticket_id": 7, "eval_trials": 10, "success_threshold": 0.8, "require_ci_low": False,
                          "target_episodes": 3, "max_iterations": 3, "data_type_override": "",
                          "hyperparams_json": json.dumps({"prompt": "put the cup in the bin"})}],
        "robot": [{"id": 1, "name": "fake-yam" if fake else "yam-1", "embodiment": "fake_yam" if fake else "i2rt_yam",
                   "adapter_version": "v1", "status": "online", "camera_config_json": "{}"}],
        "eval_run": [
            {"id": 1, "ticket_id": 7, "kind": "baseline", "status": "done", "checkpoint_uri": "gs://pi05_base",
             "training_run_id": 0, "trials": 10, "completed": 10, "successes": 3, "ci_low": 0.108, "ci_high": 0.603,
             "created_at": 2 * S, "finished_at": 3 * S},
            {"id": 2, "ticket_id": 7, "kind": "candidate", "status": "done", "checkpoint_uri": "modal-vol://ckpt/run-5",
             "training_run_id": 5, "trials": 10, "completed": 10, "successes": 9, "ci_low": 0.596, "ci_high": 0.982,
             "created_at": 20 * S, "finished_at": 21 * S}],
        "eval_trial": trials_base + trials_cand,
        "failure_cluster": [{"id": 11, "ticket_id": 7, "iteration": 0, "label": "misses the cup", "description": "",
                             "count": 7, "example_episode_ids": [103, 104], "created_at": 4 * S}],
        "data_plan": [{"id": 3, "ticket_id": 7, "iteration": 0, "data_type": "recovery_takeover", "amount": 3,
                       "unit_price_cents": 500, "est_cost_cents": 1500, "reasoning": "Failures are mid-grasp; recover.",
                       "failure_cluster_ids": [11], "status": "done", "approved_cents": 3000,
                       "approved_by": "human:web", "approved_at": 6 * S, "created_at": 5 * S}],
        "task": [{"id": 30 + i, "ticket_id": 7, "data_plan_id": 3, "type": "recovery_takeover", "status": "completed",
                  "price_cents": 500, "created_at": 7 * S} for i in range(3)]
                + [{"id": 40, "ticket_id": 7, "data_plan_id": 0, "type": "reset", "status": "completed",
                    "price_cents": 200, "created_at": 8 * S}],
        "episode": [{"id": 200 + i, "ticket_id": 7, "task_id": 30 + i, "contributor_id": 9, "dataset_offer_id": 0,
                     "kind": "recovery_takeover", "review_status": "rejected" if i == 2 else "accepted",
                     "storage_uri": f"modal-vol://rdf-media/{'FAKE-' if fake else ''}ep{i}",
                     "cameras_json": json.dumps({"wrist": "x", **({"fake": True} if fake else {})}),
                     "duration_ms": 9000, "frame_count": 270, "created_at": (9 + i) * S} for i in range(3)]
                   + [{"id": 100, "ticket_id": 7, "task_id": 0, "contributor_id": 0, "dataset_offer_id": 0,
                       "kind": "baseline", "review_status": "not_reviewed", "storage_uri": "x", "cameras_json": "{}",
                       "created_at": 2 * S}],
        "review": [{"id": 50 + i, "episode_id": 200 + i, "ticket_id": 7, "accepted": i != 2,
                    "reason": "jerk 4.1 above 3.0" if i == 2 else "", "metrics_json": "{}", "reviewer": "reviewer",
                    "created_at": (12 + i) * S} for i in range(3)],
        "payment": [{"id": 60 + i, "ticket_id": 7, "contributor_id": 9, "episode_id": 200 + i, "dataset_offer_id": 0,
                     "batch_ref": None, "amount_cents": 500, "provider": "stripe_test", "provider_ref": f"tr_{i}",
                     "status": "paid", "error": None, "created_at": (13 + i) * S} for i in range(2)]
                   + [{"id": 63, "ticket_id": 7, "contributor_id": 9, "episode_id": 0, "dataset_offer_id": 0,
                       "batch_ref": "task:40", "amount_cents": 200, "provider": "stripe_test", "provider_ref": None,
                       "status": "pending", "error": None, "created_at": 14 * S},
                      {"id": 64, "ticket_id": 7, "contributor_id": 9, "episode_id": 0, "dataset_offer_id": 0,
                       "batch_ref": "task:99", "amount_cents": 900, "provider": "stripe_test", "provider_ref": None,
                       "status": "blocked_budget", "error": "over budget", "created_at": 15 * S}],
        "dataset_offer": [],
        "training_run": [{"id": 5, "ticket_id": 7, "iteration": 0, "episode_ids": [200, 201], "status": "done",
                          "modal_call_id": "stub-abc" if fake else "fc-123", "base_checkpoint_uri": "gs://pi05_base",
                          "checkpoint_uri": "fake://tdm28/checkpoints/run-5" if fake else "modal-vol://ckpt/run-5",
                          "hyperparams_json": "{}", "metrics_json": json.dumps({"fake": True} if fake else {"loss": 0.1}),
                          "gpu_cost_cents": 0 if fake else 348, "error": None, "created_at": 16 * S,
                          "started_at": 16 * S, "finished_at": 19 * S}],
        "agent_session": [],
        "event_log": [
            _ev(1, 1, "ticket.created", {"ticket_id": 7}, actor="web"),
            _ev(2, 3, "eval.finalized", {"ticket_id": 7, "eval_run_id": 1, "summary": "3/10"}),
            _ev(3, 4, "diagnose.clusters", {"ticket_id": 7, "iteration": 0, "eval_run_id": 1, "model": "claude-opus-5-5",
                                            "reasoning": "7 trials miss the cup in the approach."}),
            _ev(4, 5, "planner.data_plan", {"ticket_id": 7, "data_plan_id": 3,
                                            "reasoning": "Failures are mid-grasp; recover.",
                                            "evidence": {"model": {"requested": "claude-opus-5-5", "served": "claude-opus-5-5"}}}),
            _ev(5, 6, "spend.approved", {"ticket_id": 7, "data_plan_id": 3, "approved_by": "human:web",
                                         "approved_cents": 3000}, actor="web"),
            _ev(6, 18, "ticket.status_changed", {"ticket_id": 7, "from": "eval", "to": "shipped", "note": "9/10"}),
            _ev(7, 19, "robot.heartbeat", {"robot_id": 1}),
            _ev(8, 21, "flywheel.shipped", {"ticket_id": 7, "checkpoint_uri": "modal-vol://ckpt/run-5",
                                            "training_run_id": 5}),
        ],
    }


def test_real_report_numbers_come_from_rows():
    r = report.assemble(tables(), 7, now_micros=99 * S)
    assert r["fake"]["any"] is False and r["fake"]["banner"] is None
    assert r["baseline"]["rate"] == 0.3 and r["baseline"]["ci"] == [0.108, 0.603]
    assert r["baseline"]["ci_text"] == "3/10, 95% CI 11–60%"
    assert r["final"]["eval_run_id"] == 2 and r["final"]["rate"] == 0.9 and r["final"]["is_deployed_checkpoint"]
    assert r["outcome"]["met_target"] is True and "met" in r["outcome"]["verdict"]
    it = r["iterations"][0]
    assert it["data_plan"]["reasoning"] == "Failures are mid-grasp; recover."
    assert it["data_plan"]["approved_by"] == "human:web" and it["data_plan"]["approvals"][0]["event_id"] == 5
    assert it["diagnosis"]["reasoning"].startswith("7 trials")
    assert it["episodes_bought"] == 3 and it["episodes_accepted"] == 2
    assert it["episodes_rejected"] == [{"episode_id": 202, "kind": "recovery_takeover",
                                        "reason": "jerk 4.1 above 3.0", "reviewer": "reviewer"}]
    assert it["cost_cents"] == {"data": 1200, "data_paid": 1000, "gpu": 348, "total": 1548}
    assert it["training_run"]["training_run_id"] == 5 and it["eval"]["eval_run_id"] == 2
    tc = r["total_cost"]
    assert tc["data"]["committed_cents"] == 1200 and tc["data"]["not_spent_cents"] == 900
    assert tc["gpu"]["cents"] == 348 and tc["total_cents"] == 1548
    assert tc["data"]["committed_by_kind_cents"] == {"episode": 1000, "task": 200}
    assert r["deployed_checkpoint"]["uri"] == "modal-vol://ckpt/run-5"
    assert r["deployed_checkpoint"]["training_run_id"] == 5 and r["deployed_checkpoint"]["fake"] is False
    assert [m["component"] for m in r["mocked"]] == ["payments"]  # Stripe test mode is always said
    assert r["event_log"]["ticket_span"]["count"] == 7  # robot.heartbeat has no ticket_id
    assert r["event_log"]["count"] == 8 and r["event_log_hash"] == r["event_log"]["hash"]
    assert [x["type"] for x in r["timeline"]][:2] == ["ticket.created", "eval.finalized"]
    assert r["missing"] == []
    md = report.render_markdown(r)
    assert "WARNING" not in md and "MOCKED (payments)" in md
    assert "3/10, 95% CI 11–60%" in md and "9/10, 95% CI 60–98%" in md
    assert "> Failures are mid-grasp; recover." in md and "jerk 4.1 above 3.0" in md and "$15.48" in md


def test_fake_inputs_are_flagged_loudly():
    r = report.assemble(tables(fake=True), 7)
    assert r["fake"]["any"] is True
    comps = {c["component"]: c for c in r["fake"]["components"]}
    assert {"ticket", "robot", "eval_trial", "episode", "training_run"} <= set(comps)
    assert comps["eval_trial"]["fake_rows"] == 20
    assert r["iterations"][0]["data_plan"]["fake"] is False  # a real planner run on fake data is not a stand-in
    assert "not robot results" in r["fake"]["banner"]
    assert any(m["component"] == "training" for m in r["mocked"])  # stub- modal call id
    assert "no GPU cost recorded (every training_run.gpu_cost_cents is 0)" in r["missing"]
    md = report.render_markdown(r)
    assert md.splitlines()[2].startswith("> **WARNING: FAKE INPUTS")


def test_missing_rows_are_listed_not_filled():
    t = tables()
    t["eval_run"] = [r for r in t["eval_run"] if r["kind"] != "baseline"]
    t["payment"] = []
    t["ticket"][0]["status"] = "eval"
    t["event_log"] = [e for e in t["event_log"] if e["type"] != "flywheel.shipped"]
    r = report.assemble(t, 7)
    assert r["baseline"] is None and r["deployed_checkpoint"] is None
    assert any("no baseline" in m for m in r["missing"])
    assert any("no data spend" in m for m in r["missing"])
    with pytest.raises(LookupError):
        report.assemble(t, 8)


def test_chain_hash_matches_tdm77_reference():
    ref_path = REPO / "spikes" / "tdm-77-events" / "chain_hash_ref.py"
    if not ref_path.exists():
        pytest.skip("TDM-77 reference not present")
    spec = importlib.util.spec_from_file_location("tdm71_chain_ref", ref_path)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    rows = tables()["event_log"] + [_ev(10, 30, "test.unicode", {"note": "naïve ✓"}, actor="human:web")]
    want = ref.chain_summary([(r["id"], r["ts"], r["actor"], r["type"], r["payload_json"]) for r in rows])
    got = report.chain_summary(list(reversed(rows)))
    assert got["hash"] == want["hash"] and got["count"] == want["count"]
    assert got["gaps"] == [[8, 10]]
    assert report.chain_summary([])["hash"] == "0" * 64


class FakeStdb:
    def __init__(self, t: dict) -> None:
        self.t, self.calls = t, []

    async def sql(self, q: str) -> list[dict]:
        words = q.split()
        table = words[3]
        rows = self.t.get(table, [])
        if "WHERE" in words:
            col, val = words[5], int(words[7])
            rows = [r for r in rows if int(r.get(col) or 0) == val]
        return rows

    async def call_reducer(self, name, *args):
        self.calls.append((name, args))


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, content_type=None):
        self.objects[key] = data
        return f"modal-vol://rdf-media/{key}"


def _client(t: dict):
    app = FastAPI()
    app.include_router(report_router)
    stdb, store = FakeStdb(t), FakeStore()
    app.dependency_overrides[get_report_stdb] = lambda: stdb
    app.dependency_overrides[get_report_storage] = lambda: store
    return TestClient(app), stdb, store


def test_routes():
    c, stdb, store = _client(tables(fake=True))
    r = c.get("/tickets/7/report")
    assert r.status_code == 200 and r.json()["fake"]["any"] is True and "markdown" not in r.json()
    assert "markdown" in c.get("/tickets/7/report?include_markdown=true").json()
    md = c.get("/tickets/7/report.md")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "FAKE INPUTS" in md.text
    assert c.get("/tickets/8/report").status_code == 404
    s = c.post("/tickets/7/report/save").json()
    assert s["fake"] is True and s["uri"].endswith(".md") and s["json_uri"].endswith(".json")
    assert len(store.objects) == 2 and all(k.startswith("reports/") for k in store.objects)
    (name, args), = stdb.calls
    assert name == "append_event" and args[:2] == ("orchestrator:report", "report.saved")
    assert json.loads(args[2])["event_log_hash"] == s["event_log_hash"]
