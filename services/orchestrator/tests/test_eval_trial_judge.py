"""Claude trial judge (trial_judge.py + handlers/trial_judge.py): fake LLM, fake snapshots, fake stdb."""
from __future__ import annotations

import json
from types import SimpleNamespace

from rdf_orchestrator import trial_judge as tj
from rdf_orchestrator.bus import Event
from rdf_orchestrator.handlers import trial_judge as h
from rdf_orchestrator.stdb import Row

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
REAL_CFG = json.dumps({"stream": {"url": "wss://robo.trycloudflare.com/ws/cameras",
                                  "http": "https://robo.trycloudflare.com", "cams": ["top", "wrist", "side"]}})


class FakeBus:
    def __init__(self, tables):
        self.tables = {t: [Row(r) for r in rows] for t, rows in tables.items()}

    def rows(self, table):
        return list(self.tables.get(table, []))

    def get(self, table, key):
        return next((r for r in self.tables.get(table, []) if r.get("id") == key), None)


class FakeStdb:
    def __init__(self, fail_command=False):
        self.calls, self.fail_command = [], fail_command

    def call_reducer(self, name, *args):
        if name == "issue_robot_command" and self.fail_command:
            raise RuntimeError("robot 4 is in estop")
        self.calls.append((name, args))

    def events(self, type_=None):
        return [(a[1], json.loads(a[2])) for n, a in self.calls if n == "append_event" and (type_ is None or a[1] == type_)]

    def commands(self):
        return [(a[0], a[1], json.loads(a[2]), a[3]) for n, a in self.calls if n == "issue_robot_command"]


def _bus(sim=False, events=(), trials=()):
    cfg = json.dumps({"sim": True}) if sim else REAL_CFG
    return FakeBus({
        "robot": [{"id": 4, "name": "yam-01", "embodiment": "yam", "camera_config_json": cfg}],
        "eval_run": [{"id": 9, "ticket_id": 3, "robot_id": 4, "trials": 5, "kind": "baseline"}],
        "ticket": [{"id": 3, "task_description": "put the cube on the plate"}],
        "event_log": list(events),
        "eval_trial": list(trials),
    })


def _ev(bus, index=1, source="live"):
    row = {"id": 100 + index, "type": tj.EVENT_AWAITING,
           "payload_json": json.dumps({"eval_run_id": 9, "index": index, "robot_id": 4, "ticket_id": 3, "trials": 5})}
    return Event(table="event_log", kind="insert", row=Row(row), source=source, bus=bus)


def _wire(monkeypatch, verdict=None, frames_exc=None, llm_exc=None, fail_command=False):
    stdb = FakeStdb(fail_command)
    seen = {}
    h._claimed.clear()
    monkeypatch.setattr(h, "make_stdb", lambda: stdb)

    def frames(robot):
        if frames_exc:
            raise frames_exc
        return [("top frame 1/2", JPEG), ("wrist frame 1/2", JPEG)]

    def ask(task, fr, trial=""):
        seen.update(task=task, frames=len(fr), trial=trial)
        if llm_exc:
            raise llm_exc
        return verdict or tj.Verdict(False, 0.82, "cube is beside the plate", "claude-opus-5-5")

    monkeypatch.setattr(h, "fetch_frames", frames)
    monkeypatch.setattr(h, "ask", ask)
    monkeypatch.delenv("RDF_EVAL_JUDGE", raising=False)
    return stdb, seen


def test_real_robot_trial_is_judged_and_outcome_sent(monkeypatch):
    stdb, seen = _wire(monkeypatch)
    h.judge_trial(_ev(_bus()))
    assert seen == {"task": "put the cube on the plate", "frames": 2, "trial": "2/5"}
    [(rid, typ, payload, tid)] = stdb.commands()
    assert (rid, typ, tid) == (4, "trial_outcome", 3)
    assert payload == {"success": False, "note": "[Claude judge] cube is beside the plate (conf 0.82)",
                       "eval_run_id": 9, "index": 1}
    [(_, ev)] = stdb.events(tj.EVENT_JUDGED)
    assert ev["success"] is False and ev["confidence"] == 0.82 and ev["model"] == "claude-opus-5-5"
    assert ev["note"] == "cube is beside the plate"


def test_one_judgment_per_trial(monkeypatch):
    stdb, _ = _wire(monkeypatch)
    bus = _bus()
    h.judge_trial(_ev(bus))
    h.judge_trial(_ev(bus))  # in-process claim
    assert len(stdb.commands()) == 1
    judged = {"id": 1, "type": tj.EVENT_JUDGED, "payload_json": json.dumps({"eval_run_id": 9, "index": 1})}
    stdb2, _ = _wire(monkeypatch)
    h.judge_trial(_ev(_bus(events=[judged])))  # after a restart: the logged judgment
    h.judge_trial(_ev(_bus(trials=[{"id": 7, "eval_run_id": 9, "trial_index": 1}])))  # trial already recorded
    assert stdb2.calls == []


def test_sim_robot_snapshot_and_switch_are_skipped(monkeypatch):
    stdb, _ = _wire(monkeypatch)
    h.judge_trial(_ev(_bus(sim=True)))
    h.judge_trial(_ev(_bus(), source="snapshot"))
    monkeypatch.setenv("RDF_EVAL_JUDGE", "0")
    h.judge_trial(_ev(_bus()))
    assert stdb.calls == []


def test_failures_log_judge_failed_and_send_nothing(monkeypatch):
    for kw, stage in ((dict(frames_exc=tj.JudgeError("snapshot", "no camera snapshot")), "snapshot"),
                      (dict(llm_exc=tj.JudgeError("llm", "APIError: overloaded")), "llm")):
        stdb, _ = _wire(monkeypatch, **kw)
        h.judge_trial(_ev(_bus()))
        assert stdb.commands() == []
        [(_, ev)] = stdb.events(tj.EVENT_FAILED)
        assert ev["stage"] == stage and ev["eval_run_id"] == 9
        assert stdb.events(tj.EVENT_JUDGED) == []
    stdb, _ = _wire(monkeypatch, fail_command=True)
    h.judge_trial(_ev(_bus()))
    assert [e["stage"] for _, e in stdb.events(tj.EVENT_FAILED)] == ["command"]


def test_fetch_frames_uses_stream_http_and_top_wrist(monkeypatch):
    got = []
    robot = {"id": 4, "name": "yam-01", "camera_config_json": REAL_CFG}
    out = tj.fetch_frames(robot, fetch=lambda base, cam, tok: got.append((base, cam, tok)) or JPEG,
                          token=lambda r: "tok", frames=2, sleep=lambda s: None)
    assert [g[1] for g in got] == ["top", "wrist", "top", "wrist"]
    assert {g[0] for g in got} == {"https://robo.trycloudflare.com"} and len(out) == 4
    # no http key: derived from the ws url
    assert tj.stream_http_base({"camera_config_json": json.dumps(
        {"stream": {"url": "wss://x.trycloudflare.com/ws/cameras"}})}) == "https://x.trycloudflare.com"
    try:
        tj.fetch_frames(robot, fetch=lambda *a: (_ for _ in ()).throw(OSError("down")), token=lambda r: "t",
                        sleep=lambda s: None)
    except tj.JudgeError as e:
        assert e.stage == "snapshot"
    else:
        raise AssertionError("expected JudgeError")


class FakeLLM:
    def __init__(self, text):
        self.text, self.kw = text, None

    def complete_structured(self, **kw):
        self.kw = kw
        return SimpleNamespace(text=self.text, meta={})


def test_ask_claude_parses_strict_json_and_sends_images():
    llm = FakeLLM(json.dumps({"success": True, "confidence": 1.4, "note": "cube sits on the plate"}))
    v = tj.ask_claude("put the cube on the plate", [("top frame 1/1", JPEG)], trial="1/5", client=llm)
    assert (v.success, v.confidence, v.note) == (True, 1.0, "cube sits on the plate")
    content = llm.kw["messages"][0]["content"]
    assert any(c["type"] == "image" for c in content) and "put the cube on the plate" in content[0]["text"]
    assert llm.kw["step"] == "eval_judge" and llm.kw["schema"]["required"] == ["success", "confidence", "note"]
    try:
        tj.ask_claude("t", [("top", JPEG)], client=FakeLLM("not json"))
    except tj.JudgeError as e:
        assert e.stage == "llm"
    else:
        raise AssertionError("expected JudgeError")
