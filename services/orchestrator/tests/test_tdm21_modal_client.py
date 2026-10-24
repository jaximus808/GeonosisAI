"""TDM-21 Modal client: launch, poll, done/failed, restart resume, stub mode (fake Modal + fake DB)."""
from __future__ import annotations

import json

import pytest

from rdf_orchestrator import modal_client as mc
from rdf_orchestrator.modal_client import PollResult, TrainingRunner, build_manifest, gpu_cost_cents


class FakeDB:
    """Applies set_training_status like the reducer does (enough of it for these tests)."""

    NEXT = {"queued": ("queued", "running", "failed", "cancelled"), "running": ("running", "done", "failed", "cancelled")}

    def __init__(self, runs, episodes=(), tickets=()):
        self.tables = {"training_run": {r["id"]: dict(r) for r in runs},
                       "episode": {e["id"]: dict(e) for e in episodes},
                       "ticket": {t["id"]: dict(t) for t in tickets}}
        self.calls = []

    def call_reducer(self, name, *args):
        assert name == "set_training_status"
        rid, status, call_id, ckpt, metrics, cost = args
        self.calls.append(args)
        run = self.tables["training_run"][rid]
        assert status in self.NEXT.get(run["status"], ()), f"{run['status']} -> {status}"
        if status == "done":
            assert ckpt or run.get("checkpoint_uri")
        run.update(status=status)
        if call_id:
            run["modal_call_id"] = call_id
        if ckpt:
            run["checkpoint_uri"] = ckpt
        if metrics is not None:
            run["metrics_json"] = metrics
            if status == "failed":
                run["error"] = json.loads(metrics).get("error")
        if cost is not None:
            run["gpu_cost_cents"] = cost

    # bus-mirror surface
    def get(self, table, key):
        return self.tables[table].get(key)

    def rows(self, table):
        return list(self.tables[table].values())


class FakeModal:
    def __init__(self):
        self.spawned, self.results, self.cancelled, self.n = [], {}, [], 0

    def spawn(self, function, **kw):
        self.n += 1
        cid = f"fc-{function}-{self.n}"
        self.spawned.append((function, cid, kw))
        self.results[cid] = PollResult("pending")
        return cid

    def poll(self, call_id):
        return self.results[call_id]

    def cancel(self, call_id):
        self.cancelled.append(call_id)


RUN = {"id": 5, "ticket_id": 2, "status": "queued", "episode_ids": [11, 12], "modal_call_id": None,
       "hyperparams_json": json.dumps({"steps": 100}), "metrics_json": "{}", "base_checkpoint_uri": "gs://base",
       "gpu_cost_cents": 0}
EPS = [{"id": 11, "ticket_id": 2, "kind": "clean_teleop", "review_status": "accepted", "cameras_json": '{"exo":"modal-vol://rdf-media/a.mp4"}',
        "actions_uri": "modal-vol://rdf-media/a.parquet", "takeover_ts": None, "handback_ts": None},
       {"id": 12, "ticket_id": 2, "kind": "recovery_takeover", "review_status": "accepted", "cameras_json": "{}",
        "actions_uri": "modal-vol://rdf-media/b.parquet", "takeover_ts": 1_000_000, "handback_ts": 2_000_000}]
TICKETS = [{"id": 2, "task_description": "put the cup in the bin"}]


def make(runs=(RUN,), monkeypatch=None):
    db, modal = FakeDB(runs, EPS, TICKETS), FakeModal()
    r = TrainingRunner(writer=lambda: db, modal=lambda: modal, poll_s=0.01)
    r.bind(db)
    return r, db, modal


def run_row(db, rid=5):
    return db.tables["training_run"][rid]


def test_manifest_from_accepted_episodes():
    m = build_manifest(RUN, TICKETS[0], {e["id"]: e for e in EPS})
    assert m["repo_id"] == "rdf/ticket-2-run-5" and m["task"] == "put the cup in the bin"
    assert [e["episode_id"] for e in m["episodes"]] == ["11", "12"]
    assert m["episodes"][1]["takeover_ts"] == 1_000_000
    with pytest.raises(ValueError, match="not found"):
        build_manifest({**RUN, "episode_ids": [99]}, None, {})


def test_happy_path_convert_then_finetune_then_done(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    fn, cid, kw = modal.spawned[0]
    assert fn == "convert" and kw["ticket_id"] == "2" and len(kw["episode_manifest"]["episodes"]) == 2
    row = run_row(db)
    assert row["status"] == "running" and row["modal_call_id"] == cid
    assert json.loads(row["metrics_json"])["phase"] == "convert"

    r.launch(dict(RUN))  # resync re-fires: the row is running now, nothing new is spawned
    assert len(modal.spawned) == 1

    r.track(row)
    assert r.poll_once(5) == "pending"
    modal.results[cid] = PollResult("done", value="modal-vol://rdf-datasets/lerobot/rdf/ticket-2-run-5")
    assert r.poll_once(5) == "finetune_spawned"
    fn2, cid2, kw2 = modal.spawned[1]
    assert fn2 == "finetune" and kw2["dataset_uri"].endswith("ticket-2-run-5")
    assert kw2["hyperparams"]["steps"] == 100 and kw2["hyperparams"]["base_checkpoint_uri"] == "gs://base"
    m = json.loads(run_row(db)["metrics_json"])
    assert run_row(db)["modal_call_id"] == cid2 and m["phase"] == "finetune" and m["convert_call_id"] == cid

    modal.results[cid2] = PollResult("done", value={"checkpoint_uri": "modal-vol://rdf-checkpoints/runs/x/99",
                                                    "metrics": {"loss": 0.1, "wall_s": 3600, "gpu": "H100"}})
    assert r.poll_once(5) == "done"
    row = run_row(db)
    assert row["status"] == "done" and row["checkpoint_uri"].endswith("/99") and row["gpu_cost_cents"] == 395
    assert json.loads(row["metrics_json"])["loss"] == 0.1
    assert r.poll_once(5) == "untracked"


def test_failed_call_marks_failed_with_message(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    cid = modal.spawned[0][1]
    r.track(run_row(db))
    modal.results[cid] = PollResult("failed", error="ConvertError: episode 12 has no human ticks")
    assert r.poll_once(5) == "failed"
    row = run_row(db)
    assert row["status"] == "failed" and "no human ticks" in row["error"] and row["error"].startswith("convert failed")


def test_transient_errors_retry_then_fail(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    cid = modal.spawned[0][1]
    r.track(run_row(db))
    modal.results[cid] = PollResult("transient", error="ConnectionError: reset")
    for _ in range(mc.MAX_TRANSIENT_POLL_ERRORS - 1):
        assert r.poll_once(5) == "transient"
    assert run_row(db)["status"] == "running"
    assert r.poll_once(5) == "failed" and "unreachable" in run_row(db)["error"]


def test_restart_resumes_from_row(monkeypatch):
    """A new runner (process restart) picks up a running finetune from the row alone."""
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    modal.results[modal.spawned[0][1]] = PollResult("done", value="modal-vol://rdf-datasets/lerobot/x")
    r.track(run_row(db))
    r.poll_once(5)
    cid2 = modal.spawned[1][1]

    r2 = TrainingRunner(writer=lambda: db, modal=lambda: modal, poll_s=0.01)  # fresh memory
    r2.bind(db)
    r2.launch(dict(run_row(db)))  # boot snapshot: on_enter(queued) does not match a running row anyway
    r2.track(dict(run_row(db)))   # boot snapshot: on_enter(running)
    modal.results[cid2] = PollResult("done", value={"checkpoint_uri": "stub://rdf-checkpoints/runs/y/99",
                                                    "metrics": {"stub": True, "gpu": None, "wall_s": 20}})
    assert r2.poll_once(5) == "done"
    assert len(modal.spawned) == 2 and run_row(db)["gpu_cost_cents"] == 0  # no duplicate spawn; CPU stub costs 0


def test_restart_during_convert_spawns_finetune_once(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    modal.results[modal.spawned[0][1]] = PollResult("done", value="modal-vol://rdf-datasets/lerobot/x")
    r2 = TrainingRunner(writer=lambda: db, modal=lambda: modal, poll_s=0.01)
    r2.bind(db)
    r2.track(dict(run_row(db)))
    assert r2.poll_once(5) == "finetune_spawned"
    assert [s[0] for s in modal.spawned] == ["convert", "finetune"]


def test_dataset_uri_skips_convert(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    run = {**RUN, "hyperparams_json": json.dumps({"dataset_uri": "modal-vol://rdf-datasets/lerobot/old"})}
    r, db, modal = make(runs=(run,))
    r.launch(dict(run))
    assert [s[0] for s in modal.spawned] == ["finetune"]
    assert "dataset_uri" not in modal.spawned[0][2]["hyperparams"]
    assert json.loads(run_row(db)["metrics_json"])["phase"] == "finetune"


def test_launch_error_fails_run(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()

    def boom(function, **kw):
        raise RuntimeError("App rdf-training not found")

    modal.spawn = boom
    r.launch(dict(RUN))
    assert run_row(db)["status"] == "failed" and "not found" in run_row(db)["error"]


def test_cancelled_run_cancels_modal_call(monkeypatch):
    monkeypatch.delenv("RDF_MODAL_STUB", raising=False)
    r, db, modal = make()
    r.launch(dict(RUN))
    cid = modal.spawned[0][1]
    r.track(run_row(db))
    db.call_reducer("set_training_status", 5, "cancelled", None, None, None, None)
    assert r.poll_once(5) == "untracked" and modal.cancelled == [cid]


def test_stub_mode_sets_stub_call_id_and_never_spawns(monkeypatch):
    monkeypatch.setenv("RDF_MODAL_STUB", "1")
    r, db, modal = make()
    r.launch(dict(RUN))
    row = run_row(db)
    assert row["status"] == "running" and row["modal_call_id"].startswith("stub-")
    assert json.loads(row["metrics_json"])["fake"] is True and not modal.spawned
    r.track(row)  # stub runs are left to the harness StubTrainer
    assert 5 not in r._tracked


def test_gpu_cost():
    assert gpu_cost_cents(3600, "H100") == 395
    assert gpu_cost_cents(1800, "A100-80GB") == 125
    assert gpu_cost_cents(100, None) == 0
    assert gpu_cost_cents(3600, "H100:2") == 395  # count comes from gpus=
    assert gpu_cost_cents(3600, "H100", gpus=2) == 790
