"""TDM-118: read-only YAM. Joint reads over CAN, NO motion and no command path.

``make_adapter("yam_readonly")`` builds :class:`Tdm118ReadOnlyYamArm`. It uses the same kinematics, joint
limits and action space as :class:`~rdf_robot_client.adapters.yam.YamArm`, so FK, the /ws/state feed and
the twin are the same as on the real adapter. It never imports the I2RT SDK's motor drivers and never enables a motor.

How it reads the joints without moving anything:
  * The DM motors in the YAM (J1-J3 DM4340, J4-J6 DM4310, gripper DM4310 on id 7) answer a
    **"refresh status" query** (standard id ``0x7FF``, data ``[id_lo, id_hi, 0xCC, 0, 0, 0, 0, 0]``) with
    their normal feedback frame (``0x10 + id``: position, velocity, torque, temperatures). The query does
    not enable, disable or command the motor, so the arm stays exactly as it is (limp if the previous
    runtime released it).
  * Every frame we transmit goes through :class:`Tdm118TxGuard`, which refuses anything that is not
    exactly that query. No enable (``..FC``), disable (``..FD``), zero (``..FE``), clear-error (``..FB``),
    MIT control frame, register write (``0x55``) or flash save (``0xAA``) can leave this process.
  * ``connect()`` first listens on the bus for ``busy_listen_s`` (0.3 s) **without sending**. If any motor
    or controller frame is seen, another runtime (vrteleop) is driving the arm and we refuse. It also refuses
    while ``vrteleop-rh.service`` is active.
  * ``TDM118_YAM_READ=listen`` sends nothing at all and only decodes feedback frames that are already on the
    bus. That's useful only while something else is talking to the motors.

Joint positions follow the SDK: ``joint = (motor_pos - offset) * direction`` (directions all +1 on YAM v1),
with a +/-2 pi wrap offset fixed from the first reading (``|pos| > pi``), the same rule as ``get_yam_robot``.
The gripper is reported 0..1 only when ``YAM_GRIPPER_LIMITS=closed,open`` is set. Otherwise it reads
``0.0``, and ``status().detail["gripper_calibrated"]`` is False, with the raw motor position in
``gripper_raw``. A linear gripper's limits come from the SDK's calibration sweep, which is motion
(TDM-119).

Every ``command_*`` / ``move_*`` / ``home`` raises :class:`ReadOnlyArmError`. ``hold()`` is a no-op (there
is nothing to hold: nothing was commanded) and ``stop()`` only latches the soft e-stop flag.
"""
from __future__ import annotations

import logging
import math
import os
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .base import ArmError, ArmNotConnected, ArmState, ArmStatus
from .yam import YamArm

log = logging.getLogger("rdf_robot_client.tdm118_yam_readonly")

# DM motor protocol, as used by the I2RT SDK's dm_driver (ReceiveMode.p16, MIT mode).
TDM118_REFRESH_ID = 0x7FF
TDM118_REFRESH_OP = 0xCC
TDM118_FEEDBACK_OFFSET = 0x10
# (CAN id, motor type) for YAM v1 (i2rt robots/config/yam.yml) + the linear_4310 gripper on id 7.
TDM118_YAM_MOTORS: tuple[tuple[int, str], ...] = (
    (1, "DM4340"), (2, "DM4340"), (3, "DM4340"), (4, "DM4310"), (5, "DM4310"), (6, "DM4310"), (7, "DM4310"),
)
TDM118_DIRECTIONS = (1, 1, 1, 1, 1, 1, 1)
# p / v / t ranges of the MIT feedback encoding, per motor type (i2rt motor_drivers/utils.py MotorType)
TDM118_MOTOR_RANGES = {"DM4340": (12.5, 10.0, 28.0), "DM4310": (12.5, 30.0, 10.0)}
TDM118_STATE_TEXT = {0x0: "disabled", 0x1: "enabled", 0x8: "overvoltage", 0x9: "undervoltage", 0xA: "overcurrent",
                     0xB: "mos_overtemp", 0xC: "coil_overtemp", 0xD: "comm_lost", 0xE: "overload"}
_FRAME = struct.Struct("=IB3x8s")  # struct can_frame
_CAN_EFF_FLAG = 0x80000000
_CAN_RTR_FLAG = 0x40000000
_CAN_ERR_FLAG = 0x20000000


class ReadOnlyArmError(ArmError):
    """A motion command reached the read-only adapter. Motion is enabled only by TDM-119."""


def tdm118_refresh_frame(motor_id: int) -> bytes:
    """The one frame this adapter may send: DM "refresh status" for ``motor_id``."""
    if not 1 <= motor_id <= 0x7F:
        raise ValueError(f"motor id {motor_id} out of range")
    return bytes([motor_id & 0xFF, (motor_id >> 8) & 0xFF, TDM118_REFRESH_OP, 0, 0, 0, 0, 0])


class Tdm118Bus(Protocol):
    def send(self, arb_id: int, data: bytes) -> None: ...
    def recv(self, timeout: float) -> tuple[int, bytes] | None: ...
    def close(self) -> None: ...


class Tdm118TxGuard:
    """Wraps a bus so the ONLY frame that can be transmitted is a refresh query to a YAM motor id."""

    def __init__(self, bus: Tdm118Bus, motor_ids: Sequence[int]):
        self.bus = bus
        self.allowed = {tdm118_refresh_frame(m) for m in motor_ids}
        self.sent = 0

    def send(self, arb_id: int, data: bytes) -> None:
        if arb_id != TDM118_REFRESH_ID or bytes(data) not in self.allowed:
            raise ReadOnlyArmError(f"TDM-118 read-only: refusing to transmit id=0x{arb_id:X} data={bytes(data).hex()}")
        self.bus.send(arb_id, bytes(data))
        self.sent += 1

    def recv(self, timeout: float) -> tuple[int, bytes] | None:
        return self.bus.recv(timeout)

    def close(self) -> None:
        self.bus.close()


class Tdm118SocketCanBus:
    """Raw SocketCAN through the stdlib (no python-can). The interface must already be UP."""

    def __init__(self, channel: str):
        if not hasattr(socket, "AF_CAN"):
            raise ArmError("SocketCAN is Linux-only (socket.AF_CAN missing)")
        self.channel = channel
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            self.sock.bind((channel,))
        except OSError as e:
            self.sock.close()
            raise ArmError(f"cannot open CAN interface {channel!r}: {e}") from e

    def send(self, arb_id: int, data: bytes) -> None:
        self.sock.send(_FRAME.pack(arb_id, len(data), bytes(data).ljust(8, b"\0")))

    def recv(self, timeout: float) -> tuple[int, bytes] | None:
        self.sock.settimeout(max(timeout, 1e-4))
        try:
            raw = self.sock.recv(_FRAME.size)
        except TimeoutError:
            return None
        arb, dlc, data = _FRAME.unpack(raw)
        if arb & (_CAN_ERR_FLAG | _CAN_RTR_FLAG):
            return None
        return arb & ~_CAN_EFF_FLAG, data[:dlc]

    def close(self) -> None:
        self.sock.close()


@dataclass(frozen=True)
class Tdm118Feedback:
    motor_id: int
    state: int  # high nibble of byte 0: 0 disabled, 1 enabled, 8+ faults
    position: float
    velocity: float
    torque: float
    t_mos: float
    t_rotor: float
    t: float

    @property
    def state_text(self) -> str:
        return TDM118_STATE_TEXT.get(self.state, f"0x{self.state:X}")


def _u2f(x: int, lim: float, bits: int) -> float:
    return x * (2 * lim) / ((1 << bits) - 1) - lim


def tdm118_parse_feedback(arb_id: int, data: bytes, motors: dict[int, str], t: float) -> Tdm118Feedback | None:
    mid = arb_id - TDM118_FEEDBACK_OFFSET
    if mid not in motors or len(data) < 8:
        return None
    p_lim, v_lim, t_lim = TDM118_MOTOR_RANGES[motors[mid]]
    p_int = (data[1] << 8) | data[2]
    v_int = (data[3] << 4) | (data[4] >> 4)
    t_int = ((data[4] & 0xF) << 8) | data[5]
    return Tdm118Feedback(mid, (data[0] & 0xF0) >> 4, _u2f(p_int, p_lim, 16), _u2f(v_int, v_lim, 12),
                          _u2f(t_int, t_lim, 12), float(data[6]), float(data[7]), t)


def tdm118_vrteleop_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "vrteleop-rh"], timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class Tdm118ReadOnlyYamArm(YamArm):
    """YAM joint reads over CAN with every motion path refused. See the module docstring."""

    name = "yam_readonly"
    embodiment = "i2rt_yam"
    adapter_version = "yam-readonly-tdm118"

    def __init__(
        self,
        *,
        read_mode: str | None = None,
        read_hz: float | None = None,
        busy_listen_s: float = 0.3,
        reply_timeout_s: float = 0.02,
        bus_factory: Callable[[str], Tdm118Bus] | None = None,
        vrteleop_check: Callable[[], bool] | None = tdm118_vrteleop_active,
        **kw: Any,
    ) -> None:
        kw.setdefault("smoothing", False)
        super().__init__(**kw)
        if self.sim:
            raise ArmError("yam_readonly reads the real arm; use --adapter sim / mujoco for a simulator")
        self.read_mode = (read_mode or os.environ.get("TDM118_YAM_READ") or "refresh").strip().lower()
        if self.read_mode not in ("refresh", "listen"):
            raise ArmError(f"TDM118_YAM_READ must be refresh|listen, not {self.read_mode!r}")
        self.read_hz = float(read_hz or os.environ.get("TDM118_YAM_READ_HZ") or 20.0)
        self.busy_listen_s = busy_listen_s
        self.reply_timeout_s = reply_timeout_s
        self._bus_factory = bus_factory or Tdm118SocketCanBus
        self._vrteleop_check = vrteleop_check
        motors = TDM118_YAM_MOTORS if self.has_gripper else TDM118_YAM_MOTORS[: self.dof]
        self.motors = dict(motors)
        self._bus: Tdm118TxGuard | None = None
        self._fb: dict[int, Tdm118Feedback] = {}
        self._offsets: dict[int, float] = {}
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._polls = 0
        self._poll_t0 = 0.0
        self._first_full = threading.Event()

    # ------------------------------------------------------------------ static facts
    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({"adapter": self.name, "read_only": True, "read_mode": self.read_mode,
                  "motion": "disabled: TDM-118 read-only (motion comes with TDM-119)",
                  "sdk": "none (stdlib SocketCAN, DM refresh-status query only)", "smoothing": False,
                  "servo_rate_hz": None})
        return d

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        with self._lock:
            if self._bus is not None:
                return
            if self._vrteleop_check is not None and self._vrteleop_check():
                raise ArmError("vrteleop-rh is running and owns the CAN buses: the user stops it first "
                               "(sudo systemctl stop vrteleop-rh). Refusing to open CAN.")
            bus = self._bus_factory(self.channel)
            try:
                if self.read_mode == "refresh":
                    deadline = time.monotonic() + self.busy_listen_s
                    while (left := deadline - time.monotonic()) > 0:
                        got = bus.recv(left)
                        if got is not None:
                            raise ArmError(f"CAN {self.channel} is busy (frame id=0x{got[0]:X} seen before we sent "
                                           "anything): another runtime is driving this arm. Refusing.")
            except BaseException:
                bus.close()
                raise
            self._bus = Tdm118TxGuard(bus, list(self.motors))
            self._fault = None
            self._reader_stop.clear()
            self._poll_t0 = time.monotonic()
            self._reader = threading.Thread(target=self._read_loop, name="tdm118-yam-read", daemon=True)
            self._reader.start()
        if not self._first_full.wait(2.0):
            log.warning("TDM-118: no complete joint reading from %s within 2 s (arm powered? motors on this bus?)",
                        self.channel)
        log.info("TDM-118 read-only YAM on %s (%s): motion disabled", self.channel, self.read_mode)

    def disconnect(self) -> None:
        self._reader_stop.set()
        t = self._reader
        if t is not None:
            t.join(timeout=2.0)
        with self._lock:
            if self._bus is not None:
                try:
                    self._bus.close()
                except OSError:
                    pass
            self._bus = None
            self._reader = None

    # ------------------------------------------------------------------ reading
    def _read_loop(self) -> None:
        period = 1.0 / self.read_hz
        while not self._reader_stop.is_set():
            t0 = time.monotonic()
            try:
                self._poll_once()
            except ReadOnlyArmError as e:  # a bug tried to transmit something else: stop reading for good
                self._fault = f"read loop stopped: {e}"
                log.error("TDM-118: %s", e)
                return
            except OSError as e:  # e.g. ENOBUFS: nobody ACKs (arm unpowered), or the link went down
                self._fault = f"CAN {self.channel}: {e}"
                self._reader_stop.wait(0.5)
                continue
            self._reader_stop.wait(max(0.0, period - (time.monotonic() - t0)))

    def _poll_once(self) -> None:
        bus = self._bus
        if bus is None:
            return
        want = set(self.motors)
        if self.read_mode == "refresh":
            for mid in self.motors:
                bus.send(TDM118_REFRESH_ID, tdm118_refresh_frame(mid))
        deadline = time.monotonic() + (self.reply_timeout_s if self.read_mode == "refresh" else 1.0 / self.read_hz)
        seen: set[int] = set()
        while seen != want and (left := deadline - time.monotonic()) > 0:
            got = bus.recv(left)
            if got is None:
                break
            fb = tdm118_parse_feedback(got[0], got[1], self.motors, time.monotonic())
            if fb is None:
                continue
            seen.add(fb.motor_id)
            with self._lock:
                if fb.motor_id not in self._offsets:
                    self._offsets[fb.motor_id] = (-2 * math.pi if fb.position < -math.pi
                                                  else 2 * math.pi if fb.position > math.pi else 0.0)
                self._fb[fb.motor_id] = fb
        self._polls += 1
        if seen == want:
            self._fault = None
            self._first_full.set()

    def _joint(self, mid: int) -> float:
        fb = self._fb[mid]
        return (fb.position - self._offsets.get(mid, 0.0)) * TDM118_DIRECTIONS[mid - 1]

    def _gripper(self) -> tuple[float, float | None]:
        if not self.has_gripper or 7 not in self._fb:
            return 0.0, None
        raw = self._joint(7)
        if not self.gripper_limits:
            return 0.0, raw
        closed, opened = self.gripper_limits
        span = (opened - closed) or 1.0
        return min(1.0, max(0.0, (raw - closed) / span)), raw

    def read_state(self) -> ArmState:
        with self._lock:
            if self._bus is None:
                raise ArmNotConnected("yam_readonly: not connected")
            missing = [m for m in range(1, self.dof + 1) if m not in self._fb]
            if missing:
                raise ArmError(f"yam_readonly: no feedback yet from motor(s) {missing} on {self.channel}")
            q = tuple(self._joint(m) for m in range(1, self.dof + 1))
            qd = tuple(self._fb[m].velocity * TDM118_DIRECTIONS[m - 1] for m in range(1, self.dof + 1))
            g, _raw = self._gripper()
        return ArmState(t=time.monotonic(), joints=q, gripper=g, ee_pose=self.fk(q), joint_velocities=qd,
                        moving=any(abs(v) > 0.02 for v in qd))

    def status(self) -> ArmStatus:
        with self._lock:
            now = time.monotonic()
            connected = self._bus is not None
            ages = {m: now - fb.t for m, fb in self._fb.items()}
            stale = [m for m in self.motors if ages.get(m, 1e9) > 1.0]
            fault = self._fault or (f"no feedback from motor(s) {stale} for > 1 s" if connected and stale else None)
            _g, raw = self._gripper()
            elapsed = now - self._poll_t0 if self._poll_t0 else 0.0
            detail: dict[str, Any] = {
                "read_only": True, "read_mode": self.read_mode, "can_channel": self.channel,
                "kinematics": self.kin.backend, "read_hz": round(self._polls / elapsed, 1) if elapsed > 0 else 0.0,
                "frames_sent": self._bus.sent if self._bus is not None else 0,
                "motors": {m: {"state": fb.state_text, "t_mos": fb.t_mos, "t_rotor": fb.t_rotor,
                               "age_ms": round(ages[m] * 1000)} for m, fb in sorted(self._fb.items())},
                "enabled_by_other": sorted(m for m, fb in self._fb.items() if fb.state == 0x1),
                "gripper_calibrated": bool(self.gripper_limits), "gripper_raw": None if raw is None else round(raw, 4),
            }
            return ArmStatus(connected=connected, enabled=False, holding=True, estopped=self._estopped,
                             fault=fault, detail=detail)

    # ------------------------------------------------------------------ motion: all refused
    def _refuse(self, what: str) -> None:
        raise ReadOnlyArmError(f"{what}: this robot runs the TDM-118 read-only adapter (no motion until TDM-119)")

    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        self._refuse("command_joints")

    def command_gripper(self, gripper: float) -> None:
        self._refuse("command_gripper")

    def command_ee(self, pose: Any, gripper: float | None = None, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        self._refuse("command_ee")
        return ()

    def move_joints(self, target: Sequence[float], **kw: Any) -> bool:  # type: ignore[override]
        self._refuse("move_joints")
        return False

    def move_to(self, pose: Any, duration: float, **kw: Any) -> bool:
        self._refuse("move_to")
        return False

    def hold(self) -> None:
        """Nothing to hold: this adapter never commanded the motors."""

    def stop(self) -> None:
        with self._lock:
            self._estopped = True
            self.last_stop_at = time.monotonic()

    def clear_stop(self) -> None:
        with self._lock:
            self._estopped = False


__all__ = [
    "TDM118_REFRESH_ID",
    "TDM118_YAM_MOTORS",
    "ReadOnlyArmError",
    "Tdm118Feedback",
    "Tdm118ReadOnlyYamArm",
    "Tdm118SocketCanBus",
    "Tdm118TxGuard",
    "tdm118_parse_feedback",
    "tdm118_refresh_frame",
    "tdm118_vrteleop_active",
]
