"""TDM-93 LLM-QA FIXTURES: synthetic PURCHASED episodes, NOT real robot data.

Every file is labelled as a fixture: parquet metadata ``fixture: true`` with the note
"TDM-93 synthetic fixture", and the mp4 comment says the same. The scenes are cartoons drawn with the
TDM-25 fixture primitives (``tdm25_fixtures``). Each episode has two cameras: ``exo`` (the TDM-25 scene
view) and ``wrist`` (a close-up that follows the gripper). The ticket's task is TDM-25's "Put the red
cup in the blue bin."

    cup_demo        a seller's genuine demo: grasps the red cup, puts it in the blue bin      -> ACCEPT
    block_stacking  a seller's episode of a different task: stacks a green block on a yellow
                    block; no cup, no bin. Kinematically clean, seller flags success          -> REJECT llm_relevance
    cup_missed      the right scene and task, but the gripper closes beside the cup and the
                    cup never leaves the table, while the seller still flags success           -> REJECT llm_success

``build_fixture_set`` returns them with the expected verdicts.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import tdm25_fixtures as fx  # noqa: E402

from rdf_common.episode_format import make_metadata, write_actions  # noqa: E402

FPS, W, H = fx.FPS, fx.W, fx.H
TASK = fx.TASK
NOTE = "TDM-93 synthetic fixture, not real robot data"
YELLOW, GREEN = (225, 190, 40), fx.BLOCK

J = {k: v[0] for k, v in fx.POSES.items()}
# name -> (joints, image-space gripper position)
POSES: dict[str, tuple[np.ndarray, tuple[int, int]]] = {
    **fx.POSES,
    "above_green": (J["above_cup"] + 0.02, (110, 120)),
    "at_green": (J["at_cup"] + 0.02, (110, 160)),
    "lift_green": (J["lift"] + 0.02, (110, 105)),
    "above_yellow": (J["above_bin"] + 0.3, (220, 105)),
    "on_yellow": (J["in_bin"] + 0.3, (220, 136)),
}
GREEN_REST, YELLOW_REST = (110, 170), (220, 170)

CUP_PLAN = fx.CLEAN_PLAN
# misses: closes on air beside the cup, carries nothing to the bin, opens, goes home
MISS_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "above_cup", 1.0, False), (3.3, "beside_cup", 1.0, False),
    (3.8, "beside_cup", 0.0, False), (4.6, "lift", 0.0, False), (7.0, "above_bin", 0.0, False),
    (7.5, "in_bin", 0.0, False), (8.0, "in_bin", 1.0, False), (10.0, "home", 1.0, False),
]
STACK_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "above_green", 1.0, False), (3.3, "at_green", 1.0, False),
    (3.8, "at_green", 0.0, False), (4.6, "lift_green", 0.0, True), (7.0, "above_yellow", 0.0, True),
    (7.6, "on_yellow", 0.0, True), (8.1, "on_yellow", 1.0, False), (10.0, "home", 1.0, False),
]


def sample_plan(plan, fps: float = FPS):
    """fx.sample_plan with this module's pose table: t, joints, pix, grip, attached."""
    end = plan[-1][0]
    t = np.arange(0.0, end + 1e-9, 1.0 / fps)
    joints, pix = np.zeros((len(t), 6)), np.zeros((len(t), 2))
    grip, attached = np.zeros(len(t)), np.zeros(len(t), dtype=bool)
    prev_t, prev_pose, prev_grip = 0.0, plan[0][1], plan[0][2]
    for until, pose, g, att in plan:
        m = (t >= prev_t - 1e-9) & (t <= until + 1e-9)
        s = fx._minjerk((t[m] - prev_t) / max(until - prev_t, 1e-6))
        j0, p0 = POSES[prev_pose]
        j1, p1 = POSES[pose]
        joints[m] = j0 + np.outer(s, j1 - j0)
        pix[m] = np.array(p0) + np.outer(s, np.array(p1) - np.array(p0))
        grip[m] = prev_grip + s * (g - prev_grip)
        attached[m] = att
        prev_t, prev_pose, prev_grip = until, pose, g
    return t, joints, pix, grip, attached


# --------------------------------------------------------------------------- rendering

def _arm(img, gx, gy, grip_open):
    sh = (160, 0)
    el = ((sh[0] + gx) / 2 + 30, (sh[1] + gy) / 2 - 10)
    fx._line(img, sh, el, 12, fx.ARM)
    fx._line(img, el, (gx, gy - 18), 10, fx.ARM)
    fx._rect(img, gx - 14, gy - 22, gx + 14, gy - 14, fx.ARM_DARK)
    spread = 4 + 9 * grip_open
    fx._rect(img, gx - spread - 3, gy - 16, gx - spread, gy + 4, fx.ARM_DARK)
    fx._rect(img, gx + spread, gy - 16, gx + spread + 3, gy + 4, fx.ARM_DARK)


def render_blocks(gx, gy, grip_open, green_xy):
    img = np.empty((H, W, 3), dtype=np.uint8)
    img[:] = (190, 200, 205)  # a different lab: grey-blue wall, grey table
    fx._rect(img, 0, 80, W, H, (150, 150, 155))
    fx._rect(img, 0, 80, W, 84, (110, 110, 115))
    yx, yy = YELLOW_REST
    fx._rect(img, yx - 12, yy - 12, yx + 12, yy + 12, YELLOW)
    bx, by = green_xy
    fx._rect(img, bx - 12, by - 12, bx + 12, by + 12, GREEN)
    _arm(img, gx, gy, grip_open)
    return img


def wrist_view(exo: np.ndarray, gx: float, gy: float) -> np.ndarray:
    """A close-up that follows the gripper: an 80x60 crop below the palm, upscaled 4x."""
    x0 = int(np.clip(gx - 40, 0, W - 80))
    y0 = int(np.clip(gy - 22, 0, H - 60))
    crop = exo[y0:y0 + 60, x0:x0 + 80]
    return np.repeat(np.repeat(crop, 4, axis=0), 4, axis=1)


def write_mp4(path: Path, frames: list[np.ndarray]) -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build the TDM-93 video fixtures")
    h, w = frames[0].shape[:2]
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(int(FPS)), "-i", "pipe:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
           "-preset", "veryfast", "-metadata", f"comment={NOTE}", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True, timeout=180)
    return len(frames)


def render(scene: str, t, pix, grip, attached) -> tuple[list[np.ndarray], list[np.ndarray]]:
    exo, wrist = [], []
    obj = np.array(fx.CUP_REST if scene == "cup" else GREEN_REST, dtype=float)
    for i in range(len(t)):
        gx, gy = pix[i]
        if scene == "cup":
            if attached[i]:
                obj = np.array([gx, gy + 14])
            img = fx.render_frame(gx, gy, grip[i], (int(obj[0]), int(obj[1])))
        else:
            if attached[i]:
                obj = np.array([gx, gy + 10])
            img = render_blocks(gx, gy, grip[i], (int(obj[0]), int(obj[1])))
        exo.append(img)
        wrist.append(wrist_view(img, gx, gy))
    return exo, wrist


# --------------------------------------------------------------------------- episodes

@dataclass
class Fixture:
    name: str
    episode: dict  # an episode row as SQL / the bus would hand it over (kind=purchased)
    expect_accept: bool
    expect_failed_check: str | None  # llm_relevance | llm_success | None
    expect_reason: tuple[str, ...]  # any one of these must appear in the reason (case-insensitive)
    files: dict[str, Path] = field(default_factory=dict)


def make_episode(out: Path, name: str, plan, scene: str, *, episode_id: int = 1, seed: int = 0,
                 success: bool | None = True, ticket_id: int = 1, offer_id: int = 1) -> tuple[dict, dict[str, Path]]:
    d = (out / name).resolve()
    d.mkdir(parents=True, exist_ok=True)
    t, joints, pix, grip, attached = sample_plan(plan)
    jpos = fx.measured(joints, seed)
    n = len(t)
    exo, wrist = render(scene, t, pix, grip, attached)
    files = {"exo": d / "exo.mp4", "wrist": d / "wrist.mp4"}
    cams = {"exo": {"frames": write_mp4(files["exo"], exo), "fps": FPS, "width": W, "height": H},
            "wrist": {"frames": write_mp4(files["wrist"], wrist), "fps": FPS, "width": W, "height": H}}
    t_us = (fx.T0_US + np.rint(t * 1e6)).astype(np.int64)
    z = (240.0 - pix[:, 1]) / 1000.0
    rows = {
        "frame_index": np.arange(n, dtype=np.int32),
        "timestamp_us": t_us,
        "joint_pos": jpos.astype(np.float32),
        "gripper_pos": grip.astype(np.float32),
        "ik_target_pos": np.column_stack([pix[:, 0] / 1000.0, 0.3 - pix[:, 1] / 2000.0, z]).astype(np.float32),
        "ik_target_quat": np.tile([1.0, 0.0, 0.0, 0.0], (n, 1)).astype(np.float32),
        "gripper_cmd": grip.astype(np.float32),
        "control_source": ["human"] * n,
        "joint_cmd": joints.astype(np.float32),
    }
    meta = make_metadata(fps=FPS, joint_names=fx.JOINTS, cameras=cams, fixture=True, note=f"{NOTE} '{name}'")
    pq = d / "actions.parquet"
    write_actions(pq, rows, meta)
    files["actions"] = pq
    episode = {
        "id": episode_id, "ticket_id": ticket_id, "robot_id": 1, "task_id": 0, "contributor_id": 0,
        "dataset_offer_id": offer_id, "kind": "purchased", "review_status": "pending", "checkpoint_uri": None,
        "storage_uri": d.as_uri(),
        "cameras_json": json.dumps({"exo": files["exo"].as_uri(), "wrist": files["wrist"].as_uri()}),
        "actions_uri": pq.as_uri(), "frame_count": n, "duration_ms": int(round((t_us[-1] - t_us[0]) / 1000)),
        "takeover_ts": None, "handback_ts": None, "success": success, "created_at": int(t_us[-1]),
    }
    return episode, files


def build_fixture_set(out: Path, *, ticket_id: int = 1, offer_id: int = 1, first_episode_id: int = 1) -> list[Fixture]:
    out.mkdir(parents=True, exist_ok=True)
    specs = [
        ("cup_demo", CUP_PLAN, "cup", 11, True, None, ("",)),
        ("block_stacking", STACK_PLAN, "blocks", 12, False, "llm_relevance", ("block", "stack")),
        ("cup_missed", MISS_PLAN, "cup", 13, False, "llm_success", ("not completed", "cannot confirm", "never")),
    ]
    out_fx = []
    for i, (name, plan, scene, seed, acc, check, why) in enumerate(specs):
        ep, files = make_episode(out, name, plan, scene, episode_id=first_episode_id + i, seed=seed,
                                 ticket_id=ticket_id, offer_id=offer_id)
        out_fx.append(Fixture(name, ep, acc, check, why, files))
    return out_fx
