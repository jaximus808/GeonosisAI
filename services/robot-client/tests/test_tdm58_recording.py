"""TDM-58: recording messages (/control) -> TDM-48 recorder. SimArm + synthetic cameras, no network.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm58_recording.py -v
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from rdf_robot_client.recorder import LocalArmDriver, Recorder, check_local, sim_motion
from rdf_robot_client.tdm58_recording import RecordingControl, RecordingTarget, lookup_task


@pytest.fixture(scope="module")
def sim_rig():
    from rdf_robot_client.cameras import CameraRig

    rig = CameraRig.from_config(synthetic=True).start()
    assert rig.wait_ready(10)
    yield rig
    rig.stop()


@pytest.fixture
def setup(sim_rig):
    from rdf_robot_client.adapters.sim import SimArm

    root = Path(tempfile.mkdtemp(prefix="tdm-58-eps-"))
    arm = SimArm()
    arm.connect()
    holder: dict = {}
    # The "executor": reports what the recording says has authority (human during the demo /
    # takeover, policy otherwise), like TDM-52's loop would with rc.control_source().
    drv = LocalArmDriver(arm, sim_motion(arm.home_joints),
                         lambda t: (holder["rc"].control_source() if "rc" in holder else None) or "policy").start()
    rec = Recorder(drv, sim_rig, root=root)
    yield rec, holder, root
    drv.stop()
    arm.disconnect()
    shutil.rmtree(root, ignore_errors=True)


def _wait(rc: RecordingControl, key: str, seconds: float = 6.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if rc.state()["can"][key]:
            return
        time.sleep(0.05)
    raise AssertionError(f"can.{key} never became true: {rc.state()}")


def test_clean_demo_start_stop(setup):
    rec, holder, _ = setup
    hooks: list = []
    rc = RecordingControl(rec, RecordingTarget(7, 3, 2, "clean_teleop"),
                          on_record_start=lambda k: hooks.append(("start", k)),
                          on_record_end=lambda last: hooks.append(("end", last["status"])))
    holder["rc"] = rc
    assert rc.state()["phase"] == "idle" and rc.state()["can"]["start"]
    r = rc.handle({"t": "record_start", "req": 1})
    assert r["ok"] and r["req"] == 1 and r["recording"]["phase"] == "recording", r
    assert not rc.handle({"t": "record_start"})["ok"]  # one at a time
    assert not rc.handle({"t": "takeover"})["ok"]       # not a recovery task
    time.sleep(2.6)
    st = rc.state()
    assert st["frames"] > 30 and st["elapsed_ms"] >= 2500 and st["kind"] == "clean_teleop"
    bad = rc.handle({"t": "record_stop"})
    assert not bad["ok"] and "success" in bad["error"]
    r = rc.handle({"t": "record_stop", "success": True, "req": "s"})
    assert r["ok"], r
    last = r["recording"]["last"]
    assert last["status"] == "recorded" and last["kind"] == "clean_teleop" and last["success"] is True
    assert last["upload"]["state"] == "none"  # no uploader in this test
    assert hooks == [("start", "clean_teleop"), ("end", "recorded")]
    assert r["recording"]["phase"] == "idle" and r["recording"]["can"]["start"]
    rep = check_local(rec.last)
    assert rep.passed, rep.reason()
    assert rec.last.meta["task_id"] == 3 and rec.last.meta["contributor_id"] == 2 and rec.last.meta["ticket_id"] == 7


def test_takeover_handback_timestamps(setup):
    rec, holder, _ = setup
    calls: list = []
    rc = RecordingControl(rec, lambda: RecordingTarget(7, 4, 2, "recovery_takeover"),
                          on_takeover=lambda: calls.append("pause"), on_handback=lambda: calls.append("resume"))
    holder["rc"] = rc
    assert rc.handle({"t": "record_start"})["ok"]
    assert rc.state()["phase"] == "policy" and rc.control_source() == "policy"
    early = rc.handle({"t": "takeover"})
    assert not early["ok"] and "policy run" in early["error"]
    assert not rc.handle({"t": "handback"})["ok"]
    _wait(rc, "takeover")
    r = rc.handle({"t": "takeover"})
    assert r["ok"] and r["recording"]["phase"] == "human" and r["recording"]["takeover_ts"], r
    assert rc.control_source() == "human"
    assert not rc.handle({"t": "record_stop", "success": True})["ok"]  # hand back first
    assert not rc.handle({"t": "handback"})["ok"]                       # < 1 s of human control
    _wait(rc, "handback")
    time.sleep(0.5)
    r = rc.handle({"t": "handback"})
    assert r["ok"] and r["recording"]["phase"] == "handed_back", r
    tk, hb = r["recording"]["takeover_ts"], r["recording"]["handback_ts"]
    assert tk < hb and r["recording"]["takeover_at_ms"] < r["recording"]["handback_at_ms"]
    assert calls == ["pause", "resume"]
    time.sleep(1.0)
    r = rc.handle({"t": "record_stop", "success": True})
    assert r["ok"], r
    m = rec.last.meta
    assert m["kind"] == "recovery_takeover" and m["takeover_ts"] == tk and m["handback_ts"] == hb
    assert m["takeover"]["marked"] is True
    rep = check_local(rec.last)
    assert rep.passed, rep.reason()
    # control_source switches policy -> human -> policy
    from rdf_common.episode_format import read_actions

    src = list(read_actions(rec.last.actions_path).control_source)
    runs = [s for i, s in enumerate(src) if i == 0 or src[i - 1] != s]
    assert runs == ["policy", "human", "policy"], runs


def test_discard_records_nothing(setup):
    rec, holder, root = setup
    rc = RecordingControl(rec, RecordingTarget(7, 5, 2, "clean_teleop"))
    holder["rc"] = rc
    assert rc.handle({"t": "record_start"})["ok"]
    key = rc.state()["key"]
    time.sleep(0.6)
    r = rc.handle({"t": "record_discard"})
    assert r["ok"] and r["recording"]["last"]["status"] == "discarded"
    assert not (root / key).exists()
    assert r["recording"]["can"]["start"]
    assert rc.handle({"t": "record_start"})["ok"]  # can record again (claim untouched)
    assert rc.abandon("socket closed") is True
    assert rc.abandon() is False


def test_refuses_non_recording_tasks_and_lookup(setup):
    rec, holder, _ = setup
    rows = [{"id": 9, "ticket_id": 7, "type": "reset", "assignee_id": 2}]
    rc = RecordingControl(rec, lambda: lookup_task(lambda q: rows, 9))
    holder["rc"] = rc
    r = rc.handle({"t": "record_start"})
    assert not r["ok"] and "reset" in r["error"]
    assert not rc.handle({"t": "bogus"})["ok"]
    with pytest.raises(LookupError):
        lookup_task(lambda q: [], 1)


def test_adopts_rollout_owned_takeover_recording(setup):
    """TDM-69 path: the rollout runner records the paused rollout; the operator only takes over/hands back."""
    rec, holder, _ = setup
    rc = RecordingControl(rec, RecordingTarget(7, 6, 2, "recovery_takeover"))
    holder["rc"] = rc
    key = rec.start(7, "recovery_takeover", 6, 2, note="rollout runner")  # not via the control
    st = rc.state()
    assert st["active"] and st["owner"] == "rollout" and st["key"] == key and st["phase"] == "policy"
    assert not st["can"]["stop"] and not st["can"]["discard"]
    assert not rc.handle({"t": "record_start"})["ok"]
    _wait(rc, "takeover")
    assert rc.handle({"t": "takeover"})["ok"]
    _wait(rc, "handback")
    assert rc.handle({"t": "handback"})["ok"]
    assert not rc.handle({"t": "record_stop", "success": True})["ok"]  # the rollout ends the trial
    assert not rc.handle({"t": "record_discard"})["ok"]
    time.sleep(0.8)
    ep = rec.stop(success=True)  # the runner finishes the trial
    st = rc.state()
    assert not st["active"] and st["last"]["key"] == key and st["last"]["status"] == "recorded"
    assert ep.meta["takeover_ts"] == st["last"]["takeover_ts"] < st["last"]["handback_ts"]
    assert check_local(ep).passed, check_local(ep).reason()
