"""TDM-69 robot-side failure detector (pure; no arm, no network)."""
from __future__ import annotations

from rdf_robot_client.interventions import Failure, FailureDetector


def det(**kw):
    d = FailureDetector(**{"trial_timeout_s": 60, "no_progress_s": 10, "min_progress": 0.01, **kw})
    d.start(0.0)
    return d


def test_off_unless_enabled():
    assert FailureDetector.from_payload(None) is None
    assert FailureDetector.from_payload({"enabled": False}) is None
    d = FailureDetector.from_payload({"enabled": True, "trial_timeout_s": 30, "no_progress_s": 5})
    assert d.trial_timeout_s == 30 and d.no_progress_s == 5


def test_steady_progress_never_fires_until_timeout():
    d = det()
    t = 0.0
    while t < 59.9:
        t += 0.1
        assert d.tick(t, t / 60) is None
    f = d.tick(60.0, 1.0)
    assert f is not None and f.detector == "timeout"
    assert d.tick(61.0, 1.0) is None  # fires once


def test_stall_fires_no_progress():
    d = det()
    f = None
    t = 0.0
    while f is None and t < 30:
        t += 0.1
        f = d.tick(t, min(0.4, t / 10))
    assert f.detector == "no_progress" and 13.9 < f.at_s < 14.3


def test_success_check_and_payload_ref():
    d = det()
    assert d.success_check(True) is None
    f = d.success_check(False, "gripper empty", now_s=9.0)
    assert f.detector == "success_check" and f.at_s == 9.0
    p = f.payload(robot_id=1, ticket_id=2, command_id=33, trial_index=4, eval_run_id=5)
    assert p["ref"] == Failure.ref(1, 33, 4) == "iv:1:33:4" and p["detector"] == "success_check"
