"""TDM-39: labelled synthetic YAM-shaped episodes for testing the LeRobot conversion.

Every file written here is marked ``fixture: true`` in the actions parquet metadata, so the
reviewer and the converter refuse it unless explicitly allowed. It is never robot data.

Shape: 6 joints + gripper, an ``exo`` and a ``wrist`` camera (H.264 mp4), 30 fps by default.
Each video frame carries its own frame number as a 10-bit black/white block code along the top
edge (``read_frame_number``), so a test can check which source frame landed where after cutting.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from rdf_common.episode_format import make_metadata, write_actions

FIXTURE_T0_US = 1_761_292_800_000_000  # 2025-10-24T08:00:00Z; any fixed epoch works
FIXTURE_TASK = "put the cup in the bin"
FIXTURE_REPO_ID = "rdf-fixtures/tdm39-yam"
CODE_BITS = 10
JOINT_NAMES = [f"joint_{i}" for i in range(1, 7)]


def _block(h: int, w: int) -> int:
    return max(8, min(h // 6, w // (CODE_BITS + 2)))


def draw_frame(i: int, h: int, w: int, cam: str) -> np.ndarray:
    """An RGB frame: per-camera background, a moving square, and frame number ``i`` in a block code."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[..., 0] = np.linspace(40, 160, w, dtype=np.uint8)[None, :]
    img[..., 1] = 90 if cam == "exo" else 150
    img[..., 2] = np.linspace(160, 40, h, dtype=np.uint8)[:, None]
    s = h // 5
    x = int((w - s) * (0.5 + 0.5 * np.sin(i / 15)))
    y = h // 2
    img[y:y + s, x:x + s] = (230, 200, 30)
    b = _block(h, w)
    for bit in range(CODE_BITS):
        val = 255 if (i >> bit) & 1 else 0
        img[0:b, bit * b:(bit + 1) * b] = val
    return img


def read_frame_number(rgb: np.ndarray) -> int:
    h, w = rgb.shape[:2]
    b = _block(h, w)
    n = 0
    for bit in range(CODE_BITS):
        cell = rgb[b // 4:3 * b // 4, bit * b + b // 4:(bit + 1) * b - b // 4].astype(np.float64)
        if cell.mean() > 127:
            n |= 1 << bit
    return n


def write_mp4(path: Path, n: int, fps: float, size: tuple[int, int], cam: str) -> None:
    import av
    from fractions import Fraction

    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = size
    with av.open(str(path), "w") as out:
        st = out.add_stream("libx264", rate=Fraction(fps).limit_denominator(1000), options={"crf": "18", "g": "15"})
        st.pix_fmt, st.height, st.width = "yuv420p", h, w
        for i in range(n):
            for pkt in st.encode(av.VideoFrame.from_ndarray(draw_frame(i, h, w, cam), format="rgb24")):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)


def write_fixture_episode(out_dir: str | Path, *, episode_id: str, kind: str, n_frames: int, fps: float = 30.0,
                          takeover_frame: int | None = None, handback_frame: int | None = None,
                          size: tuple[int, int] = (240, 320), cameras: Sequence[str] = ("exo", "wrist"),
                          with_joint_cmd: bool = True, t0_us: int = FIXTURE_T0_US) -> dict[str, Any]:
    """Write ``<out_dir>/<episode_id>/{exo,wrist}.mp4, actions.parquet``; return its manifest entry."""
    d = Path(out_dir) / episode_id
    d.mkdir(parents=True, exist_ok=True)
    i = np.arange(n_frames)
    t_us = t0_us + np.round(i * 1e6 / fps).astype(np.int64)
    tt = i / fps
    joint_pos = np.stack([0.3 * np.sin(0.8 * tt + k) + 0.1 * k for k in range(6)], axis=1).astype(np.float32)
    gripper_pos = (0.5 + 0.5 * np.cos(0.5 * tt)).astype(np.float32)
    # commanded = where the arm is one tick later, plus a small fixed lead so tests can tell it apart
    joint_cmd = np.concatenate([joint_pos[1:], joint_pos[-1:]], axis=0) + np.float32(0.001)
    gripper_cmd = np.clip(gripper_pos + 0.01, 0, 1).astype(np.float32)
    ik_pos = np.stack([0.3 + 0.05 * np.sin(tt), 0.05 * np.cos(tt), 0.2 + 0.02 * tt / max(tt[-1], 1)], axis=1)
    ik_quat = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (n_frames, 1))
    if kind == "recovery_takeover":
        tk = takeover_frame if takeover_frame is not None else n_frames // 3
        hb = handback_frame if handback_frame is not None else n_frames
        source = ["policy" if j < tk or j >= hb else "human" for j in i]
    else:
        tk = hb = None
        source = ["human"] * n_frames
    cols = {"frame_index": i.astype(np.int32), "timestamp_us": t_us, "joint_pos": joint_pos,
            "gripper_pos": gripper_pos, "ik_target_pos": ik_pos, "ik_target_quat": ik_quat,
            "gripper_cmd": gripper_cmd, "control_source": source}
    if with_joint_cmd:
        cols["joint_cmd"] = joint_cmd
    meta = make_metadata(fps=fps, joint_names=JOINT_NAMES, fixture=True,
                         cameras={c: {"frames": n_frames, "fps": fps, "width": size[1], "height": size[0]}
                                  for c in cameras},
                         note="TDM-39 synthetic fixture: labelled test data, not a robot recording")
    write_actions(d / "actions.parquet", cols, meta)
    for c in cameras:
        write_mp4(d / f"{c}.mp4", n_frames, fps, size, c)
    entry: dict[str, Any] = {"episode_id": episode_id, "kind": kind,
                             "cameras": {c: str(d / f"{c}.mp4") for c in cameras},
                             "actions_uri": str(d / "actions.parquet")}
    if kind == "recovery_takeover":
        entry["takeover_ts"] = int(t_us[tk])
        entry["handback_ts"] = int(t_us[hb]) if hb < n_frames else int(t_us[-1] + 1e6 / fps)
    return entry


# The 2-episode fixture the unit test and the proof use.
CLEAN_FRAMES = 90  # 3 s clean teleop demo
RECOVERY_FRAMES = 150  # 5 s: policy fails 0-59, human recovers 60-134, policy again 135-149
TAKEOVER_FRAME = 60
HANDBACK_FRAME = 135


def write_fixture_manifest(out_dir: str | Path, *, size: tuple[int, int] = (240, 320),
                           repo_id: str = FIXTURE_REPO_ID) -> dict[str, Any]:
    out_dir = Path(out_dir)
    eps = [
        write_fixture_episode(out_dir, episode_id="fixture-clean-0", kind="clean_teleop", n_frames=CLEAN_FRAMES,
                              size=size),
        write_fixture_episode(out_dir, episode_id="fixture-recovery-1", kind="recovery_takeover",
                              n_frames=RECOVERY_FRAMES, takeover_frame=TAKEOVER_FRAME,
                              handback_frame=HANDBACK_FRAME, size=size, with_joint_cmd=False),
    ]
    manifest = {"repo_id": repo_id, "task": FIXTURE_TASK, "episodes": eps, "allow_fixtures": True}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
