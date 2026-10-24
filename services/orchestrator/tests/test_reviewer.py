"""TDM-25 reviewer on the labelled synthetic fixture set (tests/tdm25_fixtures.py, NOT real robot data).

Offline tests use a scripted judge (they test the plumbing: Claude is only asked after every
deterministic check passed, gets the keyframes, and its verdict becomes the review). The live test
calls the real Claude API on the same fixtures:  RDF_LIVE_CLAUDE=1 uv run pytest tests/test_reviewer.py -k live
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import tdm25_fixtures as fx  # noqa: E402

from rdf_orchestrator import reviewer as rv  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed for the video fixtures")

TICKET = {"id": 1, "task_description": fx.TASK, "status": "review"}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    return {f.name: f for f in fx.build_fixture_set(tmp_path_factory.mktemp("tdm25"))}


@pytest.fixture(scope="module")
def cfg():
    return rv.load_config(allow_fixtures=True)  # the fixtures are flagged fixture:true


class ScriptedJudge:
    """Says off-task when the episode summary is for the off_task fixture's episode id."""

    model = "scripted"

    def __init__(self, off_task_ids=()):
        self.off_task_ids, self.calls = set(off_task_ids), []
        self.last_usage = None

    def judge(self, messages):
        content = messages[0]["content"]
        self.calls.append(content)
        eid = int(content[0]["text"].split("Episode ")[1].split(":")[0])
        if eid in self.off_task_ids:
            return rv.Judgement(observations="arm pushes a green block", on_task=False, task_completed="no",
                                issues=["red cup never moves"], reason="The arm moves a green block; the red cup is never touched.")
        return rv.Judgement(observations="cup lifted into the bin", on_task=True, task_completed="yes",
                            issues=[], reason="The red cup is grasped and placed in the blue bin.")


def test_fixture_set_one_accept_four_rejects(fixtures, cfg):
    judge = ScriptedJudge([fixtures["off_task"].episode["id"]])
    out = {n: rv.review_episode(f.episode, TICKET, config=cfg, judge=judge) for n, f in fixtures.items()}
    assert [n for n, d in out.items() if d.accepted] == ["clean_demo"]
    for n, f in fixtures.items():
        d = out[n]
        assert d.accepted is f.expect_accept, (n, d.reason)
        if f.expect_check and f.expect_check != "claude":
            assert d.decided_by == "deterministic"
            assert d.metrics["checks"][f.expect_check]["status"] == "fail", (n, d.metrics["checks"])
            assert f.expect_reason.lower() in d.reason.lower(), (n, d.reason)
    assert out["off_task"].decided_by == "claude" and "green block" in out["off_task"].reason
    # Claude is asked only about the two episodes that passed every deterministic check, with 3 frames each
    assert len(judge.calls) == 2
    assert all(sum(1 for b in c if b["type"] == "image") == 3 for c in judge.calls)


def test_reasons_are_human_readable(fixtures, cfg):
    d = rv.review_episode(fixtures["too_short"].episode, TICKET, config=cfg, judge=ScriptedJudge())
    assert d.reason == "Rejected: episode is 1.2 s long; minimum is 2 s."
    d = rv.review_episode(fixtures["takeover_no_handback"].episode, TICKET, config=cfg, judge=ScriptedJudge())
    assert d.reason == "Rejected: takeover episode is missing handback_ts."
    d = rv.review_episode(fixtures["jerky"].episode, TICKET, config=cfg, judge=ScriptedJudge())
    assert d.reason.startswith("Rejected: motion is jerky: RMS joint jerk")


def test_production_config_rejects_fixture_data(fixtures):
    d = rv.review_episode(fixtures["clean_demo"].episode, TICKET, config=rv.load_config(), judge=ScriptedJudge())
    assert not d.accepted and "synthetic fixture data" in d.reason


def test_takeover_keyframes_bracket_the_takeover(fixtures):
    ep = dict(fixtures["takeover_no_handback"].episode, handback_ts=fx.T0_US + int(fx.HANDBACK_AT_S * 1e6))
    from rdf_common.episode_format import read_actions

    times = rv.keyframe_times(ep, read_actions(fixtures["takeover_no_handback"].files["actions"]), 3)
    assert [round(t, 1) for _, t in times] == [3.7, 6.9, 10.9]
    assert "before the human takeover" in times[0][0]


def test_missing_camera_video_rejects(fixtures, cfg):
    ep = dict(fixtures["clean_demo"].episode, cameras_json=json.dumps({"exo": "file:///nonexistent/exo.mp4"}))
    d = rv.review_episode(ep, TICKET, config=cfg, judge=ScriptedJudge())
    assert not d.accepted and d.decided_by == "camera_media" and "camera video unusable" in d.reason


def test_unreadable_actions_rejects(fixtures, cfg, tmp_path):
    bad = tmp_path / "actions.json"
    bad.write_text('{"fake": true}')
    ep = dict(fixtures["clean_demo"].episode, actions_uri=bad.as_uri())
    d = rv.review_episode(ep, TICKET, config=cfg, judge=ScriptedJudge())
    assert not d.accepted and "actions file unreadable" in d.reason


def test_judge_error_writes_nothing(fixtures, cfg):
    class Down:
        model = "down"

        def judge(self, messages):
            raise rv.ReviewError("Claude API error: overloaded")

    with pytest.raises(rv.ReviewError):
        rv.review_episode(fixtures["clean_demo"].episode, TICKET, config=cfg, judge=Down())


class FakeStdb:
    def __init__(self):
        self.calls = []

    def call_reducer(self, name, *args):
        self.calls.append((name, args))


def test_record_review_writes_qa_rows_then_review(fixtures, cfg):
    d = rv.review_episode(fixtures["clean_demo"].episode, TICKET, config=cfg, judge=ScriptedJudge())
    s = FakeStdb()
    rv.record_review(s, d)
    names = [c[0] for c in s.calls]
    assert names[-1] == "submit_review" and set(names[:-1]) == {"record_qa_result"}
    qa = {a[1]: a[2] for n, a in s.calls[:-1]}
    assert qa == {"actions": True, "duration": True, "frame_drops": True, "jerk": True, "success": True,
                  "camera_media": True, "claude": True}
    eid, accepted, reason, metrics = s.calls[-1][1]
    assert eid == d.episode_id and accepted is True and reason.startswith("Accepted")
    m = json.loads(metrics)
    assert m["decided_by"] == "all_passed" and m["claude"]["on_task"] is True and len(metrics) < 16384


# --------------------------------------------------------------------------- the bus handler

class FakeBus:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, t):
        return list(self.tables.get(t, {}).values())

    def get(self, t, k):
        return self.tables.get(t, {}).get(k)


def test_handler_reviews_once_and_skips_reviewed(fixtures, cfg, monkeypatch):
    from rdf_orchestrator.bus import Event
    from rdf_orchestrator.handlers import reviewer as h

    s = FakeStdb()
    monkeypatch.setattr(h, "make_stdb", lambda: s)
    monkeypatch.setattr(h, "make_config", lambda: cfg)
    monkeypatch.setattr(h, "make_judge", lambda c: ScriptedJudge())
    ep = dict(fixtures["clean_demo"].episode, id=901)
    bus = FakeBus({"ticket": {1: TICKET}, "review": {}})
    h.review_episode(Event("episode", "insert", ep, bus=bus))
    h.review_episode(Event("episode", "insert", ep, bus=bus))  # duplicate delivery: claimed, no second review
    assert [c[0] for c in s.calls].count("submit_review") == 1
    ep2 = dict(ep, id=902)
    bus.tables["review"] = {7: {"id": 7, "episode_id": 902}}
    h.review_episode(Event("episode", "insert", ep2, bus=bus))
    assert [c[0] for c in s.calls].count("submit_review") == 1


def test_handler_only_watches_training_kinds():
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.handlers import reviewer  # noqa: F401
    from rdf_orchestrator.stdb import Row

    h = next(x for x in registry.handlers if x.name == "tdm25_review_episode")
    assert h.table == "episode" and h.kind == "enter" and h.include_existing
    assert h.where(Row(review_status="pending", kind="recovery_takeover"))
    assert not h.where(Row(review_status="not_reviewed", kind="baseline"))
    assert not h.where(Row(review_status="accepted", kind="clean_teleop"))


# --------------------------------------------------------------------------- live Claude

@pytest.mark.skipif(os.environ.get("RDF_LIVE_CLAUDE") != "1", reason="set RDF_LIVE_CLAUDE=1 to call the real Claude API")
def test_live_claude_fixture_set(fixtures, cfg):
    judge = rv.ClaudeJudge(cfg)
    out = {n: rv.review_episode(f.episode, TICKET, config=cfg, judge=judge) for n, f in fixtures.items()}
    for n, d in out.items():
        print(f"{n:22s} accepted={d.accepted!s:5s} by={d.decided_by:13s} {d.reason}")
    assert [n for n, d in out.items() if d.accepted] == ["clean_demo"]
    assert out["off_task"].decided_by == "claude" and not out["off_task"].accepted
