"""Agent workbench events from the shared LLM client: agent.step / agent.thought, paced per agent."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from rdf_common import llm


class FakeApi:
    """An Anthropic-like client that answers with a thinking block and a JSON text block."""

    def __init__(self, answer: str, thinking: str = "", delay: float = 0.0) -> None:
        self.answer, self.thinking, self.delay = answer, thinking, delay
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        time.sleep(self.delay)
        content = []
        if self.thinking:
            content.append(SimpleNamespace(type="thinking", thinking=self.thinking))
        content.append(SimpleNamespace(type="text", text=self.answer))
        return SimpleNamespace(id="msg_1", model=kw["model"], stop_reason="end_turn", content=content,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))


@pytest.fixture
def seen(monkeypatch):
    monkeypatch.setattr(llm, "_pacer", llm._Pacer(gap=0.05))
    monkeypatch.setenv("RDF_AGENT_HEARTBEAT_S", "0.05")
    out: list[dict] = []
    llm.add_agent_sink(out.append)
    yield out
    llm.remove_agent_sink(out.append)


def drain(timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while llm._pacer.pending() and time.monotonic() < end:
        time.sleep(0.02)
    time.sleep(0.08)


SCHEMA = {"type": "object", "properties": {"reasoning": {"type": "string"}}, "required": ["reasoning"]}


def test_llm_call_emits_start_end_and_thoughts(seen):
    client = llm.LLM(api_client=FakeApi(
        '{"reasoning": "Six of ten failures are grasp slips at the bin edge. The rest time out. Buy takeovers."}',
        thinking="The failures cluster near the bin edge.", delay=0.2), backend="api")
    with llm.agent_context(ticket_id=7, station="diagnose", agent="clusterer"):
        client.complete_structured(step="diagnose", system="s", user_content="u", schema=SCHEMA)
    drain()
    types = [(r["type"], r.get("phase")) for r in seen]
    assert types[0] == ("agent.step", "llm_start")
    assert ("agent.step", "llm_end") in types
    assert ("agent.thought", "llm_wait") in types  # heartbeat while the call ran
    thoughts = [r["text"] for r in seen if r.get("phase") in ("thinking", "reasoning")]
    assert thoughts[0] == "The failures cluster near the bin edge."
    assert "Six of ten failures are grasp slips at the bin edge." in thoughts
    assert all(r["ticket_id"] == 7 and r["station"] == "diagnose" and r["agent"] == "clusterer" for r in seen)


def test_off_switch_and_no_ticket(seen, monkeypatch):
    assert llm.agent_event("step", "no ticket in context") is False
    monkeypatch.setenv("RDF_AGENT_THOUGHTS", "0")
    with llm.agent_context(ticket_id=3, station="baseline", agent="eval_runner"):
        assert llm.agent_event("step", "Trial 1/10") is False
        llm.LLM(api_client=FakeApi('{"reasoning": "x"}'), backend="api").complete_structured(
            step="planner", system="s", user_content="u", schema=SCHEMA)
    drain()
    assert seen == []


def test_pacing_keeps_order_and_spacing(monkeypatch):
    got: list[tuple[float, str]] = []
    llm.add_agent_sink(lambda r: got.append((time.monotonic(), r["text"])))
    monkeypatch.setattr(llm, "_pacer", llm._Pacer(gap=0.1))
    try:
        for i in range(4):
            llm.agent_event("step", f"step {i}", ticket_id=1, station="baseline", agent="eval_runner")
        llm.agent_event("step", "other agent", ticket_id=1, station="diagnose", agent="clusterer")
        drain()
    finally:
        llm._agent_sinks.clear()
    mine = [(t, x) for t, x in got if x.startswith("step")]
    assert [x for _, x in mine] == ["step 0", "step 1", "step 2", "step 3"]
    gaps = [b[0] - a[0] for a, b in zip(mine, mine[1:])]
    assert min(gaps) >= 0.09
    # another agent is not held up by the first one's queue
    assert got[1][1] == "other agent"


def test_queue_drops_oldest_thought_first(monkeypatch):
    p = llm._Pacer(gap=10, max_queue=2)
    out: list[str] = []
    p.push("k", {"type": "agent.step", "text": "a"}, lambda r: out.append(r["text"]))
    for t, x in (("agent.step", "b"), ("agent.thought", "c"), ("agent.step", "d")):
        p.push("k", {"type": t, "text": x}, lambda r: out.append(r["text"]))
    assert out == ["a"]
    assert [r["text"] for r in p._q["k"]] == ["b", "d"]
    for t in p._timer.values():
        t.cancel()
