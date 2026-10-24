"""TDM-39: our recorded episodes -> a per-ticket LeRobot v2.1 dataset openpi can train on.

Input (one manifest per training run, see ``ConvertManifest``): the episodes TDM-25 defined in
``rdf_common.episode_format``. Each one is a ``<camera>.mp4`` per camera plus an ``actions.parquet``
with one row per control tick (``frame_index`` = the video frame number, ``timestamp_us``,
``joint_pos``, ``gripper_pos``, ``ik_target_pos/quat``, ``gripper_cmd``, ``control_source``,
optional ``joint_cmd``) and, for takeovers, the episode row's ``takeover_ts`` / ``handback_ts``
on the same clock as ``timestamp_us``.

Output: ``<lerobot_root>/<repo_id>/`` in LeRobot format **v2.1** (the version openpi ``215abfb``
pins through ``lerobot@0cf8648``). openpi reads it with ``LeRobotDataset(repo_id)`` when
``HF_LEROBOT_HOME=<lerobot_root>``. Also ``<assets_root>/<repo_id>/norm_stats.json`` in openpi's
format (the file ``scripts/compute_norm_stats.py`` would write for the S-05a config), so training
can skip that step. The feature schema matches the S-05a decision (``yam_dataset_spec``):

    observation.images.top    video  HxWx3  (our "exo"/"top" camera; h264, yuv420p)
    observation.images.wrist  video  HxWx3
    observation.state         float32[7]  joint_pos[0:6] (rad) + gripper_pos (0 closed .. 1 open)
    action                    float32[7]  absolute joint targets (rad) + gripper_cmd. openpi turns
                                          joints 0-5 into deltas; the gripper stays absolute.
    action.ee_target          float32[8]  commanded IK target xyz + quat wxyz + gripper_cmd (kept for
                                          an EE-space policy later; openpi ignores it)
    task                      str via tasks.jsonl -> openpi ``prompt``
    fps                       30

Joint action = ``joint_cmd`` (the IK solution actually sent) when recorded, else the next tick's
measured ``joint_pos`` (the last tick repeats its own).

Takeover cut (CONTEXT.md: "failure frames before takeover cut"): for ``recovery_takeover``
episodes every tick with ``timestamp_us < takeover_ts`` is dropped. Ticks at or after
``handback_ts`` (the policy driving again) are dropped too unless ``keep_after_handback``; the
default trains only on the human recovery segment. If ``takeover_ts`` is missing the first
``control_source == "human"`` tick is used; if there is none the episode is rejected.

Resampling: ticks are picked nearest-neighbour onto a uniform ``fps`` grid, so a 30 fps recording
passes through 1:1 and a 60 fps one keeps every other tick. The video frame for each output frame
is that tick's ``frame_index``. Videos are re-encoded (cutting and resampling need it anyway).

Base-data mixing: openpi trains on one ``repo_id``, so mixing happens here. ``base_episodes`` (any
episodes in our format, e.g. accepted demos from earlier tickets) are appended until they make up
about ``base_mix_ratio`` of the output frames. 0 (the default) means ticket data only.

Runs locally on CPU (``python -m rdf_training.convert --manifest m.json``, or ``--fixture`` to
generate and convert two labelled synthetic episodes) and inside Modal via ``modal_convert`` / ``convert_episodes`` (TDM-38
owns the app, image and volumes). Needs numpy, pyarrow and av (PyAV); no torch, lerobot or openpi.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from rdf_common.episode_format import EpisodeActions, cameras_from_json, read_actions
from rdf_common.review_checks import ts_us

CONVERTER = "rdf_training.convert"
CONVERTER_VERSION = 1
CODEBASE_VERSION = "v2.1"  # LeRobot dataset format; must match lerobot@0cf8648 (openpi 215abfb)
DEFAULT_FPS = 30
CHUNKS_SIZE = 1000
DATA_PATH = "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
VIDEO_PATH = "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
TAKEOVER_KINDS = ("recovery_takeover",)
# our camera name -> LeRobot image key (S-05a: base camera is "top", wrist camera is "wrist")
DEFAULT_CAMERA_MAP = {"exo": "observation.images.top", "top": "observation.images.top",
                      "wrist": "observation.images.wrist"}
DEFAULT_JOINT_NAMES = [f"joint_{i}" for i in range(1, 7)]
# openpi config values the norm stats depend on (S-05a / rdf_s05a_config.yam_dataset_spec)
ACTION_HORIZON = 50
JOINT_DIMS = 6
VIDEO_CODEC = "libx264"  # LeRobot accepts h264; GOP 2 keeps random-access decode cheap (as LeRobot does)
VIDEO_OPTIONS = {"g": "2", "crf": "23", "preset": "veryfast"}
IMAGE_STATS_SAMPLES = 100


class ConvertError(ValueError):
    pass


# --------------------------------------------------------------------------- manifest

@dataclass
class EpisodeSource:
    """One episode to convert. ``cameras`` maps our camera name -> mp4 uri."""

    episode_id: str
    kind: str
    cameras: dict[str, str]
    actions_uri: str
    takeover_ts: int | None = None
    handback_ts: int | None = None
    task: str | None = None  # overrides the manifest task for this episode

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "EpisodeSource":
        cams = d.get("cameras")
        if cams is None:
            cams = cameras_from_json(d.get("cameras_json"))
        else:
            cams = cameras_from_json(cams)
        return cls(episode_id=str(d.get("episode_id", d.get("id"))), kind=str(d.get("kind", "clean_teleop")),
                   cameras=cams, actions_uri=str(d["actions_uri"]),
                   takeover_ts=ts_us(d.get("takeover_ts")), handback_ts=ts_us(d.get("handback_ts")),
                   task=d.get("task"))


@dataclass
class ConvertManifest:
    repo_id: str  # e.g. "rdf/ticket-12-run-3"; the dataset dir and the openpi asset id
    task: str  # the prompt, e.g. "put the cup in the bin"
    episodes: list[EpisodeSource]
    fps: int = DEFAULT_FPS
    camera_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CAMERA_MAP))
    keep_after_handback: bool = False
    base_episodes: list[EpisodeSource] = field(default_factory=list)
    base_mix_ratio: float = 0.0
    image_size: tuple[int, int] | None = None  # (height, width) to resize to; None keeps the source size
    robot_type: str = "i2rt_yam"
    allow_fixtures: bool = False  # synthetic fixture episodes are refused unless this is set
    action_horizon: int = ACTION_HORIZON
    joint_dims: int = JOINT_DIMS

    @classmethod
    def from_mapping(cls, d: Mapping[str, Any]) -> "ConvertManifest":
        d = dict(d)
        eps = [EpisodeSource.from_mapping(e) for e in d.pop("episodes")]
        base = [EpisodeSource.from_mapping(e) for e in d.pop("base_episodes", []) or []]
        if d.get("image_size") is not None:
            d["image_size"] = tuple(int(x) for x in d["image_size"])
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ConvertError(f"unknown manifest keys {sorted(unknown)}")
        return cls(episodes=eps, base_episodes=base, **d)

    def to_dict(self) -> dict:
        return asdict(self)


def manifest_from_episode_rows(rows: Iterable[Mapping[str, Any]], *, repo_id: str, task: str,
                               accepted_only: bool = True, **kw) -> ConvertManifest:
    """Build a manifest from SpacetimeDB ``episode`` rows (dicts with the server column names)."""
    eps = []
    for r in rows:
        if accepted_only and r.get("review_status") != "accepted":
            continue
        eps.append(EpisodeSource.from_mapping({**r, "episode_id": r.get("id")}))
    return ConvertManifest(repo_id=repo_id, task=task, episodes=eps, **kw)


# --------------------------------------------------------------------------- uri resolution

DEFAULT_MOUNTS = {"rdf-media": "/media"}  # Modal Volume rdf-media mounted at /media (TDM-4)


def resolve_uri(uri: str, *, mounts: Mapping[str, str] | None = None, tmp_dir: Path | None = None) -> Path:
    """Local path for a storage uri: plain paths, ``file://``, ``modal-vol://<bucket>/<key>`` (read from
    the mounted Volume) and ``http(s)://`` (e.g. a signed GET url, downloaded to ``tmp_dir``)."""
    mounts = {**DEFAULT_MOUNTS, **(mounts or {})}
    if "://" not in uri:
        return Path(uri)
    scheme, rest = uri.split("://", 1)
    if scheme == "file":
        return Path(rest)
    if scheme in ("modal-vol", "s3"):
        bucket, _, key = rest.partition("/")
        if bucket not in mounts:
            raise ConvertError(f"no local mount for bucket {bucket!r} (uri {uri}); pass mounts={{bucket: path}}")
        return Path(mounts[bucket]) / key
    if scheme in ("http", "https"):
        tmp_dir = Path(tmp_dir or tempfile.mkdtemp(prefix="rdf-tdm39-"))
        dest = tmp_dir / f"{abs(hash(uri))}_{Path(rest.split('?')[0]).name}"
        if not dest.exists():
            with urllib.request.urlopen(uri, timeout=120) as r, open(dest, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f)
        return dest
    raise ConvertError(f"unsupported uri scheme {scheme!r}: {uri}")


# --------------------------------------------------------------------------- frame selection

@dataclass
class EpisodePlan:
    """Which source ticks become output frames, and why."""

    source: EpisodeSource
    rows: np.ndarray  # source row indices, one per output frame
    src_frames: int
    src_fps: float
    cut_before_takeover: int = 0
    cut_after_handback: int = 0
    note: str = ""

    @property
    def length(self) -> int:
        return int(len(self.rows))


def takeover_window(src: EpisodeSource, act: EpisodeActions, keep_after_handback: bool) -> tuple[int, int]:
    """Half-open [start, end) source row range to keep. Raises ConvertError for unusable takeovers."""
    n = act.n
    if src.kind not in TAKEOVER_KINDS:
        return 0, n
    t = act.timestamp_us
    tk = src.takeover_ts
    if tk is None:
        human = [i for i, s in enumerate(act.control_source) if s == "human"]
        if not human:
            raise ConvertError(f"episode {src.episode_id}: takeover episode has no takeover_ts and no human ticks")
        tk = int(t[human[0]])
    start = int(np.searchsorted(t, tk, side="left"))  # first tick at/after takeover
    end = n
    if not keep_after_handback and src.handback_ts is not None:
        end = int(np.searchsorted(t, src.handback_ts, side="left"))  # ticks before handback
    if end - start < 2:
        raise ConvertError(f"episode {src.episode_id}: only {max(0, end - start)} ticks between takeover and handback")
    return start, end


def resample_rows(t_us: np.ndarray, start: int, end: int, fps: float) -> np.ndarray:
    """Nearest source row for each point of a uniform ``fps`` grid over rows [start, end)."""
    t = t_us[start:end].astype(np.int64)
    step = 1e6 / fps
    n_out = int(math.floor((t[-1] - t[0]) / step + 1e-6)) + 1
    grid = t[0] + np.round(np.arange(n_out) * step).astype(np.int64)
    idx = np.searchsorted(t, grid)
    idx = np.clip(idx, 1, len(t) - 1)
    left, right = t[idx - 1], t[idx]
    idx = np.where(grid - left <= right - grid, idx - 1, idx)
    return (idx + start).astype(np.int64)


def plan_episode(src: EpisodeSource, act: EpisodeActions, *, fps: float, keep_after_handback: bool) -> EpisodePlan:
    start, end = takeover_window(src, act, keep_after_handback)
    dt = np.diff(act.timestamp_us[start:end]) / 1e6
    on_grid = (abs(act.fps - fps) < 1e-6 and np.all(np.diff(act.frame_index[start:end]) == 1)
               and np.all(np.abs(dt * fps - 1) < 0.25))  # no dropped ticks
    if on_grid:
        rows = np.arange(start, end, dtype=np.int64)  # already on the grid: pass through 1:1
    else:
        rows = resample_rows(act.timestamp_us, start, end, fps)
    return EpisodePlan(source=src, rows=rows, src_frames=act.n, src_fps=act.fps,
                       cut_before_takeover=start, cut_after_handback=act.n - end)


# --------------------------------------------------------------------------- per-episode arrays

def episode_arrays(act: EpisodeActions, rows: np.ndarray) -> dict[str, np.ndarray]:
    """state / action / action.ee_target for the selected rows (float32)."""
    jp = act.joint_pos
    grip_pos = np.asarray(act.extra["gripper_pos"], dtype=np.float64)
    grip_cmd = np.asarray(act.extra["gripper_cmd"], dtype=np.float64)
    if "joint_cmd" in act.extra and act.extra["joint_cmd"].shape == jp.shape:
        joint_target = act.extra["joint_cmd"]
    else:  # next measured position; the last tick holds
        joint_target = np.concatenate([jp[1:], jp[-1:]], axis=0)
    state = np.concatenate([jp, grip_pos[:, None]], axis=1)
    action = np.concatenate([joint_target, grip_cmd[:, None]], axis=1)
    ee = np.concatenate([act.extra["ik_target_pos"], act.extra["ik_target_quat"], grip_cmd[:, None]], axis=1)
    return {"observation.state": state[rows].astype(np.float32),
            "action": action[rows].astype(np.float32),
            "action.ee_target": ee[rows].astype(np.float32)}


# --------------------------------------------------------------------------- video

def transcode_frames(src_mp4: Path, dst_mp4: Path, frame_numbers: np.ndarray, fps: int,
                     image_size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Write ``dst_mp4`` whose k-th frame is source frame ``frame_numbers[k]`` (non-decreasing).
    Missing trailing source frames raise. Returns {height, width, frames, image_stats_samples}."""
    import av

    frame_numbers = np.asarray(frame_numbers, dtype=np.int64)
    if len(frame_numbers) == 0 or np.any(np.diff(frame_numbers) < 0):
        raise ConvertError("frame numbers must be non-empty and non-decreasing")
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    sample_at = set(np.round(np.linspace(0, len(frame_numbers) - 1,
                                         min(IMAGE_STATS_SAMPLES, len(frame_numbers)))).astype(int).tolist())
    samples: list[np.ndarray] = []
    out_k = 0
    hw: tuple[int, int] | None = None
    with av.open(str(src_mp4)) as inp, av.open(str(dst_mp4), "w") as out:
        stream = None
        for src_i, frame in enumerate(inp.decode(video=0)):
            if out_k >= len(frame_numbers):
                break
            if src_i < frame_numbers[out_k]:
                continue
            if image_size is not None:
                frame = frame.reformat(width=image_size[1], height=image_size[0])
            rgb = frame.to_ndarray(format="rgb24")
            if stream is None:
                hw = (rgb.shape[0], rgb.shape[1])
                stream = out.add_stream(VIDEO_CODEC, rate=fps, options=dict(VIDEO_OPTIONS))
                stream.pix_fmt = "yuv420p"
                stream.height, stream.width = hw
            while out_k < len(frame_numbers) and frame_numbers[out_k] == src_i:
                if out_k in sample_at:
                    samples.append(rgb[::4, ::4])  # stats only; LeRobot also downsamples
                vf = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                for pkt in stream.encode(vf):
                    out.mux(pkt)
                out_k += 1
        if stream is None or out_k < len(frame_numbers):
            raise ConvertError(f"{src_mp4.name}: needed source frame {int(frame_numbers[min(out_k, len(frame_numbers) - 1)])}"
                               f" but the video ended (wrote {out_k}/{len(frame_numbers)})")
        for pkt in stream.encode():
            out.mux(pkt)
    imgs = np.stack(samples).astype(np.float64) / 255.0  # (S, h, w, 3)
    stats = {"min": imgs.min(axis=(0, 1, 2)), "max": imgs.max(axis=(0, 1, 2)),
             "mean": imgs.mean(axis=(0, 1, 2)), "std": imgs.std(axis=(0, 1, 2))}
    return {"height": hw[0], "width": hw[1], "frames": out_k,
            "image_stats": {k: v.reshape(3, 1, 1).tolist() for k, v in stats.items()} | {"count": [len(samples)]}}


def count_video_frames(mp4: Path) -> int:
    import av

    with av.open(str(mp4)) as c:
        return sum(1 for _ in c.decode(video=0))


def video_info(mp4: Path) -> dict[str, Any]:
    """The ``info`` block LeRobot's ``get_video_info`` writes for a video feature."""
    import av

    with av.open(str(mp4)) as c:
        s = c.streams.video[0]
        return {"video.height": s.height, "video.width": s.width, "video.codec": s.codec_context.codec.canonical_name,
                "video.pix_fmt": s.pix_fmt, "video.is_depth_map": False, "video.fps": int(s.base_rate),
                "video.channels": 3, "has_audio": False}


# --------------------------------------------------------------------------- parquet + stats

def _feature_stats(x: np.ndarray) -> dict[str, list]:
    x = np.asarray(x, dtype=np.float64)
    keep = x.ndim == 1
    return {"min": np.min(x, axis=0, keepdims=keep).tolist(), "max": np.max(x, axis=0, keepdims=keep).tolist(),
            "mean": np.mean(x, axis=0, keepdims=keep).tolist(), "std": np.std(x, axis=0, keepdims=keep).tolist(),
            "count": [int(len(x))]}


def _hf_features(features: Mapping[str, Mapping[str, Any]]) -> dict:
    """The HF ``datasets`` feature dict LeRobot's ``to_parquet`` embeds (Value / Sequence(length))."""
    out = {}
    for k, ft in features.items():
        if ft["dtype"] in ("video", "image"):
            continue
        if tuple(ft["shape"]) == (1,):
            out[k] = {"dtype": ft["dtype"], "_type": "Value"}
        else:
            out[k] = {"feature": {"dtype": ft["dtype"], "_type": "Value"}, "length": int(ft["shape"][0]),
                      "_type": "Sequence"}
    return out


def write_episode_parquet(path: Path, cols: Mapping[str, np.ndarray], features: Mapping[str, Mapping[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    arrays, fields = [], []
    for k, ft in features.items():
        if ft["dtype"] in ("video", "image"):
            continue
        v = np.asarray(cols[k])
        if tuple(ft["shape"]) == (1,):
            typ = pa.float32() if ft["dtype"] == "float32" else pa.int64()
            arrays.append(pa.array(v.reshape(-1), type=typ))
        else:
            typ = pa.list_(pa.float32())
            arrays.append(pa.array(list(v.astype(np.float32)), type=typ))
        fields.append(pa.field(k, typ))
    md = {b"huggingface": json.dumps({"info": {"features": _hf_features(features)}}).encode()}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_arrays(arrays, schema=pa.schema(fields, metadata=md)), path)


class _OpenpiRunningStats:
    """Same statistics as ``openpi.shared.normalize.RunningStats`` given all data in one batch."""

    BINS = 5000

    @classmethod
    def compute(cls, x: np.ndarray) -> dict[str, list[float]]:
        x = np.asarray(x, dtype=np.float64).reshape(-1, x.shape[-1])
        mean = x.mean(axis=0)
        std = np.sqrt(np.maximum(0.0, (x**2).mean(axis=0) - mean**2))
        lo, hi = x.min(axis=0), x.max(axis=0)
        q01, q99 = [], []
        for i in range(x.shape[1]):
            edges = np.linspace(lo[i] - 1e-10, hi[i] + 1e-10, cls.BINS + 1)
            cum = np.cumsum(np.histogram(x[:, i], bins=edges)[0])
            q01.append(float(edges[np.searchsorted(cum, 0.01 * len(x))]))
            q99.append(float(edges[np.searchsorted(cum, 0.99 * len(x))]))
        return {"mean": mean.tolist(), "std": std.tolist(), "q01": q01, "q99": q99}


def openpi_norm_stats(episodes: Sequence[Mapping[str, np.ndarray]], *, action_horizon: int, joint_dims: int) -> dict:
    """openpi ``norm_stats.json`` content for the S-05a data config: ``state`` and ``actions`` after the
    repack -> SingleArmInputs -> DeltaActions(joints) transforms, over ``action_horizon`` chunks where
    LeRobot pads past the episode end by repeating the last frame."""
    states, chunks = [], []
    for ep in episodes:
        s, a = ep["observation.state"].astype(np.float64), ep["action"].astype(np.float64)
        n = len(s)
        idx = np.minimum(np.arange(n)[:, None] + np.arange(action_horizon)[None, :], n - 1)
        ch = a[idx].copy()  # (n, H, D)
        ch[..., :joint_dims] -= s[:, None, :joint_dims]
        states.append(s)
        chunks.append(ch)
    return {"norm_stats": {"state": _OpenpiRunningStats.compute(np.concatenate(states)),
                           "actions": _OpenpiRunningStats.compute(np.concatenate(chunks))}}


# --------------------------------------------------------------------------- the conversion

def _features(state_dim: int, joint_names: Sequence[str], video_keys: Mapping[str, tuple[int, int]],
              video_infos: Mapping[str, dict]) -> dict:
    names = list(joint_names) + ["gripper"]
    feats: dict[str, dict] = {}
    for key, (h, w) in video_keys.items():
        feats[key] = {"dtype": "video", "shape": [h, w, 3], "names": ["height", "width", "channels"],
                      "info": video_infos[key]}
    feats["observation.state"] = {"dtype": "float32", "shape": [state_dim], "names": names}
    feats["action"] = {"dtype": "float32", "shape": [state_dim], "names": names}
    feats["action.ee_target"] = {"dtype": "float32", "shape": [8],
                                 "names": ["x", "y", "z", "qw", "qx", "qy", "qz", "gripper"]}
    for k, dt in (("timestamp", "float32"), ("frame_index", "int64"), ("episode_index", "int64"),
                  ("index", "int64"), ("task_index", "int64")):
        feats[k] = {"dtype": dt, "shape": [1], "names": None}
    return feats


def _select_base(base: list[EpisodeSource], base_actions: list[EpisodeActions], ratio: float,
                 ticket_frames: int) -> list[int]:
    if ratio <= 0 or not base:
        return []
    if ratio >= 1:
        raise ConvertError("base_mix_ratio must be in [0, 1)")
    want = ratio / (1 - ratio) * ticket_frames
    picked, have = [], 0
    for i, a in enumerate(base_actions):
        if have >= want:
            break
        picked.append(i)
        have += a.n
    return picked


def convert(manifest: ConvertManifest | Mapping[str, Any], *, lerobot_root: str | Path, assets_root: str | Path,
            mounts: Mapping[str, str] | None = None, overwrite: bool = True) -> dict:
    """Convert a manifest into ``<lerobot_root>/<repo_id>`` (LeRobot v2.1) and
    ``<assets_root>/<repo_id>/norm_stats.json``. Returns a report (also written to
    ``meta/rdf_conversion.json``)."""
    m = manifest if isinstance(manifest, ConvertManifest) else ConvertManifest.from_mapping(manifest)
    if not m.episodes:
        raise ConvertError("manifest has no episodes")
    t0 = time.time()
    root = Path(lerobot_root) / m.repo_id
    if root.exists():
        if not overwrite:
            raise ConvertError(f"{root} exists")
        shutil.rmtree(root)
    tmp = Path(tempfile.mkdtemp(prefix="rdf-tdm39-"))

    def load(srcs: list[EpisodeSource]) -> list[EpisodeActions]:
        out = []
        for s in srcs:
            a = read_actions(resolve_uri(s.actions_uri, mounts=mounts, tmp_dir=tmp))
            if a.is_fixture and not m.allow_fixtures:
                raise ConvertError(f"episode {s.episode_id} is synthetic fixture data (allow_fixtures=False)")
            out.append(a)
        return out

    ticket_actions = load(m.episodes)
    base_actions = load(m.base_episodes) if m.base_mix_ratio > 0 else []
    plans = [plan_episode(s, a, fps=m.fps, keep_after_handback=m.keep_after_handback)
             for s, a in zip(m.episodes, ticket_actions)]
    picked = _select_base(m.base_episodes, base_actions, m.base_mix_ratio, sum(p.length for p in plans))
    for i in picked:
        plans.append(plan_episode(m.base_episodes[i], base_actions[i], fps=m.fps,
                                  keep_after_handback=m.keep_after_handback))
        plans[-1].note = "base"
    all_actions = ticket_actions + [base_actions[i] for i in picked]

    joint_names = all_actions[0].meta.get("joint_names") or DEFAULT_JOINT_NAMES
    n_joints = all_actions[0].joint_pos.shape[1]
    if len(joint_names) != n_joints:
        joint_names = [f"joint_{i}" for i in range(1, n_joints + 1)]
    for p, a in zip(plans, all_actions):
        if a.joint_pos.shape[1] != n_joints:
            raise ConvertError(f"episode {p.source.episode_id} has {a.joint_pos.shape[1]} joints, expected {n_joints}")

    tasks: list[str] = []
    ep_rows, ep_stats, ep_arrays, video_infos, video_hw = [], [], [], {}, {}
    total = 0
    for ep_idx, (p, a) in enumerate(zip(plans, all_actions)):
        task = p.source.task or m.task
        if task not in tasks:
            tasks.append(task)
        cams = {m.camera_map[c]: uri for c, uri in p.source.cameras.items() if c in m.camera_map}
        if ep_idx == 0:
            video_keys = sorted(cams)
            if not video_keys:
                raise ConvertError(f"episode {p.source.episode_id}: no camera matches camera_map {m.camera_map}")
        elif sorted(cams) != video_keys:
            raise ConvertError(f"episode {p.source.episode_id} cameras {sorted(cams)} != {video_keys}")
        chunk = ep_idx // CHUNKS_SIZE
        frame_numbers = a.frame_index[p.rows]
        stats: dict[str, Any] = {}
        for key in video_keys:
            src = resolve_uri(cams[key], mounts=mounts, tmp_dir=tmp)
            dst = root / VIDEO_PATH.format(episode_chunk=chunk, video_key=key, episode_index=ep_idx)
            r = transcode_frames(src, dst, frame_numbers, m.fps, m.image_size)
            hw = (r["height"], r["width"])
            if video_hw.setdefault(key, hw) != hw:
                raise ConvertError(f"{key}: episode {p.source.episode_id} is {hw}, earlier episodes {video_hw[key]}")
            if key not in video_infos:
                video_infos[key] = video_info(dst)
            stats[key] = r["image_stats"]
        arrs = episode_arrays(a, p.rows)
        n = p.length
        cols = {**arrs,
                "timestamp": (np.arange(n) / m.fps).astype(np.float32),
                "frame_index": np.arange(n, dtype=np.int64),
                "episode_index": np.full(n, ep_idx, dtype=np.int64),
                "index": np.arange(total, total + n, dtype=np.int64),
                "task_index": np.full(n, tasks.index(task), dtype=np.int64)}
        ep_arrays.append(cols)
        for k, v in cols.items():
            stats[k] = _feature_stats(v)
        ep_stats.append({"episode_index": ep_idx, "stats": stats})
        ep_rows.append({"episode_index": ep_idx, "tasks": [task], "length": n})
        total += n

    features = _features(n_joints + 1, joint_names, video_hw, video_infos)
    for ep_idx, cols in enumerate(ep_arrays):
        write_episode_parquet(root / DATA_PATH.format(episode_chunk=ep_idx // CHUNKS_SIZE, episode_index=ep_idx),
                              cols, features)

    meta = root / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    info = {"codebase_version": CODEBASE_VERSION, "robot_type": m.robot_type, "total_episodes": len(plans),
            "total_frames": total, "total_tasks": len(tasks), "total_videos": len(plans) * len(video_keys),
            "total_chunks": (len(plans) - 1) // CHUNKS_SIZE + 1, "chunks_size": CHUNKS_SIZE, "fps": int(m.fps),
            "splits": {"train": f"0:{len(plans)}"}, "data_path": DATA_PATH, "video_path": VIDEO_PATH,
            "features": features}
    (meta / "info.json").write_text(json.dumps(info, indent=4))
    _write_jsonl(meta / "tasks.jsonl", [{"task_index": i, "task": t} for i, t in enumerate(tasks)])
    _write_jsonl(meta / "episodes.jsonl", ep_rows)
    _write_jsonl(meta / "episodes_stats.jsonl", ep_stats)

    norm = openpi_norm_stats(ep_arrays, action_horizon=m.action_horizon, joint_dims=m.joint_dims)
    stats_dir = Path(assets_root) / m.repo_id
    stats_dir.mkdir(parents=True, exist_ok=True)
    (stats_dir / "norm_stats.json").write_text(json.dumps(norm, indent=2))

    report = {
        "converter": CONVERTER, "converter_version": CONVERTER_VERSION, "codebase_version": CODEBASE_VERSION,
        "repo_id": m.repo_id, "dataset_dir": str(root), "norm_stats_path": str(stats_dir / "norm_stats.json"),
        "fps": int(m.fps), "total_episodes": len(plans), "total_frames": total, "tasks": tasks,
        "video_keys": video_keys, "state_dim": n_joints + 1, "action_dim": n_joints + 1,
        "keep_after_handback": m.keep_after_handback, "base_mix_ratio": m.base_mix_ratio,
        "episodes": [{"episode_index": i, "episode_id": p.source.episode_id, "kind": p.source.kind,
                      "source": p.note or "ticket", "src_frames": p.src_frames, "src_fps": p.src_fps,
                      "frames": p.length, "cut_before_takeover": p.cut_before_takeover,
                      "cut_after_handback": p.cut_after_handback, "fixture": a.is_fixture,
                      "takeover_ts": p.source.takeover_ts, "handback_ts": p.source.handback_ts}
                     for i, (p, a) in enumerate(zip(plans, all_actions))],
        "seconds": round(time.time() - t0, 2),
    }
    (meta / "rdf_conversion.json").write_text(json.dumps(report, indent=2))
    shutil.rmtree(tmp, ignore_errors=True)
    return report


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# --------------------------------------------------------------------------- Modal entry (TDM-38 wires it)

# Called by TDM-38's ``convert`` function in services/training/modal_app.py, which mounts the
# Volumes (rdf-media read-only at /media, rdf-datasets at /datasets) and commits rdf-datasets after.
DATASETS_VOLUME = "rdf-datasets"


def modal_convert(manifest: Mapping[str, Any], *, datasets_root: str = "/datasets",
                  media_root: str = "/media") -> dict:
    """What modal_app.py's ``convert`` calls: convert a manifest dict onto the mounted Volumes and
    return the conversion report (``repo_id``, ``dataset_dir``, ``norm_stats_path``, per-episode cuts).
    The dataset uri is ``modal-vol://rdf-datasets/lerobot/<report["repo_id"]>``."""
    return convert(manifest, lerobot_root=Path(datasets_root) / "lerobot", assets_root=Path(datasets_root) / "assets",
                   mounts={"rdf-media": media_root})


def convert_episodes(ticket_id: str, episode_manifest: Mapping[str, Any] | Sequence[Mapping[str, Any]], *,
                     media_root: str = "/media", datasets_root: str = "/datasets") -> str:
    """Modal entry. ``episode_manifest`` is a full manifest dict (``ConvertManifest`` fields) or just
    the list of episode dicts, in which case ``repo_id`` is ``rdf/ticket-<ticket_id>`` and the task
    comes from the episodes' ``task`` field. Writes ``<datasets_root>/lerobot/<repo_id>`` and
    ``<datasets_root>/assets/<repo_id>/norm_stats.json``; returns
    ``modal-vol://rdf-datasets/lerobot/<repo_id>``. The fine-tune sets ``HF_LEROBOT_HOME`` to
    ``<datasets_root>/lerobot`` and the openpi ``assets_dir`` to ``<datasets_root>/assets``."""
    if isinstance(episode_manifest, Mapping):
        manifest = {"repo_id": f"rdf/ticket-{ticket_id}", **episode_manifest}
    else:
        eps = list(episode_manifest)
        tasks = {e.get("task") for e in eps if e.get("task")}
        if not tasks:
            raise ConvertError("episode list has no task; pass a manifest dict with 'task'")
        manifest = {"repo_id": f"rdf/ticket-{ticket_id}", "task": sorted(tasks)[0], "episodes": eps}
    report = modal_convert(manifest, datasets_root=datasets_root, media_root=media_root)
    return f"modal-vol://{DATASETS_VOLUME}/lerobot/{report['repo_id']}"


# --------------------------------------------------------------------------- CLI

def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", help="manifest JSON file")
    ap.add_argument("--fixture", action="store_true", help="generate 2 labelled fixture episodes and convert them")
    ap.add_argument("--out", default=os.environ.get("RDF_DATASETS_ROOT", "spikes/tdm-39-convert/tmp/datasets"),
                    help="writes <out>/lerobot/<repo_id> and <out>/assets/<repo_id>")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.fixture:
        from rdf_training.convert_fixtures import write_fixture_manifest

        manifest = write_fixture_manifest(out / "fixture-episodes")
    elif a.manifest:
        manifest = json.loads(Path(a.manifest).read_text())
    else:
        ap.error("pass --manifest or --fixture")
    report = convert(manifest, lerobot_root=out / "lerobot", assets_root=out / "assets")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
