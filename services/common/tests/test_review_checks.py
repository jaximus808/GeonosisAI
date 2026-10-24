"""TDM-25 deterministic checks on synthetic (fixture) actions built in memory. No video, no Claude."""
from __future__ import annotations

import io

import numpy as np
import pytest

from rdf_common.episode_format import (
    REQUIRED_COLUMNS, EpisodeFormatError, cameras_from_json, make_metadata, read_actions, write_actions,
)
from rdf_common.review_checks import FAIL, PASS, SKIP, ReviewThresholds, joint_jerk, run_checks, ts_us

FPS = 30.0
T0 = 1_792_900_000_000_000
TH = ReviewThresholds(allow_fixtures=True)


def _minjerk(s):
    s = np.clip(s, 0, 1)
    return 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5


def actions_bytes(seconds=10.0, *, tremor=0.0, drop_every=0, src=None, fixture=True, cameras=None, seed=0):
    n = int(seconds * FPS) + 1
    t = np.arange(n) / FPS
    s = _minjerk(t / seconds)
    rng = np.random.default_rng(seed)
    pos = np.outer(s, [0.6, -0.4, 0.5, 0.2, 0.3, -0.5]) + rng.normal(0, 1e-4, (n, 6))
    if tremor:
        pos += tremor * np.sin(2 * np.pi * 5.5 * t)[:, None]
    keep = np.ones(n, bool)
    if drop_every:
        keep[np.arange(n) % drop_every == drop_every - 1] = False
    m = int(keep.sum())
    cols = {
        "frame_index": np.arange(m, dtype=np.int32),
        "timestamp_us": (T0 + np.rint(t * 1e6)).astype(np.int64)[keep],
        "joint_pos": pos[keep], "gripper_pos": np.ones(m), "ik_target_pos": np.zeros((m, 3)),
        "ik_target_quat": np.tile([1, 0, 0, 0], (m, 1)), "gripper_cmd": np.ones(m),
        "control_source": (src[keep] if src is not None else ["human"] * m),
    }
    buf = io.BytesIO()
    write_actions(buf, cols, make_metadata(fps=FPS, joint_names=list("abcdef"), fixture=fixture, cameras=cameras))
    return buf.getvalue(), t[keep]


def episode(kind="clean_teleop", seconds=10.0, frames=None, **kw):
    e = {"id": 1, "kind": kind, "duration_ms": int(round(seconds * 1000)),
         "frame_count": frames if frames is not None else int(seconds * FPS) + 1,
         "takeover_ts": None, "handback_ts": None, "success": True}
    e.update(kw)
    return e


def statuses(report):
    return {r.name: r.status for r in report.results}


def test_clean_episode_passes_every_check():
    raw, _ = actions_bytes()
    r = run_checks(episode(), read_actions(raw), TH)
    assert r.passed, r.reason()
    assert statuses(r) == {"actions": PASS, "duration": PASS, "frame_drops": PASS, "jerk": PASS, "success": PASS}
    assert r.get("jerk").detail["jerk_rms"] < 10
    assert r.reason() == ""


def test_too_short_rejected_with_reason():
    raw, _ = actions_bytes(1.2)
    r = run_checks(episode(seconds=1.2), read_actions(raw), TH)
    assert not r.passed
    assert r.get("duration").status == FAIL
    assert "1.2 s long; minimum is 2 s" in r.reason()


def test_too_long_rejected():
    raw, _ = actions_bytes(130)
    r = run_checks(episode(seconds=130), read_actions(raw), TH)
    assert r.get("duration").status == FAIL and "maximum is 120 s" in r.reason()


def test_duration_must_match_actions_span():
    raw, _ = actions_bytes(10)
    r = run_checks(episode(seconds=20, frames=301), read_actions(raw), TH)
    assert r.get("duration").status == FAIL and "actions cover 10.0 s" in r.reason()


def test_jerky_rejected():
    raw, _ = actions_bytes(tremor=0.02)
    r = run_checks(episode(), read_actions(raw), TH)
    assert r.get("jerk").status == FAIL
    assert "jerky" in r.reason() and r.get("jerk").detail["jerk_rms"] >= TH.max_jerk_rms


def test_frame_drops_over_two_percent_rejected():
    raw, kept = actions_bytes(drop_every=20)  # 5% dropped
    r = run_checks(episode(frames=len(kept)), read_actions(raw), TH)
    fd = r.get("frame_drops")
    assert fd.status == FAIL and fd.detail["drop_ratio"] == pytest.approx(0.05, abs=0.005)
    assert "% of frames dropped" in r.reason()


def test_frame_drops_under_two_percent_pass():
    raw, kept = actions_bytes(drop_every=100)  # 1%
    r = run_checks(episode(frames=len(kept)), read_actions(raw), TH)
    assert r.get("frame_drops").status == PASS


def test_camera_frame_drops_counted():
    raw, _ = actions_bytes(cameras={"exo": {"frames": 280, "fps": 30.0}})  # 21 of 301 missing
    r = run_checks(episode(), read_actions(raw), TH)
    assert r.get("frame_drops").status == FAIL and r.get("frame_drops").detail["worst_stream"] == "camera:exo"


def test_frame_count_mismatch_rejected():
    raw, _ = actions_bytes()
    r = run_checks(episode(frames=250), read_actions(raw), TH)
    assert r.get("frame_drops").status == FAIL and "frame_count is 250" in r.reason()


def _takeover_src(t, tk_s, hb_s):
    src = np.array(["policy"] * len(t), dtype=object)
    src[(t >= tk_s) & (t < hb_s)] = "human"
    return src


def test_valid_takeover_passes():
    t = np.arange(301) / FPS
    raw, _ = actions_bytes(src=_takeover_src(t, 4.0, 9.0))
    ep = episode("recovery_takeover", takeover_ts=T0 + 4_000_000, handback_ts=T0 + 9_000_000)
    r = run_checks(ep, read_actions(raw), TH)
    assert r.passed, r.reason()
    assert r.get("failure_before_takeover").detail["pre_takeover_s"] == pytest.approx(4.0)


def test_takeover_missing_handback_rejected():
    t = np.arange(301) / FPS
    raw, _ = actions_bytes(src=_takeover_src(t, 4.0, 99))
    r = run_checks(episode("recovery_takeover", takeover_ts=T0 + 4_000_000), read_actions(raw), TH)
    assert r.get("takeover_timestamps").status == FAIL
    assert "missing handback_ts" in r.reason()
    assert r.get("failure_before_takeover").status == SKIP


def test_takeover_with_no_failure_frames_rejected():
    t = np.arange(301) / FPS
    raw, _ = actions_bytes(src=_takeover_src(t, 0.1, 9.0))
    ep = episode("recovery_takeover", takeover_ts=T0 + 100_000, handback_ts=T0 + 9_000_000)
    r = run_checks(ep, read_actions(raw), TH)
    assert r.get("failure_before_takeover").status == FAIL and "before the takeover" in r.reason()


def test_takeover_control_source_must_match_timestamps():
    raw, _ = actions_bytes()  # every frame "human", even before the takeover
    ep = episode("recovery_takeover", takeover_ts=T0 + 4_000_000, handback_ts=T0 + 9_000_000)
    r = run_checks(ep, read_actions(raw), TH)
    assert r.get("failure_before_takeover").status == FAIL and "policy-driven" in r.reason()


def test_handback_before_takeover_rejected():
    raw, _ = actions_bytes()
    ep = episode("recovery_takeover", takeover_ts=T0 + 5_000_000, handback_ts=T0 + 4_000_000)
    assert "not after takeover_ts" in run_checks(ep, read_actions(raw), TH).reason()


@pytest.mark.parametrize("flag,why", [(None, "missing"), (False, "unsuccessful")])
def test_success_flag_required(flag, why):
    raw, _ = actions_bytes()
    r = run_checks(episode(success=flag), read_actions(raw), TH)
    assert r.get("success").status == FAIL and why in r.reason()


def test_fixture_data_rejected_unless_allowed():
    raw, _ = actions_bytes(fixture=True)
    r = run_checks(episode(), read_actions(raw), ReviewThresholds())
    assert not r.passed and "synthetic fixture" in r.reason()
    assert r.get("jerk").status == PASS  # other checks still ran


def test_unreadable_actions_rejected_and_kinematics_skipped():
    with pytest.raises(EpisodeFormatError):
        read_actions(b'{"fake": true}')
    r = run_checks(episode(), None, TH, actions_error="not a readable parquet file")
    assert statuses(r)["actions"] == FAIL and statuses(r)["jerk"] == SKIP
    assert "actions file unreadable: not a readable parquet file" in r.reason()


def test_missing_required_column_rejected():
    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"frame_index": [0, 1], "timestamp_us": [0, 1]}), buf)
    with pytest.raises(EpisodeFormatError, match="missing required columns"):
        read_actions(buf.getvalue())


def test_write_actions_refuses_incomplete_rows():
    with pytest.raises(EpisodeFormatError, match="missing required"):
        write_actions(io.BytesIO(), {"frame_index": [0]}, make_metadata(joint_names=["a"]))
    assert "joint_pos" in REQUIRED_COLUMNS and "ik_target_pos" in REQUIRED_COLUMNS


def test_jerk_estimator_is_noise_robust():
    t = np.arange(301) / FPS
    smooth = np.outer(_minjerk(t / 10), [1.0])
    quantised = np.round(smooth / (2 * np.pi / 16384)) * (2 * np.pi / 16384)
    jk, _ = joint_jerk((t * 1e6).astype(np.int64), quantised, FPS, 9)
    assert np.sqrt((jk ** 2).mean()) < 10


def test_thresholds_from_yaml_mapping_and_typos():
    th = ReviewThresholds.from_mapping({"version": 1, "duration": {"min_s": 3, "max_s": 60},
                                        "takeover": {"kinds": ["recovery_takeover"]}, "claude": {"model": "x"}})
    assert th.min_duration_s == 3 and th.takeover_kinds == ("recovery_takeover",)
    with pytest.raises(ValueError, match="unknown key"):
        ReviewThresholds.from_mapping({"jerk": {"max_rms": 5}})
    with pytest.raises(ValueError, match="unknown section"):
        ReviewThresholds.from_mapping({"jerky": {}})


def test_ts_and_cameras_helpers():
    assert ts_us({"some": {"__timestamp_micros_since_unix_epoch__": 5}}) == 5
    assert ts_us({"none": []}) is None and ts_us([0, 7]) == 7 and ts_us(None) is None
    assert cameras_from_json('{"exo": "a.mp4", "wrist": {"uri": "b.mp4"}, "fake": true}') == {"exo": "a.mp4", "wrist": "b.mp4"}


def test_orchestrator_config_file_loads():
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "orchestrator" / "config" / "review.yaml"
    th = ReviewThresholds.from_yaml(p)
    assert th.min_duration_s == 2.0 and th.max_duration_s == 120.0 and th.max_frame_drop_ratio == 0.02
    assert th.allow_fixtures is False
