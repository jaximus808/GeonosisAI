"""MujocoSimArm: the I2RT YAM in a physics-simulated pick-and-place scene ([SIM], no hardware).

``make_adapter("mujoco")`` / ``rdf-robot-client --adapter mujoco --cameras mujoco``. It implements
the full ``ArmAdapter`` interface and is wrapped by the TDM-45 ``SafetyLayer`` like the real arm
(``config/safety.yaml`` profile ``mujoco``), so policy chunks, teleop clutch deltas, homing and
e-stops all take the same path as on hardware.

What is real here and what is not:
  * the arm model is the official I2RT YAM v1 + linear_4310 MJCF (vendored from
    i2rt-robotics/i2rt@120c3c8, ``sim_assets/i2rt_yam``); kinematics are TDM-46's PoE model of the
    same geometry (matches MuJoCo's ``grasp_site`` to < 1e-6 m);
  * physics is real MuJoCo stepping in its own thread at 500 Hz (scene spec ``physics.timestep``), locked to
    the wall clock: joint position servos with the YAM's own PD gains and torque limits, gravity
    compensation, finger pads with friction. Grasping is PURE FRICTION, no weld/attach assist;
  * commands are streamed at 30 Hz and linearly interpolated at the physics rate (like ``YamArm``
    smoothing), so the PD sees a ramp;
  * cameras (``MujocoCameraSource``, CameraRig source ``mujoco``) are offscreen renders of the same
    model: ``top`` (exo/scene) and ``wrist`` (D405 mount pose), 640x480 at 30 fps, stamped "SIM";
  * everything is labelled: ``describe()["sim"] = True``, embodiment ``sim_yam``, so recordings are
    ``fixture: true`` and eval notes start with ``[SIM]``.

Scene helpers for sim-only flows (never used on the real arm; ``find_sim(arm)`` returns None there):
  * the scene comes from a scene spec (``mujoco_scene.load_scene``; ``scene=`` kwarg or ``$RDF_SIM_SCENE``,
    default ``sim_assets/scenes/cube_plate.json``); ``reset_scene(seed=None)`` re-places its randomized objects;
  * ``task_status()``: success (cube resting in the empty plate, released) / failure (missed grasp,
    dropped outside the plate, off the table) computed from sim state;
  * ``privileged_state()``: cube pose, finger contacts and task status for the scripted [SIM] policy
    (``sim_policy_server.py``), served as JSON on ``GET http://127.0.0.1:$RDF_SIM_STATE_PORT/sim/state``
    when that env is set (loopback only); ``scene_state()`` is the light TDM-96 ``/ws/state`` scene.

Offscreen rendering backend: MuJoCo picks it from ``MUJOCO_GL``. On macOS this module defaults it
to ``cgl`` (Apple CGL, works off the main thread, ~2 ms per 640x480 frame on an M-series laptop;
``glfw`` needs the main thread and hangs in a render thread). On Linux it defaults to ``egl``
(headless GPU), else set ``MUJOCO_GL=osmesa``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from .base import ArmAdapter, ArmError, ArmNotConnected, ArmState, ArmStatus, ArmStopped, IKError, JointLimits, Pose
from .mujoco_scene import ARM_JOINTS, FINGER_TRAVEL, SDK_REF, Scene, build_scene_xml, load_scene
from .yam_kinematics import YAM_JOINT_NAMES, YAM_V1_LIMITS, YAM_V1_LOWER, YAM_V1_UPPER, PoeKinematics

log = logging.getLogger("rdf_robot_client.mujoco_sim")

# Same ready pose as the real YAM (tool pitched 45 deg down at (0.30, 0, 0.20) m).
SIM_HOME = (0.0, 0.98, 0.93, -0.73, 0.0, 0.0)
# URDF ranges (the real adapter's limits). Velocity: the URDF's 1 rad/s is a placeholder; the sim
# uses 2 rad/s and the safety profile caps it lower anyway.
MUJOCO_LIMITS = JointLimits(lower=YAM_V1_LOWER, upper=YAM_V1_UPPER, max_velocity=(2.0, 2.0, 2.0, 3.0, 3.0, 3.0))

_ACTIVE: "MujocoSimArm | None" = None
_ACTIVE_LOCK = threading.Lock()


def _import_mujoco() -> Any:
    if "MUJOCO_GL" not in os.environ:
        os.environ["MUJOCO_GL"] = "cgl" if sys.platform == "darwin" else "egl"
    try:
        import mujoco
    except ImportError as e:  # pragma: no cover
        raise ArmError("the MuJoCo sim needs the `sim` extra: cd services/robot-client && uv sync --extra sim "
                       "(or `uv run --extra sim ...`)") from e
    return mujoco


def active() -> "MujocoSimArm | None":
    """The MuJoCo sim of this process (the camera sources render it)."""
    with _ACTIVE_LOCK:
        return _ACTIVE


def find_sim(arm: Any) -> "MujocoSimArm | None":
    """The MujocoSimArm under any wrappers (SafetyLayer), else None (real arm, SimArm, fake)."""
    seen = 0
    while arm is not None and seen < 8:
        if isinstance(arm, MujocoSimArm):
            return arm
        arm = getattr(arm, "inner", None)
        seen += 1
    return None


def _quat_yaw(q: Sequence[float]) -> float:
    w, x, y, z = q
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class MujocoSimArm(ArmAdapter):
    name = "mujoco"
    embodiment = "sim_yam"
    adapter_version = f"mujoco-yam-v1 ({SDK_REF} model)"
    dof = 6
    control_rate_hz = 30.0

    def __init__(self, *, scene: Scene | dict[str, Any] | str | None = None, home: Sequence[float] | None = None,
                 seed: int | None = None, state_port: int | None = None, realtime: bool = True,
                 label: bool = True) -> None:
        # the scene spec: kwarg, else $RDF_SIM_SCENE (a scene name under sim_assets/scenes or a JSON path)
        self.scene = scene if isinstance(scene, Scene) else load_scene(scene or os.environ.get("RDF_SIM_SCENE") or None)
        self.limits = MUJOCO_LIMITS
        self.home_joints = tuple(home) if home is not None else SIM_HOME
        self.kin = PoeKinematics("linear_4310", YAM_V1_LIMITS)  # IK over the URDF ranges, like YamArm
        self.realtime = realtime
        self.label = label
        self._rng = np.random.default_rng(seed)
        env_port = os.environ.get("RDF_SIM_STATE_PORT", "").strip()
        self.state_port = state_port if state_port is not None else (int(env_port) if env_port else None)
        mujoco = _import_mujoco()
        self._mj = mujoco
        self.model = mujoco.MjModel.from_xml_string(build_scene_xml(self.scene))
        self.data = mujoco.MjData(self.model)
        self._bind_model()
        self._lock = threading.RLock()
        self._connected = False
        self._holding = True
        self._estopped = False
        self._gripper_cmd = 1.0
        self._seg_from = np.array(self.home_joints, dtype=float)
        self._seg_to = self._seg_from.copy()
        self._seg_t0 = time.monotonic()
        self._seg_dur = 0.0
        self._last_cmd_t: float | None = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._steps = 0
        self._t_start = 0.0
        self._lag_resets = 0
        self.commands = 0
        self.ik_failures = 0
        self.last_stop_at: float | None = None
        self.scene_seq = 0
        self._episode = {"lifted": False, "miss_since": None, "blocked_since": None}
        self._snap: dict[str, Any] | None = None
        self._held_now = False
        self._renderer: SimRenderer | None = None
        self._state_srv: ThreadingHTTPServer | None = None
        self._init_state()

    def _bind_model(self) -> None:
        """Model indices for the current ``self.model`` / ``self.scene`` (``__init__`` and ``load_scene``)."""
        m = self.model
        self._qadr = np.array([m.joint(j).qposadr[0] for j in ARM_JOINTS])
        self._vadr = np.array([m.joint(j).dofadr[0] for j in ARM_JOINTS])
        self._fadr = int(m.joint("joint7").qposadr[0])
        self._fadr2 = int(m.joint("joint8").qposadr[0])
        sc = self.scene
        # every object body; free joints for the dynamic ones
        self._obj_body = {oid: int(m.body(oid).id) for oid in sc.objects}
        self._obj_free = {oid: (int(m.joint(f"{oid}_free").qposadr[0]), int(m.joint(f"{oid}_free").dofadr[0]))
                          for oid in sc.dynamic_objects()}
        self._target = sc.target  # the object the task moves (None: a scene without a success predicate)
        self._cube_body = self._obj_body[self._target] if self._target else -1
        self._cube_geom = int(m.geom(self._target).id) if self._target else -1
        self._cube_v = self._obj_free[self._target][1] if self._target in self._obj_free else 0
        self._pad_geoms = {int(m.geom("tip_left_pad").id), int(m.geom("tip_right_pad").id)}
        self._grasp_site = int(m.site("grasp_site").id)

    # ------------------------------------------------------------------ setup
    def _init_state(self) -> None:
        mj, m, d = self._mj, self.model, self.data
        mj.mj_resetData(m, d)
        d.qpos[self._qadr] = self.home_joints
        d.ctrl[: self.dof] = self.home_joints
        self._set_fingers(self._gripper_cmd)
        self._place_objects(None)
        mj.mj_forward(m, d)

    def _set_fingers(self, g: float) -> None:
        s = FINGER_TRAVEL * min(max(g, 0.0), 1.0)
        self.data.qpos[self._fadr] = s
        self.data.qpos[self._fadr2] = s
        self.data.ctrl[self.dof] = s

    def _place_objects(self, poses: dict[str, tuple[float, float, float]] | None) -> dict[str, list[float]]:
        """Every dynamic object back to its spec pose, or (``randomize``) a random pose in its region,
        resting on the table and clear of the other randomized objects. ``poses``: {id: (x, y, yaw)}."""
        sc, d = self.scene, self.data
        placed: dict[str, list[float]] = {}
        taken: list[tuple[float, float, float]] = []
        for oid in sc.dynamic_objects():
            o = sc.objects[oid]
            qa, va = self._obj_free[oid]
            pos, yaw = list(o["pose"]["pos"]), float(o["pose"]["yaw"])
            rz = o.get("randomize")
            if poses and oid in poses:
                x, y, yaw = poses[oid]
                pos = [x, y, sc.resting_z(oid)]
            elif rz:
                reg = sc.regions[rz["region"]]
                rad = sc.footprint_radius(oid)
                for _ in range(50):
                    x, y = float(self._rng.uniform(*reg["x"])), float(self._rng.uniform(*reg["y"]))
                    if all(math.hypot(x - tx, y - ty) > rad + tr + 0.01 for tx, ty, tr in taken):
                        break
                yaw = float(self._rng.uniform(*rz["yaw"])) if rz.get("yaw") else yaw
                pos = [x, y, sc.resting_z(oid)]
                taken.append((x, y, rad))
            d.qpos[qa:qa + 7] = [pos[0], pos[1], pos[2] + 1e-4, math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
            d.qvel[va:va + 6] = 0
            placed[oid] = [round(pos[0], 4), round(pos[1], 4), round(yaw, 4)]
        self._episode = {"lifted": False, "miss_since": None, "blocked_since": None}
        return placed

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        global _ACTIVE
        with self._lock:
            if self._connected:
                return
            self._connected = True
            self._holding = True
            q = self.data.qpos[self._qadr].copy()
            self._seg_from, self._seg_to, self._seg_dur = q.copy(), q.copy(), 0.0
            self._stop_evt.clear()
            self._t_start = time.monotonic()
            self._steps = 0
            self._thread = threading.Thread(target=self._physics_loop, name="mujoco-physics", daemon=True)
            self._thread.start()
        with _ACTIVE_LOCK:
            _ACTIVE = self
        if self.state_port is not None and self._state_srv is None:
            self._start_state_server(self.state_port)
        log.info("[SIM] MuJoCo YAM connected (physics %.0f Hz, GL %s)", 1 / self.scene.timestep, os.environ.get("MUJOCO_GL"))

    def disconnect(self) -> None:
        global _ACTIVE
        with self._lock:
            if not self._connected:
                return
            self._hold_locked()
            self._connected = False
            self._stop_evt.set()
            th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout=1.0)
        if self._renderer is not None:
            self._renderer.stop()
            self._renderer = None
        if self._state_srv is not None:
            self._state_srv.shutdown()
            self._state_srv = None
        with _ACTIVE_LOCK:
            if _ACTIVE is self:
                _ACTIVE = None

    # ------------------------------------------------------------------ physics thread
    def _setpoint(self, now: float) -> np.ndarray:
        if self._seg_dur <= 0:
            return self._seg_to
        a = min(max((now - self._seg_t0) / self._seg_dur, 0.0), 1.0)
        return self._seg_from + (self._seg_to - self._seg_from) * a

    def _physics_loop(self) -> None:
        mj, m, d = self._mj, self.model, self.data
        dt = float(m.opt.timestep)
        t0_wall = time.monotonic()
        t0_sim = float(d.time)
        while not self._stop_evt.is_set():
            now = time.monotonic()
            target = t0_sim + (now - t0_wall)
            with self._lock:
                m, d = self.model, self.data  # load_scene may have swapped them (TDM-104)
                # ONE mj_step call for all the steps due (nstep): mj_step releases the GIL, so a Python
                # loop of single steps under this lock would wait for the GIL between steps while holding
                # the lock (read_state / the 30 Hz state feed then stalled 100-300 ms under load).
                n = min(int(math.ceil((target - d.time) / dt - 1e-9)), 25) if d.time < target else 0
                if n > 0:
                    d.ctrl[: self.dof] = self._setpoint(now)
                    d.ctrl[self.dof] = FINGER_TRAVEL * self._gripper_cmd
                    mj.mj_step(m, d, nstep=n)
                self._steps += n
                if d.time < target - 0.1:  # fell > 100 ms behind (laptop stall): resync, don't race
                    t0_wall, t0_sim = time.monotonic(), float(d.time)
                    self._lag_resets += 1
                self._track_episode()
                self._publish()
            self._stop_evt.wait(max(0.0, dt - (time.monotonic() - now)) if self.realtime else 0.0)

    def step_for(self, seconds: float) -> None:
        """Advance physics without the thread (tests / offline calibration; arm not connected)."""
        mj, m, d = self._mj, self.model, self.data
        with self._lock:
            for _ in range(int(round(seconds / m.opt.timestep))):
                d.ctrl[: self.dof] = self._seg_to
                d.ctrl[self.dof] = FINGER_TRAVEL * self._gripper_cmd
                mj.mj_step(m, d)
                self._steps += 1
                self._track_episode()

    # ------------------------------------------------------------------ state
    def _require(self) -> None:
        if not self._connected:
            raise ArmNotConnected("MuJoCo sim arm is not connected")

    def _gripper_measured(self) -> float:
        return float(np.clip(self.data.qpos[self._fadr] / FINGER_TRAVEL, 0.0, 1.0))

    # ---- lock-free snapshot for readers. The physics thread publishes one immutable snapshot per tick;
    # read_state / task_status / scene_state read it WITHOUT the physics lock. (Holding the lock across
    # mj_step meant readers also waited out the GIL handoffs around it: 100-300 ms stalls of the 30 Hz
    # /ws/state feed and of the control loop while a teleop or policy thread was busy.)
    def _publish(self) -> None:
        d = self.data
        tv = float(np.linalg.norm(d.qvel[self._cube_v:self._cube_v + 3])) if self._target in self._obj_free else 0.0
        self._snap = {
            "model": self.model, "mono": time.monotonic(), "sim_t": float(d.time),
            "q": tuple(float(v) for v in d.qpos[self._qadr]), "qd": tuple(float(v) for v in d.qvel[self._vadr]),
            "g": self._gripper_measured(), "held": self._held_now, "target_v": tv,
            "obj": {oid: (tuple(float(v) for v in d.xpos[b]), tuple(float(v) for v in d.xquat[b]))
                    for oid, b in self._obj_body.items()},
        }

    def _fresh(self) -> dict[str, Any] | None:
        sn = self._snap
        if sn is None or not self._connected or sn["model"] is not self.model or time.monotonic() - sn["mono"] > 0.25:
            return None
        return sn

    def read_state(self) -> ArmState:
        sn = self._fresh()
        if sn is not None:
            q, qd, g, target = sn["q"], sn["qd"], sn["g"], self._seg_to
        else:
            with self._lock:
                q = tuple(float(v) for v in self.data.qpos[self._qadr])
                qd = tuple(float(v) for v in self.data.qvel[self._vadr])
                g = self._gripper_measured()
                target = self._seg_to.copy()
        moving = any(abs(v) > 0.02 for v in qd) or bool(np.max(np.abs(target - np.array(q))) > 0.01)
        return ArmState(t=time.monotonic(), joints=q, gripper=g, ee_pose=self.fk(q), joint_velocities=qd, moving=moving)

    @property
    def joint_names(self) -> list[str]:
        return list(YAM_JOINT_NAMES)

    def status(self) -> ArmStatus:
        with self._lock:
            el = time.monotonic() - self._t_start if self._t_start else 0.0
            detail = {"sim": True, "engine": "mujoco", "physics_hz": round(self._steps / el, 1) if el > 0 and self._connected else None,
                      "timestep_s": float(self.model.opt.timestep), "lag_resets": self._lag_resets,
                      "commands": self.commands, "ik_failures": self.ik_failures, "scene_seq": self.scene_seq,
                      "gl": os.environ.get("MUJOCO_GL")}
            return ArmStatus(connected=self._connected, enabled=self._connected, holding=self._holding,
                             estopped=self._estopped, fault=None, detail=detail)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "sim": True, "engine": "mujoco", "model": f"I2RT YAM v1 + linear_4310 ({SDK_REF})",
                "physics_hz": round(1 / self.scene.timestep), "scene": self.scene.describe(), "tool_point": "grasp_site", "kinematics": "poe",
                "grasp": "friction only (no weld assist)",
                "note": "[SIM] MuJoCo physics simulation of the YAM, NOT hardware"}

    # ------------------------------------------------------------------ kinematics
    def fk(self, joints: Sequence[float]) -> Pose:
        return Pose.from_matrix(self.kin.fk(joints).tolist())

    def ik(self, pose: Pose, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        if seed is None:
            seed = self.joint_positions() if self._connected else self.home_joints
        try:
            q = self.kin.ik(np.array(pose.matrix()), seed, extra_seeds=[self.home_joints])
        except IKError:
            self.ik_failures += 1
            raise
        return tuple(float(v) for v in q)

    # ------------------------------------------------------------------ motion
    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        if len(joints) != self.dof:
            raise ArmError(f"expected {self.dof} joints, got {len(joints)}")
        q = np.array([float(v) for v in joints])
        if not np.all(np.isfinite(q)):
            raise ArmError(f"non-finite joint command {list(joints)}")
        q = np.clip(q, self.limits.lower, self.limits.upper)
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            if gripper is not None:
                self._gripper_cmd = min(max(float(gripper), 0.0), 1.0)
            now = time.monotonic()
            start = self._setpoint(now) if not self._holding else self._seg_to
            dt = now - self._last_cmd_t if self._last_cmd_t is not None else 1.0 / self.control_rate_hz
            self._seg_from = np.array(start, dtype=float)
            self._seg_to = q
            self._seg_t0 = now
            self._seg_dur = min(max(dt, 0.004), 2.0 / self.control_rate_hz)
            self._last_cmd_t = now
            self._holding = False
            self.last_joint_cmd = tuple(float(v) for v in q)
            self.commands += 1

    def command_gripper(self, gripper: float) -> None:
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            self._gripper_cmd = min(max(float(gripper), 0.0), 1.0)

    def _hold_locked(self) -> None:
        q = self.data.qpos[self._qadr].copy()
        self._seg_from, self._seg_to = q.copy(), q.copy()
        self._seg_t0 = time.monotonic()
        self._seg_dur = 0.0
        self._last_cmd_t = None
        self._holding = True

    def hold(self) -> None:
        with self._lock:
            self._hold_locked()  # gripper keeps its last command (a grasped cube stays squeezed)

    def stop(self) -> None:
        self._estopped = True
        self.last_stop_at = time.monotonic()
        with self._lock:
            self._estopped = True
            self._hold_locked()

    def clear_stop(self) -> None:
        with self._lock:
            self._estopped = False
            self._hold_locked()

    # ------------------------------------------------------------------ scene (sim only)
    def load_scene(self, scene: Scene | dict[str, Any] | str) -> dict[str, Any]:
        """TDM-104 hot swap: rebuild the MuJoCo model from another scene spec while connected.

        The arm keeps its joint pose, gripper and command segment; objects go to their spec poses (randomized
        ones re-placed); the physics thread and the renderer pick up the new model on their next tick.
        Raises ``SceneError`` / ``rdf_common.scene_spec.SceneSpecError`` (old scene untouched) on a bad spec."""
        new = scene if isinstance(scene, Scene) else load_scene(scene)
        model = self._mj.MjModel.from_xml_string(build_scene_xml(new))  # compile outside the lock
        data = self._mj.MjData(model)
        with self._lock:
            q = self.data.qpos[self._qadr].copy()
            ctrl = self.data.ctrl[: self.dof].copy()
            t = float(self.data.time)
            self.scene, self.model, self.data = new, model, data
            self._bind_model()
            data.time = t
            data.qpos[self._qadr] = q
            data.ctrl[: self.dof] = ctrl
            self._set_fingers(self._gripper_cmd)
            placed = self._place_objects(None)
            self._mj.mj_forward(model, data)
            self.scene_seq += 1
            log.info("[SIM] scene loaded: %s (%d objects)", new.name, len(new.objects))
            return {"scene": new.name, "scene_seq": self.scene_seq, "objects": list(new.objects), "placed": placed}

    def reset_scene(self, seed: int | None = None, poses: dict[str, tuple[float, float, float]] | None = None) -> dict[str, Any]:
        """Randomized objects to new poses in their regions, the rest back to their spec poses (the
        'scene reset' a person does on the real arm)."""
        with self._lock:
            if seed is not None:
                self._rng = np.random.default_rng(seed)
            placed = self._place_objects(poses)
            self._mj.mj_forward(self.model, self.data)
            self.scene_seq += 1
            out: dict[str, Any] = {"scene_seq": self.scene_seq, "scene": self.scene.name, "objects": placed}
            if self._target in placed:  # back-compat: the cube's new pose
                x, y, yaw = placed[self._target]
                out["cube"] = {"x": x, "y": y, "yaw": yaw}
            return out

    def _cube_between_pads(self) -> bool:
        """Both finger pads touch the cube (a real pinch, not one pad brushing it)."""
        d = self.data
        touching: set[int] = set()
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = int(c.geom1), int(c.geom2)
            if g1 == self._cube_geom and g2 in self._pad_geoms:
                touching.add(g2)
            elif g2 == self._cube_geom and g1 in self._pad_geoms:
                touching.add(g1)
        return len(touching) == 2

    def _track_episode(self) -> None:
        """Per physics tick (lock held): remember 'was lifted' and how long the gripper has been
        closed on nothing near the table (the missed-grasp detector)."""
        if self._target is None:
            return
        d = self.data
        half = self.scene.half_height(self._target)
        cz = float(d.xpos[self._cube_body][2])
        if cz > self.scene.table_top + half + 0.03:
            self._episode["lifted"] = True
        g = self._gripper_measured()
        tool_z = float(d.site_xpos[self._grasp_site][2])
        missed = self._gripper_cmd < 0.3 and g < 0.12 and tool_z < self.scene.table_top + 0.09
        if missed and self._episode["miss_since"] is None:
            self._episode["miss_since"] = float(d.time)
        elif not missed:
            self._episode["miss_since"] = None
        # an open hand resting on the object (a pad landed on top of it instead of beside it)
        pads = self._pads_touching()
        self._held_now = len(pads) == 2
        blocked = g > 0.85 and tool_z < self.scene.table_top + half * 2 + 0.03 and bool(pads)
        if blocked and self._episode["blocked_since"] is None:
            self._episode["blocked_since"] = float(d.time)
        elif not blocked:
            self._episode["blocked_since"] = None

    def _pads_touching(self) -> set[int]:
        d = self.data
        touching: set[int] = set()
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = int(c.geom1), int(c.geom2)
            if g1 == self._cube_geom and g2 in self._pad_geoms:
                touching.add(g2)
            elif g2 == self._cube_geom and g1 in self._pad_geoms:
                touching.add(g1)
        return touching

    def _pad_on_cube(self) -> bool:
        d = self.data
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = int(c.geom1), int(c.geom2)
            if (g1 == self._cube_geom and g2 in self._pad_geoms) or (g2 == self._cube_geom and g1 in self._pad_geoms):
                return True
        return False

    def _live_poses(self) -> dict[str, dict[str, list[float]]]:
        sn = self._fresh()
        if sn is not None:
            return {oid: {"pos": list(p), "quat": list(q)} for oid, (p, q) in sn["obj"].items()}
        with self._lock:
            return {oid: {"pos": [float(v) for v in self.data.xpos[b]], "quat": [float(v) for v in self.data.xquat[b]]}
                    for oid, b in self._obj_body.items()}

    def _live_place(self, objs: dict[str, tuple[Any, Any]]) -> tuple[float, float, float]:
        """(x, y, top z) of where the object goes, from the target's LIVE pose (it may be a dynamic object)."""
        sc = self.scene
        c = sc.container
        if c is None:
            px, py = sc.place_xy()
            return px, py, sc.table_top
        p = objs[c][0]
        o = sc.objects[c]
        top = float(p[2]) + (float(o["height"]) if o["primitive"] == "plate" else sc.half_height(c))
        return float(p[0]), float(p[1]), top

    def _in_target(self, xyz: Sequence[float]) -> tuple[bool, float]:
        """The scene's success predicate (``object_in``): (object rests in the target, xy distance)."""
        sc = self.scene
        succ = sc.success
        margin = float(succ.get("margin", 0.012))
        half = sc.half_height(self._target)
        resting_z = sc.place_top_z() + half
        if sc.container is not None:
            px, py = sc.place_xy()
            dxy = math.hypot(xyz[0] - px, xyz[1] - py)
            inside = dxy < sc.place_radius() - margin
        else:
            reg = sc.regions[succ["region"]]
            px, py = sc.place_xy()
            dxy = math.hypot(xyz[0] - px, xyz[1] - py)
            inside = reg["x"][0] + margin <= xyz[0] <= reg["x"][1] - margin and \
                reg["y"][0] + margin <= xyz[1] <= reg["y"][1] - margin
        return bool(inside and abs(xyz[2] - resting_z) < 0.012), dxy

    def task_status(self) -> dict[str, Any]:
        """Success / failure of the scene's task (``success`` predicate), from sim state."""
        if self._target is None:
            return {"success": False, "failure": None, "held": False, "sim": True, "note": "scene has no success predicate"}
        sc, tid = self.scene, self._target
        dest = sc.container or sc.success.get("region")
        sn = self._fresh()
        if sn is not None and tid in sn["obj"]:
            c = np.array(sn["obj"][tid][0])
            held, g, t, cube_v = sn["held"], sn["g"], sn["sim_t"], sn["target_v"]
            ep = self._episode
            miss_since, blocked_since, lifted = ep["miss_since"], ep["blocked_since"], ep["lifted"]
        else:
            with self._lock:
                d = self.data
                c = d.xpos[self._cube_body].copy()
                held = self._cube_between_pads()
                g = self._gripper_measured()
                miss_since = self._episode["miss_since"]
                blocked_since = self._episode["blocked_since"]
                lifted = self._episode["lifted"]
                t = float(d.time)
                cube_v = float(np.linalg.norm(d.qvel[self._cube_v:self._cube_v + 3]))
        try:  # TDM-104's predicate (same code the orchestrator and the browser twin use), on live poses
            from rdf_common import scene_spec as _ss

            ev = _ss.evaluate_success(sc.spec, self._live_poses(), held={tid} if held else ())
            in_target, dxy = bool(ev["over"] and ev["resting"]), float(ev["dxy_m"])
            success = bool(ev["success"] and cube_v < 0.05)
        except ImportError:
            in_target, dxy = self._in_target(c)
            released = not held if sc.success.get("released", True) else True
            success = bool(in_target and released and cube_v < 0.05)
        half = sc.half_height(tid)
        failure = None
        if c[2] < sc.table_top - 0.05:
            failure = f"{tid} fell off the table"
        elif miss_since is not None and t - miss_since > 0.15:
            failure = "missed grasp: gripper closed on nothing"
        elif blocked_since is not None and t - blocked_since > 1.5:
            failure = f"missed grasp: fingers landed on top of the {tid}"
        elif lifted and not held and c[2] < sc.table_top + half + 0.005 and not in_target and cube_v < 0.05 and g > 0.5:
            failure = f"{tid} dropped outside the {dest}"
        return {"success": success, "failure": failure, "object": tid, "target": dest, "in_target": in_target,
                "cube_in_plate": in_target, "held": held, "cube_xyz": [round(float(v), 4) for v in c],
                "plate_dist_m": round(dxy, 4), "sim": True}

    def scene_state(self) -> dict[str, Any]:
        """TDM-96 ``/ws/state`` ``scene`` (30 Hz to the browser's robot view): object poses.

        Generic ``objects = {id: {pos, quat, primitive, size?, radius?, height?, color}}`` plus, for
        older viewers, ``cube`` (the task's object) and ``plate`` (its target)."""
        sc = self.scene
        sn = self._fresh()
        if sn is not None:
            poses = {oid: ([round(v, 4) for v in p], [round(v, 4) for v in q]) for oid, (p, q) in sn["obj"].items()}
        else:
            with self._lock:
                poses = {oid: ([round(float(v), 4) for v in self.data.xpos[b]], [round(float(v), 4) for v in self.data.xquat[b]])
                         for oid, b in self._obj_body.items()}
        objects: dict[str, Any] = {}
        for oid, (pos, quat) in poses.items():
            o = sc.objects[oid]
            geo = {k: o[k] for k in ("size", "radius", "height") if k in o}
            objects[oid] = {"pos": pos, "quat": quat, "primitive": o["primitive"], **geo, "color": o.get("color")}
        out: dict[str, Any] = {"objects": objects, "scene": sc.name, "sim": True}
        if self._target:
            pos, quat = poses[self._target]
            out["cube"] = {"pos": pos, "quat": quat, "size": sc.half_height(self._target) * 2}
        if sc.container:
            out["plate"] = {"pos": poses[sc.container][0], "radius": sc.place_radius()}
        return out

    def privileged_state(self) -> dict[str, Any]:
        """Privileged sim state for the scripted [SIM] policy and the demo (never sent as robot data).

        ``pick``: the task's object; ``place``: where it goes; ``objects``: every object's pose.
        (``cube`` / ``plate`` are the same two, kept for older readers.)"""
        sc = self.scene
        with self._lock:
            d = self.data
            q = d.qpos[self._qadr].copy()
            g = self._gripper_measured()
            seq = self.scene_seq
            objs = {oid: (d.xpos[b].copy(), d.xquat[b].copy()) for oid, b in self._obj_body.items()}
        st = self.task_status()
        objects = {oid: {"xyz": [float(v) for v in p], "quat": [float(v) for v in qq], "yaw": _quat_yaw(qq),
                         "primitive": sc.objects[oid]["primitive"], "half": sc.half_height(oid)}
                   for oid, (p, qq) in objs.items()}
        out: dict[str, Any] = {"sim": True, "scene": sc.name, "task_text": sc.task, "scene_seq": seq, "t": time.time(),
                               "objects": objects, "joints": [float(v) for v in q], "gripper": g, "task": st}
        if self._target:
            out["pick"] = {"id": self._target, **objects[self._target]}
            px, py, ptop = self._live_place(objs)
            out["place"] = {"id": sc.container or sc.success.get("region"), "xy": [px, py],
                            "radius": sc.place_radius(), "top_z": ptop}
            out["cube"], out["plate"] = out["pick"], out["place"]
        return out

    # ------------------------------------------------------------------ rendering (camera sources)
    def renderer(self) -> "SimRenderer":
        with self._lock:
            if self._renderer is None:
                self._renderer = SimRenderer(self)
            return self._renderer

    # ------------------------------------------------------------------ loopback state server
    def _start_state_server(self, port: int) -> None:
        sim = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] == "/sim/state":
                    self._send(200, sim.privileged_state())
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path.split("?")[0] == "/sim/reset":
                    self._send(200, sim.reset_scene())
                else:
                    self._send(404, {"error": "not found"})

        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, name="sim-state", daemon=True).start()
        self._state_srv = srv
        log.info("[SIM] scene state on http://127.0.0.1:%d/sim/state", srv.server_address[1])


class SimRenderer:
    """One offscreen GL context; renders every requested camera at its fps from a snapshot of the
    physics state (so rendering never blocks the physics thread for more than a data copy)."""

    def __init__(self, sim: MujocoSimArm) -> None:
        self.sim = sim
        self.cams: dict[str, tuple[int, int]] = {}
        self.fps = 30.0
        self._cv = threading.Condition()
        self._frames: dict[str, tuple[int, int, np.ndarray]] = {}  # name -> (seq, t_ns, img)
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.render_ms = 0.0
        self.error: str | None = None

    def add(self, name: str, width: int, height: int, fps: float) -> None:
        with self._cv:
            self.cams[name] = (int(width), int(height))
            self.fps = float(fps)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="mujoco-render", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait_frame(self, name: str, after_seq: int, timeout: float = 1.0) -> tuple[int, int, np.ndarray] | None:
        with self._cv:
            self._cv.wait_for(lambda: (name in self._frames and self._frames[name][0] > after_seq)
                              or self._stop.is_set() or self.error is not None, timeout)
            f = self._frames.get(name)
            return f if f is not None and f[0] > after_seq else None

    def _run(self) -> None:
        mj = self.sim._mj
        m = self.sim.model
        scratch = mj.MjData(m)
        renderers: dict[tuple[int, int], Any] = {}
        period = 1.0 / self.fps
        nxt = time.monotonic()
        try:
            while not self._stop.is_set():
                t_ns = time.monotonic_ns()
                with self.sim._lock:
                    if self.sim.model is not m:  # TDM-104: load_scene swapped the model
                        for r in renderers.values():
                            r.close()
                        renderers.clear()
                        m = self.sim.model
                        scratch = mj.MjData(m)
                    mj.mj_copyData(scratch, m, self.sim.data)
                t0 = time.perf_counter()
                out: dict[str, np.ndarray] = {}
                with self._cv:
                    cams = dict(self.cams)
                for name, (w, h) in cams.items():
                    r = renderers.get((w, h))
                    if r is None:
                        r = renderers[(w, h)] = mj.Renderer(m, h, w)
                    r.update_scene(scratch, camera=name)
                    img = np.ascontiguousarray(r.render()[:, :, ::-1])  # RGB -> BGR (camera convention)
                    if self.sim.label:
                        _stamp(img)
                    out[name] = img
                self.render_ms = (time.perf_counter() - t0) * 1000
                with self._cv:
                    self._seq += 1
                    for name, img in out.items():
                        self._frames[name] = (self._seq, t_ns, img)
                    self._cv.notify_all()
                nxt += period
                delay = nxt - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                else:
                    nxt = time.monotonic()
        except Exception as e:  # noqa: BLE001 - surface to the camera workers (they reopen)
            self.error = f"{type(e).__name__}: {e}"
            log.exception("[SIM] MuJoCo render thread died")
            with self._cv:
                self._cv.notify_all()
        finally:
            for r in renderers.values():
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass


def _stamp(img: np.ndarray) -> None:
    """Small 'SIM' tag bottom-left so a rendered frame is never mistaken for a real camera."""
    import cv2

    h = img.shape[0]
    cv2.rectangle(img, (6, h - 26), (118, h - 6), (20, 20, 20), -1)
    cv2.putText(img, "SIM MuJoCo", (10, h - 11), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 220, 255), 1, cv2.LINE_AA)


class MujocoCameraSource:
    """CameraRig FrameSource for ``source: mujoco``: frames rendered from the process's MuJoCo sim."""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.seq = 0
        self.r: SimRenderer | None = None

    def open(self) -> dict[str, Any]:
        sim = active()
        deadline = time.monotonic() + 10.0
        while sim is None and time.monotonic() < deadline:  # the arm connects after the rig starts
            time.sleep(0.05)
            sim = active()
        if sim is None:
            raise RuntimeError("camera source 'mujoco' needs a connected MuJoCo sim arm (--adapter mujoco)")
        self.r = sim.renderer()
        self.r.add(self.cfg.name, self.cfg.width, self.cfg.height, self.cfg.fps)
        return {"device": f"mujoco:{self.cfg.name}", "backend": f"mujoco-offscreen/{os.environ.get('MUJOCO_GL')}",
                "width": self.cfg.width, "height": self.cfg.height, "fps_negotiated": self.cfg.fps, "fourcc": "",
                "control_problems": [], "sim": True}

    def read(self) -> tuple[np.ndarray | None, int | None]:
        assert self.r is not None
        if self.r.error:
            raise RuntimeError(f"mujoco renderer: {self.r.error}")
        f = self.r.wait_frame(self.cfg.name, self.seq, timeout=1.0)
        if f is None:
            return None, None
        self.seq, t_ns, img = f
        return img.copy(), t_ns

    def close(self) -> None:
        self.r = None


def success_check(step: int, state: Any = None) -> bool:
    """``RDF_EVAL_SUCCESS_CHECK=rdf_robot_client.adapters.mujoco_sim:success_check`` (sim only)."""
    sim = active()
    return bool(sim is not None and sim.task_status()["success"])


__all__ = ["MUJOCO_LIMITS", "MujocoCameraSource", "MujocoSimArm", "SIM_HOME", "SimRenderer", "active", "find_sim",
           "success_check"]
