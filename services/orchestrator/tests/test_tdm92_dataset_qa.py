"""TDM-92: deterministic dataset QA + LeRobot import, on labelled synthetic FIXTURES (no SpacetimeDB).

The end-to-end proof through a local SpacetimeDB is ``scripts/tdm92_prove_dataset_qa.py``.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for the video fixtures")

from rdf_common.episode_format import make_metadata, read_actions, write_actions  # noqa: E402

from rdf_orchestrator import dataset_qa as dq  # noqa: E402
from rdf_orchestrator.dataset_import import import_lerobot  # noqa: E402

ROBOT = {"id": 1, "embodiment": "i2rt_yam", "camera_config_json": "{}"}


@pytest.fixture(scope="module")
def cfg():
    return dq.load_config(allow_fixtures=True)


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    import tdm92_fixtures as fx

    d = tmp_path_factory.mktemp("tdm92")
    root = fx.build_lerobot_fixture(d / "lerobot")
    return root, import_lerobot(str(root), d / "imported", assume_success=True, t0_us=1_790_000_000_000_000)


def _row(i, e):
    return {"id": i, "ticket_id": 1, "kind": "purchased", "review_status": "pending", "duration_ms": e.duration_ms,
            "frame_count": e.frame_count, "cameras_json": e.cameras_json(), "actions_uri": e.actions_path.as_uri(),
            "dataset_offer_id": 1}


def test_import_maps_lerobot_into_episode_format(imported):
    _, eps = imported
    assert [e.source_index for e in eps] == [0, 1, 2, 3]
    a = read_actions(eps[0].actions_path)
    assert a.joint_pos.shape[1] == 6 and a.is_fixture and a.fps == 30.0
    src = a.meta["source"]
    assert src["format"] == "lerobot" and src["gripper_channel"] == "gripper" and not src["gripper_assumed"]
    assert a.meta["embodiment"] == "i2rt_yam" and src["ik_target"] == "absent"
    assert sorted(eps[0].cameras) == ["exo", "wrist"] and sorted(eps[2].cameras) == ["exo"]
    assert a.meta["cameras"]["exo"]["frames"] == a.n
    assert "joint_cmd" in a.columns and set(a.control_source) == {"human"}


def test_fixture_set_one_pass_three_named_rejects(imported, cfg):
    import tdm92_fixtures as fx

    _, eps = imported
    peers = []
    for i, (f, e) in enumerate(zip(fx.EPISODES, eps), start=1):
        rep = dq.run_dataset_qa(_row(i, e), read_actions(e.actions_path), robot=ROBOT, peers=peers, config=cfg)
        peers.append(rep.fingerprint)
        assert rep.results[-1].name == dq.SUMMARY_CHECK
        assert {r.name for r in rep.results} == set(dq.ALL_CHECKS)
        if f.expect_pass:
            assert rep.passed, rep.reason()
        else:
            assert rep.failed_names == [f.expect_check], rep.reason()
            assert f.expect_check in rep.reason() and f.expect_reason.lower() in rep.reason().lower()


def test_raw_lerobot_delivery_is_normalised_before_checks(imported, cfg):
    """TDM-91's unpack_lerobot ingests the RAW LeRobot parquet as actions_uri; QA normalises it."""
    root, eps = imported
    raw = root / "data/chunk-000/episode_000000.parquet"
    row = {**_row(9, eps[0]), "actions_uri": raw.as_uri()}
    acts, err, norm = dq.load_purchased_actions(row)
    assert err is None and norm == "lerobot"
    assert acts.joint_pos.shape == (eps[0].frame_count, 6) and acts.meta["embodiment"] == "i2rt_yam"
    rep = dq.run_dataset_qa(row, acts, robot=ROBOT, config=cfg)
    assert rep.passed, rep.reason()


def _synthetic(tmp_path, q, *, embodiment="i2rt_yam", fps=30.0):
    n = len(q)
    t = (1_790_000_000_000_000 + np.arange(n) * (1e6 / fps)).astype(np.int64)
    cols = {"frame_index": np.arange(n, dtype=np.int32), "timestamp_us": t, "joint_pos": q.astype(np.float32),
            "gripper_pos": np.linspace(0, 1, n).astype(np.float32), "ik_target_pos": np.zeros((n, 3), np.float32),
            "ik_target_quat": np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32),
            "gripper_cmd": np.linspace(0, 1, n).astype(np.float32), "control_source": ["human"] * n}
    p = tmp_path / "a.parquet"
    write_actions(p, cols, make_metadata(fps=fps, joint_names=[f"j{i}" for i in range(q.shape[1])],
                                         embodiment=embodiment, fixture=True))
    return read_actions(p)


def test_embodiment_bimanual_and_foreign_arm(tmp_path, cfg):
    s = np.linspace(0, 1, 150)[:, None]
    bi = _synthetic(tmp_path, 0.3 + 0.5 * s * np.ones((1, 14)), embodiment="bi_yam_follower")
    r = dq.check_embodiment(bi, ROBOT, cfg.dataset)
    assert r.failed and "14 arm joints" in r.reason and "--arm" in r.reason
    so = _synthetic(tmp_path, 0.3 + 0.5 * s * np.ones((1, 6)), embodiment="so100")
    r = dq.check_embodiment(so, ROBOT, cfg.dataset)
    assert r.failed and "'so100'" in r.reason
    yam = _synthetic(tmp_path, 0.3 + 0.5 * s * np.ones((1, 6)), embodiment="bi_yam_follower:left")
    assert dq.check_embodiment(yam, ROBOT, cfg.dataset).passed


def test_action_range_degrees_and_fps(tmp_path, cfg):
    s = np.linspace(0, 1, 150)[:, None]
    deg = _synthetic(tmp_path, 10 + 60 * s * np.ones((1, 6)))
    r = dq.check_action_range(deg, cfg.dataset)
    assert r.failed and "degrees" in r.reason
    slow = _synthetic(tmp_path, 0.3 + 0.5 * s * np.ones((1, 6)), fps=15.0)
    r = dq.check_fps(slow, cfg.dataset)
    assert r.failed and "15.0" in r.reason


class FakeStdb:
    def __init__(self):
        self.calls = []

    def call_reducer(self, name, *args):
        self.calls.append((name, args))


def test_handler_writes_summary_last_and_rejects_failures(imported, cfg):
    import tdm92_fixtures as fx
    from rdf_orchestrator.handlers import dataset_qa as h

    _, eps = imported
    passed_hook = []
    h.after_pass = lambda stdb, ep, ticket: passed_hook.append(ep["id"])
    try:
        peers = []
        for i, (f, e) in enumerate(zip(fx.EPISODES, eps), start=1):
            stdb = FakeStdb()
            rep = h.qa_episode(_row(i, e), {"id": 1}, ROBOT, peers, stdb, cfg)
            peers.append(rep.fingerprint)
            qa = [a for n, a in stdb.calls if n == "record_qa_result"]
            assert qa[-1][1] == dq.SUMMARY_CHECK and qa[-1][2] is f.expect_pass
            assert len(qa) == len(dq.ALL_CHECKS)
            reviews = [a for n, a in stdb.calls if n == "submit_review"]
            if f.expect_pass:
                assert not reviews
            else:
                assert len(reviews) == 1 and reviews[0][1] is False and f.expect_check in reviews[0][2]
                assert json.loads(reviews[0][3])["failed_checks"] == [f.expect_check]
            ev = [a for n, a in stdb.calls if n == "append_event"]
            assert ev and ev[0][1] == "qa.dataset_checked"
        assert passed_hook == [1]
    finally:
        h.after_pass = None


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("static: {min_joint_std: 0.1}\n")
    with pytest.raises(ValueError, match="unknown key static.min_joint_std"):
        dq.DatasetQAConfig.from_yaml(p)
