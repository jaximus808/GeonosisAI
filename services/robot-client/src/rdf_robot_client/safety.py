"""Safety layer (TDM-45): every arm motion passes through ``SafetyLayer.command_joints``.

``main.build_arm`` wraps the adapter as ``SafetyLayer(arm)``. Because the shared helpers on
``ArmAdapter`` (``command_ee``, ``command``, ``move_joints``, ``move_ee``, ``home``) run on the
wrapper and are written against ``self.command_joints``, policy action chunks, teleop EE targets
and homing all reach the hardware only through the checks below. Limits come from
``config/safety.yaml`` (one profile per adapter name; see that file).

Per streamed setpoint (``command_joints``), in order:
  1. latched soft stop          -> ``ArmStopped`` (nothing reaches the adapter)
  2. wrong length               -> ``ArmError`` (programming error)
  3. NaN / inf (joints/gripper) -> REJECT
  4. jump: any joint further than ``max_jump_rad`` from the last setpoint -> REJECT
  5. joint limits: clamp into the adapter's limits (tightened by the config) minus ``margin_rad``
  6. speed: per-joint velocity and acceleration caps (x the live speed scale), as a
     second-order limiter that also brakes so it stops at the target instead of overshooting
  7. workspace: FK of the result must be inside the Cartesian box and above the table floor;
     otherwise the step is cut back along its own line to the boundary (the arm stops at the
     wall; it does not slide along it). If the arm is already outside (e.g. it booted there),
     only steps that do not go further outside are allowed.
  8. gripper: clamp to [min, max]
A REJECT drops the command and holds the arm (``reject.action: hold``), or raises
``SafetyViolation`` (``reject.action: raise``). ``reject.max_consecutive`` rejects in a row raise
anyway, so a policy streaming garbage fails its job instead of silently holding forever.

Watchdog: once setpoints are streaming, if none arrives within ``watchdog_ms`` the layer calls
``hold()`` (servos on, current position). Suspended during the layer's own blocking moves
(``move_joints`` / ``home``), disarmed by ``hold()`` / ``stop()``. The next command resumes motion
from the measured position (so a stale target cannot cause a jump: check 4 applies).

Soft stop: ``stop()`` / ``soft_estop()`` latches in the layer FIRST (any concurrent or later
command raises ``ArmStopped`` before touching the adapter), then calls the adapter's ``stop()``.
Only ``clear_stop()`` unlatches both (a human at the arm: ``clear`` / ``unlock`` on the client's
stdin, or SIGUSR1; see main.py). The physical e-stop at the table is independent of all of this.

Speed scale: ``set_speed_scale(x)`` (capped by ``max_speed_scale``, and by
``unverified_max_speed_scale`` on real hardware until the profile says ``verified: true``) and
``set_slow_mode(on)`` (teleop slow mode). Both scale velocity AND acceleration caps.

Evidence: every clamp / reject / watchdog hold is counted (``stats()``, and
``status().detail["safety"]``, which the client's ``snapshot()`` / stdin ``status`` print), logged
as a JSON line (rate-limited per kind) and, if an event sink is set (main.py wires
``RobotClient.event``), appended as a rate-limited ``robot.limit_hit`` event_log row.

    python -m rdf_robot_client.safety --profile yam     # print the effective limits
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .adapters.base import ArmAdapter, ArmAdapterWrapper, ArmError, ArmStatus, ArmStopped, JointLimits

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "safety.yaml"
SIM_ADAPTERS = frozenset({"sim"})


class SafetyConfigError(ValueError):
    pass


class SafetyViolation(ArmError):
    """A command the safety layer refused (``reject.action: raise``, or too many rejects in a row)."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class Workspace:
    enabled: bool
    lo: tuple[float, float, float]
    hi: tuple[float, float, float]

    def violation(self, xyz: Sequence[float]) -> float:
        """Total distance (m) outside the box; 0.0 inside."""
        if not self.enabled:
            return 0.0
        return sum(max(lo - v, 0.0) + max(v - hi, 0.0) for v, lo, hi in zip(xyz, self.lo, self.hi))


@dataclass(frozen=True)
class SafetyConfig:
    profile: str
    verified: bool
    speed_scale: float
    max_speed_scale: float
    slow_mode_scale: float
    margin_rad: float
    lower: tuple[float, ...] | None
    upper: tuple[float, ...] | None
    max_velocity: tuple[float, ...]
    max_acceleration: tuple[float, ...]
    max_jump_rad: float
    reject_action: str
    max_consecutive_rejects: int
    workspace: Workspace
    gripper_min: float
    gripper_max: float
    gripper_max_effort: float | None
    watchdog_ms: float
    log_every_s: float
    event_every_s: float
    unverified_max_speed_scale: float
    source: str = ""

    @classmethod
    def from_dict(cls, profile: str, p: dict[str, Any], *, top: dict[str, Any] | None = None, source: str = "") -> SafetyConfig:
        top = top or {}
        j = p.get("joints") or {}
        ws = p.get("workspace") or {}
        g = p.get("gripper") or {}
        rj = p.get("reject") or {}

        def vec(v: Any, name: str) -> tuple[float, ...] | None:
            if v is None:
                return None
            if not isinstance(v, list | tuple) or not v:
                raise SafetyConfigError(f"{profile}.{name} must be a list of numbers")
            return tuple(float(x) for x in v)

        def pair(v: Any, name: str) -> tuple[float, float]:
            if not isinstance(v, list | tuple) or len(v) != 2:
                raise SafetyConfigError(f"{profile}.workspace.{name} must be [min, max]")
            return float(v[0]), float(v[1])

        enabled = bool(ws.get("enabled", True))
        if enabled:
            (x0, x1), (y0, y1), (z0, z1) = pair(ws.get("x"), "x"), pair(ws.get("y"), "y"), pair(ws.get("z"), "z")
            floor = float(ws.get("table_floor_z", z0)) + float(ws.get("table_clearance_m", 0.0))
            workspace = Workspace(True, (x0, y0, max(z0, floor)), (x1, y1, z1))
        else:
            workspace = Workspace(False, (-math.inf,) * 3, (math.inf,) * 3)  # type: ignore[arg-type]
        cfg = cls(
            profile=profile,
            verified=bool(p.get("verified", False)),
            speed_scale=float(p.get("speed_scale", 0.25)),
            max_speed_scale=float(p.get("max_speed_scale", 1.0)),
            slow_mode_scale=float(p.get("slow_mode_scale", 0.3)),
            margin_rad=float(j.get("margin_rad", 0.1)),
            lower=vec(j.get("lower"), "joints.lower"),
            upper=vec(j.get("upper"), "joints.upper"),
            max_velocity=vec(j.get("max_velocity"), "joints.max_velocity") or (),
            max_acceleration=vec(j.get("max_acceleration"), "joints.max_acceleration") or (),
            max_jump_rad=float(j.get("max_jump_rad", 0.3)),
            reject_action=str(rj.get("action", "hold")),
            max_consecutive_rejects=int(rj.get("max_consecutive", 30)),
            workspace=workspace,
            gripper_min=float(g.get("min", 0.0)),
            gripper_max=float(g.get("max", 1.0)),
            gripper_max_effort=None if g.get("max_effort") is None else float(g["max_effort"]),
            watchdog_ms=float(p.get("watchdog_ms", 200)),
            log_every_s=float(p.get("log_every_s", 1.0)),
            event_every_s=float(p.get("event_every_s", 5.0)),
            unverified_max_speed_scale=float(top.get("unverified_max_speed_scale", 0.3)),
            source=source,
        )
        cfg.validate()
        return cfg

    def validate(self, dof: int | None = None) -> None:
        def bad(msg: str) -> None:
            raise SafetyConfigError(f"safety profile {self.profile!r}: {msg}")

        if not self.max_velocity or not self.max_acceleration:
            bad("joints.max_velocity and joints.max_acceleration are required")
        nums = [self.speed_scale, self.max_speed_scale, self.slow_mode_scale, self.margin_rad, self.max_jump_rad,
                self.watchdog_ms, *self.max_velocity, *self.max_acceleration]
        if any(not math.isfinite(v) for v in nums):
            bad("non-finite number")
        if any(v <= 0 for v in (*self.max_velocity, *self.max_acceleration)):
            bad("velocity/acceleration caps must be > 0")
        if not 0 < self.speed_scale <= self.max_speed_scale <= 1.0:
            bad("need 0 < speed_scale <= max_speed_scale <= 1")
        if not 0 < self.slow_mode_scale <= 1.0:
            bad("slow_mode_scale must be in (0, 1]")
        if self.margin_rad < 0 or self.max_jump_rad <= 0 or self.watchdog_ms < 0:
            bad("margin_rad >= 0, max_jump_rad > 0, watchdog_ms >= 0 (0 = off)")
        if self.reject_action not in ("hold", "raise"):
            bad("reject.action must be hold or raise")
        if not 0.0 <= self.gripper_min < self.gripper_max <= 1.0:
            bad("need 0 <= gripper.min < gripper.max <= 1")
        if self.workspace.enabled and any(lo >= hi for lo, hi in zip(self.workspace.lo, self.workspace.hi)):
            bad(f"empty workspace box (incl. table floor): lo={self.workspace.lo} hi={self.workspace.hi}")
        if dof is not None:
            for name, v in (("max_velocity", self.max_velocity), ("max_acceleration", self.max_acceleration),
                            ("lower", self.lower), ("upper", self.upper)):
                if v is not None and len(v) != dof:
                    bad(f"joints.{name} has {len(v)} values, the arm has {dof} joints")


def load_safety_config(profile: str, path: str | os.PathLike[str] | None = None) -> SafetyConfig:
    """Read ``profile`` from the safety YAML ($RDF_SAFETY_CONFIG, else config/safety.yaml)."""
    p = Path(path or os.environ.get("RDF_SAFETY_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except OSError as e:
        raise SafetyConfigError(f"cannot read safety config {p}: {e}") from e
    profiles = data.get("profiles") or {}
    if profile not in profiles:
        raise SafetyConfigError(f"no safety profile {profile!r} in {p} (have {sorted(profiles)})")
    return SafetyConfig.from_dict(profile, profiles[profile], top=data, source=str(p))


# --------------------------------------------------------------------------- logging


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "robot-client", "mod": "safety", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


class _RateLimiter:
    """At most one emit per ``every_s`` per key; reports how many were suppressed since."""

    def __init__(self, every_s: float) -> None:
        self.every_s = every_s
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def allow(self, key: str, now: float) -> int | None:
        """None = suppressed; otherwise the number suppressed since the last emit."""
        last = self._last.get(key)
        if last is not None and now - last < self.every_s:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        self._last[key] = now
        return self._suppressed.pop(key, 0)


COUNTERS = (
    "commands",               # command_joints calls
    "sent",                   # setpoints that reached the adapter
    "clamped_joint_limit",    # target outside joint limits (+margin)
    "clamped_speed",          # step cut by the velocity / acceleration caps
    "clamped_workspace",      # step cut at the workspace box / table floor
    "clamped_gripper",        # gripper outside [min, max]
    "rejected_nonfinite",     # NaN / inf
    "rejected_jump",          # > max_jump_rad from the last setpoint
    "rejected_stopped",       # command while the soft stop is latched
    "watchdog_holds",         # no command within watchdog_ms while streaming
    "estops",                 # stop() calls
)

# --------------------------------------------------------------------------- the layer


class SafetyLayer(ArmAdapterWrapper):
    def __init__(
        self,
        inner: ArmAdapter,
        config: SafetyConfig | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        profile: str | None = None,
        event_sink: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_watchdog: bool = True,
    ) -> None:
        super().__init__(inner)
        self.config = config or load_safety_config(profile or os.environ.get("RDF_SAFETY_PROFILE") or inner.name, config_path)
        self.config.validate(dof=inner.dof)
        self._clock = clock
        self._lock = threading.RLock()
        self._event_sink = event_sink
        self._is_sim = inner.name in SIM_ADAPTERS
        self._stopped = False  # our own latch (checked before the adapter's)
        self._counts = {k: 0 for k in COUNTERS}
        self._consecutive_rejects = 0
        self._last_reason: dict[str, str] = {}
        self._log_rl = _RateLimiter(self.config.log_every_s)
        self._event_rl = _RateLimiter(self.config.event_every_s)
        # reference for the speed limiter: last setpoint sent (None = re-read the measured state)
        self._ref_q: tuple[float, ...] | None = None
        self._ref_v: tuple[float, ...] = (0.0,) * inner.dof
        self._ref_t = 0.0
        self._ref_viol = 0.0
        self._wd_armed = False
        self._last_cmd_t = 0.0
        self._blocking = 0
        self._slow_mode = False
        self._max_scale = self.config.max_speed_scale
        if not self.config.verified and not self._is_sim:
            self._max_scale = min(self._max_scale, self.config.unverified_max_speed_scale)
            _log("WARNING: safety profile is NOT verified on the arm; speed scale capped",
                 profile=self.config.profile, cap=self._max_scale, config=self.config.source)
        self._speed_scale = min(self.config.speed_scale, self._max_scale)
        env_scale = os.environ.get("RDF_SAFETY_SPEED_SCALE")
        if env_scale:
            self.set_speed_scale(float(env_scale))
        self._lower, self._upper = self._effective_bounds()
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        self._start_watchdog = start_watchdog and self.config.watchdog_ms > 0

    # ------------------------------------------------------------------ config / limits
    def _effective_bounds(self) -> tuple[tuple[float, ...], tuple[float, ...]]:
        al, au = self.inner.limits.lower, self.inner.limits.upper
        cl = self.config.lower or al
        cu = self.config.upper or au
        m = self.config.margin_rad
        lower = tuple(max(a, c) + m for a, c in zip(al, cl))
        upper = tuple(min(a, c) - m for a, c in zip(au, cu))
        if any(lo >= hi for lo, hi in zip(lower, upper)):
            raise SafetyConfigError(f"joint limits + margin leave no range: lower={lower} upper={upper}")
        return lower, upper

    @property
    def effective_scale(self) -> float:
        return self._speed_scale * (self.config.slow_mode_scale if self._slow_mode else 1.0)

    def _vmax(self) -> tuple[float, ...]:
        s = self.effective_scale
        return tuple(min(c * s, a) for c, a in zip(self.config.max_velocity, self.inner.limits.max_velocity))

    def _amax(self) -> tuple[float, ...]:
        s = self.effective_scale
        return tuple(c * s for c in self.config.max_acceleration)

    @property
    def limits(self) -> JointLimits:  # type: ignore[override]
        """The SAFE limits: adapter limits tightened by the config and margin; velocity = live cap."""
        return JointLimits(self._lower, self._upper, self._vmax())

    @property
    def speed_scale(self) -> float:
        return self._speed_scale

    @property
    def max_speed_scale(self) -> float:
        return self._max_scale

    def set_speed_scale(self, scale: float) -> float:
        """Set the global speed scale; clamped to (0.01, max_speed_scale]. Returns the value used."""
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError(f"speed scale must be a positive number, got {scale!r}")
        with self._lock:
            used = min(max(float(scale), 0.01), self._max_scale)
            if used != scale:
                self._note("speed_scale_capped", f"asked {scale}, using {used} (max {self._max_scale})", count=False)
            self._speed_scale = used
        _log("speed scale", scale=used, slow_mode=self._slow_mode, effective=self.effective_scale)
        return used

    def set_slow_mode(self, on: bool) -> None:
        """Teleop slow mode: multiply the speed scale by ``slow_mode_scale``."""
        with self._lock:
            self._slow_mode = bool(on)
        _log("slow mode", on=self._slow_mode, effective=self.effective_scale)

    @property
    def slow_mode(self) -> bool:
        return self._slow_mode

    def set_event_sink(self, sink: Callable[..., None] | None) -> None:
        """``sink(type_, **payload)``, e.g. ``RobotClient.event``; gets rate-limited robot.limit_hit rows."""
        self._event_sink = sink

    # ------------------------------------------------------------------ evidence
    def _note(self, kind: str, reason: str, *, count: bool = True, **fields: Any) -> None:
        if count and kind in self._counts:
            self._counts[kind] += 1
        self._last_reason[kind] = reason
        now = self._clock()
        sup = self._log_rl.allow(kind, now)
        if sup is not None:
            _log(f"safety {kind}", reason=reason, total=self._counts.get(kind), suppressed=sup, **fields)
        sink = self._event_sink
        if sink is not None and count:
            sup_e = self._event_rl.allow(kind, now)
            if sup_e is not None:
                try:
                    sink("robot.limit_hit", kind=kind, reason=reason[:300], total=self._counts.get(kind),
                         suppressed=sup_e, speed_scale=self.effective_scale, profile=self.config.profile)
                except Exception as e:  # noqa: BLE001 - evidence never blocks a safety action
                    _log("safety event sink failed", error=repr(e)[:200])

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {**self._counts, "consecutive_rejects": self._consecutive_rejects, "stopped": self._stopped,
                    "speed_scale": self._speed_scale, "slow_mode": self._slow_mode,
                    "effective_scale": round(self.effective_scale, 4), "profile": self.config.profile,
                    "verified": self.config.verified, "watchdog_armed": self._wd_armed,
                    "last_reason": dict(self._last_reason)}

    # ------------------------------------------------------------------ reference state
    def _reset_ref(self) -> None:
        """Re-anchor the limiter on the measured position, at rest (after hold/stop/connect)."""
        self._ref_q = None
        self._ref_v = (0.0,) * self.dof
        self._wd_armed = False

    def _ensure_ref(self, now: float) -> None:
        if self._ref_q is None:
            st = self.inner.read_state()
            self._ref_q = tuple(st.joints)
            self._ref_v = (0.0,) * self.dof
            self._ref_t = now - 1.0 / self.control_rate_hz  # first command gets one nominal period
            self._ref_viol = self._viol(self._ref_q)

    def _viol(self, q: Sequence[float]) -> float:
        ws = self.config.workspace
        if not ws.enabled:
            return 0.0
        return ws.violation(self.inner.fk(q).xyz)

    # ------------------------------------------------------------------ the filter
    def _reject(self, kind: str, reason: str) -> None:
        self._consecutive_rejects += 1
        self._note(kind, reason, consecutive=self._consecutive_rejects)
        self._hold_locked()
        if self.config.reject_action == "raise" or self._consecutive_rejects >= self.config.max_consecutive_rejects:
            raise SafetyViolation(f"safety layer rejected the command ({kind}): {reason}"
                                  f" [{self._consecutive_rejects} in a row]")

    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        if self._stopped:
            self._counts["rejected_stopped"] += 1
            raise ArmStopped("soft e-stop is latched (safety layer)")
        if len(joints) != self.dof:
            raise ArmError(f"expected {self.dof} joints, got {len(joints)}")
        with self._lock:
            if self._stopped:
                raise ArmStopped("soft e-stop is latched (safety layer)")
            self._counts["commands"] += 1
            try:
                req = tuple(float(v) for v in joints)
                g_req = None if gripper is None else float(gripper)
            except (TypeError, ValueError):
                self._reject("rejected_nonfinite", f"non-numeric command {list(joints)!r} gripper={gripper!r}")
                return
            if not all(math.isfinite(v) for v in req) or (g_req is not None and not math.isfinite(g_req)):
                self._reject("rejected_nonfinite", f"non-finite command joints={list(req)} gripper={g_req}")
                return
            now = self._clock()
            self._ensure_ref(now)
            ref_q = self._ref_q
            assert ref_q is not None
            jump = max(abs(a - b) for a, b in zip(req, ref_q))
            if jump > self.config.max_jump_rad:
                worst = max(range(self.dof), key=lambda i: abs(req[i] - ref_q[i]))
                self._reject("rejected_jump", f"joint {worst + 1} jumps {jump:.3f} rad (> {self.config.max_jump_rad})")
                return
            # joint limits (+margin)
            tgt = tuple(min(max(v, lo), hi) for v, lo, hi in zip(req, self._lower, self._upper))
            if any(abs(a - b) > 1e-9 for a, b in zip(tgt, req)):
                worst = max(range(self.dof), key=lambda i: abs(req[i] - tgt[i]))
                self._note("clamped_joint_limit", f"joint {worst + 1} {req[worst]:.3f} -> {tgt[worst]:.3f} rad")
            # speed: velocity + acceleration caps
            dt = min(max(now - self._ref_t, 0.0), 2.0 / self.control_rate_hz)
            q_new, v_new, cut = self._limit_speed(ref_q, self._ref_v, tgt, dt)
            if cut > 2e-3:
                self._note("clamped_speed", f"step cut by {cut:.4f} rad (scale {self.effective_scale:.2f})")
            # workspace box + table floor (FK of the tool point)
            viol = self._viol(q_new)
            allowed = self._ref_viol  # no epsilon: a tolerance here would let the arm creep outward
            if viol > allowed:
                q_new = self._cut_to_workspace(ref_q, q_new, allowed)
                viol = self._viol(q_new)
                v_new = tuple((a - b) / dt if dt > 0 else 0.0 for a, b in zip(q_new, ref_q))
                xyz = self.inner.fk(tgt).xyz
                self._note("clamped_workspace", f"target tool point {[round(v, 3) for v in xyz]} outside the box "
                           f"{self.config.workspace.lo}..{self.config.workspace.hi}")
            # gripper range
            g_out = None
            if g_req is not None:
                g_out = min(max(g_req, self.config.gripper_min), self.config.gripper_max)
                if g_out != g_req:
                    self._note("clamped_gripper", f"gripper {g_req:.3f} -> {g_out:.3f}")
            self.inner.command_joints(q_new, g_out)
            if self._stopped:  # stop() raced us between the check and the send: re-assert it
                self.inner.stop()
                raise ArmStopped("soft e-stop is latched (safety layer)")
            self._ref_q, self._ref_v, self._ref_t, self._ref_viol = q_new, v_new, now, viol
            self._counts["sent"] += 1
            self._consecutive_rejects = 0
            self._last_cmd_t = now
            self._wd_armed = True

    def _limit_speed(self, q0: Sequence[float], v0: Sequence[float], tgt: Sequence[float], dt: float
                     ) -> tuple[tuple[float, ...], tuple[float, ...], float]:
        """Per-joint |v| <= vmax, and speeding up by at most amax*dt per tick.

        Slowing down is never limited (it is the safe direction, and limiting it would overshoot a
        target that stopped). A reversal stops, then may speed up by amax*dt the other way.
        Returns (setpoint, velocity, how far the setpoint falls short of ``tgt``).
        """
        if dt <= 0:
            return tuple(q0), tuple(v0), max(abs(t - q) for t, q in zip(tgt, q0))
        q_out, v_out, cut = [], [], 0.0
        for q, v_prev, t, vmax, amax in zip(q0, v0, tgt, self._vmax(), self._amax()):
            v_des = (t - q) / dt
            v_des = min(max(v_des, -vmax), vmax)
            if v_des * v_prev < 0:  # reversal
                v = math.copysign(min(abs(v_des), amax * dt), v_des)
            elif abs(v_des) > abs(v_prev):  # speeding up
                v = math.copysign(min(abs(v_des), abs(v_prev) + amax * dt), v_des)
            else:  # slowing down (or steady)
                v = v_des
            qn = q + v * dt
            cut = max(cut, abs(t - qn))
            q_out.append(qn)
            v_out.append(v)
        return tuple(q_out), tuple(v_out), cut

    def _cut_to_workspace(self, q0: Sequence[float], q1: Sequence[float], allowed: float) -> tuple[float, ...]:
        """Largest fraction of the step q0 -> q1 that keeps the violation <= ``allowed`` (bisection)."""
        lo, hi = 0.0, 1.0
        for _ in range(10):
            mid = (lo + hi) / 2
            q = [a + (b - a) * mid for a, b in zip(q0, q1)]
            if self._viol(q) <= allowed:
                lo = mid
            else:
                hi = mid
        return tuple(a + (b - a) * lo for a, b in zip(q0, q1))

    def command_gripper(self, gripper: float) -> None:
        if self._stopped:
            self._counts["rejected_stopped"] += 1
            raise ArmStopped("soft e-stop is latched (safety layer)")
        with self._lock:
            try:
                g = float(gripper)
            except (TypeError, ValueError):
                g = math.nan
            if not math.isfinite(g):
                self._reject("rejected_nonfinite", f"non-finite gripper command {gripper!r}")
                return
            g_out = min(max(g, self.config.gripper_min), self.config.gripper_max)
            if g_out != g:
                self._note("clamped_gripper", f"gripper {g:.3f} -> {g_out:.3f}")
            self.inner.command_gripper(g_out)

    # ------------------------------------------------------------------ blocking moves
    @contextmanager
    def _blocking_move(self) -> Iterator[None]:
        with self._lock:
            self._blocking += 1
        try:
            yield
        finally:
            with self._lock:
                self._blocking -= 1
                self._last_cmd_t = self._clock()
                self._wd_armed = False  # the move ended (arrived / aborted): no stream to watch

    def move_joints(self, target: Sequence[float], *, gripper: float | None = None, speed_fraction: float = 0.25,
                    duration: float | None = None, should_abort: Callable[[], bool] | None = None,
                    tolerance: float = 0.01, settle_timeout: float = 2.0) -> bool:
        """Blocking move planned WITHIN the safe caps, so the limiter never lags the path.

        The goal is clamped into the safe joint range first. The smoothstep path peaks at 1.5x the
        mean velocity and 6*D/T^2 acceleration, so the duration is stretched until both fit the
        live caps (x ``speed_fraction``). The watchdog is suspended for the whole move.
        """
        if self._stopped:
            self._counts["rejected_stopped"] += 1
            raise ArmStopped("soft e-stop is latched (safety layer)")
        goal = tuple(float(v) for v in target)
        if len(goal) != self.dof:
            raise ArmError(f"move_joints: expected {self.dof} joints, got {len(goal)}")
        if not all(math.isfinite(v) for v in goal) or (gripper is not None and not math.isfinite(float(gripper))):
            with self._lock:
                self._note("rejected_nonfinite", f"move_joints to non-finite goal {list(goal)}")
            raise SafetyViolation(f"move_joints: non-finite goal {list(goal)}")
        safe_goal = tuple(min(max(v, lo), hi) for v, lo, hi in zip(goal, self._lower, self._upper))
        if safe_goal != goal:
            with self._lock:
                self._note("clamped_joint_limit", f"move_joints goal {list(goal)} -> {list(safe_goal)}")
        start = self.joint_positions()
        frac = min(max(speed_fraction, 0.01), 1.0)
        t_min = 0.0
        for g, s, vmax, amax in zip(safe_goal, start, self._vmax(), self._amax()):
            d = abs(g - s)
            if d > 0:
                t_min = max(t_min, 1.5 * d / (vmax * frac), math.sqrt(6.0 * d / (amax * frac)))
        t = max(duration or 0.0, t_min * 1.15)
        with self._blocking_move():
            return super().move_joints(safe_goal, gripper=gripper, duration=t, should_abort=should_abort,
                                       tolerance=tolerance, settle_timeout=settle_timeout)

    # ------------------------------------------------------------------ hold / stop
    def _hold_locked(self) -> None:
        self._reset_ref()
        self.inner.hold()

    def hold(self) -> None:
        with self._lock:
            self._hold_locked()

    def stop(self) -> None:
        """Soft e-stop: latch here first (no lock: must never wait), then the adapter's stop()."""
        self._stopped = True
        self._wd_armed = False
        try:
            self.inner.stop()
        except Exception as e:  # noqa: BLE001 - stop never raises
            _log("adapter stop() raised; layer latch still holds", error=repr(e)[:200])
        self._counts["estops"] += 1
        self._last_reason["estops"] = "soft e-stop"

    soft_estop = stop

    def clear_stop(self) -> None:
        """Unlatch the adapter and the layer (a human checked the arm). The arm stays holding."""
        with self._lock:
            self.inner.clear_stop()
            self._stopped = False
            self._reset_ref()
            self._consecutive_rejects = 0

    @property
    def locked(self) -> bool:
        return self._stopped

    # ------------------------------------------------------------------ watchdog
    def check_watchdog(self) -> bool:
        """Hold if the setpoint stream went quiet for ``watchdog_ms``. Returns True if it tripped."""
        if self.config.watchdog_ms <= 0:
            return False
        with self._lock:
            if not self._wd_armed or self._blocking or self._stopped:
                return False
            quiet_ms = (self._clock() - self._last_cmd_t) * 1000.0
            if quiet_ms <= self.config.watchdog_ms:
                return False
            try:
                self._hold_locked()
            except Exception as e:  # noqa: BLE001
                _log("watchdog hold failed", error=repr(e)[:200])
            self._note("watchdog_holds", f"no command for {quiet_ms:.0f} ms (> {self.config.watchdog_ms:.0f} ms): holding")
            return True

    def _watchdog_loop(self) -> None:
        poll = min(self.config.watchdog_ms / 5000.0, 0.02)
        while not self._wd_stop.wait(poll):
            try:
                self.check_watchdog()
            except Exception as e:  # noqa: BLE001 - the watchdog thread must not die
                _log("watchdog error", error=repr(e)[:200])

    # ------------------------------------------------------------------ lifecycle / status
    def connect(self) -> None:
        self.inner.connect()
        with self._lock:
            self._reset_ref()
        effort = self.config.gripper_max_effort
        setter = getattr(self.inner, "set_gripper_effort_limit", None)
        if effort is not None and callable(setter):
            setter(effort)
        if self._start_watchdog and (self._wd_thread is None or not self._wd_thread.is_alive()):
            self._wd_stop.clear()
            self._wd_thread = threading.Thread(target=self._watchdog_loop, name="rdf-safety-watchdog", daemon=True)
            self._wd_thread.start()
        _log("safety layer active", **self.summary())

    def disconnect(self) -> None:
        self._wd_stop.set()
        if self._wd_thread is not None:
            self._wd_thread.join(1.0)
            self._wd_thread = None
        self.inner.disconnect()

    def status(self) -> ArmStatus:
        st = self.inner.status()
        return dataclasses.replace(st, estopped=st.estopped or self._stopped,
                                   detail={**st.detail, "safety": self.stats()})

    def summary(self) -> dict[str, Any]:
        c = self.config
        return {"profile": c.profile, "verified": c.verified, "config": c.source,
                "speed_scale": self._speed_scale, "max_speed_scale": self._max_scale,
                "joint_lower": [round(v, 3) for v in self._lower], "joint_upper": [round(v, 3) for v in self._upper],
                "max_velocity": list(c.max_velocity), "max_acceleration": list(c.max_acceleration),
                "max_jump_rad": c.max_jump_rad, "workspace": {"enabled": c.workspace.enabled,
                                                               "lo": list(c.workspace.lo), "hi": list(c.workspace.hi)},
                "gripper": [c.gripper_min, c.gripper_max, c.gripper_max_effort], "watchdog_ms": c.watchdog_ms,
                "reject_action": c.reject_action}

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "safety": self.summary()}


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m rdf_robot_client.safety", description="print the effective safety limits")
    p.add_argument("--profile", default="yam")
    p.add_argument("--config", default=None)
    a = p.parse_args(argv)
    cfg = load_safety_config(a.profile, a.config)
    print(json.dumps(dataclasses.asdict(cfg), indent=2, default=str))


if __name__ == "__main__":
    main()

__all__ = ["SafetyConfig", "SafetyConfigError", "SafetyLayer", "SafetyViolation", "Workspace", "load_safety_config"]
