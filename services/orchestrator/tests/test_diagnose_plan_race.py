"""Diagnose -> plan_data race (seen live): TDM-23 writes its failure_cluster rows one by one and the
``diagnose.clusters`` event last, and the flywheel used to leave ``diagnose`` at the first row, so the
planner (TDM-24) planned on one cluster of three ("the only clustered failure is missed grasp").

The flywheel now waits for the completion event when a ``diagnose.started`` is on record, and the
planner takes its cluster list from the event. Writers without the TDM-23 handler (flywheel_sim, the
scripted driver) log no ``diagnose.started`` and still move on at the first row.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_llm_retry import World, diagnose_world, hd, timers  # noqa: E402,F401 (hd/timers are fixtures)
from test_tdm23_diagnose import GOOD, FakeClusterer  # noqa: E402

from rdf_orchestrator import diagnose as dx  # noqa: E402
from rdf_orchestrator import flywheel as fw  # noqa: E402
from rdf_orchestrator import planner  # noqa: E402

TID = 7


class WatchedWorld(World):
    """After every reducer call, record what the flywheel would decide for the ticket right then."""

    def __init__(self, **tables):
        super().__init__(**tables)
        self.decisions: list[tuple[str, str | None, str | None]] = []

    def call_reducer(self, name, *args):
        super().call_reducer(name, *args)
        if name == "record_failure_cluster":
            row = max(self.rows("failure_cluster"), key=lambda r: r["id"])
            row["created_at"] = time.time_ns() // 1000
        if name in ("record_failure_cluster", "append_event"):
            d = fw.decide(fw.TicketView.from_bus(self, TID))
            self.decisions.append((name, args[1], d.to))  # args[1]: event type / cluster label


def _watched_world() -> WatchedWorld:
    base = diagnose_world()
    return WatchedWorld(**{t: list(rows.values()) for t, rows in base.t.items()})


def test_no_plan_data_until_the_diagnosis_event(hd, monkeypatch):  # noqa: F811
    w = _watched_world()
    monkeypatch.setattr(hd, "make_stdb", lambda: w)
    monkeypatch.setattr(hd, "make_clusterer", lambda: FakeClusterer(GOOD))
    assert fw.decide(fw.TicketView.from_bus(w, TID)).to is None

    hd._diagnose(w, w.get("eval_run", 3), "live")

    kinds = [(n, t) for n, t, _ in w.decisions]
    assert kinds == [("append_event", dx.EVENT_STARTED), ("record_failure_cluster", "drops mid-grasp"),
                     ("record_failure_cluster", "misses the cup"), ("record_failure_cluster", "never moves"),
                     ("append_event", dx.EVENT_CLUSTERS)]
    # Clusters arrive one at a time: the ticket stays in diagnose until the event lands.
    assert [to for _, _, to in w.decisions] == [None, None, None, None, "plan_data"]
    d = fw.decide(fw.TicketView.from_bus(w, TID))
    ids = sorted(r["id"] for r in w.rows("failure_cluster"))
    assert d.to == "plan_data" and sorted(d.evidence["failure_cluster_ids"]) == ids and len(ids) == 3
    assert "drops mid-grasp x3" in d.reason and "never moves x1" in d.reason

    # The planner sees all three clusters, from the event payload.
    inp = planner.gather_inputs(w.rows, w.get("ticket", TID))
    assert sorted((c.label, c.count) for c in inp.clusters) == [("drops mid-grasp", 3), ("misses the cup", 3),
                                                                ("never moves", 1)]


def test_waiting_reason_while_rows_trickle_in():
    v = _view(clusters=[_row(11, "missed grasp", 3)], events=[_started()])
    d = fw.decide(v)
    assert d.to is None and "waiting for diagnose.clusters" in d.reason


def test_fallback_rows_without_a_diagnose_handler_move_on():
    # flywheel_sim / scripted_flywheel: rows only, no diagnose.started -> as before, at the first row.
    d = fw.decide(_view(clusters=[_row(11, "SIMULATED: misses the cup on approach", 7)]))
    assert d.to == "plan_data" and d.evidence["failure_cluster_ids"] == [11]


def test_fallback_started_but_event_never_lands_moves_on_after_settle():
    now = 1_000_000_000_000
    rows = [_row(11, "missed grasp", 3, created_at=now - 5_000_000)]
    assert fw.decide(_view(clusters=rows, events=[_started()], now_us=now)).to is None  # 5 s: still writing
    old = [_row(11, "missed grasp", 3, created_at=now - int((fw.DIAGNOSE_SETTLE_S + 1) * 1e6))]
    d = fw.decide(_view(clusters=old, events=[_started()], now_us=now))
    assert d.to == "plan_data" and d.evidence["diagnose_incomplete"] is True and "never landed" in d.reason


def test_started_for_an_older_iteration_does_not_hold_this_one():
    v = _view(clusters=[_row(11, "missed grasp", 3, iteration=1)], events=[_started(iteration=0, eval_run_id=2)],
              ticket={"iteration": 1})
    v.eval_runs = [{"id": 5, "kind": "candidate", "status": "done", "successes": 4, "trials": 10}]
    assert fw.decide(v).to == "plan_data"


def test_no_failures_event_completes_the_diagnosis():
    v = _view(events=[_started(), {"type": "diagnose.no_failures", "ticket_id": TID, "iteration": 0,
                                   "eval_run_id": 3, "event_id": 2}])
    d = fw.decide(v)
    assert d.to == "plan_data" and "no failures" in d.reason


def test_planner_takes_the_cluster_set_from_the_event():
    # A stale row on the same iteration (an earlier, superseded diagnosis) is not in the event: ignored.
    diag = {"ticket_id": TID, "iteration": 0, "eval_run_id": 3, "reasoning": "three modes", "clusters": [
        {"failure_cluster_id": 21, "label": "missed grasp", "count": 3, "example_episode_ids": [41]},
        {"failure_cluster_id": 22, "label": "stalls before placing", "count": 3, "example_episode_ids": [42]},
        {"failure_cluster_id": None, "label": "collides with rim", "count": 2, "example_episode_ids": [43]}]}
    rows = {
        "ticket": [{"id": TID, "iteration": 0, "status": "plan_data"}],
        "failure_cluster": [_row(9, "old label", 5), _row(21, "missed grasp", 3), _row(22, "stalls before placing", 3),
                            _row(23, "collides with rim", 2)],
        "event_log": [{"id": 1, "type": "diagnose.clusters", "payload_json": json.dumps(diag)}],
    }
    inp = planner.gather_inputs(lambda t: rows.get(t, []), rows["ticket"][0])
    assert [(c.id, c.label, c.count) for c in inp.clusters] == [
        (21, "missed grasp", 3), (22, "stalls before placing", 3), (23, "collides with rim", 2)]


# --------------------------------------------------------------------------- helpers

def _row(cid, label, count, *, iteration=0, created_at=1):
    return {"id": cid, "ticket_id": TID, "iteration": iteration, "label": label, "count": count,
            "example_episode_ids": [], "created_at": created_at}


def _started(iteration=0, eval_run_id=3):
    return {"type": dx.EVENT_STARTED, "ticket_id": TID, "iteration": iteration, "eval_run_id": eval_run_id,
            "attempt": 1, "event_id": 1}


def _view(*, clusters=(), events=(), now_us=None, ticket=None) -> fw.TicketView:
    t = {"id": TID, "robot_id": 1, "status": "diagnose", "iteration": 0, "budget_cents": 5000, "spent_cents": 0,
         "target_success": 0.8, **(ticket or {})}
    v = fw.TicketView(ticket=t, settings={"max_iterations": 3, "success_threshold": 0.8}, clusters=list(clusters),
                      eval_runs=[{"id": 3, "kind": "baseline", "status": "done", "successes": 2, "trials": 10}])
    v.diagnose_events = list(events)
    v.now_us = now_us
    return v
