"""TDM-52: teleop executor on the robot. The browser drives the arm over ``/control``.

The ``/ws/control`` WebSocket (alias ``/control``) lives on the TDM-55 stream server, so it uses the same port, the same
cloudflared tunnel and the same HMAC token as the cameras (``docs/WORKER_GUIDE.md``, "Teleop
contract"). ``install_control(stream, hub)`` mounts it, and ``main.py`` does this when the stream
is on. The URL is published as ``robot.camera_config_json["stream"]["control_url"]``; it is the camera
URL with ``/ws/cameras`` swapped for ``/ws/control``, which is what the TDM-56 page derives.

Protocol ``rdf-control/1`` (JSON text frames; the type key is ``t``, and ``type`` is accepted too)::

  browser -> robot
    {t:"auth", token}                    (the page sends {type:"auth", t:"auth", token}) first frame (within 5 s), and again with a fresh token at half-life
    {t:"cmd", seq, ts_ms, clutch, d_pos:[dx,dy,dz] m, d_rot:[rx,ry,rz] rad, gripper:0..1, speed_scale:0..1}
                                         ~30 Hz while the clutch is held: base-frame EE deltas from the pose
                                         at clutch-down (axis-angle rotation)
    {t:"stop"}                           soft e-stop. Always honoured on an authenticated socket, even
                                         after the lease is lost. It LATCHES; only a human at the arm clears it.
    {t:"takeover"}                       paused_for_takeover -> teleop. The first clutch-down while paused
                                         also does it (TeleopConfig.takeover_on_clutch)
    {t:"handback", success?}             takeover: back to the policy (mark_handback + resume); success is
                                         the claimant's "Did you recover the task?" (TDM-69: the paused
                                         rollout writes it as the recovery episode's success). Clean
                                         teleop: ends the start_teleop session
    {t:"record_start"} / {t:"record_stop", success:bool}   a clean demo's recording
    {t:"ping", ts_ms}
  robot -> browser
    {t:"hello", protocol, robot_id, task_id, exp, rate_hz, cmd_timeout_ms, state_hz}
    {t:"ack", seq}                       every cmd (``ok:false, code`` when it was refused)
    {t:"ack", op, ok, ...}               stop / takeover / handback / record_* results
    {t:"state", ...}                     ~10 Hz: robot_state, mode, control_source, clutch, hold_reason,
                                         pose {xyz, quat}, target, joints, gripper, gripper_cmd, speed_scale,
                                         locked/stopped, watchdog (holding on a cmd gap), clamps (safety-layer
                                         clamps hit in the last 1 s, as {kind: true}), clamp_counts (totals),
                                         teleop counters, recording, lease
    {t:"lease_lost", code, detail}       then close 4004 (or 4001 if the token ran out)
    {t:"auth_ok", exp} / {t:"pong", ts_ms, server_ns} / {t:"error", code}
  close codes: 4001 token expired, 4003 bad token / not authorised, 4004 lease lost or revoked
               (TDM-57 guard), 4009 replaced by a newer /control connection

Exclusive control: only the claimant of a live TDM-10 lease on a task on this robot gets in. The TDM-57
``ControlLeaseGuard`` checks the ``task`` row at auth, re-reads it every 2 s and judges every message
without I/O. When the lease is lost the arm holds and the socket closes. Only one socket drives at a
time; a newer authenticated one replaces the older (for example, the same operator reconnecting).

Motion (``TeleopHub._drive``, at the arm's control rate, on the job thread or the takeover thread):
  1. clutch-relative target: at clutch-down the anchor is set so that target == the measured EE pose,
     then target = anchor (+) delta (position added, rotation ``R(d_rot) * R_anchor`` in the base frame)
  2. workspace limit BEFORE IK: the target is clamped into the safety profile's box, shrunk by a few mm.
     The anchor slips with it, so pulling back from a wall moves at once.
  3. smoothing: first-order low-pass (``smoothing_tau_s``), then a Cartesian rate limit
     (``max_lin_speed`` / ``max_ang_speed`` x the browser's ``speed_scale``)
  4. ``arm.command_ee(target, gripper)``: IK, then the TDM-45 SafetyLayer (joint limits, velocity and
     acceleration caps, jump / NaN rejection, FK workspace box, gripper range, watchdog)
  5. ``client.record_command(EETarget, "human")``: one ControlTick per streamed command (IK target +
     ``joint_cmd`` after safety) for the TDM-48 recorder
  The arm HOLDS on clutch release, on a command gap > ``cmd_timeout_s`` (200 ms), on socket close,
  lease loss or link loss, and when the robot is not in ``teleop``. The safety layer's own 200 ms
  watchdog sits underneath as a second layer. An unreachable target (IKError) is skipped and the anchor
  slips. A SafetyViolation holds the arm and waits for a new clutch-down.

Flows:
  * clean demo: the ``start_teleop`` job (replaces the TDM-49 placeholder). It enters ``teleop``
    and drives until ``stop_teleop`` / ``{t:"handback"}``. ``record_start`` starts ``recorder_for(ctx)``
    with the job's kind (default ``clean_teleop``), and ``record_stop(success)`` stops it.
  * DAgger takeover (TDM-69): ``pause_for_takeover`` holds the policy (the loop's control). The
    browser's ``takeover`` switches the state to ``teleop``, calls ``mark_takeover()`` on the running
    recording (unless TDM-69 already marked it), stamped at the moment the policy was PAUSED, so the
    hold while a contributor claims and connects is in the human window (TDM-48 relabels those idle
    ticks human) and the pre-takeover frames are all policy, and starts a driver thread. The rollout's job thread
    stays parked in ``run_control_loop`` until the state is ``rollout`` again. ``handback``: hold,
    ``mark_handback()``, ``teleop -> rollout`` (what ``resume_policy`` does) and a synthetic
    ``handback`` Command on ``ctx.signals``. A DB ``handback`` / ``resume_policy`` / ``stop_teleop``
    ends the driver the same way.

Laptop / Jetson tools (no SpacetimeDB needed)::

    python -m rdf_robot_client.teleop serve --sim --synthetic --allow-ops [--tunnel none|cloudflared]
    python -m rdf_common.stream_token mint --robot-name test.teleop-sim --sub ops:me --ttl 600
    python -m rdf_robot_client.teleop probe ws://127.0.0.1:8091/ws/control --token <t> --pattern square --size 0.02
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from rdf_common.stream_token import StreamClaims, StreamTokenError

from .adapters.base import ArmError, ArmStopped, EETarget, IKError, Pose
from .commands import Cancelled, Command
from .jobs import JobContext, job_handler
from .state import RobotState

log = logging.getLogger("rdf_robot_client.teleop")
S = RobotState

PROTOCOL = "rdf-control/1"
CONTROL_PATH = "/ws/control"
CONTROL_PATHS = (CONTROL_PATH, "/control")  # the contract's name + the path the TDM-56 page derives
CLOSE_EXPIRED, CLOSE_FORBIDDEN, CLOSE_LEASE_LOST, CLOSE_REPLACED = 4001, 4003, 4004, 4009
AUTH_TIMEOUT_S = 5.0
OPS = ("takeover", "handback", "record_start", "record_stop", "record_discard")
SAFETY_KEYS = ("clamped_joint_limit", "clamped_speed", "clamped_workspace", "clamped_gripper",
               "rejected_nonfinite", "rejected_jump", "rejected_stopped", "watchdog_holds", "estops")


def _jlog(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "robot-client", "mod": "teleop", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


@dataclass
class TeleopConfig:
    cmd_timeout_s: float = 0.2        # no cmd for this long while clutched -> hold (contract: 200 ms)
    smoothing_tau_s: float = 0.08     # low-pass time constant on the EE target
    max_lin_speed: float = 0.25       # m/s at speed_scale 1 (the joint caps of the safety layer apply too)
    max_ang_speed: float = 1.5        # rad/s at speed_scale 1
    default_speed_scale: float = 0.5  # when a cmd has no speed_scale
    min_speed_scale: float = 0.05
    max_delta_m: float = 1.0          # |d_pos| above this is a broken client -> refused
    max_delta_rad: float = math.pi
    workspace_margin_m: float = 0.005  # clamp IK targets this far inside the safety box
    state_hz: float = 10.0
    takeover_on_clutch: bool = True   # first clutch-down while paused_for_takeover = takeover
    allow_ops_tokens: bool = False    # dev/HIL: ops tokens (task_id 0, sub ops:*) may drive without a lease


# ------------------------------------------------------------------------------------ rotation math

Quat = tuple[float, float, float, float]  # (w, x, y, z), like adapters.base.Pose


def q_mul(a: Quat, b: Quat) -> Quat:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz, aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw)


def q_conj(q: Quat) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def q_norm(q: Quat) -> Quat:
    n = math.sqrt(sum(v * v for v in q)) or 1.0
    q = tuple(v / n for v in q)  # type: ignore[assignment]
    return q if q[0] >= 0 else tuple(-v for v in q)  # type: ignore[return-value]


def q_from_rotvec(v: Any) -> Quat:
    a = math.sqrt(sum(x * x for x in v))
    if a < 1e-12:
        return (1.0, 0.5 * v[0], 0.5 * v[1], 0.5 * v[2])
    s = math.sin(a / 2) / a
    return (math.cos(a / 2), v[0] * s, v[1] * s, v[2] * s)


def q_to_rotvec(q: Quat) -> tuple[float, float, float]:
    w, x, y, z = q_norm(q)
    s = math.sqrt(x * x + y * y + z * z)
    if s < 1e-12:
        return (2 * x, 2 * y, 2 * z)
    a = 2 * math.atan2(s, w)
    return (x / s * a, y / s * a, z / s * a)


# ------------------------------------------------------------------------------------ commands

@dataclass(frozen=True)
class ControlCmd:
    seq: int
    ts_ms: float
    clutch: bool
    d_pos: tuple[float, float, float]
    d_rot: tuple[float, float, float]
    gripper: float | None
    speed_scale: float
    rx: float  # time.monotonic() when it arrived


def parse_cmd(m: dict[str, Any], cfg: TeleopConfig, rx: float | None = None) -> ControlCmd:
    """Validate one ``{t:"cmd"}``. Raises ValueError (non-finite, wrong shapes, absurd deltas)."""
    def vec(name: str) -> tuple[float, float, float]:
        v = m.get(name)
        if v is None:
            return (0.0, 0.0, 0.0)
        if not isinstance(v, list | tuple) or len(v) != 3:
            raise ValueError(f"{name} must be [x, y, z]")
        out = tuple(float(x) for x in v)
        if not all(math.isfinite(x) for x in out):
            raise ValueError(f"{name} is not finite")
        return out  # type: ignore[return-value]

    d_pos, d_rot = vec("d_pos"), vec("d_rot")
    if math.sqrt(sum(x * x for x in d_pos)) > cfg.max_delta_m:
        raise ValueError(f"|d_pos| > {cfg.max_delta_m} m")
    if math.sqrt(sum(x * x for x in d_rot)) > cfg.max_delta_rad:
        raise ValueError(f"|d_rot| > {cfg.max_delta_rad:.2f} rad")
    g = m.get("gripper")
    gripper = None if g is None else float(g)
    if gripper is not None:
        if not math.isfinite(gripper):
            raise ValueError("gripper is not finite")
        gripper = min(max(gripper, 0.0), 1.0)
    sp = m.get("speed_scale")
    speed = cfg.default_speed_scale if sp is None else float(sp)
    if not math.isfinite(speed):
        raise ValueError("speed_scale is not finite")
    speed = min(max(speed, cfg.min_speed_scale), 1.0)
    return ControlCmd(seq=int(m.get("seq") or 0), ts_ms=float(m.get("ts_ms") or 0), clutch=bool(m.get("clutch")),
                      d_pos=d_pos, d_rot=d_rot, gripper=gripper, speed_scale=speed,
                      rx=time.monotonic() if rx is None else rx)


class ClutchFilter:
    """Clutch-relative EE target with workspace clamp, low-pass and rate limit (pure; unit-tested)."""

    def __init__(self, cfg: TeleopConfig, workspace: tuple[tuple[float, ...], tuple[float, ...]] | None = None):
        self.cfg = cfg
        self.workspace = workspace
        self.engaged = False
        self.anchor_p = (0.0, 0.0, 0.0)
        self.anchor_q: Quat = (1.0, 0.0, 0.0, 0.0)
        self.p = (0.0, 0.0, 0.0)
        self.q: Quat = (1.0, 0.0, 0.0, 0.0)
        self._prev: tuple[tuple[float, float, float], Quat] | None = None
        self.clamped = 0

    def engage(self, current: Pose, cmd: ControlCmd) -> None:
        """Clutch-down (or resume): anchor so that the raw target equals ``current`` (no jump)."""
        self.anchor_p = tuple(c - d for c, d in zip(current.xyz, cmd.d_pos))  # type: ignore[assignment]
        self.anchor_q = q_norm(q_mul(q_conj(q_from_rotvec(cmd.d_rot)), current.quat))
        self.p, self.q = tuple(current.xyz), q_norm(current.quat)  # type: ignore[assignment]
        self._prev = None
        self.engaged = True

    def release(self) -> None:
        self.engaged = False
        self._prev = None

    def raw(self, cmd: ControlCmd) -> tuple[tuple[float, float, float], Quat]:
        p = [a + d for a, d in zip(self.anchor_p, cmd.d_pos)]
        if self.workspace is not None:
            lo, hi = self.workspace
            m = self.cfg.workspace_margin_m
            c = [min(max(v, lo_i + m), hi_i - m) for v, lo_i, hi_i in zip(p, lo, hi)]
            if any(abs(a - b) > 1e-12 for a, b in zip(c, p)):
                self.clamped += 1
                # slip: move the anchor so the operator's next motion away from the wall acts at once
                self.anchor_p = tuple(a + (ci - pi) for a, ci, pi in zip(self.anchor_p, c, p))  # type: ignore[assignment]
                p = c
        q = q_norm(q_mul(q_from_rotvec(cmd.d_rot), self.anchor_q))
        return (p[0], p[1], p[2]), q

    def step(self, cmd: ControlCmd, dt: float) -> Pose:
        raw_p, raw_q = self.raw(cmd)
        self._prev = (self.p, self.q)
        alpha = 1.0 - math.exp(-dt / self.cfg.smoothing_tau_s) if self.cfg.smoothing_tau_s > 0 else 1.0
        vlin = self.cfg.max_lin_speed * cmd.speed_scale * dt
        vang = self.cfg.max_ang_speed * cmd.speed_scale * dt
        dp = [(r - f) * alpha for r, f in zip(raw_p, self.p)]
        n = math.sqrt(sum(v * v for v in dp))
        if n > vlin > 0:
            dp = [v * vlin / n for v in dp]
        self.p = tuple(f + d for f, d in zip(self.p, dp))  # type: ignore[assignment]
        e = q_to_rotvec(q_mul(raw_q, q_conj(self.q)))
        st = [v * alpha for v in e]
        n = math.sqrt(sum(v * v for v in st))
        if n > vang > 0:
            st = [v * vang / n for v in st]
        self.q = q_norm(q_mul(q_from_rotvec(st), self.q))
        return Pose(self.p, self.q)

    def reject_last(self, cmd: ControlCmd) -> None:
        """The last step's target was unreachable (IK): go back to it and let the anchor slip there."""
        if self._prev is None:
            return
        self.p, self.q = self._prev
        self.anchor_p = tuple(f - d for f, d in zip(self.p, cmd.d_pos))  # type: ignore[assignment]
        self.anchor_q = q_norm(q_mul(q_conj(q_from_rotvec(cmd.d_rot)), self.q))
        self._prev = None


# ------------------------------------------------------------------------------------ sessions

_ids = itertools.count(1)


class _OpsGuard:
    """Lease guard stand-in for an ops token (``allow_ops_tokens``): expiry only, no task."""

    def __init__(self, claims: StreamClaims):
        self.claims = claims
        self.on_lost: Callable[[Any], Any] | None = None
        self._lost: Any = None

    @property
    def lost(self) -> Any:
        return self._lost

    def _decision(self) -> Any:
        from .tdm57_lease import LeaseDecision

        if time.time() > self.claims.exp:
            if self._lost is None:
                self._lost = LeaseDecision(False, "token_expired", "control token expired; fetch a new one")
                if self.on_lost:
                    self.on_lost(self._lost)
            return self._lost
        return LeaseDecision.yes()

    allowed = _decision

    async def admit_async(self) -> Any:
        return self._decision()

    async def watch(self) -> Any:
        while self._lost is None:
            await asyncio.sleep(1.0)
            self._decision()
        return self._lost

    def update_token(self, claims: StreamClaims) -> Any:
        from .tdm57_lease import LeaseDecision

        if claims.sub != self.claims.sub or claims.task_id != self.claims.task_id:
            return LeaseDecision(False, "token_mismatch", "a refreshed token must name the same holder")
        self.claims = claims
        return self._decision()

    def check_message(self, msg: dict[str, Any]) -> Any:
        return self._decision()

    def status(self) -> dict[str, Any]:
        return {"task_id": 0, "sub": self.claims.sub, "token_exp": self.claims.exp, "ops": True,
                "lost": getattr(self._lost, "code", None)}


class ControlSession:
    """One authenticated /control socket. Thread-safe send/close (the hub calls from other threads)."""

    def __init__(self, ws: Any, loop: asyncio.AbstractEventLoop, claims: StreamClaims, guard: Any):
        self.id = next(_ids)
        self.ws, self.loop, self.claims, self.guard = ws, loop, claims, guard
        self.revoked: str | None = None
        self.rc: Any = None  # TDM-58 RecordingControl (lazy; False = not available)
        self.peer = str(getattr(ws, "remote_address", ""))

    @property
    def contributor_id(self) -> int:
        sub = self.claims.sub or ""
        if sub.startswith("contributor:"):
            try:
                return int(sub.split(":", 1)[1])
            except ValueError:
                return 0
        return 0

    async def send(self, obj: dict[str, Any]) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            await self.ws.send(json.dumps(obj, default=str))
        except ConnectionClosed:
            pass

    def send_threadsafe(self, obj: dict[str, Any]) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.send(obj), self.loop)
        except RuntimeError:
            pass

    def close_threadsafe(self, code: int, reason: str) -> None:
        async def _close() -> None:
            try:
                await self.ws.close(code, reason)
            except Exception:  # noqa: BLE001
                pass
        try:
            asyncio.run_coroutine_threadsafe(_close(), self.loop)
        except RuntimeError:
            pass


# ------------------------------------------------------------------------------------ the hub

def _rec_marks(rec: Any) -> tuple[int | None, int | None]:
    """(takeover_ns, handback_ns) of the running recording (TDM-48 keeps them on its session)."""
    s = getattr(rec, "_s", None)
    return (getattr(s, "takeover_ns", None), getattr(s, "handback_ns", None)) if s is not None else (None, None)


class TeleopHub:
    """Joins the /control sockets (asyncio thread) and the arm driver (job / takeover thread).

    One per robot client: ``hub_for(client)`` (``services["teleop"]``). ``guard_factory(claims)`` makes
    the per-socket lease guard. The default is the TDM-57 ``ControlLeaseGuard`` reading the ``task`` row
    through ``client.http``. Tests pass their own.
    """

    def __init__(self, client: Any, *, config: TeleopConfig | None = None,
                 guard_factory: Callable[[StreamClaims], Any] | None = None) -> None:
        self.client = client
        self.cfg = config or TeleopConfig()
        self.guard_factory = guard_factory
        self._lock = threading.RLock()
        self._session: ControlSession | None = None
        self._cmd: ControlCmd | None = None
        self._ops = ThreadPoolExecutor(1, thread_name_prefix="rdf-teleop-ops")
        self.mode: str | None = None          # "teleop" (start_teleop job) | "takeover" (paused rollout)
        self._ctx: JobContext | None = None   # the job being driven
        self._job_task_id = 0                 # start_teleop payload task_id (0 = any lease holder)
        self._kind = "clean_teleop"
        self._handback = threading.Event()
        self._driver: threading.Thread | None = None
        self._auto_takeover = False
        self._takeover_task_id = 0            # the claimant's task during a DAgger takeover
        self._handback_success: bool | None = None  # TDM-69: the claimant's "recovered?" answer at handback
        self._rec_owned: str | None = None    # episode key started via record_start (we may stop it)
        self.episodes: list[dict[str, Any]] = []
        self._last_handback: dict[str, Any] = {}
        self.counters = {"cmds": 0, "cmds_refused": 0, "engagements": 0, "gap_holds": 0, "release_holds": 0,
                         "ik_skips": 0, "safety_violations": 0, "workspace_clamps": 0, "takeovers": 0,
                         "handbacks": 0, "stops": 0, "sessions": 0, "lease_lost": 0, "steps": 0}
        self._clamp_seen: dict[str, tuple[int, float]] = {}  # kind -> (last count, monotonic t of last increase)
        self._drive_status: dict[str, Any] = {"engaged": False, "hold_reason": "no_session", "seq": None,
                                              "target": None, "gripper_cmd": None, "speed_scale": None}

    # ---------------------------------------------------------------- guards
    def make_guard(self, claims: StreamClaims) -> Any:
        if claims.task_id <= 0 and (claims.sub or "").startswith("ops:") and self.cfg.allow_ops_tokens:
            return _OpsGuard(claims)
        if self.guard_factory is not None:
            return self.guard_factory(claims)
        from .tdm57_lease import ControlLeaseGuard, spacetime_task_fetcher

        http = getattr(self.client, "http", None)
        if http is None:
            raise RuntimeError("no SpacetimeDB client to check the task lease with")
        return ControlLeaseGuard(claims, spacetime_task_fetcher(http), robot_id=getattr(self.client, "robot_id", None))

    # ---------------------------------------------------------------- sessions (asyncio thread)
    def attach(self, sess: ControlSession) -> ControlSession | None:
        with self._lock:
            old, self._session, self._cmd = self._session, sess, None
            self.counters["sessions"] += 1
        self.client.event("robot.teleop_connected", sub=sess.claims.sub, task_id=sess.claims.task_id,
                          replaced=old.id if old else None)
        return old

    def detach(self, sess: ControlSession, reason: str = "closed") -> None:
        with self._lock:
            if self._session is not sess:
                return
            self._session, self._cmd = None, None
        self.client.event("robot.teleop_disconnected", sub=sess.claims.sub, task_id=sess.claims.task_id, reason=reason)

    def revoke(self, sess: ControlSession, code: str) -> None:
        """Lease lost / token expired: drop its commands now (the driver holds within one tick)."""
        sess.revoked = code
        with self._lock:
            if self._session is sess:
                self._cmd = None
            self.counters["lease_lost"] += 1
        self.client.event("robot.teleop_lease_lost", sub=sess.claims.sub, task_id=sess.claims.task_id, code=code)

    def _authorized(self, sess: ControlSession) -> str | None:
        if sess.revoked:
            return sess.revoked
        if self._session is not sess:
            return "not_active"
        if self.mode == "teleop" and self._job_task_id and sess.claims.task_id not in (0, self._job_task_id):
            return "wrong_task"
        return None

    def on_cmd(self, sess: ControlSession, m: dict[str, Any]) -> str | None:
        """Store the newest cmd. Returns None, or a refusal code for the ack."""
        why = self._authorized(sess)
        if why:
            self.counters["cmds_refused"] += 1
            return why
        try:
            cmd = parse_cmd(m, self.cfg)
        except (TypeError, ValueError) as e:
            self.counters["cmds_refused"] += 1
            return f"bad_cmd: {e}"
        with self._lock:
            prev = self._cmd
            if prev is not None and cmd.seq and prev.seq and cmd.seq < prev.seq:
                return "out_of_order"
            self._cmd = cmd
            self.counters["cmds"] += 1
            auto = (self.cfg.takeover_on_clutch and cmd.clutch and self.mode is None and not self._auto_takeover
                    and self.client.sm.state is S.PAUSED_FOR_TAKEOVER)
            if auto:
                self._auto_takeover = True
        if auto:
            self._ops.submit(self._op_safe, sess, "takeover", {"auto": True})
        return None

    def stop(self, sess: ControlSession | None, reason: str = "browser stop") -> None:
        """Soft e-stop from the browser: latch the safety layer + the state machine. Never refused."""
        try:
            self.client.arm.stop()
        finally:
            who = sess.claims.sub if sess else "?"
            self.client.sm.lock(f"teleop {reason} via /control ({who})")
            with self._lock:
                self._cmd = None
                self.counters["stops"] += 1
        self.client.event("robot.teleop_stop", sub=who, task_id=sess.claims.task_id if sess else None, reason=reason)
        _jlog("SOFT E-STOP from /control: arm stopped and latched", sub=who)

    def submit_op(self, sess: ControlSession, op: str, m: dict[str, Any]) -> asyncio.Future[dict[str, Any]]:
        return asyncio.wrap_future(self._ops.submit(self._op_safe, sess, op, m))

    def _op_safe(self, sess: ControlSession, op: str, m: dict[str, Any]) -> dict[str, Any]:
        try:
            why = self._authorized(sess)
            if why:
                return {"ok": False, "code": why}
            rc = self._rc_for(sess)
            if rc is not None and (op.startswith("record_") or self._rc_owns_takeover(rc)):
                return rc.handle({**m, "t": op})  # TDM-58: {t:"record_ack", op, req, ok, error?, recording}
            if op == "record_discard":
                return {"ok": False, "code": "unsupported", "detail": "record_discard needs TDM-58 recording controls"}
            return getattr(self, f"_op_{op}")(sess, m)
        except Exception as e:
            log.exception("teleop op %s failed", op)
            return {"ok": False, "code": "error", "detail": f"{type(e).__name__}: {e}"[:300]}
        finally:
            if op == "takeover" and m.get("auto"):
                self._auto_takeover = False

    # ---------------------------------------------------------------- TDM-58 recording controls
    def _rc_for(self, sess: ControlSession) -> Any:
        """The session's TDM-58 ``RecordingControl`` (task-scoped recording), or None (ops token, no
        cameras, or TDM-58 not installed: then record_start/stop below handle a clean demo)."""
        if sess.rc is False:
            return None
        if sess.rc is None:
            try:
                from .tdm58_recording import RecordingControl, lookup_task
            except ImportError:
                sess.rc = False
                return None
            from .recorder import recorder_for

            rec = recorder_for(self.client) if sess.claims.task_id > 0 else None
            if rec is None:
                sess.rc = False
                return None
            http = getattr(self.client, "http", None)
            sql = http.sql if http is not None else self.client.sql
            sm = self.client.sm
            sess.rc = RecordingControl(rec, lambda: lookup_task(sql, sess.claims.task_id),
                                       on_record_start=self._rc_record_start, on_takeover=lambda: self._rc_takeover(sess),
                                       on_handback=lambda: self._rc_handback(sess),
                                       # DAgger: takeover_ts = when the policy was paused (same as _op_takeover's own mark)
                                       paused_at_ns=lambda: self._paused_at_ns(sm) if sm.state is S.PAUSED_FOR_TAKEOVER else None)
        return sess.rc

    @staticmethod
    def _rc_owns_takeover(rc: Any) -> bool:
        """True when the recording control tracks a recovery_takeover episode (it then stamps the marks)."""
        try:
            st = rc.state()
        except Exception:  # noqa: BLE001
            return False
        return bool(st.get("active")) and st.get("kind") == "recovery_takeover"

    def _rc_record_start(self, kind: str) -> None:
        # a recovery episode opens with policy frames: resume a paused rollout before recording starts
        sm = self.client.sm
        if kind == "recovery_takeover" and self.mode is None and sm.state is S.PAUSED_FOR_TAKEOVER and self._job():
            sm.transition(S.ROLLOUT, "record_start (recovery): the policy drives first", allowed_from=[S.PAUSED_FOR_TAKEOVER])

    def _rc_takeover(self, sess: ControlSession) -> None:
        sm = self.client.sm
        if sm.state is S.ROLLOUT and self.mode is None and self._job():  # recording controls take over a running policy
            sm.transition(S.PAUSED_FOR_TAKEOVER, "takeover via /control: policy paused", allowed_from=[S.ROLLOUT])
            self.client.arm.hold()
        res = self._op_takeover(sess, {"_mark": False})
        if not res.get("ok"):
            raise RuntimeError(res.get("detail") or res.get("code"))

    def _rc_handback(self, sess: ControlSession) -> None:
        rc = sess.rc if sess.rc not in (None, False) else None
        res = self._op_handback(sess, {"success": getattr(rc, "handback_success", None)})
        if not res.get("ok"):
            raise RuntimeError(res.get("detail") or res.get("code"))

    # ---------------------------------------------------------------- ops (ops thread)
    def _job(self) -> JobContext | None:
        return getattr(self.client, "current_job", None) or self._ctx

    def _op_takeover(self, sess: ControlSession, m: dict[str, Any]) -> dict[str, Any]:
        sm = self.client.sm
        with self._lock:
            if self.mode == "takeover" and sm.state is S.TELEOP:
                return {"ok": True, "noop": True}
            if self.mode == "teleop":
                return {"ok": False, "code": "teleop_session", "detail": "already in a clean teleop session"}
            job = self._job()
            if sm.state is not S.PAUSED_FOR_TAKEOVER or job is None:
                return {"ok": False, "code": "not_paused", "detail": f"robot is {sm.state}, not paused_for_takeover"}
            sm.transition(S.TELEOP, f"takeover via /control ({sess.claims.sub})", command_id=job.command.id,
                          allowed_from=[S.PAUSED_FOR_TAKEOVER])
            self.mode, self._ctx = "takeover", job
            self._takeover_task_id = sess.claims.task_id
            self._handback_success = None  # this takeover's answer comes with its own handback
            self._handback.clear()
            self.counters["takeovers"] += 1
        ts = None
        paused_ns = self._paused_at_ns(sm)
        rec = self.client.services.get("recorder")
        if rec is not None and rec.recording and m.get("_mark", True):
            tk, hb = _rec_marks(rec)
            if tk is None or hb is not None:  # TDM-69 may have marked it at the pause already
                # takeover_ts = the moment the policy was frozen, so the held/connecting gap counts as the
                # human window (TDM-48 relabels its idle ticks human) and not as a policy-failure frame
                start_ns = getattr(getattr(rec, "_s", None), "start_ns", 0) or 0
                ts = rec.mark_takeover(paused_ns if paused_ns and paused_ns > start_ns else None)
        self.client.event("robot.takeover", command_id=job.command.id, ticket_id=job.command.ticket_id,
                          sub=sess.claims.sub, task_id=sess.claims.task_id, takeover_ts=ts, auto=bool(m.get("auto")),
                          recording=rec.current_key if rec is not None and rec.recording else None)
        t = threading.Thread(target=self._takeover_main, args=(job,), name="rdf-teleop-takeover", daemon=True)
        self._driver = t
        t.start()
        return {"ok": True, "takeover_ts": ts, "state": str(sm.state)}

    @staticmethod
    def _paused_at_ns(sm: Any) -> int | None:
        """monotonic_ns of the last entry into paused_for_takeover (from the state machine's history)."""
        for tr in reversed(getattr(sm, "history", [])):
            if tr.dst is S.PAUSED_FOR_TAKEOVER:
                return time.monotonic_ns() - int(max(time.time() - tr.at, 0.0) * 1e9)
        return None

    def _op_handback(self, sess: ControlSession, m: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "takeover":
            ok = m.get("success")
            self._handback_success = ok if isinstance(ok, bool) else None  # TDM-69: "Did you recover the task?"
            self._handback.set()
            t = self._driver
            if t is not None:
                t.join(3.0)
            return {"ok": True, "state": str(self.client.sm.state), **self._last_handback}
        if self.mode == "teleop" and self._ctx is not None:
            self._ctx.finish_requested.set()
            return {"ok": True, "ending": "start_teleop"}
        return {"ok": False, "code": "nothing_to_hand_back", "detail": f"robot is {self.client.sm.state}"}

    def _op_record_start(self, sess: ControlSession, m: dict[str, Any]) -> dict[str, Any]:
        from .recorder import recorder_for

        if self.mode is None:
            return {"ok": False, "code": "not_driving", "detail": "start_teleop or a takeover first"}
        rec = recorder_for(self.client)
        if rec is None:
            return {"ok": False, "code": "no_recorder", "detail": "robot client runs without cameras"}
        if rec.recording:
            return {"ok": False, "code": "already_recording", "episode_key": rec.current_key}
        job = self._ctx
        kind = self._kind if self.mode == "teleop" else "recovery_takeover"
        key = rec.start(job.command.ticket_id if job else 0, kind, sess.claims.task_id, sess.contributor_id,
                        note=f"teleop /control ({sess.claims.sub})")
        self._rec_owned = key
        return {"ok": True, "episode_key": key, "kind": kind}

    def _op_record_stop(self, sess: ControlSession, m: dict[str, Any]) -> dict[str, Any]:
        rec = self.client.services.get("recorder")
        if rec is None or not rec.recording:
            return {"ok": False, "code": "not_recording"}
        if rec.current_key != self._rec_owned:
            return {"ok": False, "code": "not_owner", "detail": "this recording belongs to the running job"}
        success = m.get("success")
        ep = self._stop_recording(rec, None if success is None else bool(success), "record_stop via /control")
        return {"ok": True, **ep}

    def _stop_recording(self, rec: Any, success: bool | None, note: str) -> dict[str, Any]:
        ep = rec.stop(success=success, note=note)
        self._rec_owned = None
        info = {"episode_key": ep.key, "status": ep.meta.get("status"), "frame_count": ep.meta.get("frame_count"),
                "duration_ms": ep.meta.get("duration_ms"), "success": success, "dir": str(ep.dir)}
        self.episodes.append(info)
        return info

    # ---------------------------------------------------------------- clean teleop job
    def run_teleop_job(self, ctx: JobContext) -> dict[str, Any]:
        p = ctx.payload
        kind = str(p.get("kind") or "clean_teleop")
        ctx.enter(S.TELEOP, f"start_teleop {kind}")
        ctx.arm.hold()
        with self._lock:
            self.mode, self._ctx, self._kind = "teleop", ctx, kind
            self._job_task_id = int(p.get("task_id") or 0)
            first_episode = len(self.episodes)
        ctx.event("robot.teleop_started", kind=kind, task_id=self._job_task_id)
        t0 = time.monotonic()
        steps = 0
        try:
            steps = self._drive(ctx, until=ctx.finish_requested.is_set)
        finally:
            try:
                if not self.client.sm.locked:
                    ctx.arm.hold()
            finally:
                rec = self.client.services.get("recorder")
                if rec is not None and rec.recording and rec.current_key == self._rec_owned:
                    self._stop_recording(rec, None, "teleop session ended before record_stop")
                with self._lock:
                    self.mode, self._ctx, self._job_task_id = None, None, 0
        return {"teleop": "ended", "kind": kind, "steps": steps, "seconds": round(time.monotonic() - t0, 2),
                "counters": dict(self.counters), "episodes": self.episodes[first_episode:]}

    # ---------------------------------------------------------------- takeover driver
    def _takeover_main(self, job: JobContext) -> None:
        sm = self.client.sm
        ended = "handback"
        try:
            self._drive(job, until=lambda: self._handback.is_set() or sm.state is not S.TELEOP)
            if not self._handback.is_set():
                ended = f"state {sm.state}"
        except Cancelled as e:
            ended = f"cancelled: {e.reason}"
        except Exception as e:
            ended = f"error: {type(e).__name__}: {e}"
            log.exception("takeover driver failed")
        finally:
            if not sm.locked:
                try:
                    self.client.arm.hold()
                except Exception:  # noqa: BLE001
                    pass
            ts = None
            rec = self.client.services.get("recorder")
            if rec is not None and rec.recording:
                tk, hb = _rec_marks(rec)
                if tk is not None and hb is None:
                    ts = rec.mark_handback()  # before the policy's first new tick
            resumed = False
            if self._handback.is_set() and sm.state is S.TELEOP:
                sm.transition(S.ROLLOUT, "handback via /control (resume_policy)", command_id=job.command.id)
                resumed = True
                job.signals.put(Command(id=0, seq=0, robot_id=job.command.robot_id, ticket_id=job.command.ticket_id,
                                        type="handback", payload={"via": "/control", "handback_ts": ts,
                                                                  "success": self._handback_success},
                                        status="acked", issued_by="teleop:/control", issued_at_us=int(time.time() * 1e6)))
            self.counters["handbacks"] += 1
            self._last_handback = {"handback_ts": ts, "resumed_policy": resumed, "ended": ended}
            self.client.event("robot.handback", command_id=job.command.id, ticket_id=job.command.ticket_id,
                              handback_ts=ts, resumed_policy=resumed, ended=ended)
            if resumed and self._takeover_task_id:  # TDM-69 contract: the orchestrator logs the handback from this
                self.client.event("intervention.handed_back", task_id=self._takeover_task_id,
                                  command_id=job.command.id, ticket_id=job.command.ticket_id, handback_ts=ts,
                                  via="/control", success=self._handback_success)
            with self._lock:
                self.mode, self._ctx = None, None

    # ---------------------------------------------------------------- the control loop
    def _workspace(self) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
        ws = getattr(getattr(self.client.arm, "config", None), "workspace", None)
        if ws is None or not getattr(ws, "enabled", False):
            return None
        return tuple(ws.lo), tuple(ws.hi)

    def _status(self, **kw: Any) -> None:
        self._drive_status = {**self._drive_status, **kw}

    def _drive(self, ctx: JobContext, until: Callable[[], bool]) -> int:
        """Stream clutch targets at the control rate until ``until()``. Raises Cancelled on e-stop."""
        arm = ctx.arm
        period = 1.0 / arm.control_rate_hz
        filt = ClutchFilter(self.cfg, self._workspace())
        held = True
        need_reclutch = False
        steps = 0
        last_t = time.monotonic()
        next_t = last_t

        def hold(reason: str) -> None:
            nonlocal held
            if filt.engaged or not held:
                arm.hold()
            held = True
            filt.release()
            self._status(engaged=False, hold_reason=reason)

        while True:
            ctx.check()
            if until():
                break
            now = time.monotonic()
            if self.client.sm.state is not S.TELEOP or not self.client.link_ok():
                hold("not_teleop" if self.client.sm.state is not S.TELEOP else "link_down")
            else:
                with self._lock:
                    cmd = self._cmd if self._session is not None and not self._session.revoked else None
                fresh = cmd is not None and now - cmd.rx <= self.cfg.cmd_timeout_s
                if cmd is None or not fresh or not cmd.clutch:
                    released = cmd is not None and not cmd.clutch
                    if filt.engaged:
                        self.counters["release_holds" if released else "gap_holds"] += 1
                    if released:  # an explicit clutch release clears a pending re-clutch
                        need_reclutch = False
                    if self._session is None:
                        reason = "no_session"
                    elif cmd is None:
                        reason = "no_cmd"
                    else:
                        reason = "clutch_released" if released else "cmd_gap"
                    hold(reason)
                elif need_reclutch:
                    hold("reclutch_needed")
                else:
                    if not filt.engaged:
                        filt.engage(arm.read_state().ee_pose, cmd)
                        self.counters["engagements"] += 1
                        seed = None
                    else:
                        seed = arm.last_joint_cmd
                    dt = min(max(now - last_t, 0.0), 2 * period)
                    pose = filt.step(cmd, dt or period)
                    try:
                        arm.command_ee(pose, cmd.gripper, seed=seed)
                    except ArmStopped as e:
                        raise Cancelled("estop", str(e)) from e
                    except IKError:
                        self.counters["ik_skips"] += 1
                        filt.reject_last(cmd)
                        self._status(hold_reason="ik_unreachable")
                    except ArmError as e:  # SafetyViolation: too many rejects -> hold, wait for a new clutch
                        if type(e).__name__ != "SafetyViolation":
                            raise
                        self.counters["safety_violations"] += 1
                        need_reclutch = True
                        hold("safety_violation")
                    else:
                        held = False
                        steps += 1
                        self.counters["steps"] += 1
                        self.counters["workspace_clamps"] = filt.clamped
                        self.client.record_command(EETarget(pose, cmd.gripper), "human")
                        self._status(engaged=True, hold_reason=None, seq=cmd.seq, target=pose.to_dict(),
                                     gripper_cmd=cmd.gripper, speed_scale=cmd.speed_scale)
            last_t = now
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                ctx.sleep(delay)
            else:
                next_t = time.monotonic()
        hold("ended")
        return steps

    # ---------------------------------------------------------------- state for the browser
    def state_message(self, sess: ControlSession | None = None) -> dict[str, Any]:
        arm, sm = self.client.arm, self.client.sm
        st = arm.read_state()
        stats = getattr(arm, "stats", None)
        safety = stats() if callable(stats) else {}
        try:
            source = self.client.control_tick().control_source
        except Exception:  # noqa: BLE001
            source = None
        rec = self.client.services.get("recorder")
        recording: dict[str, Any] = {"active": False}
        rc = self._rc_for(sess) if sess is not None and sess.claims.task_id > 0 else None
        if rc is not None:
            recording = rc.state()
            source = rc.control_source() or source
        elif rec is not None and rec.recording:
            tk, hb = _rec_marks(rec)
            recording = {"active": True, "episode_key": rec.current_key, "owned": rec.current_key == self._rec_owned,
                         "takeover_marked": tk is not None, "handback_marked": hb is not None}
        d = dict(self._drive_status)
        job = self._job()
        now = time.monotonic()
        recent: dict[str, bool] = {}
        for k in SAFETY_KEYS:
            n = int(safety.get(k, 0) or 0)
            prev, at = self._clamp_seen.get(k, (0, -1e9))
            if n > prev:
                self._clamp_seen[k] = (n, now)
                at = now
            if now - at < 1.0:
                recent[k] = True
        return {
            "t": "state", "ts_ms": int(time.time() * 1000), "server_ns": time.monotonic_ns(),
            "robot_state": str(sm.state), "locked": sm.locked, "stopped": sm.locked or bool(safety.get("stopped")),
            "mode": self.mode, "control_source": source, "watchdog": d["hold_reason"] == "cmd_gap",
            "active": sess is not None and sess is self._session and not sess.revoked,
            "clutch": d["engaged"], "hold_reason": d["hold_reason"], "seq": d["seq"],
            "pose": st.ee_pose.to_dict(), "target": d["target"], "joints": [round(v, 4) for v in st.joints],
            "gripper": round(st.gripper, 3), "gripper_cmd": d["gripper_cmd"], "speed_scale": d["speed_scale"],
            "effective_speed_scale": safety.get("effective_scale"),
            "clamps": recent, "clamp_counts": {k: safety.get(k, 0) for k in SAFETY_KEYS},
            "teleop": dict(self.counters),
            "recording": recording,
            "can_takeover": sm.state is S.PAUSED_FOR_TAKEOVER and job is not None and self.mode is None,
            "can_handback": self.mode == "takeover" and sm.state is S.TELEOP,
            "lease": sess.guard.status() if sess is not None and hasattr(sess.guard, "status") else None,
        }

    def close(self) -> None:
        self._ops.shutdown(wait=False, cancel_futures=True)


def hub_for(client: Any, **kw: Any) -> TeleopHub:
    """The client's shared hub (``services["teleop"]``), created on first use."""
    services = client.services
    hub = services.get("teleop")
    if hub is None:
        hub = services["teleop"] = TeleopHub(client, **kw)
    return hub


@job_handler("start_teleop")
def start_teleop(ctx: JobContext) -> dict[str, Any]:
    """TDM-52 clean teleop session: drive from /control until stop_teleop (or the browser's handback)."""
    hub = hub_for(ctx.client)
    res = hub.run_teleop_job(ctx)
    if ctx.services.get("control_url") is None:
        res["note"] = "no /control endpoint on this client (stream off): the arm was held"
    return res


# ------------------------------------------------------------------------------------ the socket

def _type(m: dict[str, Any]) -> str:
    return str(m.get("t") or m.get("type") or "")


async def control_ws(server: Any, ws: Any, hub: TeleopHub) -> None:
    """``/control`` handler, mounted on the TDM-55 ``CameraStreamServer`` (its asyncio thread)."""
    from websockets.exceptions import ConnectionClosed

    loop = asyncio.get_running_loop()

    async def refuse(code: str, close: int, kind: str = "error", detail: str = "") -> None:
        try:
            await ws.send(json.dumps({"t": kind, "code": code, "detail": detail}))
            await ws.close(close, code)
        except ConnectionClosed:
            pass

    try:
        first = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S)
        msg = json.loads(first) if isinstance(first, str) else {}
        if _type(msg) != "auth":
            raise StreamTokenError("auth_required")
        claims = server.check_token(str(msg.get("token", "")))
        if getattr(claims, "scope", "control") != "control":  # TDM-96: view tokens watch, never drive
            raise StreamTokenError("view_only")
    except (TimeoutError, json.JSONDecodeError, StreamTokenError) as e:
        code = e.code if isinstance(e, StreamTokenError) else "auth_required"
        server.denied += 1
        await refuse(code, CLOSE_EXPIRED if code == "expired" else CLOSE_FORBIDDEN)
        return
    except ConnectionClosed:
        return
    try:
        guard = hub.make_guard(claims)
        d = await guard.admit_async()
    except Exception as e:
        log.exception("control: lease guard unavailable")
        await refuse("lease_unavailable", CLOSE_LEASE_LOST, "lease_lost", f"{type(e).__name__}")
        return
    if not d.ok:
        await refuse(d.code, CLOSE_EXPIRED if d.code == "token_expired" else CLOSE_LEASE_LOST, "lease_lost", d.detail)
        return

    sess = ControlSession(ws, loop, claims, guard)
    closing = asyncio.Event()
    lost: list[Any] = []

    def on_lost(dec: Any) -> None:  # on this loop (guard.watch / check_message); the arm holds next tick
        if lost:
            return
        lost.append(dec)
        hub.revoke(sess, dec.code)
        closing.set()

    guard.on_lost = on_lost
    old = hub.attach(sess)
    if old is not None:
        old.close_threadsafe(CLOSE_REPLACED, "replaced")
    _jlog("control: connected", sub=claims.sub, task_id=claims.task_id, peer=sess.peer, session=sess.id)
    await sess.send({"t": "hello", "protocol": PROTOCOL, "robot_id": claims.robot_id, "task_id": claims.task_id,
                     "exp": claims.exp, "rate_hz": hub.client.arm.control_rate_hz,
                     "cmd_timeout_ms": int(hub.cfg.cmd_timeout_s * 1000), "state_hz": hub.cfg.state_hz,
                     "session": sess.id})

    async def receiver() -> None:
        async for raw in ws:
            if not isinstance(raw, str):
                continue
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(m, dict):
                continue
            t = _type(m)
            if t == "stop":  # first, and never gated
                hub.stop(sess)
                await sess.send({"t": "ack", "op": "stop", "ok": True})
                continue
            if t == "ping":
                await sess.send({"t": "pong", "ts_ms": m.get("ts_ms"), "server_ns": time.monotonic_ns()})
                continue
            if t == "auth":
                try:
                    c = server.check_token(str(m.get("token", "")))
                    if getattr(c, "scope", "control") != "control":  # TDM-96: no downgrade to a view token
                        raise StreamTokenError("view_only")
                except StreamTokenError as e:
                    await sess.send({"t": "error", "code": e.code})
                    continue
                dd = guard.update_token(c)
                if dd.ok:
                    sess.claims = getattr(guard, "claims", c)
                    await sess.send({"t": "auth_ok", "exp": sess.claims.exp})
                else:
                    await sess.send({"t": "error", "code": dd.code})
                continue
            if t not in ("cmd", *OPS):
                await sess.send({"t": "error", "code": "unknown_type", "detail": t[:40]})
                continue
            dd = guard.check_message({**m, "t": t})
            if not dd.ok:  # a latched loss also fires on_lost -> hold + lease_lost + close
                if t == "cmd":
                    await sess.send({"t": "ack", "seq": m.get("seq"), "ok": False, "code": dd.code})
                else:
                    await sess.send({"t": "ack", "op": t, "ok": False, "code": dd.code})
                continue
            if t == "cmd":
                why = hub.on_cmd(sess, m)
                await sess.send({"t": "ack", "seq": m.get("seq")} if why is None
                                else {"t": "ack", "seq": m.get("seq"), "ok": False, "code": why})
            else:
                async def run_op(op: str = t, mm: dict[str, Any] = m) -> None:
                    res = await hub.submit_op(sess, op, mm)
                    await sess.send(res if "t" in res else {"t": "ack", "op": op, "req": mm.get("req"), **res})
                asyncio.ensure_future(run_op())

    async def sender() -> None:
        period = 1.0 / max(hub.cfg.state_hz, 0.5)
        while True:
            try:
                state = hub.state_message(sess)
            except Exception as e:  # noqa: BLE001 - state is best effort
                state = {"t": "state", "error": f"{type(e).__name__}: {e}"[:200]}
            await sess.send(state)
            await asyncio.sleep(period)

    async def watcher() -> None:
        await guard.watch()
        await asyncio.Event().wait()  # park: closer() ends the session

    async def closer() -> None:
        await closing.wait()
        dec = lost[0]
        await refuse(dec.code, CLOSE_EXPIRED if dec.code == "token_expired" else CLOSE_LEASE_LOST,
                     "lease_lost", getattr(dec, "detail", ""))

    tasks = [asyncio.create_task(x) for x in (receiver(), sender(), watcher(), closer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except ConnectionClosed:
        pass
    finally:
        for tk in tasks:
            tk.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        hub.detach(sess, sess.revoked or "closed")
        if sess.rc:  # TDM-58: never upload half a demo when the operator's socket is gone
            try:
                await asyncio.to_thread(sess.rc.abandon, f"operator disconnected ({sess.revoked or 'closed'})")
            except Exception:
                log.exception("control: abandon recording failed")
        _jlog("control: disconnected", sub=claims.sub, session=sess.id, reason=sess.revoked or "closed")


def install_control(stream: Any, hub: TeleopHub) -> None:
    """Mount ``/control`` on a TDM-55 ``StreamHandle`` (or ``CameraStreamServer``)."""
    server = getattr(stream, "server", stream)

    async def handler(srv: Any, ws: Any) -> None:
        await control_ws(srv, ws, hub)

    for path in CONTROL_PATHS:
        server.add_ws_route(path, handler)
    hub.client.services["control_url"] = CONTROL_PATH


# ------------------------------------------------------------------------------------ CLI

def _cmd_serve(a: argparse.Namespace) -> int:
    """Standalone teleop server (no SpacetimeDB): SimArm or YAM + safety + cameras + stream + /control."""
    from .cameras import CameraRig
    from .main import build_arm
    from .recorder import Recorder
    from .rollout import StandaloneRobot
    from .stream import start_stream

    arm = build_arm("sim" if a.sim else (a.adapter or "yam"))
    arm.connect()
    rig = CameraRig.from_config(synthetic=a.synthetic).start()
    if not rig.wait_ready(10):
        print("[teleop] cameras not ready after 10 s", file=sys.stderr)
    robot = StandaloneRobot(arm, services={"cameras": rig})
    robot.current_job = None  # type: ignore[attr-defined]
    robot.services["recorder"] = Recorder(robot, rig, root=a.episodes_dir)
    hub = hub_for(robot, config=TeleopConfig(allow_ops_tokens=a.allow_ops))
    stream = start_stream(rig, robot_name=a.robot_name, port=a.port, tunnel=a.tunnel)
    install_control(stream, hub)
    from .stream import install_state_feed  # TDM-96: /ws/state on the same server

    install_state_feed(stream, robot, sim=a.sim)
    print(json.dumps(stream.info(), indent=2), flush=True)
    ctx = robot.context({"kind": "clean_teleop"}, type_="start_teleop")
    robot.current_job = ctx  # type: ignore[attr-defined]
    try:
        print(json.dumps(hub.run_teleop_job(ctx), indent=2, default=str))
    except KeyboardInterrupt:
        ctx.finish_requested.set()
    finally:
        stream.stop()
        rig.stop()
        arm.disconnect()
    return 0


async def probe(url: str, token: str, *, pattern: str = "square", size: float = 0.02, seconds: float = 8.0,
                rate_hz: float = 30.0, gripper: float | None = None, speed_scale: float = 0.5,
                stop_at_end: bool = False) -> dict[str, Any]:
    """Scripted browser: clutch, trace a small shape (base-frame deltas), release, report states."""
    import websockets

    states: list[dict[str, Any]] = []
    acks = 0
    async with websockets.connect(url, open_timeout=20, max_size=1 << 20) as ws:
        await ws.send(json.dumps({"t": "auth", "token": token}))
        hello = json.loads(await ws.recv())
        if hello.get("t") != "hello":
            return {"error": hello}

        async def rx() -> None:
            nonlocal acks
            async for raw in ws:
                m = json.loads(raw)
                if m.get("t") == "state":
                    states.append(m)
                elif m.get("t") == "ack":
                    acks += 1
                elif m.get("t") in ("lease_lost", "error"):
                    states.append(m)

        rt = asyncio.create_task(rx())
        t0 = time.monotonic()
        seq = 0
        while (t := time.monotonic() - t0) < seconds:
            ph = 2 * math.pi * t / seconds
            if pattern == "square":
                s = (t / seconds) * 4
                k, f = int(s) % 4, s - int(s)
                pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
                x = (pts[k][0] + (pts[k + 1][0] - pts[k][0]) * f) * size
                y = (pts[k][1] + (pts[k + 1][1] - pts[k][1]) * f) * size
                d = [x, y, 0.0]
            elif pattern == "circle":
                d = [size * (1 - math.cos(ph)), size * math.sin(ph), 0.0]
            else:  # "z": up and down
                d = [0.0, 0.0, size * math.sin(ph)]
            seq += 1
            await ws.send(json.dumps({"t": "cmd", "seq": seq, "ts_ms": time.time() * 1000, "clutch": True,
                                      "d_pos": d, "d_rot": [0, 0, 0], "gripper": gripper,
                                      "speed_scale": speed_scale}))
            await asyncio.sleep(1.0 / rate_hz)
        await ws.send(json.dumps({"t": "cmd", "seq": seq + 1, "clutch": False}))
        await asyncio.sleep(0.5)
        if stop_at_end:
            await ws.send(json.dumps({"t": "stop"}))
            await asyncio.sleep(0.3)
        rt.cancel()
    last = next((s for s in reversed(states) if s.get("t") == "state"), {})
    return {"hello": hello, "cmds": seq, "acks": acks, "states": len(states),
            "last_state": {k: last.get(k) for k in ("robot_state", "mode", "hold_reason", "pose", "clamps", "teleop")},
            "errors": [s for s in states if s.get("t") != "state"][:5]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_robot_client.teleop", description="TDM-52 teleop /control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="standalone teleop: arm + cameras + stream + /control (no SpacetimeDB)")
    s.add_argument("--sim", action="store_true")
    s.add_argument("--adapter", default=None)
    s.add_argument("--synthetic", action="store_true", help="synthetic cameras")
    s.add_argument("--robot-name", default="test.teleop-sim", help="tokens must name this robot")
    s.add_argument("--port", type=int, default=8091)
    s.add_argument("--tunnel", choices=["cloudflared", "none"], default="none")
    s.add_argument("--allow-ops", action="store_true", help="ops tokens may drive without a task lease (HIL/dev)")
    s.add_argument("--episodes-dir", default=None)
    s.set_defaults(fn=_cmd_serve)
    p = sub.add_parser("probe", help="scripted browser: clutch + small shape")
    p.add_argument("url")
    p.add_argument("--token", required=True)
    p.add_argument("--pattern", choices=["square", "circle", "z"], default="square")
    p.add_argument("--size", type=float, default=0.02, help="metres (keep it small on the real arm)")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--speed-scale", type=float, default=0.5)
    p.add_argument("--stop-at-end", action="store_true", help="send {t:stop} at the end (latches!)")
    p.set_defaults(fn=lambda a: (print(json.dumps(asyncio.run(probe(
        a.url, a.token, pattern=a.pattern, size=a.size, seconds=a.seconds, speed_scale=a.speed_scale,
        stop_at_end=a.stop_at_end)), indent=2, default=str)), 0)[1])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())

__all__ = ["ClutchFilter", "ControlCmd", "ControlSession", "TeleopConfig", "TeleopHub", "control_ws", "hub_for",
           "install_control", "parse_cmd", "probe", "start_teleop"]
