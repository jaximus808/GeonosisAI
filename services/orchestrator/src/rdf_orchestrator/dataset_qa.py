"""TDM-92: deterministic QA for PURCHASED episodes (dataset offers, TDM-14), before the LLM stage.

Every purchased episode (``episode.kind == "purchased"``, inserted by ``ingest_purchased_episode``)
gets two layers of deterministic checks:

  per-episode (reused, NOT duplicated: ``rdf_common.review_checks.run_checks`` with the reviewer's
  ``config/review.yaml`` thresholds)
      actions, duration, frame_drops, jerk
      (``success`` is left out: for bought data it is the LLM's call, TDM-93 ``llm_success``)

  dataset-level (this module, ``config/dataset_qa.yaml``): does this data fit OUR robot and OUR
  dataset? The per-episode reviewer never asks these because our own recorder can't get them wrong.
      embodiment    6 arm joints + a gripper, and a YAM-compatible embodiment / robot_type
      fps           declared fps and the fps measured from timestamps within +-10% of 30
      camera_set    the robot's camera roles (exo + wrist) are all present (aliases: top -> exo, ...)
      action_range  joint_pos / joint_cmd inside the YAM URDF limits (+margin), gripper in 0..1,
                    no tick-to-tick joint step faster than the URDF velocity limit
      static        the arm actually moves (largest per-joint std >= floor, or gripper travel)
      duplicate     not the same trajectory as any earlier episode of the ticket (exact action hash,
                    near-identical resampled trajectory, or first/middle/last exo frames matching by
                    perceptual hash with a close trajectory)

Then a summary row ``deterministic_qa`` is written LAST (the TDM-93 gate contract): passed=true
only when nothing above failed. The bus handler (``handlers/dataset_qa.py``) writes one
``record_qa_result`` per check, then on any failure ``submit_review(reject, "<check>: <reason>; ...")``;
on a pass it leaves the review to the LLM stage (TDM-93 fires on ``deterministic_qa`` passed=true).

Pure logic plus optional media reads (ffmpeg, for the perceptual hash). No SpacetimeDB here.

    cfg = load_config()
    report = run_dataset_qa(episode_row, actions, robot=robot_row, peers=[Fingerprint, ...], config=cfg)
    report.passed, report.failed_names, report.reason(), report.results (CheckResult each)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from rdf_common.episode_format import EpisodeActions, cameras_from_json
from rdf_common.review_checks import FAIL, PASS, SKIP, CheckResult, ReviewThresholds, run_checks

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "dataset_qa.yaml"
DEFAULT_REVIEW_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "review.yaml"
EPISODE_CHECKS = ("actions", "duration", "frame_drops", "jerk")  # from review_checks
DATASET_CHECKS = ("embodiment", "fps", "camera_set", "action_range", "static", "duplicate")
SUMMARY_CHECK = "deterministic_qa"  # written last; TDM-93's LLM QA gates on it
ALL_CHECKS = EPISODE_CHECKS + DATASET_CHECKS + (SUMMARY_CHECK,)
TRAINING_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
QA_VERSION = 1


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class DatasetQAConfig:
    arm_joints: int = 6
    require_gripper: bool = True
    compatible: tuple[str, ...] = ("i2rt_yam", "yam")
    require_compatible_type: bool = True
    fps_target: float = 30.0
    fps_tolerance: float = 0.10
    cameras_required: tuple[str, ...] = ("exo", "wrist")
    camera_aliases: Mapping[str, str] = field(default_factory=lambda: {"top": "exo"})
    cameras_allow_extra: bool = True
    joint_lower: tuple[float, ...] = (-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944)
    joint_upper: tuple[float, ...] = (3.13, 3.65, 3.13, 1.5708, 1.5708, 2.0944)
    margin_rad: float = 0.05
    command_margin_rad: float = 0.15
    gripper_min: float = -0.05
    gripper_max: float = 1.05
    max_joint_speed_rad_s: float = 10.0
    min_joint_std_rad: float = 0.02
    min_gripper_range: float = 0.05
    action_quantum_rad: float = 0.001
    near_max_rad: float = 0.01
    signature_points: int = 48
    length_ratio_max: float = 1.15
    phash_max_hamming: int = 8
    phash_trajectory_max_rad: float = 0.08
    version: int = 1

    _YAML = {
        "embodiment": {"arm_joints": "arm_joints", "require_gripper": "require_gripper", "compatible": "compatible",
                       "require_compatible_type": "require_compatible_type"},
        "fps": {"target": "fps_target", "tolerance": "fps_tolerance"},
        "cameras": {"required": "cameras_required", "aliases": "camera_aliases", "allow_extra": "cameras_allow_extra"},
        "action_range": {"joint_lower": "joint_lower", "joint_upper": "joint_upper", "margin_rad": "margin_rad", "command_margin_rad": "command_margin_rad",
                         "gripper_min": "gripper_min", "gripper_max": "gripper_max",
                         "max_joint_speed_rad_s": "max_joint_speed_rad_s"},
        "static": {"min_joint_std_rad": "min_joint_std_rad", "min_gripper_range": "min_gripper_range"},
        "duplicate": {"action_quantum_rad": "action_quantum_rad", "near_max_rad": "near_max_rad",
                      "signature_points": "signature_points", "length_ratio_max": "length_ratio_max",
                      "phash_max_hamming": "phash_max_hamming",
                      "phash_trajectory_max_rad": "phash_trajectory_max_rad"},
    }

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> "DatasetQAConfig":
        kw: dict[str, Any] = {}
        for section, body in (cfg or {}).items():
            if section == "version":
                kw["version"] = int(body)
                continue
            keys = cls._YAML.get(section)
            if keys is None or not isinstance(body, Mapping):
                raise ValueError(f"dataset_qa config: unknown section {section!r}")
            for k, v in body.items():
                if k not in keys:
                    raise ValueError(f"dataset_qa config: unknown key {section}.{k}")
                if isinstance(v, list):
                    v = tuple(str(x).lower() if k == "compatible" else x for x in v)
                if k == "aliases":
                    v = {str(a).lower(): str(b).lower() for a, b in dict(v).items()}
                kw[keys[k]] = v
        c = cls(**kw)
        if len(c.joint_lower) != c.arm_joints or len(c.joint_upper) != c.arm_joints:
            raise ValueError("dataset_qa config: action_range joint limits need arm_joints entries")
        return c

    @classmethod
    def from_yaml(cls, path: str | Path) -> "DatasetQAConfig":
        import yaml

        return cls.from_mapping(yaml.safe_load(Path(path).read_text()) or {})


@dataclass(frozen=True)
class QAConfig:
    dataset: DatasetQAConfig
    review: ReviewThresholds  # the reviewer's thresholds (config/review.yaml), success check dropped


def load_config(path: str | Path | None = None, review_path: str | Path | None = None,
                **review_overrides: Any) -> QAConfig:
    ds = DatasetQAConfig.from_yaml(path or os.environ.get("RDF_DATASET_QA_CONFIG") or DEFAULT_CONFIG_PATH)
    th = ReviewThresholds.from_yaml(review_path or os.environ.get("RDF_REVIEW_CONFIG") or DEFAULT_REVIEW_CONFIG_PATH,
                                    ignore=("version", "claude"))
    # Success of bought data is judged by the LLM stage (TDM-93 llm_success), not by a seller's flag.
    th = th.replace(success_required_kinds=tuple(k for k in th.success_required_kinds if k != "purchased"),
                    **review_overrides)
    return QAConfig(ds, th)


# --------------------------------------------------------------------------- helpers

def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def norm_camera(name: str, aliases: Mapping[str, str]) -> str:
    n = str(name).lower().strip()
    for pre in ("observation.images.", "images.", "image_", "cam_"):
        if n.startswith(pre) and n[len(pre):]:
            short = n[len(pre):]
            if short in aliases or pre != "cam_":
                n = short
    if n in aliases:
        return aliases[n]
    if "wrist" in n:
        return "wrist"
    return n


def robot_camera_roles(robot: Any, cfg: DatasetQAConfig) -> tuple[list[str], str]:
    """(required camera roles, where they came from). ``robot.camera_config_json`` lists names only
    when the robot client registers them (``{"cameras": {"top": {...}, "wrist": {...}}}`` or a list);
    the TDM-47 client currently sends a mode string, so the config's roles are the fallback."""
    raw = _g(robot, "camera_config_json") if robot is not None else None
    try:
        cc = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    except ValueError:
        cc = {}
    cams = cc.get("cameras") if isinstance(cc, dict) else None
    names: list[str] = []
    if isinstance(cams, dict):
        names = [k for k, v in cams.items() if isinstance(v, dict)]
    elif isinstance(cams, list):
        names = [c if isinstance(c, str) else str(c.get("name", "")) for c in cams if c]
    names = [n for n in names if n]
    if names:
        return sorted({norm_camera(n, cfg.camera_aliases) for n in names}), "robot.camera_config_json"
    return list(cfg.cameras_required), "config/dataset_qa.yaml cameras.required"


def episode_cameras(episode: Any, actions: EpisodeActions | None, cfg: DatasetQAConfig) -> dict[str, str]:
    """normalised role -> original name, from cameras_json (the uris) and the parquet metadata."""
    names = list(cameras_from_json(_g(episode, "cameras_json")))
    if actions is not None:
        names += [k for k in (actions.meta.get("cameras") or {})]
    out: dict[str, str] = {}
    for n in names:
        out.setdefault(norm_camera(n, cfg.camera_aliases), n)
    return out


def _gripper(actions: EpisodeActions, col: str):
    import numpy as np

    v = actions.extra.get(col)
    return None if v is None else np.asarray(v, dtype=np.float64).reshape(-1)


# --------------------------------------------------------------------------- dataset-level checks

def check_embodiment(actions: EpisodeActions, robot: Any, cfg: DatasetQAConfig) -> CheckResult:
    src = actions.meta.get("source") or {}
    joints = int(actions.joint_pos.shape[1])
    emb = str(actions.meta.get("embodiment") or src.get("robot_type") or "unknown").lower()
    base = emb.split(":", 1)[0]
    robot_emb = str(_g(robot, "embodiment") or "").lower() if robot is not None else ""
    ok_types = set(cfg.compatible) | ({robot_emb} if robot_emb else set())
    has_gripper = src.get("gripper_channel", "gripper_pos") is not None
    d = {"joints": joints, "expected_joints": cfg.arm_joints, "gripper": has_gripper,
         "embodiment": emb, "robot_embodiment": robot_emb or None, "compatible": sorted(ok_types),
         "joint_names": list(actions.meta.get("joint_names") or [])}
    if src.get("state_names"):
        d["source_state_names"] = list(src["state_names"])[:32]
    if joints != cfg.arm_joints:
        extra = ""
        if joints == 2 * cfg.arm_joints or joints == 2 * (cfg.arm_joints + 1):
            extra = " (looks bimanual: import one arm with --arm left|right)"
        import numpy as np

        if float(np.abs(actions.joint_pos).max()) > 2 * np.pi:
            extra += f"; joint values up to {float(np.abs(actions.joint_pos).max()):.0f} look like degrees, not radians"
        return CheckResult("embodiment", FAIL, (
            f"embodiment mismatch: {joints} arm joints but the YAM has {cfg.arm_joints} "
            f"(source '{emb}'){extra}"), d)
    if cfg.require_gripper and not has_gripper:
        return CheckResult("embodiment", FAIL, "embodiment mismatch: the source has no gripper channel", d)
    if cfg.require_compatible_type and base not in ok_types:
        return CheckResult("embodiment", FAIL, (
            f"embodiment mismatch: recorded on '{emb}', not a YAM-compatible arm "
            f"(accepted: {', '.join(sorted(ok_types))})"), d)
    return CheckResult("embodiment", PASS, "", d)


def check_fps(actions: EpisodeActions, cfg: DatasetQAConfig) -> CheckResult:
    import numpy as np

    declared = actions.fps
    dt = np.diff(actions.timestamp_us.astype(np.float64)) / 1e6
    dt = dt[dt > 0]
    measured = float(1.0 / np.median(dt)) if dt.size else 0.0
    lo, hi = cfg.fps_target * (1 - cfg.fps_tolerance), cfg.fps_target * (1 + cfg.fps_tolerance)
    d = {"declared_fps": _r(declared, 3), "measured_fps": _r(measured, 3), "target_fps": cfg.fps_target,
         "allowed": [_r(lo, 3), _r(hi, 3)]}
    cams = actions.meta.get("cameras") or {}
    cam_fps = {k: float(v.get("fps")) for k, v in cams.items() if isinstance(v, dict) and v.get("fps")}
    if cam_fps:
        d["camera_fps"] = cam_fps
    for label, v in (("declared", declared), ("measured", measured), *((f"camera {k}", f) for k, f in cam_fps.items())):
        if not (lo <= v <= hi):
            return CheckResult("fps", FAIL, (
                f"{label} fps {v:.1f} is outside {cfg.fps_target:g} +-{cfg.fps_tolerance * 100:g}% "
                f"({lo:.1f}..{hi:.1f})"), d)
    return CheckResult("fps", PASS, "", d)


def check_camera_set(episode: Any, actions: EpisodeActions, robot: Any, cfg: DatasetQAConfig) -> CheckResult:
    required, origin = robot_camera_roles(robot, cfg)
    have = episode_cameras(episode, actions, cfg)
    uris = cameras_from_json(_g(episode, "cameras_json"))
    missing = [c for c in required if c not in have]
    extra = sorted(set(have) - set(required))
    cams_meta = actions.meta.get("cameras") or {}
    empty = [have[c] for c in required if c in have and isinstance(cams_meta.get(have[c]), dict)
             and cams_meta[have[c]].get("frames") == 0]
    no_uri = [have[c] for c in required if c in have and have[c] not in uris]
    d = {"required": required, "required_from": origin, "present": {k: v for k, v in sorted(have.items())},
         "missing": missing, "extra": extra, "count": len(have), "expected_count": len(required)}
    if missing:
        return CheckResult("camera_set", FAIL, (
            f"camera set mismatch: {len(have)} camera(s) {sorted(have) or '[]'} but the robot records "
            f"{required}; missing {missing}"), d)
    if no_uri:
        return CheckResult("camera_set", FAIL, f"camera(s) {no_uri} have no video uri in cameras_json", d)
    if empty:
        return CheckResult("camera_set", FAIL, f"camera(s) {empty} have 0 video frames", d)
    if extra and not cfg.cameras_allow_extra:
        return CheckResult("camera_set", FAIL, f"unexpected extra camera(s) {extra}", d)
    return CheckResult("camera_set", PASS, "", d)


def check_action_range(actions: EpisodeActions, cfg: DatasetQAConfig) -> CheckResult:
    import numpy as np

    lo = np.asarray(cfg.joint_lower) - cfg.margin_rad
    hi = np.asarray(cfg.joint_upper) + cfg.margin_rad
    d: dict[str, Any] = {"limits_lower": list(cfg.joint_lower), "limits_upper": list(cfg.joint_upper),
                         "margin_rad": cfg.margin_rad, "command_margin_rad": cfg.command_margin_rad}
    if actions.joint_pos.shape[1] != len(lo):
        return CheckResult("action_range", SKIP, "skipped: joint count does not match the YAM (see embodiment)", d)
    streams = {"joint_pos": actions.joint_pos}
    if "joint_cmd" in actions.extra and np.asarray(actions.extra["joint_cmd"]).shape == actions.joint_pos.shape:
        streams["joint_cmd"] = np.asarray(actions.extra["joint_cmd"], dtype=np.float64)
    d["min"] = {k: [_r(x, 3) for x in v.min(axis=0)] for k, v in streams.items()}
    d["max"] = {k: [_r(x, 3) for x in v.max(axis=0)] for k, v in streams.items()}
    for name, q in streams.items():
        m = cfg.margin_rad if name == "joint_pos" else cfg.command_margin_rad
        lo = np.asarray(cfg.joint_lower) - m
        hi = np.asarray(cfg.joint_upper) + m
        below, above = q < lo, q > hi
        if below.any() or above.any():
            bad = below | above
            j = int(np.argmax(bad.sum(axis=0)))
            frac = float(bad.any(axis=1).mean())
            worst = float(q[:, j].min() if below[:, j].any() else q[:, j].max())
            hint = " (values look like degrees, not radians)" if np.abs(q).max() > 2 * np.pi else ""
            d.update(stream=name, joint=j, worst=_r(worst, 3), out_of_range_fraction=_r(frac))
            return CheckResult("action_range", FAIL, (
                f"{name} joint {j + 1} reaches {worst:.3f} rad, outside the YAM limit "
                f"[{cfg.joint_lower[j]:g}, {cfg.joint_upper[j]:g}] (+-{m:g}) on "
                f"{frac * 100:.0f}% of ticks{hint}"), d)
    for col in ("gripper_pos", "gripper_cmd"):
        g = _gripper(actions, col)
        if g is None or not g.size:
            continue
        d[f"{col}_range"] = [_r(g.min(), 3), _r(g.max(), 3)]
        if g.min() < cfg.gripper_min or g.max() > cfg.gripper_max:
            return CheckResult("action_range", FAIL, (
                f"{col} spans {g.min():.3f}..{g.max():.3f}; expected 0 (closed) .. 1 (open)"), d)
    t = actions.timestamp_us.astype(np.float64) / 1e6
    dt = np.diff(t)
    ok = dt > 0
    if ok.any():
        speed = np.abs(np.diff(actions.joint_pos, axis=0))[ok] / dt[ok, None]
        vmax = float(speed.max())
        d["max_joint_speed_rad_s"] = _r(vmax, 2)
        if vmax > cfg.max_joint_speed_rad_s:
            j = int(np.argmax(speed.max(axis=0)))
            return CheckResult("action_range", FAIL, (
                f"joint {j + 1} jumps at {vmax:.1f} rad/s between ticks; the YAM limit is "
                f"{cfg.max_joint_speed_rad_s:g} rad/s (a teleport or a units/ordering error)"), d)
    return CheckResult("action_range", PASS, "", d)


def check_static(actions: EpisodeActions, cfg: DatasetQAConfig) -> CheckResult:
    import numpy as np

    std = actions.joint_pos.std(axis=0)
    g = _gripper(actions, "gripper_pos")
    grange = float(g.max() - g.min()) if g is not None and g.size else 0.0
    d = {"max_joint_std_rad": _r(std.max()), "joint_std_rad": [_r(x) for x in std],
         "gripper_range": _r(grange), "min_joint_std_rad": cfg.min_joint_std_rad,
         "min_gripper_range": cfg.min_gripper_range,
         "path_length_rad": _r(float(np.abs(np.diff(actions.joint_pos, axis=0)).sum()), 3)}
    if std.max() < cfg.min_joint_std_rad and grange < cfg.min_gripper_range:
        return CheckResult("static", FAIL, (
            f"static episode: the arm barely moves (largest joint std {std.max() * 1000:.1f} mrad < "
            f"{cfg.min_joint_std_rad * 1000:g} mrad, gripper travel {grange:.3f} < {cfg.min_gripper_range:g})"), d)
    return CheckResult("static", PASS, "", d)


# --------------------------------------------------------------------------- duplicates

@dataclass
class Fingerprint:
    episode_id: int
    n: int
    action_hash: str
    signature: list[list[float]]  # (signature_points, joints) resampled joint_pos
    phash: list[str | None] = field(default_factory=list)  # first / middle / last exo frame
    camera: str | None = None
    version: int = QA_VERSION

    def to_dict(self) -> dict:
        return {"episode_id": self.episode_id, "n": self.n, "action_hash": self.action_hash,
                "signature": self.signature, "phash": self.phash, "camera": self.camera, "version": self.version}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Fingerprint":
        return cls(int(d["episode_id"]), int(d["n"]), str(d["action_hash"]), [list(r) for r in d["signature"]],
                   list(d.get("phash") or []), d.get("camera"), int(d.get("version", QA_VERSION)))


def action_fingerprint(episode_id: int, actions: EpisodeActions, cfg: DatasetQAConfig,
                       phash: Sequence[str | None] = (), camera: str | None = None) -> Fingerprint:
    import numpy as np

    q = actions.joint_pos
    h = hashlib.sha256()
    h.update(str(q.shape).encode())
    h.update(np.rint(q / cfg.action_quantum_rad).astype(np.int64).tobytes())
    g = _gripper(actions, "gripper_pos")
    if g is not None:
        h.update(np.rint(g / 0.001).astype(np.int64).tobytes())
    k = cfg.signature_points
    x = np.linspace(0, 1, len(q))
    xs = np.linspace(0, 1, k)
    sig = np.column_stack([np.interp(xs, x, q[:, j]) for j in range(q.shape[1])]) if len(q) else np.zeros((0, 0))
    return Fingerprint(int(episode_id), int(len(q)), h.hexdigest()[:32], [[round(float(v), 4) for v in r] for r in sig],
                       list(phash), camera)


def hamming(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def compare_fingerprints(a: Fingerprint, b: Fingerprint, cfg: DatasetQAConfig) -> dict:
    import numpy as np

    out: dict[str, Any] = {"episode_id": b.episode_id}
    out["exact"] = a.action_hash == b.action_hash
    sa, sb = np.asarray(a.signature, dtype=float), np.asarray(b.signature, dtype=float)
    ratio = max(a.n, b.n) / max(1, min(a.n, b.n))
    out["length_ratio"] = _r(ratio, 3)
    dist = float(np.abs(sa - sb).max()) if sa.shape == sb.shape and sa.size and ratio <= cfg.length_ratio_max else None
    out["trajectory_max_diff_rad"] = None if dist is None else _r(dist)
    hd = [hamming(x, y) for x, y in zip(a.phash, b.phash)]
    out["phash_hamming"] = hd
    visual = bool(hd) and len(hd) == 3 and all(h is not None and h <= cfg.phash_max_hamming for h in hd)
    out["visual_match"] = visual
    if out["exact"]:
        out["match"] = "identical joint trajectory (action hash)"
    elif dist is not None and dist <= cfg.near_max_rad:
        out["match"] = f"near-identical joint trajectory (max diff {dist * 1000:.1f} mrad)"
    elif visual and dist is not None and dist <= cfg.phash_trajectory_max_rad:
        out["match"] = (f"first/middle/last frames match (hamming {hd}) and the trajectory is within "
                        f"{dist * 1000:.0f} mrad")
    else:
        out["match"] = None
    return out


def check_duplicate(fp: Fingerprint, peers: Iterable[Fingerprint], cfg: DatasetQAConfig) -> CheckResult:
    compared = []
    hit = None
    for p in peers:
        if p.episode_id == fp.episode_id:
            continue
        c = compare_fingerprints(fp, p, cfg)
        compared.append(c)
        if c["match"] and hit is None:
            hit = c
    d = {"fingerprint": fp.to_dict(), "compared": len(compared),
         "closest": min(compared, key=lambda c: (c["trajectory_max_diff_rad"] is None, c["trajectory_max_diff_rad"] or 0),
                        default=None)}
    if hit is not None:
        d["duplicate_of"] = hit["episode_id"]
        d["closest"] = hit
        return CheckResult("duplicate", FAIL, f"duplicate of episode {hit['episode_id']}: {hit['match']}", d)
    return CheckResult("duplicate", PASS, "", d)


def fingerprint_from_qa_detail(detail_json: str | None) -> Fingerprint | None:
    try:
        d = json.loads(detail_json or "")
        return Fingerprint.from_dict(d["fingerprint"])
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------- perceptual hash (media)

def _dct_matrix(n: int):
    import numpy as np

    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    m[0] *= 1 / np.sqrt(2)
    return m * np.sqrt(2 / n)


def phash_gray32(img) -> str:
    """64-bit DCT perceptual hash of a 32x32 grayscale frame (hex)."""
    import numpy as np

    a = np.asarray(img, dtype=np.float64).reshape(32, 32)
    m = _dct_matrix(32)
    low = (m @ a @ m.T)[:8, :8].reshape(-1)
    bits = low > np.median(low[1:])
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def video_phashes(video: Path, times_s: Sequence[float]) -> list[str | None]:
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return [None] * len(times_s)
    out: list[str | None] = []
    for t in times_s:
        cmd = [ffmpeg, "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1",
               "-vf", "scale=32:32:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            out.append(None)
            continue
        out.append(phash_gray32(np.frombuffer(r.stdout[:1024], dtype=np.uint8)) if len(r.stdout) >= 1024 else None)
    return out


def episode_phashes(episode: Any, actions: EpisodeActions | None, cfg: DatasetQAConfig,
                    fetch: Callable[..., Any] | None = None) -> tuple[str | None, list[str | None]]:
    """(camera, [first, middle, last] phash) of the exo (else first) camera. Unreadable media -> Nones;
    the duplicate check then relies on the trajectory alone."""
    from .reviewer import fetch_local, pick_camera

    cams = cameras_from_json(_g(episode, "cameras_json"))
    by_role = {norm_camera(k, cfg.camera_aliases): k for k in cams}
    name = by_role.get("exo") or (pick_camera(cams) or (None,))[0]
    if not name:
        return None, [None, None, None]
    fps = actions.fps if actions is not None else cfg.fps_target
    n = actions.n if actions is not None else int(_g(episode, "frame_count") or 0)
    frames = (actions.meta.get("cameras") or {}).get(name, {}).get("frames") if actions is not None else None
    n = int(frames or n or 1)
    times = [0.0, (n // 2) / fps, max(0, n - 2) / fps]
    try:
        with (fetch or fetch_local)(cams[name], suffix=".mp4") as p:
            return name, video_phashes(Path(p), times)
    except Exception:  # noqa: BLE001 - media problems are judged by camera_set / the LLM stage
        return name, [None, None, None]


# --------------------------------------------------------------------------- loading (with normalisation)

class QAInfraError(RuntimeError):
    """Our side failed (storage outage): write nothing, retry later."""


def load_purchased_actions(episode: Any, fetch: Callable[..., Any] | None = None
                           ) -> tuple[EpisodeActions | None, str | None, str | None]:
    """(actions in OUR format, error, normalised_from). A delivery whose ``actions_uri`` is a raw LeRobot
    parquet (TDM-91 ``unpack_lerobot``) is normalised in memory by ``dataset_import`` using the
    dataset's ``meta/info.json`` next to it; anything unreadable is the seller's problem (a reject)."""
    from rdf_common.episode_format import EpisodeFormatError, read_actions

    from .dataset_import import is_lerobot_parquet, normalize_lerobot_parquet
    from .reviewer import fetch_local

    fetch = fetch or fetch_local
    uri = (_g(episode, "actions_uri") or "").strip()
    if not uri:
        return None, "episode has no actions_uri", None
    try:
        with fetch(uri, suffix=".parquet") as p:
            if not is_lerobot_parquet(p):
                return read_actions(p), None, None
            info = None
            if "/data/" in uri:
                try:
                    with fetch(uri.rsplit("/data/", 1)[0] + "/meta/info.json", suffix=".json") as ip:
                        info = json.loads(Path(ip).read_text())
                except Exception:  # noqa: BLE001 - no info.json: normalise from the parquet alone
                    info = None
            acts = normalize_lerobot_parquet(p, info, dataset=uri.rsplit("/data/", 1)[0])
            return acts, None, "lerobot" + ("" if info else " (no meta/info.json found)")
    except EpisodeFormatError as e:
        return None, str(e), None
    except FileNotFoundError as e:
        return None, str(e), None
    except Exception as e:  # noqa: BLE001
        import httpx

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404):
            return None, f"actions file not found in storage (HTTP {e.response.status_code})", None
        if isinstance(e, (KeyError, ValueError, IndexError)):
            return None, f"actions file could not be normalised: {type(e).__name__}: {e}", None
        raise QAInfraError(f"could not fetch actions {uri}: {type(e).__name__}: {e}") from e


# --------------------------------------------------------------------------- the run

@dataclass
class QAReport:
    episode_id: int
    results: list[CheckResult]
    fingerprint: Fingerprint | None = None

    @property
    def summary(self) -> CheckResult:
        return next(r for r in self.results if r.name == SUMMARY_CHECK)

    @property
    def passed(self) -> bool:
        return self.summary.passed

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.failed and r.name != SUMMARY_CHECK]

    @property
    def failed_names(self) -> list[str]:
        return [r.name for r in self.failures]

    def get(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def reason(self) -> str:
        """The submit_review reason: check names first, so the offer card can show them."""
        if not self.failures:
            return ""
        return (f"Rejected by deterministic dataset QA ({', '.join(self.failed_names)}): "
                + "; ".join(f"{r.name}: {r.reason}" for r in self.failures))

    def metrics(self) -> dict:
        return {"qa": "tdm92_dataset_qa", "version": QA_VERSION, "decided_by": "deterministic_qa",
                "failed_checks": self.failed_names,
                "checks": {r.name: {"status": r.status, **({"reason": r.reason} if r.reason else {}),
                                    **{k: v for k, v in r.detail.items() if k != "fingerprint"}}
                           for r in self.results if r.name != SUMMARY_CHECK}}

    def metrics_json(self, limit: int = 15000) -> str:
        m = self.metrics()
        s = json.dumps(m, default=str)
        if len(s) > limit:  # submit_review caps metrics_json; keep the verdict, drop the bulky detail
            m["checks"] = {k: {"status": v["status"], **({"reason": v["reason"]} if "reason" in v else {})}
                           for k, v in m["checks"].items()}
            s = json.dumps(m, default=str)
        return s


def run_dataset_qa(episode: Any, actions: EpisodeActions | None, *, robot: Any = None,
                   peers: Iterable[Fingerprint] = (), config: QAConfig | None = None,
                   actions_error: str | None = None, phash: tuple[str | None, list[str | None]] | None = None) -> QAReport:
    """Every deterministic check for one purchased episode, summary last. ``peers`` are fingerprints of
    the ticket's earlier episodes; ``phash`` is ``episode_phashes(...)`` (None: computed here)."""
    cfg = config or load_config()
    ds = cfg.dataset
    eid = int(_g(episode, "id") or 0)
    base = run_checks(episode, actions, cfg.review, actions_error=actions_error)
    results = [r for r in base.results if r.name in EPISODE_CHECKS]
    usable = actions if (actions is not None and base.results[0].passed) else None
    if usable is None and actions is not None and base.results[0].reason.startswith("actions are synthetic"):
        usable = actions  # still run the dataset checks on a rejected fixture so the report is complete
    fp = None
    if usable is None or usable.n < 2:
        results += [CheckResult(n, SKIP, "skipped: actions unusable") for n in DATASET_CHECKS]
    else:
        results.append(check_embodiment(usable, robot, ds))
        results.append(check_fps(usable, ds))
        results.append(check_camera_set(episode, usable, robot, ds))
        results.append(check_action_range(usable, ds))
        results.append(check_static(usable, ds))
        cam, hashes = phash if phash is not None else episode_phashes(episode, usable, ds)
        fp = action_fingerprint(eid, usable, ds, hashes, cam)
        results.append(check_duplicate(fp, peers, ds))
    failed = [r.name for r in results if r.failed]
    results.append(CheckResult(SUMMARY_CHECK, FAIL if failed else PASS,
                               f"failed: {', '.join(failed)}" if failed else "",
                               {"failed_checks": failed, "checks": [r.name for r in results],
                               "version": QA_VERSION, "next": "reject" if failed else "llm_qa (TDM-93)"}))
    return QAReport(eid, results, fp)
