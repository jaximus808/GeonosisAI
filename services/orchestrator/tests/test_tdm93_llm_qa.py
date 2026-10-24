"""TDM-93 LLM QA on the labelled synthetic PURCHASED fixtures (tests/tdm93_fixtures.py, NOT real robot data).

Offline tests use a scripted judge and an in-memory SpacetimeDB stand-in. They test the plumbing: the
gate on TDM-92's deterministic_qa row, the 6 frames (exo + wrist, start/middle/end) and the action
summary in one call, the verdict mapping, the qa_result rows and submit_review, outage -> qa.error with
no review, the per-offer call cap, and the approve-everything guard. The live test calls the real
Claude API on the same fixtures:  RDF_LIVE_CLAUDE=1 uv run pytest tests/test_tdm93_llm_qa.py -k live
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import tdm93_fixtures as f93  # noqa: E402

from rdf_orchestrator import llm_qa as lq  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg needed for the video fixtures")

TICKET = {"id": 1, "task_description": f93.TASK, "status": "review"}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    return {f.name: f for f in f93.build_fixture_set(tmp_path_factory.mktemp("tdm93"))}


@pytest.fixture(scope="module")
def cfg():
    return lq.load_config()


class ScriptedJudge:
    """Verdicts keyed by episode id (parsed from the summary text), like the TDM-25 ScriptedJudge."""

    model = "scripted"

    def __init__(self, verdicts: dict[int, str] | None = None, fail: bool = False):
        self.verdicts, self.fail, self.calls, self.last_usage = verdicts or {}, fail, [], None

    def judge(self, messages):
        content = messages[0]["content"]
        self.calls.append(content)
        if self.fail:
            raise lq.LlmQaError("Claude API error: APIConnectionError: scripted outage")
        eid = int(re.search(r"Purchased episode (\d+)", content[0]["text"]).group(1))
        kind = self.verdicts.get(eid, "good")
        if kind == "off_task":
            return lq.LlmJudgement(observations="two blocks, no cup", scene_matches=False, on_task=False,
                                   mismatch="stacks a green block on a yellow block; there is no red cup or blue bin",
                                   task_succeeded="no", quality=1, issues=["different task"],
                                   reason="This episode shows block stacking, not putting a cup in a bin.")
        if kind == "failed":
            return lq.LlmJudgement(observations="gripper closes beside the cup", scene_matches=True, on_task=True,
                                   task_succeeded="no", quality=2, issues=["cup never lifted"],
                                   reason="The gripper closes beside the red cup, which stays on the table.")
        if kind == "low_quality":
            return lq.LlmJudgement(observations="cup placed", scene_matches=True, on_task=True,
                                   task_succeeded="yes", quality=2, reason="Succeeds but wanders a lot.")
        return lq.LlmJudgement(observations="cup lifted into the bin", scene_matches=True, on_task=True,
                               task_succeeded="yes", quality=5, reason="The red cup is grasped and placed in the blue bin.")


class FakeStdb:
    """The few SQL shapes llm_qa uses (SELECT * FROM t [WHERE col = v]) plus the reducers it calls."""

    def __init__(self, episodes=(), offer=None, ticket=TICKET):
        self.tables: dict[str, list[dict]] = {"episode": [dict(e) for e in episodes], "qa_result": [], "review": [],
                                              "dataset_offer": [offer] if offer else [], "ticket": [ticket],
                                              "run_settings": [{"ticket_id": 1, "hyperparams_json": json.dumps(
                                                  {"prompt": "put the red cup in the blue bin"})}],
                                              "event_log": []}
        self.calls: list[tuple] = []

    def sql(self, q: str):
        m = re.match(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (.+))?$", q.strip())
        assert m, q
        rows = self.tables[m.group(1)]
        if m.group(2):
            v = m.group(3).strip("'")
            rows = [r for r in rows if str(r.get(m.group(2))) == v]
        return [dict(r) for r in rows]

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name == "record_qa_result":
            eid, check, passed, detail = args
            self.tables["qa_result"].append({"id": len(self.tables["qa_result"]) + 1, "episode_id": eid,
                                             "check_name": check, "passed": passed, "detail_json": detail})
        elif name == "submit_review":
            eid, accepted, reason, metrics = args
            if any(r["episode_id"] == eid for r in self.tables["review"]):
                raise RuntimeError(f"episode {eid} already reviewed")
            self.tables["review"].append({"id": len(self.tables["review"]) + 1, "episode_id": eid,
                                          "accepted": accepted, "reason": reason, "metrics_json": metrics})
            for e in self.tables["episode"]:
                if e["id"] == eid:
                    e["review_status"] = "accepted" if accepted else "rejected"
        elif name == "append_event":
            self.tables["event_log"].append({"actor": args[0], "type": args[1], "payload": json.loads(args[2])})

    def gate(self, eid, passed=True, extra=()):
        for name, ok in extra:
            self.call_reducer("record_qa_result", eid, name, ok, "{}")
        self.call_reducer("record_qa_result", eid, "deterministic_qa", passed, json.dumps({"simulated": "tdm92"}))
        self.calls.clear()

    def events(self, type_):
        return [e["payload"] for e in self.tables["event_log"] if e["type"] == type_]

    def reducers(self):
        return [c[0] for c in self.calls]


OFFER = {"id": 1, "ticket_id": 1, "needed_count": 3, "status": "qa"}


# --------------------------------------------------------------------------- the gate

def test_gate_needs_tdm92_summary_passed(fixtures, cfg):
    ep = fixtures["cup_demo"].episode
    row = lambda i, n, p: {"id": i, "check_name": n, "passed": p}  # noqa: E731
    assert not lq.gate_status(ep, [], False, cfg).ready
    assert "waiting" in lq.gate_status(ep, [row(1, "jerk", True)], False, cfg).why
    assert not lq.gate_status(ep, [row(1, "deterministic_qa", False)], False, cfg).ready
    g = lq.gate_status(ep, [row(1, "duplicate", False), row(2, "deterministic_qa", True)], False, cfg)
    assert not g.ready and "duplicate" in g.why
    # latest row per check wins: a re-run that passes reopens the gate
    assert lq.gate_status(ep, [row(1, "duplicate", False), row(3, "duplicate", True),
                               row(4, "deterministic_qa", True)], False, cfg).ready
    assert not lq.gate_status(ep, [row(1, "deterministic_qa", True)], True, cfg).ready  # already reviewed
    assert not lq.gate_status({**ep, "kind": "clean_teleop"}, [row(1, "deterministic_qa", True)], False, cfg).ready


def test_gate_closed_means_no_claude_call(fixtures, cfg):
    ep = fixtures["cup_demo"].episode
    db, judge = FakeStdb([ep], OFFER), ScriptedJudge()
    assert lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=judge) is None  # no deterministic_qa yet
    db.gate(ep["id"], passed=False)
    assert lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=judge) is None
    assert judge.calls == [] and db.calls == []


# --------------------------------------------------------------------------- the call

def test_one_call_six_frames_exo_and_wrist_plus_action_summary(fixtures, cfg):
    ep = fixtures["cup_demo"].episode
    judge = ScriptedJudge()
    d = lq.judge_episode(ep, TICKET, cfg=cfg, judge=judge, prompt="put the red cup in the blue bin")
    assert len(judge.calls) == 1
    content = judge.calls[0]
    images = [c for c in content if c["type"] == "image"]
    labels = [c["text"] for c in content if c["type"] == "text" and c["text"].startswith("Frame:")]
    assert len(images) == 6
    assert sum("exo camera" in lbl for lbl in labels) == 3 and sum("wrist camera" in lbl for lbl in labels) == 3
    assert [lbl.split(", ")[-1] for lbl in labels] == ["start", "start", "middle", "middle", "end", "end"]
    head = content[0]["text"]
    assert f93.TASK in head and "Policy prompt" in head and "closes at [3.57]" in head and "travels" in head
    assert d.accepted and [c.name for c in d.checks] == ["llm_relevance", "llm_success"]
    assert d.metrics["cameras"] == ["exo", "wrist"] and d.metrics["claude"]["quality"] == 5


def test_verdicts_map_to_checks_and_reasons(fixtures, cfg):
    eps = {n: f.episode for n, f in fixtures.items()}
    judge = ScriptedJudge({eps["block_stacking"]["id"]: "off_task", eps["cup_missed"]["id"]: "failed"})
    out = {n: lq.judge_episode(e, TICKET, cfg=cfg, judge=judge) for n, e in eps.items()}
    assert out["cup_demo"].accepted
    off = out["block_stacking"]
    assert not off.accepted and "not the ticket's task" in off.reason and "green block" in off.reason
    assert {c.name: c.status for c in off.checks} == {"llm_relevance": "fail", "llm_success": "fail"}
    miss = out["cup_missed"]
    assert not miss.accepted and "not completed" in miss.reason
    assert {c.name: c.status for c in miss.checks} == {"llm_relevance": "pass", "llm_success": "fail"}


def test_quality_below_threshold_rejects(fixtures, cfg):
    ep = fixtures["cup_demo"].episode
    d = lq.judge_episode(ep, TICKET, cfg=cfg, judge=ScriptedJudge({ep["id"]: "low_quality"}))
    assert not d.accepted and "quality 2/5 is below 3" in d.reason


def test_run_writes_qa_rows_then_review_and_rate(fixtures, cfg):
    ep = fixtures["block_stacking"].episode
    db = FakeStdb([ep], OFFER)
    db.gate(ep["id"], extra=[("jerk", True), ("duplicate", True)])
    d = lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=ScriptedJudge({ep["id"]: "off_task"}))
    assert not d.accepted
    assert db.reducers() == ["record_qa_result", "record_qa_result", "submit_review", "append_event"]
    assert [(c[2], c[3]) for c in db.calls[:2]] == [("llm_relevance", False), ("llm_success", False)]
    assert db.calls[2][2] is False and "block" in db.calls[2][3]
    rate = db.events("qa.acceptance_rate")[0]
    assert rate["reviewed"] == 1 and rate["accepted"] == 0 and rate["suspect"] is False


# --------------------------------------------------------------------------- outages and cost

def test_outage_is_qa_error_not_rejection(fixtures, cfg):
    ep = fixtures["cup_demo"].episode
    db = FakeStdb([ep], OFFER)
    db.gate(ep["id"])
    with pytest.raises(lq.LlmQaError):
        lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=ScriptedJudge(fail=True))
    assert db.reducers() == ["append_event"]  # no qa_result, no submit_review
    err = db.events("qa.error")[0]
    assert err["stage"] == "llm_qa" and "scripted outage" in err["error"] and "pending" in err["effect"]
    assert db.tables["episode"][0]["review_status"] == "pending"
    # retry after the outage works
    d = lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=ScriptedJudge())
    assert d.accepted


def test_per_offer_call_cap(fixtures, cfg):
    assert lq.call_cap({"needed_count": 1}, cfg) == 4 and lq.call_cap({"needed_count": 5}, cfg) == 10
    ep = fixtures["cup_demo"].episode
    db = FakeStdb([ep], {**OFFER, "needed_count": 1})
    db.gate(ep["id"])
    judge = ScriptedJudge()
    assert lq.run_after_deterministic(db, ep, TICKET, cfg=cfg, judge=judge, extra_calls=4) is None
    assert judge.calls == [] and db.events("qa.llm_capped")[0]["cap"] == 4
    assert db.tables["episode"][0]["review_status"] == "pending"


def test_acceptance_guard_flags_approve_everything(cfg):
    db = FakeStdb([{"id": i, "dataset_offer_id": 1, "kind": "purchased"} for i in range(1, 7)])
    db.tables["review"] = [{"id": i, "episode_id": i, "accepted": True} for i in range(1, 7)]
    assert lq.acceptance_guard(db, 1, 1, cfg)["suspect"] is True
    db.tables["review"][0]["accepted"] = False  # 5/6 = 83%
    assert lq.acceptance_guard(db, 1, 1, cfg)["suspect"] is False


def test_handler_registered_on_gate_row():
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.handlers import llm_qa as h  # noqa: F401
    from rdf_orchestrator.handlers import reviewer as rh

    hs = [x for x in registry.handlers if x.name == "tdm93_llm_qa"]
    assert len(hs) == 1 and hs[0].table == "qa_result" and hs[0].include_existing
    assert "purchased" not in rh.HANDLER_KINDS  # TDM-25 reviewer leaves purchased episodes to dataset QA


@pytest.mark.skipif(not os.environ.get("RDF_LIVE_CLAUDE"), reason="set RDF_LIVE_CLAUDE=1 to call the real Claude API")
def test_live_claude_on_fixtures(fixtures, cfg):
    judge = lq.ClaudeLlmQaJudge(cfg)
    for f in fixtures.values():
        d = lq.judge_episode(f.episode, TICKET, cfg=cfg, judge=judge)
        print(f"\n{f.name}: accepted={d.accepted} reason={d.reason}")
        assert d.accepted == f.expect_accept, (f.name, d.reason)
        if f.expect_failed_check:
            assert any(c.name == f.expect_failed_check and c.status == "fail" for c in d.checks), (f.name, d.checks)
            assert any(w in d.reason.lower() for w in f.expect_reason), (f.name, d.reason)
