"""TDM-32 onboarding checks: is this robot's adapter, camera rig and safety profile fit to run a ticket?

The Onboarding Droid (``rdf_factory.droids.onboarding``) supplies the judgement and writes the files;
this module does the checking, so a confused droid cannot talk its way past it. It is used through
the droid's single allow-listed tool (``python -m rdf_robot_client.onboarding_tool``) and is plain
library code otherwise (tests, the HIL script).

Check groups (``run_checks``), each ``{ok, errors, warnings, info}``:

* ``contract``    the adapter against the ``ArmAdapter`` interface (adapters/base.py): it constructs
                  from the config, implements every abstract method, its static facts agree (dof,
                  limits, home, joint names, describe()), and, in SIMULATION only, the live
                  protocol: connect, read_state/status, a homing move and a small EE move through
                  the TDM-45 SafetyLayer, hold, soft e-stop latch + latency, clear, disconnect.
* ``cameras``     the TDM-47 rig config: loads and validates, has the two policy cameras
                  (top -> observation/image, wrist -> observation/wrist_image), fps / size / MJPG /
                  stable device paths; in simulation a short synthetic capture (drops + pair skew).
* ``safety``      the TDM-45 profile for this adapter, plus the droid's optional overrides, which may
                  only TIGHTEN (``tighten_only``): smaller caps, larger margins, a smaller workspace,
                  never ``verified: true``. Sanity: the home pose is inside the safe joint range and
                  its tool point inside the workspace box, caps vs the adapter's own limits.
* ``kinematics``  FK -> IK -> FK round trip on seeded random joint configurations inside the safe
                  range plus the home pose (position / rotation error, IK success rate, timing).

HARDWARE NEVER MOVES HERE. When the adapter config is not a simulation, ``run_checks`` does not call
``connect()`` at all (connecting a YAM energises the motors, and a linear gripper runs its calibration
sweep on connect). It runs read-only probes instead (SDK importable, CAN interface up, robot heartbeat,
camera device nodes) plus the offline safety / kinematics checks, and lists every motion check in
``hil_required``: the droid parks on a human confirmation (``onboarding_hil``) for those.

Files the droid writes in its ticket dir and this module validates:
  adapter_config.yaml   which adapter + kwargs + safety profile (+ tighten-only overrides) + camera config
  success_check.yaml    how a trial of THIS ticket's task is judged: by the operator, never automatically
  reset_routine.yaml    home pose + gripper + what the human must put back between trials
  onboarding_report.md  the droid's write-up (published as is)
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import math
import os
import random
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from .adapters import ADAPTERS, ArmAdapter, ArmError, ArmStopped, IKError, Pose, make_adapter
from .adapters.base import pose_error

ROBOT_CLIENT_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = ROBOT_CLIENT_DIR.parents[1]
DEFAULT_CAMERAS = ROBOT_CLIENT_DIR / "config" / "cameras.yaml"
DEFAULT_SAFETY = ROBOT_CLIENT_DIR / "config" / "safety.yaml"

POLICY_CAMERAS = ("top", "wrist")  # S-05a feature schema: observation/image + observation/wrist_image
ROUND_TRIP_SAMPLES = 40
POS_TOL_M = 1e-3
ROT_TOL_RAD = 1e-2
IK_SUCCESS_MIN = 0.95
ESTOP_MAX_S = 0.05  # base.py: stop() must return in < 50 ms
SUCCESS_DECIDERS = ("operator", "human")

# Motion checks a human runs at the real arm (docs/yam-sdk-notes.md section 8, docs/safety-jetson-checklist.md).
HIL_MOTION_CHECKS = (
    "connect: arm holds its boot pose (no sag, no buzz); status().ok, sdk_loop_hz ~250",
    "zero pose: folded rest reads ~0 rad on every joint",
    "FK sanity: tape-measure the grasp point at 2-3 poses vs ee_pose (base frame +x forward, +z up)",
    "home: rest -> home pose at speed_scale <= 0.25 clears the table and fixtures",
    "gripper: open -> closed -> open matches gripper readings within 0.1 (record YAM_GRIPPER_LIMITS)",
    "CP-1 demo-square (10 cm, 2 cm/s, safety layer on): smooth, passed: true, state stream ~30 Hz",
    "soft e-stop mid-move halts within a tick; physical e-stop cuts power",
    "workspace: jog the tool to each face of the usable volume; shrink safety.yaml's box by 2-3 cm",
    "reset routine: run it once by hand and time it",
)


class OnboardingError(ValueError):
    """A droid-written file is malformed or breaks a rule (e.g. loosens a safety limit)."""


def _group() -> dict[str, Any]:
    return {"ok": True, "errors": [], "warnings": [], "info": {}}


def _fail(g: dict, msg: str) -> None:
    g["ok"] = False
    g["errors"].append(msg)


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(Path(p).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise OnboardingError(f"cannot read {p}: {e}") from e
    if not isinstance(d, dict):
        raise OnboardingError(f"{p} must be a YAML mapping")
    return d


# =========================================================================== adapter config

def draft_adapter_config(robot: dict, *, cameras_config: Path = DEFAULT_CAMERAS) -> dict:
    """A starting adapter config for a registered ``robot`` row (the droid validates or edits it).

    embodiment ``sim_yam`` with an ``arm.adapter == "sim"`` announcement -> the TDM-49 SimArm;
    other ``sim_*`` -> YamArm on the SDK's MuJoCo sim; anything else -> the real YAM over CAN.
    """
    try:
        cc = yaml.safe_load(robot.get("camera_config_json") or "{}") or {}
    except yaml.YAMLError:
        cc = {}
    arm = cc.get("arm") if isinstance(cc, dict) and isinstance(cc.get("arm"), dict) else {}
    emb = str(robot.get("embodiment") or "")
    if arm.get("adapter") == "sim" or emb == "sim":
        adapter, kwargs = "sim", {}
    elif emb.startswith("sim_"):
        adapter, kwargs = "yam", {"sim": True, "arm_type": arm.get("arm_type") or "yam",
                                  "gripper_type": arm.get("gripper_type") or "linear_4310", "kinematics": "auto"}
    else:
        adapter, kwargs = "yam", {"sim": False, "channel": arm.get("can_channel") or "can0",
                                  "arm_type": arm.get("arm_type") or "yam",
                                  "gripper_type": arm.get("gripper_type") or "linear_4310", "kinematics": "auto"}
    return {"robot": robot.get("name"), "adapter": adapter, "kwargs": kwargs, "safety_profile": adapter,
            "safety_overrides": {}, "cameras_config": _rel(cameras_config), "rationale": []}


def _rel(p: Path) -> str:
    p = Path(p).resolve()
    try:
        return str(p.relative_to(REPO_DIR))
    except ValueError:
        return str(p)


def is_sim(cfg: dict) -> bool:
    return cfg.get("adapter") == "sim" or bool((cfg.get("kwargs") or {}).get("sim"))


def validate_adapter_config(cfg: dict) -> list[str]:
    errs = []
    if cfg.get("adapter") not in ADAPTERS:
        errs.append(f"adapter must be one of {sorted(ADAPTERS)}, got {cfg.get('adapter')!r}")
    if not isinstance(cfg.get("kwargs") or {}, dict):
        errs.append("kwargs must be a mapping")
    if not isinstance(cfg.get("safety_overrides") or {}, dict):
        errs.append("safety_overrides must be a mapping (or empty)")
    if not cfg.get("cameras_config"):
        errs.append("cameras_config is required (path to the camera rig YAML)")
    bad = sorted(set(cfg.get("kwargs") or {}) - {"sim", "channel", "arm_type", "gripper_type", "kinematics",
                                                  "gripper_limits", "smoothing", "servo_rate_hz", "control_rate_hz",
                                                  "home", "start", "limits"})
    if bad:
        errs.append(f"unknown adapter kwargs {bad}")
    if not [str(x) for x in (cfg.get("rationale") or []) if str(x).strip()]:
        errs.append("rationale: say in 2-5 lines why this adapter/kwargs/profile fits the registered robot")
    return errs


# =========================================================================== stand-in for the SDK sim

class TeleportRobot:
    """Duck-types the I2RT ``Robot`` protocol YamArm uses: teleports to setpoints, no dynamics.

    Only for ``YamArm(sim=True)`` when the I2RT SDK (and so its MuJoCo SimRobot) is not installed:
    the real YamArm code (servo thread, smoothing, clip, hold/stop) runs against it. Labelled
    ``yam_sim_backend: teleport_stand_in`` in the results.
    """

    def __init__(self, q0: Sequence[float], g0: float = 1.0) -> None:
        import threading

        import numpy as np

        self._np = np
        self.q = np.array(q0, dtype=float)
        self.g = float(g0)
        self.lock = threading.Lock()

    def get_observations(self) -> dict:
        np = self._np
        with self.lock:
            return {"joint_pos": self.q.copy(), "joint_vel": np.zeros(len(self.q)), "gripper_pos": np.array([self.g])}

    def command_joint_pos(self, cmd: Any) -> None:
        with self.lock:
            self.q = self._np.array(cmd[: len(self.q)], dtype=float)
            if len(cmd) > len(self.q):
                self.g = float(cmd[len(self.q)])

    def close(self) -> None:
        pass


def build_adapter(cfg: dict) -> tuple[ArmAdapter, dict]:
    """Construct (never connect) the adapter the config names. Returns (arm, notes)."""
    kw = dict(cfg.get("kwargs") or {})
    notes: dict[str, Any] = {}
    if cfg.get("adapter") == "yam" and kw.get("sim"):
        if importlib.util.find_spec("i2rt") is None:
            from .adapters.yam import YAM_REST

            kw["robot_factory"] = lambda _arm: TeleportRobot(YAM_REST)
            notes["yam_sim_backend"] = "teleport_stand_in (I2RT SDK not installed: no MuJoCo SimRobot)"
        else:
            notes["yam_sim_backend"] = "i2rt MuJoCo SimRobot"
    for k in ("home", "start"):
        if k in kw and kw[k] is not None:
            kw[k] = tuple(float(v) for v in kw[k])
    return make_adapter(str(cfg["adapter"]), **kw), notes


# =========================================================================== safety: tighten-only

def _num(v: Any) -> float:
    return float(v)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# key path -> "le" (new <= old), "ge" (new >= old), "vec_le", "vec_ge", "range" (inside), special
_TIGHTEN_RULES: dict[tuple[str, ...], str] = {
    ("speed_scale",): "le", ("max_speed_scale",): "le", ("slow_mode_scale",): "le",
    ("verified",): "verified",
    ("joints", "margin_rad"): "ge", ("joints", "lower"): "vec_lower", ("joints", "upper"): "vec_upper",
    ("joints", "max_velocity"): "vec_le", ("joints", "max_acceleration"): "vec_le", ("joints", "max_jump_rad"): "le",
    ("reject", "action"): "same", ("reject", "max_consecutive"): "le",
    ("workspace", "enabled"): "enabled", ("workspace", "x"): "range", ("workspace", "y"): "range",
    ("workspace", "z"): "range", ("workspace", "table_floor_z"): "ge", ("workspace", "table_clearance_m"): "ge",
    ("gripper", "min"): "ge", ("gripper", "max"): "le", ("gripper", "max_effort"): "le_or_new",
    ("watchdog_ms",): "watchdog",
}


def _leaves(d: dict, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    out = []
    for k, v in d.items():
        path = (*prefix, str(k))
        if isinstance(v, dict):
            out += _leaves(v, path)
        else:
            out.append((path, v))
    return out


def _get(d: dict, path: tuple[str, ...]) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)  # type: ignore[assignment]
    return d


def tighten_only(base: dict, overrides: dict, *, adapter_limits: Any = None) -> list[str]:
    """Every override must be at least as strict as ``base``. Returns the violations ([] = ok).

    ``adapter_limits`` (JointLimits) stands in for a ``null`` joints.lower / upper in ``base``.
    """
    errs: list[str] = []
    for path, new in _leaves(overrides or {}):
        name = ".".join(path)
        rule = _TIGHTEN_RULES.get(path)
        old = _get(base, path)
        if rule is None:
            errs.append(f"{name}: not an overridable safety key (allowed: "
                        f"{sorted('.'.join(p) for p in _TIGHTEN_RULES)})")
            continue
        try:
            if rule == "le" and not _num(new) <= _num(old):
                errs.append(f"{name}: {new} > {old} loosens the limit (may only go down)")
            elif rule == "ge" and not _num(new) >= _num(old):
                errs.append(f"{name}: {new} < {old} loosens the limit (may only go up)")
            elif rule == "verified" and bool(new) and not bool(old):
                errs.append("verified: only a human at the arm may set verified: true (safety checklist)")
            elif rule == "same" and new != old:
                errs.append(f"{name}: {new!r} != {old!r} (reject behaviour is not overridable)")
            elif rule == "enabled" and not bool(new) and bool(old):
                errs.append("workspace.enabled: disabling the workspace box loosens the limits")
            elif rule == "watchdog" and (_num(new) <= 0 or _num(new) > _num(old)):
                errs.append(f"watchdog_ms: {new} must be > 0 and <= {old} (a longer or disabled watchdog loosens it)")
            elif rule == "le_or_new" and new is None and old is not None:
                errs.append(f"{name}: null removes the {old} limit")
            elif rule == "le_or_new" and new is not None and old is not None and not _num(new) <= _num(old):
                errs.append(f"{name}: {new} > {old} loosens the limit")
            elif rule == "range":
                lo, hi = (_num(new[0]), _num(new[1]))
                olo, ohi = (_num(old[0]), _num(old[1]))
                if lo < olo or hi > ohi or lo >= hi:
                    errs.append(f"{name}: {list(new)} is not a non-empty sub-range of {list(old)}")
            elif rule in ("vec_le", "vec_lower", "vec_upper"):
                if old is None and rule != "vec_le":
                    if adapter_limits is None:
                        errs.append(f"{name}: no adapter limits to compare with")
                        continue
                    old = adapter_limits.lower if rule == "vec_lower" else adapter_limits.upper
                if old is None or len(new) != len(old):
                    errs.append(f"{name}: needs {len(old) if old is not None else '?'} values, got {len(new)}")
                    continue
                for i, (n, o) in enumerate(zip(new, old)):
                    bad = (_num(n) > _num(o)) if rule in ("vec_le", "vec_upper") else (_num(n) < _num(o))
                    if bad:
                        errs.append(f"{name}[{i}]: {n} vs {o} loosens the limit")
        except (TypeError, ValueError, IndexError) as e:
            errs.append(f"{name}: bad value {new!r} ({type(e).__name__})")
    return errs


def safety_config_for(cfg: dict, arm: ArmAdapter, *, safety_path: Path = DEFAULT_SAFETY) -> tuple[Any, dict, dict]:
    """(SafetyConfig with the overrides applied, base profile dict, merged profile dict).

    Raises OnboardingError if the overrides loosen anything.
    """
    from .safety import SafetyConfig

    data = load_yaml(safety_path)
    profile = str(cfg.get("safety_profile") or arm.name)
    profiles = data.get("profiles") or {}
    if profile not in profiles:
        raise OnboardingError(f"no safety profile {profile!r} in {safety_path} (have {sorted(profiles)})")
    base = profiles[profile]
    over = cfg.get("safety_overrides") or {}
    errs = tighten_only(base, over, adapter_limits=arm.limits)
    if errs:
        raise OnboardingError("safety_overrides refused: " + "; ".join(errs))
    merged = _deep_merge(base, over)
    return SafetyConfig.from_dict(profile, merged, top=data, source=f"{safety_path} + onboarding overrides"), base, merged


# =========================================================================== check groups

def check_contract(cfg: dict, *, live: bool) -> dict:
    """Interface contract. ``live`` (simulation only) also drives the adapter through the SafetyLayer."""
    g = _group()
    try:
        arm, notes = build_adapter(cfg)
    except Exception as e:  # noqa: BLE001
        _fail(g, f"adapter {cfg.get('adapter')!r} does not construct from the config: {type(e).__name__}: {e}")
        return g
    g["info"].update(notes)
    cls = type(arm)
    g["info"].update({"class": f"{cls.__module__}.{cls.__name__}", "name": arm.name, "embodiment": arm.embodiment,
                      "adapter_version": arm.adapter_version, "dof": arm.dof, "control_rate_hz": arm.control_rate_hz})
    missing = sorted(getattr(cls, "__abstractmethods__", set()))
    if not isinstance(arm, ArmAdapter) or inspect.isabstract(cls) or missing:
        _fail(g, f"{cls.__name__} does not implement the ArmAdapter interface (missing {missing})")
    lim = arm.limits
    n = arm.dof
    for what, v in (("limits.lower", lim.lower), ("limits.upper", lim.upper), ("limits.max_velocity", lim.max_velocity),
                    ("home_joints", arm.home_joints), ("joint_names", arm.joint_names)):
        if len(v) != n:
            _fail(g, f"{what} has {len(v)} entries, dof is {n}")
    if any(lo >= hi for lo, hi in zip(lim.lower, lim.upper)):
        _fail(g, "a joint has lower >= upper limit")
    if any(v <= 0 for v in lim.max_velocity):
        _fail(g, "a joint has max_velocity <= 0")
    if not lim.within(arm.home_joints):
        _fail(g, f"home_joints {list(arm.home_joints)} outside the adapter's joint limits")
    if not str(arm.adapter_version).strip():
        _fail(g, "adapter_version is empty: robot.adapter_version needs a value")
    if not 1 <= arm.control_rate_hz <= 1000:
        _fail(g, f"control_rate_hz {arm.control_rate_hz} is implausible")
    d = arm.describe()
    for k in ("adapter", "embodiment", "adapter_version", "dof", "control_rate_hz"):
        if k not in d:
            _fail(g, f"describe() lacks {k!r}")
    sim = is_sim(cfg)
    if sim and not (str(arm.embodiment).startswith("sim") or d.get("sim")):
        _fail(g, f"simulated adapter reports embodiment {arm.embodiment!r}: it must not pass for hardware")
    if abs(arm.control_rate_hz - 30.0) > 1e-6:
        g["warnings"].append(f"control_rate_hz {arm.control_rate_hz}: the policy and recorder run at 30 Hz")
    g["info"]["describe"] = {k: v for k, v in d.items() if k not in ("action_space",)}
    if not live:
        g["info"]["live"] = "skipped: hardware is never connected or moved by onboarding (see hil_required)"
        return g
    _live_protocol(cfg, arm, g)
    return g


def _live_protocol(cfg: dict, arm: ArmAdapter, g: dict) -> None:
    """Simulation only: the runtime half of the contract, through the TDM-45 SafetyLayer."""
    from .safety import SafetyLayer

    steps: list[str] = []
    try:
        scfg, _, _ = safety_config_for(cfg, arm)
        safe = SafetyLayer(arm, scfg, start_watchdog=False)
    except Exception as e:  # noqa: BLE001
        _fail(g, f"cannot wrap the adapter in the SafetyLayer: {type(e).__name__}: {e}")
        return
    try:
        safe.connect()
        safe.connect()  # idempotent
        steps.append("connect x2")
        st = safe.status()
        if not st.connected:
            _fail(g, "status().connected is false after connect()")
        s = safe.read_state()
        if len(s.joints) != arm.dof or len(s.state_vector()) != arm.dof + 1:
            _fail(g, f"read_state: {len(s.joints)} joints / state_vector {len(s.state_vector())} (dof {arm.dof})")
        if not 0.0 <= s.gripper <= 1.0:
            _fail(g, f"read_state: gripper {s.gripper} outside 0..1")
        qn = math.sqrt(sum(v * v for v in s.ee_pose.quat))
        if abs(qn - 1.0) > 1e-3:
            _fail(g, f"read_state: ee_pose quaternion norm {qn:.4f}")
        steps.append("read_state/status")
        safe.set_speed_scale(1.0)
        t0 = time.monotonic()
        arrived = safe.home(speed_fraction=0.5)
        steps.append(f"home via SafetyLayer ({time.monotonic() - t0:.1f}s, arrived={arrived})")
        g["info"]["home_s"] = round(time.monotonic() - t0, 2)
        err_home = max(abs(a - b) for a, b in zip(safe.joint_positions(), arm.home_joints))
        g["info"]["home_err_rad"] = round(err_home, 4)
        if err_home > 0.02:
            _fail(g, f"home(): ended {err_home:.3f} rad from home_joints")
        p0 = safe.ee_pose()
        target = p0.translated((0.0, 0.02, 0.0))
        for _ in range(int(arm.control_rate_hz * 1.0)):  # 1 s of streamed EE setpoints, like teleop
            safe.command_ee(target)
            time.sleep(1.0 / arm.control_rate_hz)
        dp, _ = pose_error(target, safe.ee_pose())
        g["info"]["ee_move_err_m"] = round(dp, 4)
        if dp > 0.005:
            g["warnings"].append(f"2 cm EE move ended {dp * 1000:.1f} mm from target after 1 s (speed caps?)")
        steps.append("command_ee 2 cm (streamed 1 s)")
        safe.command_gripper(0.0)
        safe.command_gripper(1.0)
        safe.hold()
        steps.append("gripper + hold")
        t0 = time.monotonic()
        safe.stop()
        dt = time.monotonic() - t0
        g["info"]["estop_return_ms"] = round(dt * 1000, 2)
        if dt > ESTOP_MAX_S:
            _fail(g, f"stop() took {dt * 1000:.0f} ms (contract: < 50 ms)")
        try:
            safe.command_joints(safe.joint_positions())
            _fail(g, "command_joints after stop() did not raise ArmStopped (e-stop does not latch)")
        except ArmStopped:
            pass
        if not safe.status().estopped:
            _fail(g, "status().estopped is false while the soft e-stop is latched")
        safe.clear_stop()
        safe.command_joints(safe.joint_positions())
        steps.append("soft e-stop latch + clear")
        st = safe.status()
        if not st.ok:
            _fail(g, f"status() not ok after clear_stop: fault={st.fault} estopped={st.estopped}")
        if hasattr(safe, "stats"):
            g["info"]["safety_counters"] = {k: v for k, v in safe.stats().items() if isinstance(v, int | float) and v}
    except (ArmError, IKError) as e:
        _fail(g, f"live protocol failed: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        _fail(g, f"live protocol crashed: {type(e).__name__}: {e}")
    finally:
        try:
            safe.disconnect()
            safe.disconnect()  # idempotent
            steps.append("disconnect x2")
        except Exception as e:  # noqa: BLE001
            _fail(g, f"disconnect() raised: {type(e).__name__}: {e}")
    g["info"]["live_steps"] = steps


def resolve_camera_path(cfg: dict, *, ticket_dir: Path | None = None) -> Path:
    p = Path(str(cfg.get("cameras_config") or DEFAULT_CAMERAS))
    if p.is_absolute():
        return p
    if ticket_dir is not None and (ticket_dir / p).exists():
        return ticket_dir / p
    return REPO_DIR / p


def check_cameras(cfg: dict, *, live: bool, robot: dict | None = None, ticket_dir: Path | None = None,
                  capture_s: float = 3.0) -> dict:
    g = _group()
    path = resolve_camera_path(cfg, ticket_dir=ticket_dir)
    g["info"]["path"] = _rel(path)
    try:
        from .cameras import load_config
    except Exception as e:  # noqa: BLE001 - cv2 missing etc.
        _fail(g, f"camera module does not import: {type(e).__name__}: {e}")
        return g
    try:
        rig = load_config(path)
    except Exception as e:  # noqa: BLE001
        _fail(g, f"camera config {path} does not load: {type(e).__name__}: {e}")
        return g
    names = [c.name for c in rig.cameras]
    g["info"].update({"cameras": names, "reference": rig.reference, "policy_size": rig.policy_size,
                      "sha256": sha256_file(path)})
    for want in POLICY_CAMERAS:
        if want not in names:
            _fail(g, f"no {want!r} camera: the pi0.5 policy needs {POLICY_CAMERAS} "
                     "(observation/image + observation/wrist_image)")
    if rig.policy_size != 224:
        _fail(g, f"policy_size {rig.policy_size}: S-05a trains and infers at 224")
    devices = [str(c.device) for c in rig.cameras if c.source == "v4l2" and c.device is not None]
    if len(set(devices)) != len(devices):
        _fail(g, f"two cameras share a device: {devices}")
    big = [c for c in rig.cameras if c.width * c.height >= 640 * 480 and c.fps >= 30]
    for c in rig.cameras:
        if c.fps != 30:
            g["warnings"].append(f"{c.name}: {c.fps} fps; episodes and the policy run at 30 fps")
        if len(big) > 1 and c in big and c.source == "v4l2" and (c.fourcc or "").upper() != "MJPG":
            _fail(g, f"{c.name}: {c.width}x{c.height}@{c.fps} without MJPG; two such YUYV streams do not fit one USB 2 bus")
        if c.source == "v4l2" and str(c.device).startswith("/dev/video"):
            g["warnings"].append(f"{c.name}: device {c.device} is an index path that can swap on reboot; "
                                 "use /dev/v4l/by-id/... (`python -m rdf_robot_client.cameras list` on the Jetson)")
    if robot:
        try:
            cc = yaml.safe_load(robot.get("camera_config_json") or "{}") or {}
        except yaml.YAMLError:
            cc = {}
        announced = sorted((cc.get("cameras") or {}).keys()) if isinstance(cc.get("cameras"), dict) else None
        g["info"]["robot_announced_cameras"] = announced
        if announced and set(announced) != set(names):
            g["warnings"].append(f"robot row announces cameras {announced}, config has {names}")
    if live:
        try:
            from .cameras import CameraRig, run_capture_check, synthetic_config

            r = CameraRig(synthetic_config(rig))
            rep = run_capture_check(r, seconds=capture_s, warmup=0.5)
            r.stop()
            g["info"]["synthetic_capture"] = {"pass": rep["pass"], "per_camera": rep.get("per_camera"),
                                              "sync": {k: (rep.get("sync") or {}).get(k) for k in
                                                       ("skew_ms_p50", "skew_ms_p95", "out_of_sync", "pass")},
                                              "note": "SYNTHETIC frames with the configured size/fps: checks the "
                                                      "pairing and timing path, not the real lenses"}
            if not rep["pass"]:
                _fail(g, f"synthetic capture check failed: {rep.get('reason') or rep.get('sync')}")
        except Exception as e:  # noqa: BLE001
            _fail(g, f"synthetic capture check crashed: {type(e).__name__}: {e}")
    else:
        found = {c.name: _device_present(c.device) for c in rig.cameras if c.source == "v4l2"}
        g["info"]["device_nodes"] = found
        g["info"]["live"] = "no capture: read-only probe of device nodes only"
    return g


def _device_present(dev: Any) -> str:
    import glob

    if not str(dev).startswith("/dev/"):
        return "not a device path"
    if not Path("/dev").exists() or os.uname().sysname != "Linux":
        return "not checked (not the robot computer)"
    return "present" if glob.glob(str(dev)) else "MISSING"


def check_safety(cfg: dict, arm: ArmAdapter | None = None) -> dict:
    g = _group()
    try:
        arm = arm or build_adapter(cfg)[0]
        scfg, base, merged = safety_config_for(cfg, arm)
        scfg.validate(dof=arm.dof)
    except Exception as e:  # noqa: BLE001
        _fail(g, f"{type(e).__name__}: {e}")
        return g
    from .safety import SafetyLayer

    over = cfg.get("safety_overrides") or {}
    g["info"].update({"profile": scfg.profile, "verified": scfg.verified, "overrides": over,
                      "tightened": [".".join(p) for p, _ in _leaves(over)]})
    try:
        layer = SafetyLayer(arm, scfg, start_watchdog=False)
        lo, hi = layer._effective_bounds()
    except Exception as e:  # noqa: BLE001
        _fail(g, f"safety layer refuses this profile on {arm.name}: {type(e).__name__}: {e}")
        return g
    g["info"]["safe_joint_range"] = {"lower": [round(v, 3) for v in lo], "upper": [round(v, 3) for v in hi]}
    home = arm.home_joints
    outside = [i for i, (q, a, b) in enumerate(zip(home, lo, hi)) if not a - 1e-9 <= q <= b + 1e-9]
    if outside:
        _fail(g, f"home pose joints {outside} are outside the safe range (limits - margin): homing would be clamped")
    try:
        p = arm.fk(home).xyz
        g["info"]["home_tool_xyz"] = [round(v, 4) for v in p]
        v = scfg.workspace.violation(p)
        if v > 0:
            _fail(g, f"home tool point {[round(x, 3) for x in p]} is {v * 100:.1f} cm outside the workspace box "
                     f"{list(scfg.workspace.lo)}..{list(scfg.workspace.hi)}")
    except Exception as e:  # noqa: BLE001
        _fail(g, f"fk(home) failed: {type(e).__name__}: {e}")
    over_cap = [i for i, (c, a) in enumerate(zip(scfg.max_velocity, arm.limits.max_velocity)) if c > a]
    if over_cap:
        g["warnings"].append(f"joints {over_cap}: safety cap above the adapter's own max_velocity "
                             f"({[scfg.max_velocity[i] for i in over_cap]} vs {[arm.limits.max_velocity[i] for i in over_cap]}); "
                             "the layer uses the lower of the two")
    sim = is_sim(cfg) or arm.name == "sim"
    if not scfg.verified:
        msg = (f"profile {scfg.profile!r} is NOT verified on the arm: speed is capped at "
               f"{scfg.unverified_max_speed_scale} on hardware until a human runs docs/safety-jetson-checklist.md")
        (g["warnings"] if not sim else g["info"].setdefault("notes", [])).append(msg)
    if scfg.watchdog_ms <= 0:
        _fail(g, "watchdog disabled (watchdog_ms 0)")
    if not scfg.workspace.enabled:
        _fail(g, "workspace box disabled")
    return g


def check_kinematics(cfg: dict, arm: ArmAdapter | None = None, *, samples: int = ROUND_TRIP_SAMPLES,
                     seed: int = 32) -> dict:
    g = _group()
    try:
        arm = arm or build_adapter(cfg)[0]
        scfg, _, _ = safety_config_for(cfg, arm)
        from .safety import SafetyLayer

        lo, hi = SafetyLayer(arm, scfg, start_watchdog=False)._effective_bounds()
    except Exception as e:  # noqa: BLE001
        _fail(g, f"{type(e).__name__}: {e}")
        return g
    rng = random.Random(seed)
    qs = [tuple(arm.home_joints)]
    for _ in range(samples):
        # near-home configurations (the task's working set): home +/- 0.6 rad, inside the safe range
        qs.append(tuple(min(max(h + rng.uniform(-0.6, 0.6), a), b) for h, a, b in zip(arm.home_joints, lo, hi)))
    pos_errs, rot_errs, times, fails = [], [], [], []
    for i, q in enumerate(qs):
        pose = arm.fk(q)
        guess = tuple(v + rng.uniform(-0.05, 0.05) for v in q)
        t0 = time.perf_counter()
        try:
            q2 = arm.ik(pose, guess)
        except IKError as e:
            fails.append({"sample": i, "error": str(e)[:120]})
            continue
        times.append((time.perf_counter() - t0) * 1000)
        dp, dr = pose_error(pose, arm.fk(q2))
        pos_errs.append(dp)
        rot_errs.append(dr)
        if not arm.limits.within(q2, tol=1e-6):
            fails.append({"sample": i, "error": "IK solution outside joint limits"})
    n = len(qs)
    ok_n = n - len(fails)
    times.sort()
    g["info"].update({
        "samples": n, "ik_success": ok_n, "ik_success_rate": round(ok_n / n, 3),
        "pos_err_max_mm": round(max(pos_errs) * 1000, 4) if pos_errs else None,
        "rot_err_max_mrad": round(max(rot_errs) * 1000, 4) if rot_errs else None,
        "ik_ms_p50": round(times[len(times) // 2], 2) if times else None,
        "ik_ms_p95": round(times[min(len(times) - 1, int(0.95 * len(times)))], 2) if times else None,
        "backend": getattr(getattr(arm, "kin", None), "backend", arm.name), "seed": seed,
        "tolerances": {"pos_m": POS_TOL_M, "rot_rad": ROT_TOL_RAD, "success_min": IK_SUCCESS_MIN},
    })
    if fails:
        g["info"]["failures"] = fails[:5]
    if ok_n / n < IK_SUCCESS_MIN:
        _fail(g, f"IK round trip succeeded on {ok_n}/{n} near-home configurations (< {IK_SUCCESS_MIN:.0%})")
    if pos_errs and max(pos_errs) > POS_TOL_M:
        _fail(g, f"FK->IK->FK position error {max(pos_errs) * 1000:.2f} mm > {POS_TOL_M * 1000:.1f} mm")
    if rot_errs and max(rot_errs) > ROT_TOL_RAD:
        _fail(g, f"FK->IK->FK rotation error {max(rot_errs):.4f} rad > {ROT_TOL_RAD}")
    if times and times[int(0.95 * (len(times) - 1))] > 1000 / 30 / 2:
        g["warnings"].append(f"IK p95 {g['info']['ik_ms_p95']} ms is over half a 30 Hz tick")
    return g


def hardware_probe(cfg: dict, robot: dict | None) -> dict:
    """Read-only facts about the real setup. Never opens the arm."""
    g = _group()
    kw = cfg.get("kwargs") or {}
    sdk = importlib.util.find_spec("i2rt") is not None
    g["info"]["i2rt_sdk_importable"] = sdk
    if cfg.get("adapter") == "yam" and not sdk:
        g["warnings"].append("I2RT SDK not importable here: run onboarding's hardware probe on the Jetson "
                             "(docs/yam-sdk-notes.md section 1)")
    ch = str(kw.get("channel") or os.environ.get("YAM_CAN_CHANNEL", "can0"))
    net = Path("/sys/class/net") / ch
    if net.exists():
        state = (net / "operstate").read_text().strip() if (net / "operstate").exists() else "unknown"
        g["info"]["can"] = {"interface": ch, "operstate": state}
        if state not in ("up", "unknown"):
            g["warnings"].append(f"CAN interface {ch} is {state}: `sudo ip link set {ch} up type can bitrate 1000000`")
    else:
        g["info"]["can"] = {"interface": ch, "present": False, "note": "not on the robot computer, or CAN not set up"}
    if robot:
        g["info"]["robot_status"] = robot.get("status")
        g["info"]["robot_last_heartbeat"] = robot.get("last_heartbeat")
        if robot.get("status") not in ("online", "busy", "paused"):
            g["warnings"].append(f"robot row status is {robot.get('status')!r}: the robot client is not running")
    g["info"]["note"] = "read-only: the arm was not connected or moved"
    return g


def run_checks(cfg: dict, *, robot: dict | None = None, ticket_dir: Path | None = None,
               capture_s: float = 3.0) -> dict:
    """All groups. ``live`` motion/capture only when the config is a simulation."""
    errs = validate_adapter_config(cfg)
    sim = is_sim(cfg)
    out: dict[str, Any] = {"mode": "sim" if sim else "hardware", "config_errors": errs}
    if errs:
        out.update({"ok": False, "groups": {}, "hil_required": []})
        return out
    groups = {"contract": check_contract(cfg, live=sim)}
    try:
        arm = build_adapter(cfg)[0]
    except Exception:  # noqa: BLE001 - reported by contract
        arm = None
    groups["cameras"] = check_cameras(cfg, live=sim, robot=robot, ticket_dir=ticket_dir, capture_s=capture_s)
    groups["safety"] = check_safety(cfg, arm) if arm else {**_group(), "ok": False, "errors": ["no adapter"]}
    groups["kinematics"] = check_kinematics(cfg, arm) if arm else {**_group(), "ok": False, "errors": ["no adapter"]}
    if not sim:
        groups["hardware_probe"] = hardware_probe(cfg, robot)
    out["groups"] = groups
    out["ok"] = all(gr["ok"] for gr in groups.values())
    out["hil_required"] = list(HIL_MOTION_CHECKS)
    out["hil_note"] = ("simulation passed; these motion checks still need a human at the real arm before "
                       "hardware use" if sim else "hardware: nothing moved; a human must run these and confirm")
    return out


# =========================================================================== droid-written specs

def validate_success_check(spec: dict, *, ticket_id: int, cameras: Sequence[str] = POLICY_CAMERAS) -> list[str]:
    """The success check is a spec the OPERATOR applies. Nothing here (or anywhere) auto-decides success."""
    errs = []
    if int(spec.get("ticket_id") or -1) != int(ticket_id):
        errs.append(f"ticket_id must be {ticket_id}")
    if not str(spec.get("task") or "").strip():
        errs.append("task: the ticket's task text")
    if spec.get("decided_by") not in SUCCESS_DECIDERS:
        errs.append(f"decided_by must be one of {SUCCESS_DECIDERS}: success is judged by a person, never automatically")
    if spec.get("auto_decide") is not False:
        errs.append("auto_decide must be present and false")
    for k in ("success_when", "failure_when"):
        v = spec.get(k)
        if not isinstance(v, list) or not [x for x in v if str(x).strip()]:
            errs.append(f"{k}: a non-empty list of observable conditions")
    if spec.get("camera") not in cameras:
        errs.append(f"camera must be one of {list(cameras)} (the view the operator judges from)")
    try:
        tl = float(spec.get("time_limit_s"))
        if not 5 <= tl <= 600:
            errs.append("time_limit_s must be 5..600")
    except (TypeError, ValueError):
        errs.append("time_limit_s: number of seconds per trial")
    for k in ("start_state", "operator_prompt"):
        if not str(spec.get(k) or "").strip():
            errs.append(f"{k}: required text")
    return errs


def validate_reset_routine(spec: dict, arm: ArmAdapter, scfg: Any) -> list[str]:
    """Description of the between-trials reset. Checked against the arm model; nothing is executed."""
    errs = []
    q = spec.get("home_joints")
    try:
        q = tuple(float(v) for v in q)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return ["home_joints: a list of joint angles (rad)"]
    if len(q) != arm.dof:
        errs.append(f"home_joints has {len(q)} values, the arm has {arm.dof}")
    else:
        from .safety import SafetyLayer

        lo, hi = SafetyLayer(arm, scfg, start_watchdog=False)._effective_bounds()
        bad = [i for i, (v, a, b) in enumerate(zip(q, lo, hi)) if not a - 1e-9 <= v <= b + 1e-9]
        if bad:
            errs.append(f"home_joints {bad} outside the safe joint range")
        if scfg.workspace.violation(arm.fk(q).xyz) > 0:
            errs.append("home_joints puts the tool point outside the workspace box")
    try:
        g = float(spec.get("gripper"))
        if not 0.0 <= g <= 1.0:
            errs.append("gripper must be 0..1 (1 = open)")
    except (TypeError, ValueError):
        errs.append("gripper: 0..1 (1 = open)")
    try:
        sf = float(spec.get("speed_fraction"))
        if not 0 < sf <= 0.5:
            errs.append("speed_fraction must be in (0, 0.5]")
    except (TypeError, ValueError):
        errs.append("speed_fraction: (0, 0.5]")
    hs = spec.get("human_steps")
    if not isinstance(hs, list) or not [x for x in hs if str(x).strip()]:
        errs.append("human_steps: what the person puts back between trials (non-empty list)")
    if spec.get("requires_human") is not True:
        errs.append("requires_human must be true (objects are reset by a person)")
    if not str(spec.get("operator_prompt") or "").strip():
        errs.append("operator_prompt: the text shown to the person doing the reset")
    try:
        if not 5 <= float(spec.get("est_seconds")) <= 900:
            errs.append("est_seconds must be 5..900")
    except (TypeError, ValueError):
        errs.append("est_seconds: estimated reset time")
    return errs


__all__ = [
    "HIL_MOTION_CHECKS", "OnboardingError", "TeleportRobot", "build_adapter", "check_cameras", "check_contract",
    "check_kinematics", "check_safety", "draft_adapter_config", "hardware_probe", "is_sim", "load_yaml",
    "run_checks", "safety_config_for", "sha256_file", "tighten_only", "validate_adapter_config",
    "validate_reset_routine", "validate_success_check",
]
