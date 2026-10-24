"""Deterministic episode checks, shared by the reviewer (TDM-25) and dataset-purchase QA (TDM-92).

Pure functions over an ``episode`` row (a mapping with the SpacetimeDB column names) and its
decoded actions parquet (``rdf_common.episode_format.EpisodeActions``). No I/O, no Claude.

    from rdf_common.episode_format import read_actions
    from rdf_common.review_checks import ReviewThresholds, run_checks

    th = ReviewThresholds.from_yaml("services/orchestrator/config/review.yaml")
    report = run_checks(episode_row, read_actions(path), th)
    report.passed         # True only if no check failed
    report.reason()       # "episode is 1.2 s long; minimum is 2.0 s; ..." (human-readable)
    report.metrics()      # {"duration": {"status": "pass", "duration_s": 9.9, ...}, ...}
    for r in report.results:   # one qa_result row each: r.name, r.passed, r.detail_json()
        ...

If the parquet could not be fetched or decoded, pass ``actions=None, actions_error="..."``: the
``actions`` check fails with that reason and the kinematic checks are skipped.

Checks (name: what fails it). Thresholds come from ``ReviewThresholds`` / ``config/review.yaml``.
  actions          parquet unreadable, required columns missing, < 2 rows, non-finite joint values,
                   unknown control_source values, or synthetic fixture data when fixtures are not allowed
  duration         duration_ms outside [min_duration_s, max_duration_s], or duration_ms disagrees with
                   the parquet's timestamp span by more than duration_tolerance_s
  frame_drops      dropped-frame ratio >= max_frame_drop_ratio. Drops = ticks missing from the
                   timestamp stream at ``fps`` plus video frames missing per camera (metadata
                   ``cameras.*.frames``); non-increasing timestamps; or episode.frame_count != rows
                   by more than max_frame_count_mismatch
  jerk             RMS joint jerk (rad/s^3) >= max_jerk_rms. Positions are resampled to a uniform
                   ``fps`` grid and the third derivative is a Savitzky-Golay cubic fit over
                   ``jerk_window`` ticks (raw third differences are dominated by encoder noise)
  takeover_timestamps   (takeover kinds) takeover_ts / handback_ts missing, handback <= takeover, or
                   either outside the recorded span (+- takeover_ts_tolerance_s)
  failure_before_takeover   (takeover kinds) less than min_pre_takeover_s of policy-driven frames before
                   the takeover (the failure segment the Data Droid cuts), a human segment shorter than
                   min_takeover_s, or control_source disagreeing with the timestamps
  success          (success-required kinds) the success flag is missing or false
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .episode_format import CONTROL_SOURCES, EpisodeActions

PASS, FAIL, SKIP = "pass", "fail", "skip"
CHECK_ORDER = ("actions", "duration", "frame_drops", "jerk", "takeover_timestamps",
               "failure_before_takeover", "success")


# --------------------------------------------------------------------------- thresholds

@dataclass(frozen=True)
class ReviewThresholds:
    min_duration_s: float = 2.0
    max_duration_s: float = 120.0
    duration_tolerance_s: float = 0.5
    max_frame_drop_ratio: float = 0.02
    max_frame_count_mismatch: int = 2
    max_jerk_rms: float = 60.0  # rad/s^3; see services/orchestrator/config/review.yaml for the calibration
    jerk_window: int = 9
    takeover_kinds: tuple[str, ...] = ("recovery_takeover",)
    min_pre_takeover_s: float = 0.5
    min_takeover_s: float = 1.0
    takeover_ts_tolerance_s: float = 0.5
    min_control_source_agreement: float = 0.9
    success_required_kinds: tuple[str, ...] = ("clean_teleop", "recovery_takeover", "purchased")
    allow_fixtures: bool = False

    # YAML section -> {yaml key: field name}
    _YAML = {
        "duration": {"min_s": "min_duration_s", "max_s": "max_duration_s", "tolerance_s": "duration_tolerance_s"},
        "frame_drops": {"max_ratio": "max_frame_drop_ratio", "max_frame_count_mismatch": "max_frame_count_mismatch"},
        "jerk": {"max_rms_rad_s3": "max_jerk_rms", "window": "jerk_window"},
        "takeover": {"kinds": "takeover_kinds", "min_pre_takeover_s": "min_pre_takeover_s",
                     "min_takeover_s": "min_takeover_s", "ts_tolerance_s": "takeover_ts_tolerance_s",
                     "min_control_source_agreement": "min_control_source_agreement"},
        "success": {"required_kinds": "success_required_kinds"},
        "fixtures": {"allow": "allow_fixtures"},
    }

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any], *, ignore: tuple[str, ...] = ("version", "claude")) -> "ReviewThresholds":
        """Build from the nested ``config/review.yaml`` shape. Unknown sections/keys raise (typos in
        a threshold file must not silently fall back to defaults); sections in ``ignore`` belong to
        other readers (the reviewer's ``claude`` block)."""
        kw: dict[str, Any] = {}
        for section, body in (cfg or {}).items():
            if section in ignore:
                continue
            keys = cls._YAML.get(section)
            if keys is None or not isinstance(body, Mapping):
                raise ValueError(f"review config: unknown section {section!r}")
            for k, v in body.items():
                if k not in keys:
                    raise ValueError(f"review config: unknown key {section}.{k}")
                kw[keys[k]] = tuple(v) if isinstance(v, list) else v
        th = cls(**kw)
        th.validate()
        return th

    @classmethod
    def from_yaml(cls, path: str | Path, **kw) -> "ReviewThresholds":
        import yaml

        return cls.from_mapping(yaml.safe_load(Path(path).read_text()) or {}, **kw)

    def replace(self, **kw) -> "ReviewThresholds":
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d.update(kw)
        th = ReviewThresholds(**d)
        th.validate()
        return th

    def validate(self) -> None:
        if not (0 < self.min_duration_s < self.max_duration_s):
            raise ValueError("review config: need 0 < duration.min_s < duration.max_s")
        if not (0 <= self.max_frame_drop_ratio < 1):
            raise ValueError("review config: frame_drops.max_ratio must be in [0, 1)")
        if self.max_jerk_rms <= 0:
            raise ValueError("review config: jerk.max_rms_rad_s3 must be > 0")
        if self.jerk_window < 5 or self.jerk_window % 2 == 0:
            raise ValueError("review config: jerk.window must be odd and >= 5")
        if not (0 < self.min_control_source_agreement <= 1):
            raise ValueError("review config: takeover.min_control_source_agreement must be in (0, 1]")

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}


# --------------------------------------------------------------------------- results

@dataclass
class CheckResult:
    name: str
    status: str  # pass | fail | skip
    reason: str = ""  # human-readable; set for fail (and skip)
    detail: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def detail_json(self) -> str:
        """The ``qa_result.detail_json`` payload."""
        return json.dumps({"status": self.status, "reason": self.reason, **self.detail}, default=_jsonable)


@dataclass
class ChecksReport:
    results: list[CheckResult]
    thresholds: ReviewThresholds

    @property
    def passed(self) -> bool:
        return not any(r.failed for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.failed]

    def get(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def reason(self) -> str:
        return "; ".join(r.reason for r in self.failures)

    def metrics(self) -> dict:
        return {r.name: {"status": r.status, **({"reason": r.reason} if r.reason else {}), **r.detail}
                for r in self.results}


def _jsonable(v: Any) -> Any:
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
        if isinstance(v, np.ndarray):
            return v.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(v)


def _r(x: float, nd: int = 3) -> float | None:
    x = float(x)
    return None if math.isnan(x) else round(x, nd)


# --------------------------------------------------------------------------- helpers

def ts_us(v: Any) -> int | None:
    """An option<timestamp> in any shape the clients produce -> int micros, or None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        if "none" in v:
            return None
        if "some" in v:
            return ts_us(v["some"])
        if "__timestamp_micros_since_unix_epoch__" in v:
            return int(v["__timestamp_micros_since_unix_epoch__"])
    if isinstance(v, (list, tuple)):
        if len(v) == 1:
            return ts_us(v[0])
        if len(v) == 2 and isinstance(v[0], int):  # positional option [tag, value]
            return ts_us(v[1]) if v[0] == 0 else None
    if hasattr(v, "timestamp"):  # datetime
        return int(v.timestamp() * 1_000_000)
    raise ValueError(f"cannot read timestamp {v!r}")


def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _success_flag(v: Any) -> bool | None:
    if isinstance(v, dict):
        return None if "none" in v else _success_flag(v.get("some"))
    return v if isinstance(v, bool) else None


def sg_third_derivative_kernel(window: int):
    """Savitzky-Golay (cubic) kernel: dot(kernel, x[i-h:i+h+1]) = 3rd-derivative coefficient * 6,
    in units of x per tick^3."""
    import numpy as np

    h = window // 2
    k = np.arange(-h, h + 1, dtype=np.float64)
    a = np.vstack([k ** p for p in range(4)]).T
    return 6.0 * np.linalg.pinv(a)[3]


def joint_jerk(t_us, joint_pos, fps: float, window: int):
    """(jerk array (M, J) in rad/s^3 on a uniform fps grid, grid dt). Empty when too short."""
    import numpy as np

    t = (np.asarray(t_us, dtype=np.float64) - float(t_us[0])) / 1e6
    dt = 1.0 / fps
    grid = np.arange(0.0, t[-1] + 1e-9, dt)
    if len(grid) < window:
        return np.zeros((0, joint_pos.shape[1])), dt
    pos = np.column_stack([np.interp(grid, t, joint_pos[:, j]) for j in range(joint_pos.shape[1])])
    ker = sg_third_derivative_kernel(window)[::-1]  # convolve flips; pre-flip so it is a correlation
    jerk = np.column_stack([np.convolve(pos[:, j], ker, mode="valid") for j in range(pos.shape[1])])
    return jerk / dt ** 3, dt


# --------------------------------------------------------------------------- checks

def check_actions(actions: EpisodeActions | None, th: ReviewThresholds, error: str | None = None) -> CheckResult:
    import numpy as np

    if actions is None:
        return CheckResult("actions", FAIL, f"actions file unreadable: {error or 'not provided'}")
    d: dict[str, Any] = {"rows": actions.n, "joints": int(actions.joint_pos.shape[1]) if actions.n else 0,
                         "fps": actions.fps, "columns": actions.columns, "fixture": actions.is_fixture,
                         "format_version": actions.meta.get("version")}
    if actions.n < 2:
        return CheckResult("actions", FAIL, f"actions file has {actions.n} rows; need at least 2", d)
    if d["joints"] < 1:
        return CheckResult("actions", FAIL, "actions file has no joint positions", d)
    bad = int((~np.isfinite(actions.joint_pos)).sum())
    if bad:
        return CheckResult("actions", FAIL, f"actions file has {bad} non-finite joint values", d | {"non_finite": bad})
    unknown = sorted(set(actions.control_source) - set(CONTROL_SOURCES))
    if unknown:
        return CheckResult("actions", FAIL, f"unknown control_source values {unknown}", d)
    if actions.is_fixture and not th.allow_fixtures:
        return CheckResult("actions", FAIL, "actions are synthetic fixture data, not a real recording", d)
    return CheckResult("actions", PASS, "", d)


def check_duration(episode: Any, actions: EpisodeActions | None, th: ReviewThresholds) -> CheckResult:
    dur_s = (int(_g(episode, "duration_ms") or 0)) / 1000.0
    d: dict[str, Any] = {"duration_s": _r(dur_s), "min_s": th.min_duration_s, "max_s": th.max_duration_s}
    if actions is not None and actions.n >= 2:
        span = (int(actions.timestamp_us[-1]) - int(actions.timestamp_us[0])) / 1e6
        d["actions_span_s"] = _r(span)
    if dur_s < th.min_duration_s:
        return CheckResult("duration", FAIL, f"episode is {dur_s:.1f} s long; minimum is {th.min_duration_s:g} s", d)
    if dur_s > th.max_duration_s:
        return CheckResult("duration", FAIL, f"episode is {dur_s:.1f} s long; maximum is {th.max_duration_s:g} s", d)
    if "actions_span_s" in d and abs(d["actions_span_s"] - dur_s) > th.duration_tolerance_s:
        return CheckResult("duration", FAIL, (
            f"duration_ms says {dur_s:.1f} s but the actions cover {d['actions_span_s']:.1f} s "
            f"(tolerance {th.duration_tolerance_s:g} s)"), d)
    return CheckResult("duration", PASS, "", d)


def check_frame_drops(episode: Any, actions: EpisodeActions, th: ReviewThresholds) -> CheckResult:
    import numpy as np

    fps = actions.fps
    t = actions.timestamp_us.astype(np.int64)
    gaps = np.diff(t) / 1e6 * fps  # in ticks
    span_ticks = (int(t[-1]) - int(t[0])) / 1e6 * fps
    expected = int(round(span_ticks)) + 1
    non_increasing = int((gaps <= 0).sum())
    missing_ticks = int(np.clip(np.rint(gaps) - 1, 0, None).sum())
    ratios = {"actions": missing_ticks / expected if expected else 1.0}
    cams = actions.meta.get("cameras") or {}
    cam_detail = {}
    for name, c in cams.items():
        frames = c.get("frames") if isinstance(c, dict) else None
        if isinstance(frames, int):
            cam_expected = int(round(span_ticks * float(c.get("fps") or fps) / fps)) + 1
            ratios[f"camera:{name}"] = max(0, cam_expected - frames) / cam_expected if cam_expected else 1.0
            cam_detail[name] = {"frames": frames, "expected": cam_expected}
    worst_src, worst = max(ratios.items(), key=lambda kv: kv[1])
    fc = int(_g(episode, "frame_count") or 0)
    d = {"fps": fps, "expected_frames": expected, "rows": actions.n, "missing_ticks": missing_ticks,
         "drop_ratio": _r(worst, 4), "worst_stream": worst_src, "max_ratio": th.max_frame_drop_ratio,
         "episode_frame_count": fc, "non_increasing_timestamps": non_increasing, "cameras": cam_detail}
    if non_increasing:
        return CheckResult("frame_drops", FAIL, f"{non_increasing} actions timestamps do not increase", d)
    if abs(fc - actions.n) > th.max_frame_count_mismatch:
        return CheckResult("frame_drops", FAIL,
                           f"episode.frame_count is {fc} but the actions file has {actions.n} rows", d)
    if worst >= th.max_frame_drop_ratio:
        return CheckResult("frame_drops", FAIL, (
            f"{worst * 100:.1f}% of frames dropped ({worst_src}); limit is {th.max_frame_drop_ratio * 100:g}%"), d)
    return CheckResult("frame_drops", PASS, "", d)


def check_jerk(actions: EpisodeActions, th: ReviewThresholds) -> CheckResult:
    import numpy as np

    jerk, _ = joint_jerk(actions.timestamp_us, actions.joint_pos, actions.fps, th.jerk_window)
    if jerk.size == 0:
        return CheckResult("jerk", SKIP, f"too few frames for a {th.jerk_window}-tick jerk estimate",
                           {"window": th.jerk_window})
    rms = float(np.sqrt(np.mean(jerk ** 2)))
    per_joint = np.sqrt(np.mean(jerk ** 2, axis=0))
    d = {"jerk_rms": _r(rms, 2), "jerk_p99": _r(float(np.percentile(np.abs(jerk), 99)), 2),
         "jerk_rms_per_joint": [_r(x, 2) for x in per_joint], "worst_joint": int(np.argmax(per_joint)),
         "max_rms": th.max_jerk_rms, "window": th.jerk_window, "units": "rad/s^3"}
    if rms >= th.max_jerk_rms:
        return CheckResult("jerk", FAIL, (
            f"motion is jerky: RMS joint jerk {rms:.0f} rad/s^3 (worst joint {d['worst_joint']}) "
            f">= limit {th.max_jerk_rms:g}"), d)
    return CheckResult("jerk", PASS, "", d)


def check_takeover_timestamps(episode: Any, actions: EpisodeActions | None, th: ReviewThresholds) -> CheckResult:
    tk, hb = ts_us(_g(episode, "takeover_ts")), ts_us(_g(episode, "handback_ts"))
    d: dict[str, Any] = {"takeover_ts": tk, "handback_ts": hb}
    missing = [n for n, v in (("takeover_ts", tk), ("handback_ts", hb)) if v is None]
    if missing:
        return CheckResult("takeover_timestamps", FAIL, f"takeover episode is missing {' and '.join(missing)}", d)
    if hb <= tk:
        return CheckResult("takeover_timestamps", FAIL, "handback_ts is not after takeover_ts", d)
    d["takeover_s"] = _r((hb - tk) / 1e6)
    if actions is not None and actions.n >= 2:
        t0, t1 = int(actions.timestamp_us[0]), int(actions.timestamp_us[-1])
        tol = th.takeover_ts_tolerance_s * 1e6
        d.update(takeover_offset_s=_r((tk - t0) / 1e6), handback_offset_s=_r((hb - t0) / 1e6), span_s=_r((t1 - t0) / 1e6))
        if not (t0 - tol <= tk <= t1 + tol and t0 - tol <= hb <= t1 + tol):
            return CheckResult("takeover_timestamps", FAIL,
                               "takeover/handback timestamps fall outside the recorded actions", d)
    return CheckResult("takeover_timestamps", PASS, "", d)


def check_failure_before_takeover(episode: Any, actions: EpisodeActions, th: ReviewThresholds) -> CheckResult:
    import numpy as np

    tk, hb = ts_us(_g(episode, "takeover_ts")), ts_us(_g(episode, "handback_ts"))
    t = actions.timestamp_us
    src = np.asarray(actions.control_source)
    pre = t < tk
    during = (t >= tk) & (t < hb)
    pre_s = (tk - int(t[0])) / 1e6
    takeover_s = (hb - tk) / 1e6
    pre_policy = float((src[pre] == "policy").mean()) if pre.any() else 0.0
    during_human = float((src[during] == "human").mean()) if during.any() else 0.0
    d = {"pre_takeover_frames": int(pre.sum()), "pre_takeover_s": _r(pre_s), "takeover_frames": int(during.sum()),
         "takeover_s": _r(takeover_s), "pre_policy_fraction": _r(pre_policy), "takeover_human_fraction": _r(during_human),
         "min_pre_takeover_s": th.min_pre_takeover_s, "min_takeover_s": th.min_takeover_s}
    if pre_s < th.min_pre_takeover_s or not pre.any():
        return CheckResult("failure_before_takeover", FAIL, (
            f"only {max(pre_s, 0):.2f} s recorded before the takeover; need {th.min_pre_takeover_s:g} s of the "
            "policy failure to cut"), d)
    if takeover_s < th.min_takeover_s:
        return CheckResult("failure_before_takeover", FAIL,
                           f"human takeover lasted {takeover_s:.2f} s; minimum is {th.min_takeover_s:g} s", d)
    if pre_policy < th.min_control_source_agreement:
        return CheckResult("failure_before_takeover", FAIL, (
            f"only {pre_policy * 100:.0f}% of frames before takeover_ts are policy-driven; "
            "the failure segment does not match the timestamps"), d)
    if during_human < th.min_control_source_agreement:
        return CheckResult("failure_before_takeover", FAIL, (
            f"only {during_human * 100:.0f}% of frames between takeover and handback are human-driven"), d)
    return CheckResult("failure_before_takeover", PASS, "", d)


def check_success(episode: Any) -> CheckResult:
    s = _success_flag(_g(episode, "success"))
    if s is None:
        return CheckResult("success", FAIL, "task success flag is missing (operator did not confirm the outcome)",
                           {"success": None})
    if not s:
        return CheckResult("success", FAIL, "episode is marked unsuccessful (task not completed)", {"success": False})
    return CheckResult("success", PASS, "", {"success": True})


def run_checks(episode: Any, actions: EpisodeActions | None, th: ReviewThresholds | None = None,
               *, actions_error: str | None = None) -> ChecksReport:
    """Every deterministic check for one episode, in ``CHECK_ORDER``. Checks that do not apply to
    the episode's kind are left out; checks that need actions are skipped when actions failed."""
    th = th or ReviewThresholds()
    kind = _g(episode, "kind")
    res = [check_actions(actions, th, actions_error)]
    ok = res[0].passed or (actions is not None and res[0].reason.startswith("actions are synthetic"))
    usable = actions if ok else None
    res.append(check_duration(episode, usable, th))
    skip = lambda n: CheckResult(n, SKIP, "skipped: actions unusable")  # noqa: E731
    res.append(check_frame_drops(episode, usable, th) if usable is not None else skip("frame_drops"))
    res.append(check_jerk(usable, th) if usable is not None else skip("jerk"))
    if kind in th.takeover_kinds:
        ts = check_takeover_timestamps(episode, usable, th)
        res.append(ts)
        if usable is None:
            res.append(skip("failure_before_takeover"))
        elif not ts.passed:
            res.append(CheckResult("failure_before_takeover", SKIP, "skipped: takeover timestamps invalid"))
        else:
            res.append(check_failure_before_takeover(episode, usable, th))
    if kind in th.success_required_kinds:
        res.append(check_success(episode))
    return ChecksReport(res, th)
