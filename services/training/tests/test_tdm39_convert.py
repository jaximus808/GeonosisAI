"""TDM-39: LeRobot v2.1 conversion on the labelled 2-episode YAM fixture.

Run: cd services/training && uv run pytest tests/test_tdm39_convert.py -q
The LeRobot load test runs only where lerobot@0cf8648 is importable (the openpi pin); see
spikes/tdm-39-convert/README.md for the venv that has it.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from rdf_common.episode_format import read_actions
from rdf_training import convert as cv
from rdf_training import convert_fixtures as fx

REPO = fx.FIXTURE_REPO_ID


def _decode_numbers(mp4: Path) -> list[int]:
    import av

    with av.open(str(mp4)) as c:
        return [fx.read_frame_number(f.to_ndarray(format="rgb24")) for f in c.decode(video=0)]


@pytest.fixture(scope="module")
def fixture_manifest(tmp_path_factory) -> dict:
    return fx.write_fixture_manifest(tmp_path_factory.mktemp("episodes"))


@pytest.fixture(scope="module")
def converted(fixture_manifest, tmp_path_factory) -> tuple[dict, Path, Path]:
    out = tmp_path_factory.mktemp("datasets")
    report = cv.convert(fixture_manifest, lerobot_root=out / "lerobot", assets_root=out / "assets")
    return report, out / "lerobot" / REPO, out / "assets" / REPO


def test_frame_counts_and_takeover_cut(converted):
    report, root, _ = converted
    clean, rec = report["episodes"]
    assert clean["frames"] == fx.CLEAN_FRAMES and clean["cut_before_takeover"] == 0
    # recovery: the 60 policy (failure) ticks before takeover are cut, as are the 15 after handback
    assert rec["frames"] == fx.HANDBACK_FRAME - fx.TAKEOVER_FRAME == 75
    assert rec["cut_before_takeover"] == fx.TAKEOVER_FRAME
    assert rec["cut_after_handback"] == fx.RECOVERY_FRAMES - fx.HANDBACK_FRAME
    info = json.loads((root / "meta" / "info.json").read_text())
    assert info["codebase_version"] == "v2.1" and info["fps"] == 30
    assert info["total_frames"] == 90 + 75 and info["total_episodes"] == 2 and info["total_videos"] == 4
    lengths = [json.loads(line)["length"] for line in (root / "meta" / "episodes.jsonl").read_text().splitlines()]
    assert lengths == [90, 75]


def test_video_frames_match_kept_ticks(converted):
    _, root, _ = converted
    for key in ("observation.images.top", "observation.images.wrist"):
        assert _decode_numbers(root / f"videos/chunk-000/{key}/episode_000000.mp4") == list(range(90))
        # first frame of the recovery episode is the takeover frame: no failure frames survive
        assert _decode_numbers(root / f"videos/chunk-000/{key}/episode_000001.mp4") == list(range(60, 135))


def test_state_and_action_columns(converted, fixture_manifest):
    _, root, _ = converted
    src_clean = read_actions(fixture_manifest["episodes"][0]["actions_uri"])
    src_rec = read_actions(fixture_manifest["episodes"][1]["actions_uri"])
    t0 = pq.read_table(root / "data/chunk-000/episode_000000.parquet").to_pydict()
    t1 = pq.read_table(root / "data/chunk-000/episode_000001.parquet").to_pydict()
    s1, a1 = np.array(t1["observation.state"]), np.array(t1["action"])
    assert s1.shape == (75, 7) and a1.shape == (75, 7)
    np.testing.assert_allclose(s1[:, :6], src_rec.joint_pos[60:135], atol=1e-6)
    np.testing.assert_allclose(s1[:, 6], src_rec.extra["gripper_pos"][60:135], atol=1e-6)
    # recovery fixture has no joint_cmd: joint action = next measured joint_pos
    np.testing.assert_allclose(a1[:, :6], src_rec.joint_pos[61:136], atol=1e-6)
    np.testing.assert_allclose(a1[:, 6], src_rec.extra["gripper_cmd"][60:135], atol=1e-6)
    # clean fixture records joint_cmd: that is the action
    np.testing.assert_allclose(np.array(t0["action"])[:, :6], src_clean.extra["joint_cmd"], atol=1e-6)
    np.testing.assert_allclose(t1["timestamp"], np.arange(75) / 30, atol=1e-6)
    assert t1["frame_index"] == list(range(75)) and t1["index"] == list(range(90, 165))
    assert set(t1["episode_index"]) == {1} and set(t1["task_index"]) == {0}
    assert np.array(t1["action.ee_target"]).shape == (75, 8)


def test_norm_stats_openpi_format(converted):
    _, _, assets = converted
    ns = json.loads((assets / "norm_stats.json").read_text())["norm_stats"]
    for k in ("state", "actions"):
        assert set(ns[k]) == {"mean", "std", "q01", "q99"} and len(ns[k]["mean"]) == 7
    # actions are joint deltas (small) except the absolute gripper
    assert max(abs(x) for x in ns["actions"]["mean"][:6]) < 0.5
    assert 0.0 < ns["actions"]["mean"][6] <= 1.0


def test_keep_after_handback_and_missing_takeover_ts(fixture_manifest, tmp_path):
    m = json.loads(json.dumps(fixture_manifest))
    m["keep_after_handback"] = True
    del m["episodes"][1]["takeover_ts"]  # falls back to the first human tick
    r = cv.convert(m, lerobot_root=tmp_path / "l", assets_root=tmp_path / "a")
    rec = r["episodes"][1]
    assert rec["cut_before_takeover"] == 60 and rec["frames"] == 90 and rec["cut_after_handback"] == 0


def test_fixtures_refused_by_default(fixture_manifest, tmp_path):
    m = {**fixture_manifest, "allow_fixtures": False}
    with pytest.raises(cv.ConvertError, match="fixture"):
        cv.convert(m, lerobot_root=tmp_path / "l", assets_root=tmp_path / "a")


def test_resample_60fps_to_30(tmp_path):
    ep = fx.write_fixture_episode(tmp_path / "eps", episode_id="fast", kind="clean_teleop", n_frames=120, fps=60.0,
                                  size=(96, 128), cameras=("exo", "wrist"))
    m = {"repo_id": "rdf-fixtures/fast", "task": "t", "episodes": [ep], "allow_fixtures": True}
    r = cv.convert(m, lerobot_root=tmp_path / "l", assets_root=tmp_path / "a")
    assert r["episodes"][0]["frames"] == 60  # 2 s at 30 fps (grid 0..1.9667 s)
    nums = _decode_numbers(tmp_path / "l/rdf-fixtures/fast/videos/chunk-000/observation.images.top/episode_000000.mp4")
    assert nums == list(range(0, 120, 2))


def test_base_mix_and_convert_episodes(fixture_manifest, tmp_path):
    base = [fixture_manifest["episodes"][0]]
    m = {**fixture_manifest, "episodes": [fixture_manifest["episodes"][1]], "base_episodes": base,
         "base_mix_ratio": 0.5}
    uri = cv.convert_episodes("t7", m, media_root=str(tmp_path / "media"), datasets_root=str(tmp_path / "ds"))
    assert uri == f"modal-vol://rdf-datasets/lerobot/{REPO}"
    rep = json.loads((tmp_path / "ds/lerobot" / REPO / "meta/rdf_conversion.json").read_text())
    assert [e["source"] for e in rep["episodes"]] == ["ticket", "base"]
    assert (tmp_path / "ds/assets" / REPO / "norm_stats.json").exists()


def test_resolve_uri_modal_volume(tmp_path):
    p = cv.resolve_uri("modal-vol://rdf-media/tickets/1/episodes/2/exo.mp4", mounts={"rdf-media": str(tmp_path)})
    assert p == tmp_path / "tickets/1/episodes/2/exo.mp4"


def test_lerobot_v21_loads(converted, monkeypatch):
    """The real LeRobot loader at openpi's pin reads the dataset, with the S-05a action chunks."""
    lds = pytest.importorskip("lerobot.common.datasets.lerobot_dataset")
    _, root, _ = converted
    ds = lds.LeRobotDataset(REPO, root=root, video_backend="pyav",
                            delta_timestamps={"action": [t / 30 for t in range(50)]})
    assert ds.num_episodes == 2 and ds.num_frames == 165 and ds.fps == 30
    item = ds[90]  # first frame of the recovery episode
    assert tuple(item["observation.images.top"].shape) == (3, 240, 320)
    assert tuple(item["observation.state"].shape) == (7,) and tuple(item["action"].shape) == (50, 7)
    assert item["task"] == fx.FIXTURE_TASK
    img = (item["observation.images.top"].permute(1, 2, 0).numpy() * 255).astype(np.uint8)
    assert fx.read_frame_number(img) == 60


def test_modal_convert_matches_modal_app(fixture_manifest, tmp_path):
    """services/training/modal_app.py calls modal_convert(manifest, datasets_root=, media_root=) -> report."""
    rep = cv.modal_convert(dict(fixture_manifest), datasets_root=str(tmp_path / "ds"), media_root=str(tmp_path / "m"))
    assert rep["repo_id"] == REPO and rep["total_frames"] == 165
    assert Path(rep["dataset_dir"]) == tmp_path / "ds/lerobot" / REPO


def test_tdm25_fixture_episode_rows(tmp_path):
    """Episode rows exactly as TDM-25's fixtures (and the episode table) carry them: file:// uris in
    cameras_json, int-micros takeover/handback. Single exo camera, so only the cut is checked."""
    import sys

    tests_dir = Path(__file__).resolve().parents[2] / "orchestrator" / "tests"
    sys.path.insert(0, str(tests_dir))
    t25 = pytest.importorskip("tdm25_fixtures")
    row, _ = t25.make_episode(tmp_path, "takeover_ok", t25.TAKEOVER_PLAN, kind="recovery_takeover", seed=4,
                              takeover_s=t25.TAKEOVER_AT_S, handback_s=t25.HANDBACK_AT_S)
    row["review_status"] = "accepted"
    m = cv.manifest_from_episode_rows([row], repo_id="rdf-fixtures/tdm25", task=t25.TASK, allow_fixtures=True)
    r = cv.convert(m, lerobot_root=tmp_path / "l", assets_root=tmp_path / "a")
    e = r["episodes"][0]
    src = read_actions(row["actions_uri"].removeprefix("file://"))
    t = src.timestamp_us
    assert e["cut_before_takeover"] == int((t < row["takeover_ts"]).sum()) > 0
    assert e["frames"] == int(((t >= row["takeover_ts"]) & (t < row["handback_ts"])).sum())
    assert r["video_keys"] == ["observation.images.top"]
