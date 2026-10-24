"""Bounded, restart-safe retries for the Claude-backed handlers (llm_retry.py).

A fake Claude refuses N times. Every handler must then: log one ``<area>.error`` per attempt, retry
after a backoff (a refusal with a rephrased / reduced prompt), stop after max_attempts with one
``<area>.failed``, and count attempts from event_log so a restart carries on. diagnose / planner
failures make the flywheel fail the ticket; review / LLM QA failures never write a rejection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from rdf_orchestrator import diagnose as dx  # noqa: E402
from rdf_orchestrator import flywheel as fw  # noqa: E402
from rdf_orchestrator import llm_retry  # noqa: E402
from rdf_orchestrator.stdb import Row  # noqa: E402

REFUSED = "model refused (category=cyber)"
POLICY = llm_retry.RetryPolicy(max_attempts=3, base_s=5.0, factor=3.0, max_s=600.0)


# --------------------------------------------------------------------------- a tiny in-memory "database + mirror"

class World:
    """Bus mirror and SpacetimeDB in one: reducer writes show up in rows() at once (``lag`` hides
    event_log writes, like a mirror that has not caught up)."""

    KEYS = {"run_settings": "ticket_id"}

    def __init__(self, **tables):
        self.t: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.lag = False
        self._hidden: list[Row] = []
        for name, rows in tables.items():
            for r in rows:
                self.put(name, r)

    def put(self, table, row):
        row = Row(row)
        if table == "event_log" and "id" not in row:
            row["id"] = 1 + max([0] + [int(r["id"]) for r in self.t.get("event_log", {}).values()]
                                + [int(r["id"]) for r in self._hidden])
        self.t.setdefault(table, {})[row.get(self.KEYS.get(table, "id"))] = row
        return row

    # bus side
    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def call_reducer_threadsafe(self, reducer, args, timeout=10.0):
        self.call_reducer(reducer, *args)
        return 1

    # stdb side
    def call_reducer(self, name, *args):
        self.calls.append((name, args))
        if name == "append_event":
            row = {"actor": args[0], "type": args[1], "payload_json": args[2]}
            if self.lag:
                row["id"] = 1000 + len(self._hidden)
                self._hidden.append(Row(row))
            else:
                self.put("event_log", row)
        elif name == "record_failure_cluster":
            tid, label, count, ex = args
            cid = 100 + len(self.rows("failure_cluster"))
            self.put("failure_cluster", {"id": cid, "ticket_id": tid, "iteration": self.get("ticket", tid)["iteration"],
                                         "label": label, "count": count, "example_episode_ids": json.loads(ex)})
        elif name == "record_data_plan":
            tid, dtype, amount, reasoning, evidence = args
            self.put("data_plan", {"id": 200 + len(self.rows("data_plan")), "ticket_id": tid,
                                   "iteration": self.get("ticket", tid)["iteration"], "data_type": dtype,
                                   "amount": amount, "status": "proposed", "reasoning": reasoning})
        elif name == "submit_review":
            eid, accepted = args[0], args[1]
            self.put("review", {"id": 300 + len(self.rows("review")), "episode_id": eid, "accepted": accepted})
            self.get("episode", eid)["review_status"] = "accepted" if accepted else "rejected"

    def sql(self, q):
        assert "failure_cluster" in q, q
        tid = int(q.rsplit("=", 1)[1])
        return [dict(r) for r in self.rows("failure_cluster") if r["ticket_id"] == tid]

    def close(self):
        pass

    def events(self, type_):
        return [json.loads(r["payload_json"]) for r in sorted(self.rows("event_log"), key=lambda r: r["id"])
                if r["type"] == type_]

    def reducers(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def timers(monkeypatch):
    """Scheduled retries are collected, not run: the test fires them (fn) and checks the delays."""
    out: list[tuple[float, object]] = []
    monkeypatch.setattr(llm_retry, "schedule", lambda delay, fn, name: out.append((delay, fn)))
    return out


def fire(timers):
    delay, fn = timers.pop(0)
    try:
        fn()
    except Exception:  # noqa: BLE001 - a failed retry raises like the bus handler would
        pass
    return delay


# --------------------------------------------------------------------------- the helper itself

def test_classify_refusal_transient_and_other():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    import anthropic

    assert llm_retry.classify(dx.DiagnoseError(REFUSED)) == "refusal"
    assert llm_retry.classify(RuntimeError("Claude declined to plan (refusal, category=None)")) == "refusal"
    api = anthropic.APIConnectionError(request=req)
    assert llm_retry.classify(api) == "transient"
    wrapped = RuntimeError("Claude API error: overloaded")
    wrapped.__cause__ = anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=req), body=None)
    assert llm_retry.classify(wrapped) == "transient"
    assert llm_retry.classify(ConnectionRefusedError("[Errno 61] Connection refused")) == "transient"
    assert llm_retry.classify(dx.DiagnoseError("structured output did not validate")) == "error"


def test_backoff_and_counts_with_reset():
    assert [POLICY.delay_s(n) for n in (1, 2, 3)] == [5.0, 15.0, 45.0]
    assert llm_retry.RetryPolicy(5, 100, 10, 600).delay_s(3) == 600
    rows = [Row(id=i, type=t, payload_json=json.dumps(p)) for i, (t, p) in enumerate([
        ("review.error", {"episode_id": 1, "kind": "refusal"}), ("review.error", {"episode_id": 2}),
        ("review.failed", {"episode_id": 1}), ("review.retry_requested", {"episode_id": 1}),
        ("review.error", {"episode_id": 1, "kind": "transient"})], start=1)]
    one = lambda p: p.get("episode_id") == 1  # noqa: E731
    assert [p["kind"] for p in llm_retry.prior_failures(rows, "review.error", one, "review.retry_requested")] == ["transient"]
    assert len(llm_retry.prior_failures(rows, "review.error", one)) == 2
    assert llm_retry.find_event(rows, "review.failed", one, "review.retry_requested") is None
    assert llm_retry.find_event(rows, "review.failed", one) == {"episode_id": 1}


def test_with_preface_does_not_touch_the_original():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "facts"}]}]
    out = llm_retry.with_preface(msgs)
    assert out[0]["content"][0]["text"] == llm_retry.REFUSAL_PREFACE and out[0]["content"][1]["text"] == "facts"
    assert len(msgs[0]["content"]) == 1
    assert llm_retry.with_preface([{"role": "user", "content": "x"}])[0]["content"].endswith("\n\nx")


# --------------------------------------------------------------------------- diagnose (TDM-23)

class RefusingClusterer:
    """Refuses ``n`` times (or raises ``exc``), then clusters like the TDM-23 fixture expects."""

    model = "fake"

    def __init__(self, n, exc=None):
        from test_tdm23_diagnose import GOOD

        self.n, self.exc, self.good, self.messages = n, exc, GOOD, []

    def cluster(self, messages):
        self.messages.append(messages)
        if len(self.messages) <= self.n:
            raise self.exc or dx.DiagnoseError(REFUSED)
        return self.good


class Frames:
    def frames(self, episode, n):
        return [(f"f{i}", b"\xff\xd8fake") for i in range(n)]


def diagnose_world():
    from test_tdm23_diagnose import load_fixture

    inputs, _ = load_fixture()
    return World(ticket=[dict(inputs.ticket, status="diagnose", budget_cents=5000, spent_cents=0)],
                 eval_run=[inputs.eval_run], eval_trial=inputs.trials, episode=list(inputs.episodes.values()),
                 event_log=[])


@pytest.fixture
def hd(monkeypatch):
    import rdf_orchestrator.handlers.diagnose as hd

    monkeypatch.setattr(hd, "RETRY", POLICY)
    monkeypatch.setattr(hd, "make_frames", lambda: Frames())
    hd._claimed.clear()
    hd._attempts.clear()
    yield hd
    hd._claimed.clear()  # don't leak claims into other test modules
    hd._attempts.clear()


def _images(messages):
    return sum(1 for b in messages[0]["content"] if b.get("type") == "image")


def test_diagnose_refusal_is_retried_with_a_reduced_prompt(hd, monkeypatch, timers):
    w, fc = diagnose_world(), RefusingClusterer(1)
    monkeypatch.setattr(hd, "make_stdb", lambda: w)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc)
    run = w.get("eval_run", 3)
    with pytest.raises(dx.DiagnoseError):
        hd._diagnose(w, run, "live")
    (err,) = w.events(dx.EVENT_ERROR)
    assert (err["attempt"], err["kind"], err["max_attempts"], err["retry_in_s"]) == (1, "refusal", 3, 5.0)
    assert [d for d, _ in timers] == [5.0]
    hd._diagnose(w, run, "live")  # a resync while the retry is pending: claimed, no second call
    assert len(fc.messages) == 1
    fire(timers)
    assert len(fc.messages) == 2 and _images(fc.messages[0]) > 0 and _images(fc.messages[1]) == 0
    assert fc.messages[1][0]["content"][0]["text"] == llm_retry.REFUSAL_PREFACE
    (done,) = w.events(dx.EVENT_CLUSTERS)
    assert done["prompt"] == "reduced" and done["media"]["frames_sent"] == 0 and len(done["clusters"]) == 3
    assert not w.events(dx.EVENT_FAILED) and not timers
    d = fw.decide(fw.TicketView.from_bus(w, 7))
    assert d.to == "plan_data"


def test_diagnose_gives_up_after_n_and_the_flywheel_fails_the_ticket(hd, monkeypatch, timers):
    w, fc = diagnose_world(), RefusingClusterer(99)
    monkeypatch.setattr(hd, "make_stdb", lambda: w)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc)
    assert fw.decide(fw.TicketView.from_bus(w, 7)).to is None  # waiting for clusters
    with pytest.raises(dx.DiagnoseError):
        hd._diagnose(w, w.get("eval_run", 3), "live")
    delays = [fire(timers), fire(timers)]
    assert delays == [5.0, 15.0] and not timers and len(fc.messages) == 3
    errs = w.events(dx.EVENT_ERROR)
    assert [e["attempt"] for e in errs] == [1, 2, 3] and errs[-1]["retry_in_s"] is None
    (failed,) = w.events(dx.EVENT_FAILED)
    assert failed["eval_run_id"] == 3 and failed["attempts"] == 3 and failed["kinds"] == ["refusal"] * 3
    assert REFUSED in failed["last_error"] and failed["iteration"] == 0
    assert "record_failure_cluster" not in w.reducers()
    d = fw.decide(fw.TicketView.from_bus(w, 7))
    assert d.to == "failed" and d.evidence["stop"] == "step_failures" and "diagnose failed 3 times" in d.reason
    hd._claimed.clear()
    hd._diagnose(w, w.get("eval_run", 3), "snapshot")  # a restart does not try again
    assert len(fc.messages) == 3


def test_diagnose_restart_continues_the_count_from_event_log(hd, monkeypatch, timers):
    w, fc = diagnose_world(), RefusingClusterer(99, exc=dx.DiagnoseError("model output hit max_tokens"))
    monkeypatch.setattr(hd, "make_stdb", lambda: w)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc)
    for n in (1, 2):  # a previous process failed twice, then died
        w.call_reducer("append_event", "orchestrator.diagnose", dx.EVENT_ERROR,
                       json.dumps({"ticket_id": 7, "eval_run_id": 3, "attempt": n, "kind": "transient", "error": "x"}))
    with pytest.raises(dx.DiagnoseError):
        hd._diagnose(w, w.get("eval_run", 3), "snapshot")
    assert len(fc.messages) == 1 and not timers  # attempt 3 of 3: terminal
    assert w.events(dx.EVENT_FAILED)[0]["kinds"] == ["transient", "transient", "error"]
    # Exhausted with no diagnose.failed (crash right after the 3rd error): log it, no Claude call.
    w2, fc2 = diagnose_world(), RefusingClusterer(0)
    monkeypatch.setattr(hd, "make_stdb", lambda: w2)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc2)
    for n in (1, 2, 3):
        w2.call_reducer("append_event", "orchestrator.diagnose", dx.EVENT_ERROR,
                        json.dumps({"ticket_id": 7, "eval_run_id": 3, "attempt": n, "error": "x"}))
    hd._claimed.clear()
    hd._attempts.clear()
    hd._diagnose(w2, w2.get("eval_run", 3), "snapshot")
    assert not fc2.messages and len(w2.events(dx.EVENT_FAILED)) == 1


def test_diagnose_transient_error_keeps_the_full_prompt_and_survives_mirror_lag(hd, monkeypatch, timers):
    import anthropic

    w = diagnose_world()
    fc = RefusingClusterer(1, exc=anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")))
    monkeypatch.setattr(hd, "make_stdb", lambda: w)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc)
    w.lag = True  # our own diagnose.error has not reached the mirror yet: the in-process count covers it
    with pytest.raises(anthropic.APIConnectionError):
        hd._diagnose(w, w.get("eval_run", 3), "live")
    assert hd.failures(w, 3) == ["transient"]
    w.lag = False
    fire(timers)
    assert _images(fc.messages[1]) > 0 and w.events(dx.EVENT_CLUSTERS)[0]["prompt"] == "full"


# --------------------------------------------------------------------------- planner (TDM-24)

class RefusingClaude:
    """anthropic.Anthropic stand-in: refuses ``n`` times, then answers with a valid plan."""

    def __init__(self, n, plan):
        self.n, self.plan, self.calls = n, plan, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.n:
            return SimpleNamespace(id="m", model="claude-opus-5-5", stop_reason="refusal", content=[],
                                   stop_details=SimpleNamespace(category="cyber"), usage=None)
        return SimpleNamespace(id="m", model="claude-opus-5-5", stop_reason="end_turn", usage=None,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.plan))])


@pytest.fixture
def hp(monkeypatch):
    from rdf_orchestrator.handlers import planner as hp

    monkeypatch.setattr(hp, "RETRY", POLICY)
    hp._claims.clear()
    hp._attempts.clear()
    yield hp
    hp._claims.clear()
    hp._attempts.clear()
    hp.set_client_factory(__import__("rdf_orchestrator.planner", fromlist=["x"]).anthropic_client)


def planner_world():
    from test_tdm24_planner import DROPS_MID_GRASP, tables

    tbl = tables(DROPS_MID_GRASP, successes=3)
    return World(**{k: v for k, v in tbl.items() if k != "contributor"})


def test_planner_refuses_n_times_then_planner_failed_fails_the_ticket(hp, timers):
    from test_tdm24_planner import answer

    w, fake = planner_world(), RefusingClaude(99, answer("recovery_takeover", 5, '"drops mid-grasp"'))
    hp.set_client_factory(lambda: fake)
    with pytest.raises(Exception, match="refusal"):
        hp._plan(w, w.get("ticket", 7), "live")
    fire(timers)
    fire(timers)
    assert not timers and len(fake.calls) == 3
    errs = w.events("planner.error")
    assert [(e["attempt"], e["kind"]) for e in errs] == [(1, "refusal"), (2, "refusal"), (3, "refusal")]
    (failed,) = w.events("planner.failed")
    assert failed["iteration"] == 0 and failed["attempts"] == 3 and "record_data_plan" not in w.reducers()
    d = fw.decide(fw.TicketView.from_bus(w, 7))
    assert d.to == "failed" and d.evidence["stop"] == "step_failures" and "planner failed 3 times" in d.reason
    hp._claims.clear()
    hp._plan(w, w.get("ticket", 7), "snapshot")  # restart: given up, no new call
    assert len(fake.calls) == 3


def test_planner_refusal_then_plan_with_reduced_prompt(hp, timers):
    from test_tdm24_planner import answer

    w, fake = planner_world(), RefusingClaude(1, answer("recovery_takeover", 5, 'Takeovers for "drops mid-grasp".'))
    hp.set_client_factory(lambda: fake)
    with pytest.raises(Exception):
        hp._plan(w, w.get("ticket", 7), "live")
    fire(timers)
    first, second = (c["messages"][0]["content"] for c in fake.calls)
    assert "fixture failure" in first and "fixture failure" not in second  # free-text notes dropped
    assert second.startswith(llm_retry.REFUSAL_PREFACE)
    (plan,) = w.rows("data_plan")
    assert plan["data_type"] == "recovery_takeover" and not w.events("planner.failed")
    assert fw.decide(fw.TicketView.from_bus(w, 7)).to is None  # waiting for approve_spend, not failed


# --------------------------------------------------------------------------- reviewer (TDM-25)

class RefusingJudge:
    model = "fake"
    last_usage = None

    def __init__(self, n, err):
        self.n, self.err, self.calls = n, err, []

    def judge(self, messages):
        self.calls.append(messages)
        if len(self.calls) <= self.n:
            raise self.err
        return "ok"


def review_world():
    return World(ticket=[{"id": 1, "status": "review"}], review=[], event_log=[],
                 episode=[{"id": 901, "ticket_id": 1, "kind": "clean_teleop", "review_status": "pending"}])


@pytest.fixture
def hr(monkeypatch):
    from rdf_orchestrator import reviewer as rv
    from rdf_orchestrator.handlers import reviewer as hr

    def fake_review(ep, ticket, *, config, judge):  # the deterministic part is TDM-25's; only the judge matters here
        judge.judge([{"role": "user", "content": [{"type": "text", "text": f"Episode {ep['id']}: ..."}]}])
        return rv.ReviewDecision(int(ep["id"]), True, "Accepted: all checks passed.", "all_passed", [], {})

    monkeypatch.setattr(hr, "RETRY", POLICY)
    monkeypatch.setattr(hr, "make_config", lambda: SimpleNamespace(claude_enabled=True))
    monkeypatch.setattr(hr.rv, "review_episode", fake_review)
    hr._claimed.clear()
    hr._attempts.clear()
    yield hr
    hr._claimed.clear()
    hr._attempts.clear()


def test_review_refusals_never_reject_and_a_human_can_retry(hr, monkeypatch, timers):
    from rdf_orchestrator import reviewer as rv
    from rdf_orchestrator.bus import Event

    w, judge = review_world(), RefusingJudge(3, rv.ReviewError("Claude refused the review (category=cyber)"))
    monkeypatch.setattr(hr, "make_stdb", lambda: w)
    monkeypatch.setattr(hr, "make_judge", lambda cfg: judge)
    with pytest.raises(rv.ReviewError):
        hr.review_episode(Event("episode", "insert", w.get("episode", 901), bus=w))
    fire(timers)
    fire(timers)
    assert not timers and len(judge.calls) == 3
    assert judge.calls[1][0]["content"][0]["text"] == llm_retry.REFUSAL_PREFACE  # rephrased after the refusal
    assert [e["attempt"] for e in w.events(rv.EVENT_ERROR)] == [1, 2, 3]
    (failed,) = w.events(rv.EVENT_FAILED)
    assert failed["episode_id"] == 901 and "never a rejection" in failed["effect"]
    assert "submit_review" not in w.reducers() and "record_qa_result" not in w.reducers()
    assert w.get("episode", 901)["review_status"] == "pending"
    hr._claimed.clear()
    hr.review_episode(Event("episode", "insert", w.get("episode", 901), bus=w, source="snapshot"))
    assert len(judge.calls) == 3  # restart: still given up
    req = w.put("event_log", {"actor": "human", "type": rv.EVENT_RETRY_REQUESTED, "payload_json": '{"episode_id": 901}'})
    hr.review_retry_requested(Event("event_log", "insert", req, bus=w))
    assert len(judge.calls) == 4 and w.reducers().count("submit_review") == 1
    assert w.get("episode", 901)["review_status"] == "accepted"


# --------------------------------------------------------------------------- LLM QA (TDM-93)

def test_llm_qa_refusals_never_reject_and_failed_calls_count_from_event_log(monkeypatch, timers):
    from rdf_orchestrator import llm_qa as lq
    from rdf_orchestrator.bus import Event
    from rdf_orchestrator.handlers import llm_qa as hq

    w = World(ticket=[{"id": 1, "status": "review"}], review=[], event_log=[],
              episode=[{"id": 77, "ticket_id": 1, "kind": "purchased", "review_status": "pending", "dataset_offer_id": 5}])
    # a previous process already burnt one call on another episode of the same offer
    w.call_reducer("append_event", "orchestrator.llm_qa", lq.EVENT_ERROR,
                   json.dumps({"stage": "llm_qa", "episode_id": 76, "dataset_offer_id": 5, "error": "x"}))
    judge = RefusingJudge(99, lq.LlmQaError("Claude refused the QA call (category=cyber)"))
    seen_extra: list[int] = []

    def fake_run(stdb, ep, ticket, *, cfg, judge, extra_calls, log_errors):
        assert log_errors is False
        seen_extra.append(extra_calls)
        judge.judge([{"role": "user", "content": [{"type": "text", "text": "Purchased episode 77"}]}])
        raise AssertionError("unreachable: the judge refuses")

    monkeypatch.setattr(hq, "RETRY", POLICY)
    monkeypatch.setattr(hq, "make_stdb", lambda: w)
    monkeypatch.setattr(hq, "make_config", lambda: SimpleNamespace(summary_check="deterministic_qa"))
    monkeypatch.setattr(hq, "make_judge", lambda cfg: judge)
    monkeypatch.setattr(hq.lq, "run_after_deterministic", fake_run)
    hq._claimed.clear()
    hq._attempts.clear()
    gate = Row(id=1, episode_id=77, check_name="deterministic_qa", passed=True)
    with pytest.raises(lq.LlmQaError):
        hq.llm_qa_after_deterministic(Event("qa_result", "insert", gate, bus=w))
    fire(timers)
    fire(timers)
    assert not timers and len(judge.calls) == 3 and seen_extra == [1, 2, 3]
    assert judge.calls[2][0]["content"][0]["text"] == llm_retry.REFUSAL_PREFACE
    errs = [e for e in w.events(lq.EVENT_ERROR) if e["episode_id"] == 77]
    assert [(e["attempt"], e["kind"]) for e in errs] == [(1, "refusal"), (2, "refusal"), (3, "refusal")]
    (failed,) = w.events(lq.EVENT_FAILED)
    assert failed["episode_id"] == 77 and failed["dataset_offer_id"] == 5
    assert "submit_review" not in w.reducers() and w.get("episode", 77)["review_status"] == "pending"
    hq._claimed.clear()
    hq._attempts.clear()
