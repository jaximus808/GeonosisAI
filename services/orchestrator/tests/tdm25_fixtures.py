"""TDM-25 reviewer FIXTURES: synthetic episodes, NOT real robot data.

Every file written here is labelled as a fixture (parquet metadata ``fixture: true``, note
"TDM-25 synthetic fixture"). The video is a rendered cartoon of the task "put the red cup in the
blue bin" (top-down-ish exo view: wall, table, a grey arm with a two-finger gripper, a red cup, a
blue bin); the joint trajectories are min-jerk moves with YAM-like encoder noise.

The five-episode set the TDM-25 "done when" names (``build_fixture_set``):

    clean_demo        clean_teleop, 10 s, smooth, cup ends in bin, success   -> ACCEPT
    too_short         clean_teleop, 1.2 s                                     -> reject: duration
    jerky             clean_teleop, 10 s, 4-7 Hz tremor on every joint        -> reject: jerk
    takeover_no_handback  recovery_takeover, takeover_ts set, handback_ts None -> reject: takeover_timestamps
    off_task          clean_teleop, 10 s, smooth, but the arm pushes a green block around and never
                      touches the cup (kinematically clean, success flag true) -> reject: Claude judgement

``make_episode(...)`` builds variants for the unit tests (frame drops, a valid takeover, ...).
Deterministic: fixed seeds, so thresholds calibrated on these stay put.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rdf_common.episode_format import make_metadata, write_actions

FPS = 30.0
W, H = 320, 240
T0_US = 1_792_900_000_000_000  # fixed epoch for reproducible timestamps (2026-10-24)
JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
TASK = "Put the red cup in the blue bin."
ENCODER_LSB = 2 * np.pi / 16384  # 14-bit encoder quantisation
NOISE_RAD = 1e-4

# joint-space poses (rad) and matching image-space gripper positions (px) for each named pose
POSES = {
    "home": (np.array([0.0, -0.6, 1.2, 0.0, 0.6, 0.0]), (160, 60)),
    "above_cup": (np.array([0.55, -0.2, 0.9, 0.1, 0.8, 0.3]), (90, 125)),
    "at_cup": (np.array([0.55, 0.05, 0.75, 0.1, 0.95, 0.3]), (90, 152)),
    "lift": (np.array([0.55, -0.25, 0.95, 0.1, 0.75, 0.3]), (90, 112)),
    "above_bin": (np.array([-0.6, -0.25, 0.95, -0.1, 0.75, -0.2]), (240, 112)),
    "in_bin": (np.array([-0.6, -0.05, 0.85, -0.1, 0.9, -0.2]), (240, 138)),
    "beside_cup": (np.array([0.42, 0.05, 0.75, 0.1, 0.95, 0.3]), (68, 152)),
    "block_a": (np.array([0.2, 0.0, 0.8, 0.0, 0.9, 0.0]), (150, 170)),
    "block_b": (np.array([0.3, 0.05, 0.8, 0.0, 0.9, 0.0]), (140, 205)),
    "block_c": (np.array([-0.1, 0.05, 0.8, 0.0, 0.9, 0.0]), (185, 210)),
}

# (until_s, pose, gripper_open 0..1, cup_attached) segments; motion between consecutive poses is min-jerk
CLEAN_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "above_cup", 1.0, False), (3.3, "at_cup", 1.0, False),
    (3.8, "at_cup", 0.0, False), (4.6, "lift", 0.0, True), (7.0, "above_bin", 0.0, True),
    (7.5, "in_bin", 0.0, True), (8.0, "in_bin", 1.0, False), (10.0, "home", 1.0, False),
]
SHORT_PLAN = [(0.3, "home", 1.0, False), (1.2, "above_cup", 1.0, False)]
# policy misses the grasp (closes beside the cup) then a human takes over at 4.0 s and finishes
TAKEOVER_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "above_cup", 1.0, False), (3.2, "beside_cup", 1.0, False),
    (3.6, "beside_cup", 0.0, False), (4.0, "beside_cup", 0.0, False),          # failure: closed on air
    (4.6, "beside_cup", 1.0, False), (5.4, "at_cup", 1.0, False), (5.9, "at_cup", 0.0, False),
    (6.7, "lift", 0.0, True), (8.8, "above_bin", 0.0, True), (9.3, "in_bin", 0.0, True),
    (9.8, "in_bin", 1.0, False), (11.5, "home", 1.0, False),
]
TAKEOVER_AT_S, HANDBACK_AT_S = 4.0, 9.8
# off-task: pushes a green block around the front of the table; cup and bin untouched
OFF_TASK_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "block_a", 0.0, False), (4.5, "block_b", 0.0, False),
    (6.5, "block_c", 0.0, False), (8.0, "block_a", 0.0, False), (10.0, "home", 1.0, False),
]

CUP_REST = (90, 160)
BIN = (240, 150)


@dataclass
class Fixture:
    name: str
    episode: dict  # an episode row as the bus / SQL would hand it to the reviewer
    expect_accept: bool
    expect_check: str | None  # the deterministic check that must fail, "claude", or None for accept
    expect_reason: str  # substring the review reason must contain (case-insensitive)
    files: dict[str, Path] = field(default_factory=dict)


# --------------------------------------------------------------------------- kinematics

def _minjerk(s: np.ndarray) -> np.ndarray:
    s = np.clip(s, 0.0, 1.0)
    return 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5


def sample_plan(plan, fps: float = FPS):
    """-> t (s), joints (N, 6), pix (N, 2), grip (N,), cup_attached (N,) bool."""
    end = plan[-1][0]
    t = np.arange(0.0, end + 1e-9, 1.0 / fps)
    joints = np.zeros((len(t), 6))
    pix = np.zeros((len(t), 2))
    grip = np.zeros(len(t))
    attached = np.zeros(len(t), dtype=bool)
    prev_t, prev_pose, prev_grip = 0.0, plan[0][1], plan[0][2]
    for until, pose, g, att in plan:
        m = (t >= prev_t - 1e-9) & (t <= until + 1e-9)
        s = _minjerk((t[m] - prev_t) / max(until - prev_t, 1e-6))
        j0, p0 = POSES[prev_pose]
        j1, p1 = POSES[pose]
        joints[m] = j0 + np.outer(s, j1 - j0)
        pix[m] = np.array(p0) + np.outer(s, np.array(p1) - np.array(p0))
        grip[m] = prev_grip + s * (g - prev_grip)
        attached[m] = att
        prev_t, prev_pose, prev_grip = until, pose, g
    return t, joints, pix, grip, attached


def measured(joints: np.ndarray, seed: int, tremor_rad: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = joints + rng.normal(0.0, NOISE_RAD, joints.shape)
    if tremor_rad:
        n = len(joints)
        tt = np.arange(n) / FPS
        for j in range(joints.shape[1]):
            f1, f2 = rng.uniform(4.0, 7.0, 2)
            out[:, j] += tremor_rad * (np.sin(2 * np.pi * f1 * tt + rng.uniform(0, 6)) +
                                       0.6 * np.sin(2 * np.pi * f2 * tt + rng.uniform(0, 6)))
    return np.round(out / ENCODER_LSB) * ENCODER_LSB


# --------------------------------------------------------------------------- rendering

WALL, TABLE, TABLE_EDGE = (205, 210, 218), (196, 160, 112), (150, 112, 70)
ARM, ARM_DARK, CUP, CUP_RIM = (120, 124, 130), (70, 72, 78), (205, 30, 35), (140, 15, 20)
BIN_C, BIN_DARK, BLOCK = (35, 80, 200), (20, 45, 130), (40, 165, 70)


def _rect(img, x0, y0, x1, y1, c):
    x0, x1 = int(max(0, min(x0, x1))), int(min(W, max(x0, x1)))
    y0, y1 = int(max(0, min(y0, y1))), int(min(H, max(y0, y1)))
    if x1 > x0 and y1 > y0:
        img[y0:y1, x0:x1] = c


def _line(img, p0, p1, width, c):
    n = int(max(abs(p1[0] - p0[0]), abs(p1[1] - p0[1]))) + 1
    for s in np.linspace(0, 1, n):
        x, y = p0[0] + s * (p1[0] - p0[0]), p0[1] + s * (p1[1] - p0[1])
        _rect(img, x - width / 2, y - width / 2, x + width / 2, y + width / 2, c)


def _cup(img, cx, cy):
    _rect(img, cx - 10, cy - 26, cx + 10, cy, CUP)
    _rect(img, cx - 11, cy - 28, cx + 11, cy - 24, CUP_RIM)
    _rect(img, cx + 10, cy - 20, cx + 15, cy - 8, CUP_RIM)  # handle


def _bin_back(img):
    bx, by = BIN
    _rect(img, bx - 32, by - 40, bx + 32, by + 4, BIN_DARK)


def _bin_front(img):
    bx, by = BIN
    _rect(img, bx - 34, by - 18, bx + 34, by + 6, BIN_C)
    _rect(img, bx - 34, by - 40, bx - 28, by + 6, BIN_C)
    _rect(img, bx + 28, by - 40, bx + 34, by + 6, BIN_C)


def render_frame(gx, gy, grip_open, cup_xy, block_xy=None):
    img = np.empty((H, W, 3), dtype=np.uint8)
    img[:] = WALL
    _rect(img, 0, 80, W, H, TABLE)
    _rect(img, 0, 80, W, 84, TABLE_EDGE)
    _bin_back(img)
    if block_xy is not None:
        bx, by = block_xy
        _rect(img, bx - 12, by - 12, bx + 12, by + 12, BLOCK)
    cup_in_bin = cup_xy[0] > BIN[0] - 30 and cup_xy[0] < BIN[0] + 30 and cup_xy[1] > BIN[1] - 30
    if not cup_in_bin:
        _cup(img, *cup_xy)
    else:
        _cup(img, cup_xy[0], min(cup_xy[1], BIN[1] - 2))
    _bin_front(img)
    # arm: shoulder at the top centre, elbow offset, wrist above the gripper
    sh = (160, 0)
    el = ((sh[0] + gx) / 2 + 30, (sh[1] + gy) / 2 - 10)
    wr = (gx, gy - 18)
    _line(img, sh, el, 12, ARM)
    _line(img, el, wr, 10, ARM)
    _rect(img, gx - 14, gy - 22, gx + 14, gy - 14, ARM_DARK)  # gripper palm
    spread = 4 + 9 * grip_open
    _rect(img, gx - spread - 3, gy - 16, gx - spread, gy + 4, ARM_DARK)
    _rect(img, gx + spread, gy - 16, gx + spread + 3, gy + 4, ARM_DARK)
    return img


def render_video(path: Path, t, pix, grip, attached, *, seed: int, jitter_px: float = 0.0,
                 block_path: np.ndarray | None = None) -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build the TDM-25 video fixtures")
    rng = np.random.default_rng(seed + 1000)
    cup = np.array(CUP_REST, dtype=float)
    frames = []
    for i in range(len(t)):
        gx, gy = pix[i]
        if jitter_px:
            gx += rng.normal(0, jitter_px)
            gy += rng.normal(0, jitter_px)
        if attached[i]:
            cup = np.array([gx, gy + 14])
        elif cup[1] < CUP_REST[1] - 1 and not (abs(cup[0] - BIN[0]) < 30):
            cup = np.array([cup[0], CUP_REST[1]])  # dropped: falls back to the table
        frames.append(render_frame(gx, gy, grip[i], (int(cup[0]), int(cup[1])),
                                   None if block_path is None else tuple(block_path[i].astype(int))))
    cmd = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(int(FPS)), "-i", "pipe:0", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28",
           "-preset", "veryfast", "-metadata", "comment=TDM-25 synthetic fixture, not real robot data", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True, timeout=120)
    return len(frames)


# --------------------------------------------------------------------------- episodes

def make_episode(out: Path, name: str, plan, *, kind: str = "clean_teleop", episode_id: int = 1, seed: int = 0,
                 tremor_rad: float = 0.0, jitter_px: float = 0.0, success: bool | None = True,
                 takeover_s: float | None = None, handback_s: float | None = None,
                 drop_every: int = 0, video: bool = True, block: bool = False,
                 control_source: str = "human", ticket_id: int = 1) -> tuple[dict, dict[str, Path]]:
    """Write one fixture episode (actions.parquet [+ exo.mp4]) under ``out/name`` and return
    (episode row, files)."""
    d = (out / name).resolve()
    d.mkdir(parents=True, exist_ok=True)
    t, joints, pix, grip, attached = sample_plan(plan)
    jpos = measured(joints, seed, tremor_rad)
    n = len(t)
    src = np.array([control_source] * n, dtype=object)
    if takeover_s is not None:
        src[t < takeover_s] = "policy"
        src[t >= takeover_s] = "human"
        if handback_s is not None:
            src[t >= handback_s] = "policy"
    keep = np.ones(n, dtype=bool)
    if drop_every:
        keep[np.arange(n) % drop_every == drop_every - 1] = False
    t_us = (T0_US + np.rint(t * 1e6)).astype(np.int64)
    block_path = None
    if block:
        block_path = np.clip(pix + np.array([0, 22]), 0, [W - 1, H - 1])
    files: dict[str, Path] = {}
    cams: dict[str, Any] = {}
    if video:
        mp4 = d / "exo.mp4"
        frames = render_video(mp4, t, pix, grip, attached, seed=seed, jitter_px=jitter_px, block_path=block_path)
        files["exo"] = mp4
        cams["exo"] = {"frames": frames, "fps": FPS, "width": W, "height": H}
    rows = {
        "frame_index": np.arange(int(keep.sum()), dtype=np.int32),
        "timestamp_us": t_us[keep],
        "joint_pos": jpos[keep].astype(np.float32),
        "gripper_pos": grip[keep].astype(np.float32),
        "ik_target_pos": np.column_stack([pix[keep] / 1000.0, np.full(int(keep.sum()), 0.1)]).astype(np.float32),
        "ik_target_quat": np.tile([1.0, 0.0, 0.0, 0.0], (int(keep.sum()), 1)).astype(np.float32),
        "gripper_cmd": grip[keep].astype(np.float32),
        "control_source": list(src[keep]),
        "joint_cmd": joints[keep].astype(np.float32),
    }
    meta = make_metadata(fps=FPS, joint_names=JOINTS, cameras=cams, fixture=True,
                         note=f"TDM-25 synthetic fixture '{name}', not real robot data")
    pq = d / "actions.parquet"
    write_actions(pq, rows, meta)
    files["actions"] = pq
    dur_ms = int(round((t_us[keep][-1] - t_us[keep][0]) / 1000))
    episode = {
        "id": episode_id, "ticket_id": ticket_id, "robot_id": 1, "task_id": 0, "contributor_id": 0,
        "dataset_offer_id": 0, "kind": kind, "review_status": "pending", "checkpoint_uri": None,
        "storage_uri": d.as_uri(), "cameras_json": json.dumps({k: v.as_uri() for k, v in files.items() if k != "actions"}),
        "actions_uri": pq.as_uri(), "frame_count": int(keep.sum()), "duration_ms": dur_ms,
        "takeover_ts": None if takeover_s is None else int(T0_US + takeover_s * 1e6),
        "handback_ts": None if handback_s is None else int(T0_US + handback_s * 1e6),
        "success": success, "created_at": int(t_us[-1]),
    }
    return episode, files


def build_fixture_set(out: Path, *, ticket_id: int = 1, first_episode_id: int = 1) -> list[Fixture]:
    """The five TDM-25 fixtures (see module docstring)."""
    out.mkdir(parents=True, exist_ok=True)
    specs = [
        ("clean_demo", CLEAN_PLAN, dict(seed=1), True, None, ""),
        ("too_short", SHORT_PLAN, dict(seed=2), False, "duration", "minimum is 2"),
        ("jerky", CLEAN_PLAN, dict(seed=3, tremor_rad=0.02, jitter_px=2.5), False, "jerk", "jerky"),
        ("takeover_no_handback", TAKEOVER_PLAN, dict(seed=4, kind="recovery_takeover", takeover_s=TAKEOVER_AT_S,
                                                     handback_s=None), False, "takeover_timestamps", "handback_ts"),
        ("off_task", OFF_TASK_PLAN, dict(seed=5, block=True), False, "claude", ""),
    ]
    fixtures = []
    for i, (name, plan, kw, acc, check, why) in enumerate(specs):
        ep, files = make_episode(out, name, plan, episode_id=first_episode_id + i, ticket_id=ticket_id, **kw)
        fixtures.append(Fixture(name, ep, acc, check, why, files))
    return fixtures
