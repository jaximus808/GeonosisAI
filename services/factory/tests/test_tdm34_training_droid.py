"""TDM-34 unit tests: dataset checks, hyperparam choice, retry plan, loss analysis, the droid tool's
guardrails and the FactoryClient turn limit. No network, no credits, no GPU."""

from __future__ import annotations

import asyncio
import json
import math
import re

import pytest

from rdf_factory import FactoryClient, FactoryConfig
from rdf_factory.backends import RawTurn, _command_allowed
from rdf_factory.droids import training as T
from rdf_factory.droids import training_tool as tool


def ep(i, frames=600, kind="clean_teleop", status="accepted", cams=True, actions=True, dur=None):
    return {"id": i, "ticket_id": 1, "kind": kind, "review_status": status, "frame_count": frames,
            "duration_ms": int((dur if dur is not None else frames / 30) * 1000),
            "cameras_json": json.dumps({"exo": f"modal-vol://rdf-media/e{i}/exo.mp4"} if cams else {}),
            "actions_uri": f"modal-vol://rdf-media/e{i}/actions.parquet" if actions else "",
            "storage_uri": f"modal-vol://rdf-media/e{i}", "takeover_ts": None}


# --------------------------------------------------------------------------- pure logic

def test_check_dataset_excludes_and_warns():
    eps = [ep(1), ep(2), ep(3, frames=20), ep(4, cams=False), ep(5, kind="eval"), ep(6, status="rejected"),
           ep(7, dur=40)]  # 600 frames over 40 s = 15 fps
    c = T.check_dataset(eps)
    assert c["ok"] and c["episode_ids"] == [1, 2, 7]
    reasons = {x["episode_id"]: x["reason"] for x in c["excluded"]}
    assert set(reasons) == {3, 4, 5} and "action horizon" in reasons[3] and "rollout" in reasons[5]
    assert any("frames/s" in w for w in c["warnings"])
    assert c["stats"]["samples_per_epoch"] == 3 * (600 - 49)
    bad = T.check_dataset([ep(1, frames=10)])
    assert not bad["ok"] and bad["errors"]


def test_recommend_sizes_steps_from_dataset_and_respects_pins():
    c = T.check_dataset([ep(i) for i in range(1, 6)])  # 5 x 20 s demos: the S-05a shape
    r = T.recommend(c["stats"], json.loads(json.dumps(T.DEFAULT_HP)), prompt_fallback="pick up the cube")
    hp = r["hyperparams"]
    assert hp["batch_size"] == 32 and hp["lora_rank"] == 16 and hp["lr"] == 2.5e-5
    assert 1500 <= hp["steps"] <= 2000 and hp["steps"] % 100 == 0  # ~20 epochs, near the recipe's 2000
    assert hp["prompt"] == "pick up the cube" and r["pinned"] == [] and len(r["rationale"]) == 4
    assert 15 <= r["estimate"]["epochs"] <= 25
    pinned = T.recommend(c["stats"], {**T.DEFAULT_HP, "steps": 1200, "batch_size": 16})
    assert pinned["hyperparams"]["steps"] == 1200 and pinned["pinned"] == ["batch_size", "steps"]
    assert math.isclose(pinned["hyperparams"]["lr"], 1.77e-5)
    cheap = T.recommend(c["stats"], T.DEFAULT_HP, budget_left_cents=100)
    assert cheap["estimate"]["gpu_cost_cents"] <= 100 and any("budget" in w for w in cheap["rationale"])
    assert "error" in T.recommend(c["stats"], T.DEFAULT_HP, budget_left_cents=5)


def test_validate_bounds():
    ok = {"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16}
    assert T.validate(ok) == []
    assert T.validate({**ok, "lr": 1e-2}) and T.validate({**ok, "steps": 90000}) and T.validate({**ok, "lora_rank": 0})
    assert T.validate({**ok, "batch_size": 24}) and T.validate(ok, budget_left_cents=50)


def test_retry_plan_by_failure_kind():
    hp = {"lr": 2.5e-5, "steps": 1800, "batch_size": 32, "lora_rank": 16, "prompt": "x"}
    p = T.retry_plan("finetune failed: XlaRuntimeError: RESOURCE_EXHAUSTED: Out of memory while trying to allocate", hp)
    assert p["kind"] == "oom" and p["retry"]
    assert p["hyperparams"]["batch_size"] == 16 and p["hyperparams"]["steps"] == 3600
    assert math.isclose(p["hyperparams"]["lr"], 1.77e-5)
    assert not T.retry_plan("CUDA out of memory", {**hp, "batch_size": 8})["retry"]
    assert T.retry_plan("FunctionTimeoutError: timed out", hp)["hyperparams"]["steps"] < 1800
    assert T.retry_plan("Modal unreachable for 8 polls", hp)["hyperparams"] == hp
    assert not T.retry_plan("launch failed: ValueError: episodes [3] not found", hp)["retry"]
    assert not T.retry_plan("RESOURCE_EXHAUSTED", hp, budget_left_cents=10)["retry"]


def curve(f, n=40, steps=2000):
    return [[int(i * steps / n), f(i / n)] for i in range(1, n + 1)]


def test_analyze_loss():
    healthy = {"loss_curve": curve(lambda x: 0.05 + 0.4 * math.exp(-4 * x) + 0.004 * (1 - x)), "steps": 2000}
    a = T.analyze_loss(healthy, samples_per_epoch=2755, batch_size=32, episodes=5)
    assert a["verdict"] in ("ok", "suspect") and "diverged" not in a["flags"] and a["drop_pct"] > 70
    div = T.analyze_loss({"loss_curve": curve(lambda x: 0.5 - 0.6 * x if x < 0.5 else 0.2 + 1.2 * (x - 0.5))})
    assert div["verdict"] == "diverged"
    assert T.analyze_loss({"loss_curve": [[10, 0.5], [20, float("nan")], [30, 0.4]]})["verdict"] == "diverged"
    flat = T.analyze_loss({"loss_curve": curve(lambda x: 0.5 - 0.02 * x)})
    assert "not_learning" in flat["flags"]
    over = T.analyze_loss({"loss_curve": curve(lambda x: 0.5 * math.exp(-8 * x)), "steps": 4000},
                          samples_per_epoch=1000, batch_size=32, episodes=3)
    assert "overfit_risk" in over["flags"]
    assert T.analyze_loss({"loss": 0.0})["verdict"] == "no_curve"


def test_waiting_line_is_a_valid_bridge_predicate():
    line = T.waiting_line(7)
    pred = json.loads(line.removeprefix("WAITING_FOR "))
    assert [l["value"] for l in pred["any"]] == ["done", "failed", "cancelled"] and pred["any"][0]["id"] == 7
    assert _command_allowed(f"{T.TOOL_PREFIX} inspect --ctx /tmp/x/droid_context.json", [T.TOOL_PREFIX])
    assert not _command_allowed(f"{T.TOOL_PREFIX} inspect --ctx a; rm -rf /", [T.TOOL_PREFIX])
    assert not _command_allowed("uv run python -c 'import os'", [T.TOOL_PREFIX])


# --------------------------------------------------------------------------- the tool against a fake DB

class FakeDB:
    """Tables as lists of dicts; `SELECT * FROM t [WHERE col = n]`; the reducers the tool calls."""

    def __init__(self):
        self.t = {"ticket": [{"id": 1, "task_description": "pick up the cube", "status": "train", "iteration": 1,
                              "budget_cents": 5000, "spent_cents": 1000}],
                  "run_settings": [{"ticket_id": 1, "hyperparams_json": json.dumps(T.DEFAULT_HP)}],
                  "episode": [ep(i) for i in range(1, 6)], "dataset": [], "training_run": [],
                  "agent_session": [{"id": 9, "ticket_id": 1, "role": "training_droid", "status": "running"}]}
        self.events = []

    def sql(self, q):
        m = re.match(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (\d+))?", q)
        rows = self.t[m.group(1)]
        if m.group(2):
            rows = [r for r in rows if int(r[m.group(2)]) == int(m.group(3))]
        return [dict(r) for r in rows]

    def call_reducer(self, name, *args):
        if name == "queue_training_run":
            t, ids, hp = args
            if any(r["status"] in ("queued", "running") for r in self.t["training_run"]):
                raise RuntimeError("already active")
            self.t["training_run"].append({"id": len(self.t["training_run"]) + 1, "ticket_id": t, "iteration": 1,
                                           "episode_ids": json.loads(ids), "status": "queued", "hyperparams_json": hp,
                                           "metrics_json": "{}", "gpu_cost_cents": 0, "error": None})
        elif name == "append_event":
            self.events.append((args[0], args[1], json.loads(args[2])))


def run_tool(db, *argv):
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = tool.main(list(argv), client=db)
    return code, json.loads(buf.getvalue())


@pytest.fixture
def tctx(tmp_path, monkeypatch):
    monkeypatch.delenv("SPACETIME_HOST", raising=False)
    monkeypatch.delenv("SPACETIME_DB_NAME", raising=False)
    tdir = tmp_path / "training-t1-i1"
    ctx = T.write_context(tdir, host="http://127.0.0.1:1", db="x", ticket_id=1, iteration=1)
    return tdir, str(ctx)


def write_cfg(tdir, name, hp, **extra):
    import yaml

    p = tdir / "configs" / name
    p.write_text(yaml.safe_dump({"ticket_id": 1, "iteration": 1, "episode_ids": [1, 2, 3, 4, 5], "hyperparams": hp,
                                 "rationale": ["recipe defaults sized to 5 demos"], **extra}))
    return str(p)


def test_tool_inspect_queue_retry_report(tctx):
    tdir, ctx = tctx
    db = FakeDB()
    code, out = run_tool(db, "inspect", "--ctx", ctx)
    assert code == 0 and out["checks"]["ok"] and out["agent_session_id"] == 9 and out["budget_left_cents"] == 4000
    hp = out["recommendation"]["hyperparams"]

    # no rationale -> refused; out-of-bounds -> refused
    p = write_cfg(tdir, "bad.yaml", hp, rationale=[])
    assert run_tool(db, "queue", "--ctx", ctx, "--config", p)[0] == 1
    p = write_cfg(tdir, "bad2.yaml", {**hp, "lr": 0.5})
    assert "lr" in run_tool(db, "queue", "--ctx", ctx, "--config", p)[1]["error"]

    p = write_cfg(tdir, "train_1_1.yaml", hp)
    code, q = run_tool(db, "queue", "--ctx", ctx, "--config", p)
    assert code == 0 and q["training_run_id"] == 1 and q["waiting_line"] == T.waiting_line(1)
    stored = json.loads(db.t["training_run"][0]["hyperparams_json"])
    assert stored["steps"] == hp["steps"] and stored["droid_session_id"] == 9
    actor, typ, payload = db.events[-1]
    assert (actor, typ) == ("droid:training", "training.config") and payload["rationale"]

    # a second queue while running is refused
    assert run_tool(db, "queue", "--ctx", ctx, "--config", p)[0] == 1
    db.t["training_run"][0].update(status="failed", error="finetune failed: RESOURCE_EXHAUSTED: out of memory")
    code, s = run_tool(db, "status", "--ctx", ctx, "--run", "1")
    assert s["retry_plan"]["kind"] == "oom" and s["retry_plan"]["retry"] and s["retries_left"] == 1
    # retry must name retry_of
    p2 = write_cfg(tdir, "train_1_1_retry1.yaml", s["retry_plan"]["hyperparams"])
    assert "retry_of" in run_tool(db, "queue", "--ctx", ctx, "--config", p2)[1]["error"]
    p2 = write_cfg(tdir, "train_1_1_retry1.yaml", s["retry_plan"]["hyperparams"], retry_of=1)
    code, q2 = run_tool(db, "queue", "--ctx", ctx, "--config", p2)
    assert code == 0 and q2["hyperparams"]["batch_size"] == 16 and q2["hyperparams"]["retry_of"] == 1

    # a third run is refused (one retry per droid session)
    db.t["training_run"][1].update(status="failed", error="RESOURCE_EXHAUSTED")
    assert "retry limit" in run_tool(db, "queue", "--ctx", ctx, "--config", p2)[1]["error"]
    assert run_tool(db, "status", "--ctx", ctx, "--run", "2")[1]["retry_plan"]["retry"] is False

    db.t["training_run"][1].update(status="done", checkpoint_uri="fake://ckpt", metrics_json=json.dumps(
        {"loss_curve": curve(lambda x: 0.05 + 0.4 * math.exp(-4 * x)), "steps": 3600, "fake": True}))
    code, s2 = run_tool(db, "status", "--ctx", ctx, "--run", "2")
    assert s2["analysis"]["verdict"] in ("ok", "suspect") and s2["fake"]
    note = tdir / "training_note_1_1.md"
    note.write_text("run 2 done after OOM retry; loss 0.45 -> 0.06")
    code, r = run_tool(db, "report", "--ctx", ctx, "--run", "2", "--note", str(note))
    assert code == 0 and db.events[-1][1] == "training.report" and "OOM" in db.events[-1][2]["note"]
    # notes/configs outside the ticket dir are refused
    assert run_tool(db, "report", "--ctx", ctx, "--run", "2", "--note", "/etc/hosts")[0] == 1


def test_tool_refuses_a_different_database(tctx, monkeypatch):
    _, ctx = tctx
    monkeypatch.setenv("SPACETIME_HOST", "https://maincloud.spacetimedb.com")
    code, out = run_tool(FakeDB(), "inspect", "--ctx", ctx)
    assert code == 1 and "refusing" in out["error"]


def test_tool_refuses_bad_dataset(tctx):
    tdir, ctx = tctx
    db = FakeDB()
    db.t["episode"] = [ep(1, frames=10)]
    code, out = run_tool(db, "inspect", "--ctx", ctx)
    assert code == 0 and not out["checks"]["ok"]
    p = write_cfg(tdir, "train_1_1.yaml", T.DEFAULT_HP, episode_ids=[1])
    assert "dataset checks fail" in run_tool(db, "queue", "--ctx", ctx, "--config", p)[1]["error"]
    assert db.t["training_run"] == []


# --------------------------------------------------------------------------- FactoryClient bounds

class Stdb:
    def __init__(self):
        self.rows, self.events, self.n = {}, [], 0

    async def call_reducer(self, name, *a):
        if name == "upsert_agent_session":
            if a[0] == 0:
                self.n += 1
                self.rows[self.n] = {"id": self.n, "role": a[1], "name": a[5], "status": a[3], "ticket_id": a[6],
                                     "factory_session_id": None, "factory_session_ids_json": "[]"}
            else:
                self.rows[a[0]].update(status=a[3], current_step=a[4], factory_session_id=a[2] or self.rows[a[0]]["factory_session_id"])
        elif name == "agent_wait_for":
            self.rows[a[0]].update(status="waiting_for")
        elif name == "agent_resume":
            self.rows[a[0]].update(status="resuming")
        elif name == "append_event":
            self.events.append((a[0], a[1]))

    async def sql(self, q):
        m = re.search(r"name = '(.*)'", q)
        if m:
            return [dict(r) for r in self.rows.values() if r["name"] == m.group(1)]
        m = re.search(r"id = (\d+)", q)
        r = self.rows.get(int(m.group(1))) if m else None
        return [dict(r)] if r else []

    async def aclose(self):
        pass


class Backend:
    mode = "local"

    def __init__(self):
        self.specs = []

    async def run_turn(self, spec, fsid, text, emit, on_session_id):
        self.specs.append(spec)
        if not fsid:
            fsid = "fs-1"
            await on_session_id(fsid)
        return RawTurn(fsid, "success", True, "parked\n" + T.waiting_line(1), credits=100.0)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


def test_client_turn_limit_and_write_roots(tmp_path):
    async def go():
        cfg = FactoryConfig(state_dir=tmp_path / "state", work_root=tmp_path / "sessions", repo_dir=tmp_path)
        be, db = Backend(), Stdb()
        fc = FactoryClient(cfg, stdb=db, backends={"local": be})
        extra = tmp_path / "ticketdir"
        r = await fc.start_session("training_droid", "train", ticket_id=1, write_roots=[str(extra)],
                                   allow_commands=[T.TOOL_PREFIX], max_turns=2)
        assert r.status == "waiting_for" and r.waiting_for["any"][0]["id"] == 1
        spec = be.specs[0]
        assert spec.write_roots[0] == spec.workdir and str(extra.resolve()) in spec.write_roots
        r2 = await fc.resume(r.agent_session_id, "run 1 failed")
        assert r2.status == "waiting_for" and r2.turn == 2
        r3 = await fc.resume(r.agent_session_id, "run 2 done")
        assert r3.status == "failed" and "turn limit" in r3.error and len(be.specs) == 2
        assert db.rows[r.agent_session_id]["status"] == "failed"
        assert ("droid:training", "droid.error") in db.events

    asyncio.run(go())


# --------------------------------------------------------------------------- flywheel step

class _View:
    def __init__(self, run=None, ids=(1, 2)):
        self.training_run, self.accepted_episode_ids, self.iteration = run, list(ids), 1


class _Bus:
    def __init__(self, sessions):
        self.s = sessions

    def rows(self, table):
        return self.s if table == "agent_session" else []


class _Ctx:
    def __init__(self, view, sessions=()):
        self.view, self.bus, self.ticket_id = view, _Bus(list(sessions)), 1
        self.logs, self.events = [], []

    def log(self, msg, **f):
        self.logs.append((msg, f))

    async def event(self, t, p, actor=None):
        self.events.append((t, p))


def test_training_droid_step_is_idempotent(monkeypatch):
    launched = []

    async def fake_launch(self, ctx, iteration):
        launched.append((ctx.ticket_id, iteration))

    monkeypatch.setattr(T.TrainingDroidStep, "_launch", fake_launch)

    async def go():
        step = T.TrainingDroidStep()
        c = _Ctx(_View(run={"id": 3, "status": "running"}))
        await step.run(c)
        assert "already running" in c.logs[-1][1]["reason"]
        c = _Ctx(_View(), sessions=[{"id": 5, "role": "training_droid", "ticket_id": 1, "status": "waiting_for"}])
        await step.run(c)
        assert "agent_session 5" in c.logs[-1][1]["reason"]
        c = _Ctx(_View(ids=()))
        await step.run(c)
        assert c.logs[-1][1]["reason"] == "no accepted episodes"
        c = _Ctx(_View(run={"id": 3, "status": "failed"}),
                 sessions=[{"id": 5, "role": "training_droid", "ticket_id": 1, "status": "done"}])
        await step.run(c)
        await asyncio.sleep(0)
        assert launched == [(1, 1)] and c.events[0][0] == "training.droid_starting"

    asyncio.run(go())
