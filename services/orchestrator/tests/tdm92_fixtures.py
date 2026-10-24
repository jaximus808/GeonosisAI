"""TDM-92 dataset-QA FIXTURES: a synthetic LeRobot v2.1 dataset, NOT real robot data.

A seller's "purchased dataset" in a FOREIGN format (LeRobot v2.1: meta/info.json, episodes.jsonl,
per-episode data parquet + one mp4 per camera), so the proof goes through the import/normalisation
path (``rdf_orchestrator.dataset_import``) before QA. Labelled everywhere: info.json
``rdf_fixture: true`` (the importer turns it into parquet metadata ``fixture: true``), a README, and
the mp4 comment. Trajectories and the cartoon video come from the TDM-25 fixtures (min-jerk moves with
YAM-like encoder noise), shifted into the YAM URDF joint limits (joint 2 is 0..3.65 rad on the YAM).

    ep 0  good             clean pick-and-place, exo + wrist                  -> passes deterministic QA
    ep 1  duplicate        ep 0 re-sent: same trajectory + 0.2 mrad noise,    -> reject: duplicate
                           same video
    ep 2  wrong cameras    a different (slower, regrasp) demo whose wrist     -> reject: camera_set
                           video is missing from the delivery: 1 camera
    ep 3  static           the arm holds its home pose for 8 s                -> reject: static
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import tdm25_fixtures as f25

FPS = f25.FPS
JOINT_OFFSET = np.array([0.0, 0.8, 0.0, 0.0, 0.0, 0.0])  # TDM-25 poses -> inside the YAM URDF limits
STATE_NAMES = [f"joint_{i}" for i in range(1, 7)] + ["gripper"]
TASK = f25.TASK
STATIC_PLAN = [(8.0, "home", 1.0, False)]


@dataclass
class FixtureEpisode:
    index: int
    name: str
    expect_pass: bool
    expect_check: str | None  # the check that must fail (None: passes)
    expect_reason: str  # substring of that check's reason


EPISODES = [
    FixtureEpisode(0, "good", True, None, ""),
    FixtureEpisode(1, "duplicate", False, "duplicate", "duplicate of episode"),
    FixtureEpisode(2, "wrong_camera_count", False, "camera_set", "missing ['wrist']"),
    FixtureEpisode(3, "static", False, "static", "barely moves"),
]


def _episode_arrays(plan, seed: int, extra_noise: float = 0.0, noise_seed: int = 0):
    t, joints, pix, grip, attached = f25.sample_plan(plan)
    joints = joints + JOINT_OFFSET
    meas = f25.measured(joints, seed)
    if extra_noise:
        meas = meas + np.random.default_rng(noise_seed).normal(0.0, extra_noise, meas.shape)
    return t, joints, meas, pix, grip, attached


def _write_parquet(path: Path, ep_index: int, first_index: int, t, joints_cmd, joints_meas, grip):
    import pyarrow as pa
    import pyarrow.parquet as pq

    n = len(t)
    state = np.column_stack([joints_meas, grip]).astype(np.float32)
    action = np.column_stack([joints_cmd, grip]).astype(np.float32)
    table = pa.table({
        "observation.state": pa.array(state.tolist(), type=pa.list_(pa.float32())),
        "action": pa.array(action.tolist(), type=pa.list_(pa.float32())),
        "timestamp": pa.array(t.astype(np.float32)),
        "frame_index": pa.array(np.arange(n, dtype=np.int64)),
        "episode_index": pa.array(np.full(n, ep_index, dtype=np.int64)),
        "index": pa.array(np.arange(first_index, first_index + n, dtype=np.int64)),
        "task_index": pa.array(np.zeros(n, dtype=np.int64)),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return n


def build_lerobot_fixture(root: Path) -> Path:
    """Write the four-episode fixture dataset at ``root`` (replaced if present). Returns root."""
    root = Path(root).resolve()
    if root.exists():
        shutil.rmtree(root)
    (root / "meta").mkdir(parents=True)
    specs = [
        # (plan, seed, extra_noise, wrist video?)
        (f25.CLEAN_PLAN, 1, 0.0, True),
        (f25.CLEAN_PLAN, 1, 2e-4, True),
        (f25.TAKEOVER_PLAN, 4, 0.0, False),
        (STATIC_PLAN, 6, 0.0, True),
    ]
    episodes, total = [], 0
    for i, (plan, seed, noise, wrist) in enumerate(specs):
        t, jc, jm, pix, grip, att = _episode_arrays(plan, seed, noise, noise_seed=100 + i)
        n = _write_parquet(root / f"data/chunk-000/episode_{i:06d}.parquet", i, total, t, jc, jm, grip)
        vdir = root / "videos/chunk-000"
        top = vdir / "observation.images.top" / f"episode_{i:06d}.mp4"
        top.parent.mkdir(parents=True, exist_ok=True)
        f25.render_video(top, t, pix, grip, att, seed=seed)
        if wrist:
            w = vdir / "observation.images.wrist" / f"episode_{i:06d}.mp4"
            w.parent.mkdir(parents=True, exist_ok=True)
            f25.render_video(w, t, pix, grip, att, seed=seed, jitter_px=0.0)
        episodes.append({"episode_index": i, "tasks": [TASK], "length": n})
        total += n
    feat_vec = {"dtype": "float32", "shape": [7], "names": STATE_NAMES}
    vid = {"dtype": "video", "shape": [f25.H, f25.W, 3], "names": ["height", "width", "channels"],
           "info": {"video.fps": FPS, "video.codec": "h264"}}
    info = {
        "codebase_version": "v2.1", "robot_type": "i2rt_yam", "fps": int(FPS), "total_episodes": len(episodes),
        "total_frames": total, "total_tasks": 1, "total_videos": 7, "total_chunks": 1, "chunks_size": 1000,
        "splits": {"train": f"0:{len(episodes)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {"observation.state": feat_vec, "action": feat_vec,
                     "observation.images.top": vid, "observation.images.wrist": vid,
                     "timestamp": {"dtype": "float32", "shape": [1], "names": None},
                     "frame_index": {"dtype": "int64", "shape": [1], "names": None},
                     "episode_index": {"dtype": "int64", "shape": [1], "names": None},
                     "index": {"dtype": "int64", "shape": [1], "names": None},
                     "task_index": {"dtype": "int64", "shape": [1], "names": None}},
        "rdf_fixture": True,
        "rdf_note": "TDM-92 synthetic fixture dataset, not real robot data",
    }
    (root / "meta/info.json").write_text(json.dumps(info, indent=2))
    (root / "meta/episodes.jsonl").write_text("".join(json.dumps(e) + "\n" for e in episodes))
    (root / "meta/tasks.jsonl").write_text(json.dumps({"task_index": 0, "task": TASK}) + "\n")
    (root / "README.md").write_text(
        "# TDM-92 FIXTURE dataset (synthetic, not real robot data)\n\n"
        + "\n".join(f"- episode {e.index}: {e.name} (expect {'pass' if e.expect_pass else 'reject: ' + e.expect_check})"
                    for e in EPISODES) + "\n")
    return root
