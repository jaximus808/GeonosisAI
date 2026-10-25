"""AgentWorkbench: station narration rows (agent.step / agent.thought) and their event_log writer."""
from __future__ import annotations

import json
import time

import pytest

from rdf_common import llm
from rdf_orchestrator import agent_steps
from rdf_orchestrator.admin_llm_api import CallEventWriter
from rdf_orchestrator.bus import Event
from rdf_orchestrator.handlers import agent_narration as an


class FakeBus:
    def __init__(self, **tables: dict) -> None:
        self.t = tables

    def get(self, table: str, key):
        return self.t.get(table, {}).get(key)


@pytest.fixture
def seen(monkeypatch):
    monkeypatch.setattr(llm, "_pacer", llm._Pacer(gap=0.0))
    monkeypatch.delenv("RDF_AGENT_THOUGHTS", raising=False)
    out: list[dict] = []
    llm.add_agent_sink(out.append)
    yield out
    llm.remove_agent_sink(out.append)


def test_trial_and_run_lines(seen):
    bus = FakeBus(eval_run={5: {"id": 5, "ticket_id": 9, "kind": "baseline", "trials": 10, "robot_id": 2,
                                "successes": 3, "status": "running"}},
                  robot={2: {"id": 2, "name": "sim-01"}})
    an.narrate_run_start(Event("eval_run", "update", bus.t["eval_run"][5], source="live", bus=bus))
    an.narrate_trial(Event("eval_trial", "insert", {"eval_run_id": 5, "trial_index": 3, "success": False,
                                                    "failure_note": "[SIM] Auto outcome from sim state: cube dropped"},
                           source="live", bus=bus))
    an.narrate_trial(Event("eval_trial", "insert", {"eval_run_id": 5, "trial_index": 4, "success": True},
                           source="snapshot", bus=bus))  # history is never replayed
    an.narrate_run_done(Event("eval_run", "update", {**bus.t["eval_run"][5], "status": "done"}, source="live", bus=bus))
    assert [r["text"] for r in seen] == ["Running 10 trials of the baseline on sim-01", "Trial 4/10: fail · cube dropped",
                                         "Finished: 3/10 passed (30%)"]
    assert all(r["ticket_id"] == 9 and r["station"] == "baseline" and r["agent"] == "eval_runner" for r in seen)
    assert seen[1]["is_error"] is True and seen[2]["decision"] is True


def test_training_status_and_progress(seen):
    row = {"id": 3, "ticket_id": 9, "status": "running", "modal_call_id": "fc-1", "hyperparams_json": '{"num_train_steps": 2000}',
           "metrics_json": "{}"}
    an.narrate_training(Event("training_run", "update", row, old={**row, "status": "queued"}, source="live"))
    an.narrate_training(Event("training_run", "update", {**row, "metrics_json": '{"step": 1200, "loss": 0.0421}'},
                              old=row, source="live"))
    assert [r["text"] for r in seen] == ["Training on Modal", "Step 1,200/2,000 · loss 0.042"]
    assert {r["station"] for r in seen} == {"train"} and {r["agent"] for r in seen} == {"trainer"}


def test_work_context_tags_steps(seen):
    with agent_steps.work(4, "plan_data"):
        agent_steps.step("Reading 2 failure clusters")
        agent_steps.step("Proposing 8 recovery takeovers", decision=True)
    agent_steps.step("outside any context")  # no ticket: dropped
    assert [(r["station"], r["agent"], r["text"]) for r in seen] == [
        ("plan_data", "planner", "Reading 2 failure clusters"), ("plan_data", "planner", "Proposing 8 recovery takeovers")]


def test_writer_uses_agent_type_and_actor():
    calls: list[tuple] = []

    class Stdb:
        def call_reducer(self, *a):
            calls.append(a)

    w = CallEventWriter(Stdb())
    w({"type": "agent.step", "ticket_id": 1, "station": "diagnose", "agent": "clusterer", "text": "x"})
    w({"step": "diagnose", "ok": True})
    end = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    w.stop()
    assert calls[0][:3] == ("append_event", "orchestrator:agents", "agent.step")
    assert json.loads(calls[0][3]) == {"ticket_id": 1, "station": "diagnose", "agent": "clusterer", "text": "x"}
    assert calls[1][:3] == ("append_event", "orchestrator:llm", "llm.call")
