"""TDM-122: policy DRY RUN. Ask the pi0.5 policy for action chunks and check every action against
``config/safety.yaml``. NOTHING is executed: no arm adapter is connected and no command is sent anywhere.

Where the observations come from (``--source``):

  recorded:<episode dir>   one of our episodes (TDM-25 format: actions.parquet + exo.mp4 + wrist.mp4),
                           e.g. a real-arm teleop demo, or ``dataset_import`` output of a LeRobot dataset.
                           One observation every ``--every`` frames (default 10 = the rollout's re-query rate).
  stream:<base url>        the RUNNING robot client's TDM-55 stream server on the Jetson, read-only:
                           state from ``/ws/state``, images from ``/snapshot/<cam>.jpg``. A ``view``-scope
                           token is minted from RDF_STREAM_TOKEN_SECRET (``--robot-id``), or pass ``--token``.
                           The arm is not touched; the client keeps whatever it is doing (should be idle).
  rig[:<cameras.yaml>]     open the cameras directly (``CameraRig``; only while the robot client is NOT
                           holding them). State from ``--state`` (default the ready pose).

``--state fixed:ready|rest|q1,q2,q3,q4,q5,q6,g`` overrides the state for any source (the arm can stay off).

The policy (``--backend``, same switch as the rollout: ``POLICY_BACKEND``):
  production  the deployed TDM-41 server (POLICY_SERVER_URL / POLICY_SERVER_TOKEN). ``--checkpoint URI``
              is sent with every request, so the server refuses (``checkpoint_mismatch``) if another
              checkpoint is active. ``--activate`` makes the URI active first (a first load is ~44 s of GPU).
  spike       TDM-37 endpoint (base pi0.5 only).
  fake        a local [FAKE] server (``fake_policy_server``): zero cost, for trying the harness itself.

Per chunk it reports (all against the YAM profile, the same numbers ``SafetyLayer`` uses):
  * shape: horizon, action_dim (7 for a fine-tuned YAM checkpoint), action_space, finite values
  * static checks of the mapped targets (``rollout.map_actions``, the rollout's own mapping):
    joints outside LARP's hard joint limits (docs/real-arm-safety.md; independent of safety.yaml),
    joints outside the safe range (URDF limits tightened by the config, minus ``margin_rad``),
    first-step jump from the observed state vs ``max_jump_rad`` (the layer would REJECT it), per-step
    jumps, peak joint speed vs the speed cap at the live scale, tool point outside the workspace box
    or below ``table_floor_z + table_clearance_m`` (PoE FK), gripper outside [min, max]
  * every target's distance from the OBSERVED pose: any target more than ``max_jump_rad`` away is flagged
    (``targets_beyond_max_jump_from_state``), even when the chunk itself moves there in small steps
  * replay: the RAW chunk streamed at 30 Hz through the REAL ``SafetyLayer`` wrapped around a shadow arm
    (an in-memory YAM that only remembers its last setpoint), with a simulated clock. Its counters say
    exactly what the layer would have done: ``clamped_*`` (motion altered) or ``rejected_*`` (held).
    This is conservative: the real rollout first passes targets through ``ChunkExecutor``'s per-tick rate
    limit, which ramps from the measured pose, so a far chunk is approached slowly instead of rejected.
  * verdict per chunk: ``ok`` | ``clamp`` (the layer would alter it) | ``reject`` (it would refuse/hold)
And over the run: RTT p50/p95/max (warm-up calls excluded), server/GPU ms, verdict counts.

    uv run python -m rdf_robot_client.tdm122_dryrun --source recorded:<dir> --backend fake            # laptop, $0
    uv run python -m rdf_robot_client.tdm122_dryrun --source stream:http://127.0.0.1:8091 --robot-id <id> \\
        --backend production --checkpoint <uri> --prompt "<training task string>" --calls 30 --out dryrun.json

Exit code: 0 = no chunk would be rejected, 2 = at least one would, 1 = the run itself failed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .adapters.base import ArmAdapter, ArmState, ArmStatus, JointLimits, Pose
from .adapters.yam_kinematics import YAM_V1_LIMITS, PoeKinematics
from .policy_client import BASE_ALIASES, Observation, PolicyClient, PolicyConfig, PolicyReply, latency_summary
from .rollout import ExecConfig, map_actions
from .safety import COUNTERS, SafetyConfig, SafetyLayer, _RateLimiter, load_safety_config

YAM_DOF = 6
YAM_ACTION_DIM = YAM_DOF + 1  # 6 joints (rad) + gripper (0 closed .. 1 open), gripper LAST
YAM_READY = (0.0, 0.98, 0.93, -0.73, 0.0, 0.0)  # adapters/yam.py (kept here so this module never imports the SDK)
YAM_REST = (0.0,) * 6
IMAGE_SIZE = 224
# docs/real-arm-safety.md (LARP's i2rt fork on the northstar arms): the hard joint range, rad. i2rt clips at
# these +/- 0.15 and aborts at +/- 0.25. Every target must sit inside, independent of safety.yaml.
LARP_LOWER = (-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944)
LARP_UPPER = (3.05433, 3.65, 3.66519, 1.5708, 1.5708, 2.0944)
ROLE_TO_KEY = {"exo": "observation/image", "top": "observation/image", "wrist": "observation/wrist_image"}


# --------------------------------------------------------------------------- shadow arm (never hardware)


class ShadowYamArm(ArmAdapter):
    """An in-memory YAM: URDF limits + PoE FK, state = the last setpoint it was given. Moves nothing.

    ``name = "yam"`` on purpose, so ``SafetyLayer`` loads the ``yam`` profile and applies the same
    unverified-hardware speed cap it applies on the real arm.
    """

    name = "yam"
    dof = YAM_DOF
    control_rate_hz = 30.0

    def __init__(self, joints: Sequence[float] = YAM_READY, gripper: float = 1.0, gripper_type: str = "linear_4310",
                 limits: JointLimits = YAM_V1_LIMITS) -> None:
        self.limits = limits
        self.home_joints = tuple(YAM_READY)
        self.kin = PoeKinematics(gripper_type, limits)
        self.q = tuple(float(v) for v in joints)
        self.g = float(gripper)
        self.commands = 0

    def reset(self, joints: Sequence[float], gripper: float) -> None:
        self.q, self.g = tuple(float(v) for v in joints), float(gripper)

    def connect(self) -> None:  # nothing to connect to
        pass

    def disconnect(self) -> None:
        pass

    def read_state(self) -> ArmState:
        return ArmState(t=time.monotonic(), joints=self.q, gripper=self.g, ee_pose=self.fk(self.q))

    def status(self) -> ArmStatus:
        return ArmStatus(connected=True, enabled=True, detail={"shadow": True})

    def fk(self, joints: Sequence[float]) -> Pose:
        return Pose.from_matrix(self.kin.fk(joints).tolist())

    def ik(self, pose: Pose, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        raise NotImplementedError("the dry run checks joint-space chunks only")

    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        self.commands += 1
        self.q = tuple(float(v) for v in joints)
        if gripper is not None:
            self.g = float(gripper)

    def command_gripper(self, gripper: float) -> None:
        self.g = float(gripper)

    def hold(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def clear_stop(self) -> None:
        pass


# --------------------------------------------------------------------------- the envelope + chunk check


@dataclass(frozen=True)
class Envelope:
    """safety.yaml's YAM profile, resolved the way SafetyLayer resolves it."""

    lower: tuple[float, ...]
    upper: tuple[float, ...]
    vmax: tuple[float, ...]  # rad/s at the effective speed scale
    max_jump_rad: float
    speed_scale: float
    workspace_lo: tuple[float, float, float]
    workspace_hi: tuple[float, float, float]
    workspace_enabled: bool
    floor_z: float
    gripper_min: float
    gripper_max: float
    verified: bool
    profile: str

    @classmethod
    def from_layer(cls, layer: SafetyLayer) -> "Envelope":
        c: SafetyConfig = layer.config
        ws = c.workspace
        return cls(lower=layer._lower, upper=layer._upper, vmax=layer._vmax(), max_jump_rad=c.max_jump_rad,  # noqa: SLF001
                   speed_scale=layer.effective_scale, workspace_lo=tuple(ws.lo), workspace_hi=tuple(ws.hi),
                   workspace_enabled=ws.enabled, floor_z=float(_floor_z(c)), gripper_min=c.gripper_min,
                   gripper_max=c.gripper_max, verified=c.verified, profile=c.profile)

    def to_dict(self) -> dict[str, Any]:
        r = lambda v: [round(float(x), 4) for x in v]  # noqa: E731
        return {"profile": self.profile, "verified": self.verified, "speed_scale": self.speed_scale,
                "inside_larp_limits": bool(all(lo >= ll - 1e-9 for lo, ll in zip(self.lower, LARP_LOWER))
                                           and all(hi <= lu + 1e-9 for hi, lu in zip(self.upper, LARP_UPPER))),
                "joint_lower": r(self.lower), "joint_upper": r(self.upper), "vmax_rad_s": r(self.vmax),
                "max_jump_rad": self.max_jump_rad, "workspace": {"lo": r(self.workspace_lo), "hi": r(self.workspace_hi),
                                                                 "enabled": self.workspace_enabled},
                "tool_floor_z": round(self.floor_z, 4), "gripper": [self.gripper_min, self.gripper_max]}


def _floor_z(c: SafetyConfig) -> float:
    """``table_floor_z + table_clearance_m`` (the lowest the tool point may go), from the raw profile."""
    import yaml

    raw = yaml.safe_load(Path(c.source).read_text()) if c.source and Path(c.source).exists() else {}
    ws = ((raw.get("profiles") or {}).get(c.profile) or {}).get("workspace") or {}
    return float(ws.get("table_floor_z", 0.0)) + float(ws.get("table_clearance_m", 0.0))


class ChunkChecker:
    """Checks one policy chunk against the envelope, then replays it through the real SafetyLayer."""

    def __init__(self, *, safety_config: str | None = None, profile: str = "yam", gripper_type: str = "linear_4310",
                 exec_config: ExecConfig | None = None) -> None:
        self.cfg = exec_config or ExecConfig()
        self.arm = ShadowYamArm(gripper_type=gripper_type)
        self._t = 0.0
        self.layer = SafetyLayer(self.arm, load_safety_config(profile, safety_config), clock=lambda: self._t,
                                 start_watchdog=False)
        self.layer._log_rl = _RateLimiter(3600.0)  # noqa: SLF001 - one line per clamp kind; the counts are the report
        self.env = Envelope.from_layer(self.layer)
        self.vmax_arm = np.asarray(self.layer._vmax(), dtype=float)  # noqa: SLF001 - the rollout uses the arm's cap too

    def tool_xyz(self, q: Sequence[float]) -> tuple[float, float, float]:
        m = self.arm.kin.fk(q)
        return float(m[0, 3]), float(m[1, 3]), float(m[2, 3])

    def check(self, actions: np.ndarray | None, action_space: str | None, state: Sequence[float],
              replay_steps: int | None = None) -> dict[str, Any]:
        env, dof = self.env, YAM_DOF
        st = np.asarray(state, dtype=float).reshape(-1)
        out: dict[str, Any] = {"action_space": action_space, "problems": []}
        if actions is None:
            out.update(verdict="reject", problems=["no actions"])
            return out
        a = np.asarray(actions, dtype=float)
        out["shape"] = list(a.shape)
        if a.ndim != 2 or a.shape[0] == 0:
            out.update(verdict="reject", problems=[f"bad shape {list(a.shape)}"])
            return out
        if not np.all(np.isfinite(a)):
            out.update(verdict="reject", problems=["non-finite values"])
            return out
        if action_space == "joint_pos_abs" and a.shape[1] != YAM_ACTION_DIM:
            out["problems"].append(f"action_dim {a.shape[1]} != {YAM_ACTION_DIM} (6 joints + gripper): not a YAM checkpoint")
        if action_space != "joint_pos_abs":
            out["problems"].append(f"action_space {action_space!r}: base pi0.5 / DROID layout, MEANINGLESS on the YAM "
                                   "(interpreted as joint velocities by rollout.map_actions)")
        tg = map_actions(a, action_space, np.concatenate([st[:dof], st[dof:dof + 1] if len(st) > dof else [1.0]]),
                         self.cfg, self.vmax_arm)
        q = tg[:, :dof]
        lo, hi = np.asarray(env.lower), np.asarray(env.upper)
        over = np.maximum(lo - q, 0.0) + np.maximum(q - hi, 0.0)  # rad outside the safe range, per target/joint
        larp_over = np.maximum(np.asarray(LARP_LOWER) - q, 0.0) + np.maximum(q - np.asarray(LARP_UPPER), 0.0)
        first_jump = float(np.max(np.abs(q[0] - st[:dof])))
        from_state = np.max(np.abs(q - st[None, :dof]), axis=1)  # each target's distance from the observed pose
        steps = np.abs(np.diff(np.vstack([st[None, :dof], q]), axis=0))
        speed = steps * (1.0 / self.cfg.period_s)
        xyz = np.array([self.tool_xyz(row) for row in q])
        ws_lo, ws_hi = np.asarray(env.workspace_lo), np.asarray(env.workspace_hi)
        ws_out = (np.maximum(ws_lo - xyz, 0.0) + np.maximum(xyz - ws_hi, 0.0)).sum(axis=1) if env.workspace_enabled \
            else np.zeros(len(xyz))
        below = np.maximum(env.floor_z - xyz[:, 2], 0.0)
        g = tg[:, dof]
        g_raw = a[:, dof] if a.shape[1] > dof and action_space == "joint_pos_abs" else g
        out["static"] = {
            "targets_outside_joint_range": int(np.count_nonzero(over.max(axis=1) > 1e-9)),
            "worst_joint_over_rad": round(float(over.max()), 4),
            "worst_joint": int(over.max(axis=0).argmax()) + 1 if over.max() > 0 else None,
            "targets_outside_larp_limits": int(np.count_nonzero(larp_over.max(axis=1) > 1e-9)),
            "worst_larp_over_rad": round(float(larp_over.max()), 4),
            "first_step_jump_rad": round(first_jump, 4),
            "targets_beyond_max_jump_from_state": int(np.count_nonzero(from_state > env.max_jump_rad)),
            "max_from_state_rad": round(float(from_state.max()), 4),
            "max_step_rad": round(float(steps.max()), 4),
            "peak_speed_rad_s": [round(float(v), 3) for v in speed.max(axis=0)],
            "speed_over_cap": bool(np.any(speed.max(axis=0) > np.asarray(env.vmax) * 1.001 + 1e-6)),
            "targets_outside_workspace": int(np.count_nonzero(ws_out > 1e-9)),
            "worst_workspace_m": round(float(ws_out.max()), 4),
            "targets_below_floor": int(np.count_nonzero(below > 1e-9)),
            "tool_z_min_m": round(float(xyz[:, 2].min()), 4),
            "gripper_raw_range": [round(float(g_raw.min()), 4), round(float(g_raw.max()), 4)],
            "gripper_outside_range": int(np.count_nonzero((g_raw < env.gripper_min - 1e-9) | (g_raw > env.gripper_max + 1e-9))),
        }
        if first_jump > env.max_jump_rad:
            out["problems"].append(f"first target is {first_jump:.3f} rad from the observed state (> max_jump_rad "
                                   f"{env.max_jump_rad}): the safety layer would REJECT it")
        n_larp = out["static"]["targets_outside_larp_limits"]
        if n_larp:
            out["problems"].append(f"{n_larp}/{len(q)} targets outside LARP's joint limits (worst "
                                   f"{out['static']['worst_larp_over_rad']} rad over; docs/real-arm-safety.md)")
        n_far = out["static"]["targets_beyond_max_jump_from_state"]
        if n_far:
            out["problems"].append(f"{n_far}/{len(q)} targets are more than max_jump_rad {env.max_jump_rad} from the "
                                   f"observed pose (max {out['static']['max_from_state_rad']} rad). Streamed raw, the "
                                   "layer rejects the step; the rollout's ChunkExecutor ramps toward them instead "
                                   "(from the measured pose, <= min(0.5 x live cap, 0.3 rad/s), 15-tick soft start).")
        out["replay"] = self.replay(tg, st, replay_steps)
        rej = {k: v for k, v in out["replay"].items() if k.startswith("rejected") and v}
        clamps = {k: v for k, v in out["replay"].items() if k.startswith("clamped") and v}
        if rej or out["replay"].get("violation"):
            out["verdict"] = "reject"
        elif clamps or out["static"]["gripper_outside_range"] or n_far or n_larp:
            out["verdict"] = "clamp"
        else:
            out["verdict"] = "ok"
        if action_space != "joint_pos_abs" or (a.shape[1] != YAM_ACTION_DIM):
            out["verdict"] = "reject" if out["verdict"] == "reject" else "unmapped"
        return out

    def replay(self, targets: np.ndarray, state: np.ndarray, steps: int | None = None) -> dict[str, Any]:
        """Stream ``targets`` at 30 Hz through the real SafetyLayer around the shadow arm (fresh each chunk)."""
        from .safety import SafetyViolation

        dof = YAM_DOF
        self.arm.reset(state[:dof], float(state[dof]) if len(state) > dof else 1.0)
        self.layer._reset_ref()  # noqa: SLF001 - re-anchor on the observed state, as after hold()
        self.layer._consecutive_rejects = 0  # noqa: SLF001
        before = {k: self.layer.stats()[k] for k in COUNTERS}
        n = len(targets) if steps is None else min(steps, len(targets))
        violation = None
        for i in range(n):
            self._t += self.cfg.period_s
            try:
                self.layer.command_joints(tuple(float(v) for v in targets[i, :dof]),
                                          float(np.clip(targets[i, dof], 0.0, 1.0)))
            except SafetyViolation as e:
                violation = str(e)[:200]
                break
        after = self.layer.stats()
        diff: dict[str, Any] = {k: int(after[k]) - int(before[k]) for k in COUNTERS if after[k] != before[k]}
        diff["steps"] = n
        q_end = np.asarray(self.arm.q)
        diff["lag_at_end_rad"] = round(float(np.max(np.abs(q_end - targets[n - 1, :dof]))), 4) if n else 0.0
        if violation:
            diff["violation"] = violation
        return diff


# --------------------------------------------------------------------------- observation sources


@dataclass
class Sample:
    state: list[float]  # 6 joints (rad) + gripper (0..1), gripper last
    images: list[np.ndarray]  # [base/top, wrist], 224x224 RGB uint8
    label: str
    t: float = field(default_factory=time.monotonic)
    meta: dict[str, Any] = field(default_factory=dict)


def parse_state(spec: str | None) -> list[float] | None:
    if not spec:
        return None
    v = spec.split(":", 1)[1] if spec.startswith("fixed:") else spec
    if v == "ready":
        return [*YAM_READY, 1.0]
    if v == "rest":
        return [*YAM_REST, 1.0]
    vals = [float(x) for x in v.split(",") if x.strip()]
    if len(vals) != YAM_ACTION_DIM:
        raise SystemExit(f"--state needs 7 values (6 joints rad + gripper 0..1), got {len(vals)}")
    return vals


def _letterbox(rgb: np.ndarray) -> np.ndarray:
    from .cameras import resize_with_pad

    return resize_with_pad(rgb, IMAGE_SIZE)


def recorded_samples(episode_dir: str | Path, every: int = 10, start: int = 0, limit: int | None = None
                     ) -> Iterator[Sample]:
    """Our episode format: state from actions.parquet (joint_pos + gripper_pos), frames from exo/wrist mp4."""
    import cv2

    from rdf_common.episode_format import read_actions

    d = Path(episode_dir)
    act = read_actions(d / "actions.parquet")
    jp = np.asarray(act.joint_pos, dtype=float)
    gp = np.asarray(act.extra["gripper_pos"], dtype=float).reshape(-1)
    caps = {}
    for role in ("exo", "wrist"):
        p = d / f"{role}.mp4"
        if not p.exists():
            raise SystemExit(f"{d}: {role}.mp4 missing (need exo + wrist)")
        caps[role] = cv2.VideoCapture(str(p))
    frames: dict[str, np.ndarray | None] = {"exo": None, "wrist": None}
    pos = -1
    n = 0
    task = ((act.meta.get("source") or {}).get("task")) or act.meta.get("task")
    try:
        for i in range(start, act.n, max(1, every)):
            while pos < i:  # decode sequentially (cheap, and seeking mp4s is not frame-exact)
                for role, cap in caps.items():
                    ok, f = cap.read()
                    if ok:
                        frames[role] = f
                pos += 1
            if frames["exo"] is None or frames["wrist"] is None:
                break
            imgs = [_letterbox(cv2.cvtColor(frames[r], cv2.COLOR_BGR2RGB)) for r in ("exo", "wrist")]
            yield Sample(state=[*map(float, jp[i, :YAM_DOF]), float(gp[i])], images=imgs, label=f"frame {i}",
                         meta={"frame": i, "task": task, "native_hw": list(frames["exo"].shape[:2])})
            n += 1
            if limit is not None and n >= limit:
                return
    finally:
        for cap in caps.values():
            cap.release()


class StreamSource:
    """Read-only view of the running robot client: ``/ws/state`` + ``/snapshot/<cam>.jpg`` (TDM-55/96)."""

    def __init__(self, base_url: str, token: str, cams: tuple[str, str] = ("top", "wrist")) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.cams = cams
        self._ws = None

    def _state(self) -> dict[str, Any]:
        from websockets.sync.client import connect

        if self._ws is None:
            url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/ws/state"
            self._ws = connect(url, open_timeout=10, max_size=2**22).__enter__()
            self._ws.send(json.dumps({"type": "auth", "token": self.token}))
        end = time.monotonic() + 5.0
        while time.monotonic() < end:
            m = json.loads(self._ws.recv(timeout=5.0))
            if m.get("t") == "state":
                return m
            if m.get("t") == "error" or m.get("type") == "error":
                raise RuntimeError(f"/ws/state refused: {m}")
        raise TimeoutError("no state message from /ws/state within 5 s")

    def _snapshot(self, cam: str) -> np.ndarray:
        import cv2

        with urllib.request.urlopen(f"{self.base}/snapshot/{cam}.jpg?token={self.token}", timeout=10) as r:
            buf = np.frombuffer(r.read(), np.uint8)
        bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if bgr is None:
            raise RuntimeError(f"snapshot {cam}: not a JPEG")
        return bgr

    def sample(self) -> Sample:
        import cv2

        s = self._state()
        raw = [self._snapshot(c) for c in self.cams]
        imgs = [_letterbox(cv2.cvtColor(b, cv2.COLOR_BGR2RGB)) for b in raw]
        joints = [float(v) for v in (s.get("joints") or [])][:YAM_DOF]
        if len(joints) != YAM_DOF:
            raise RuntimeError(f"/ws/state sent {len(s.get('joints') or [])} joints, want 6")
        return Sample(state=[*joints, float(s.get("gripper", 1.0))], images=imgs, label=f"live t_ms={s.get('t_ms')}",
                      meta={"robot_state": s.get("robot_state"), "control_source": s.get("control_source"),
                            "estop": s.get("estop"), "sim": s.get("sim"), "snapshot_hw": list(raw[0].shape[:2])})

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


def mint_view_token(robot_id: int, robot_name: str = "") -> str:
    from rdf_common.stream_token import load_secret, mint

    tok, _ = mint(load_secret(), robot_id=robot_id, robot_name=robot_name, sub="tdm122-dryrun", ttl_s=60, scope="view")
    return tok


def rig_samples(config: str | None, state: list[float]) -> Iterator[Sample]:
    from .cameras import CameraRig

    rig = CameraRig.from_config(config) if config else CameraRig.from_config()
    rig.start()
    try:
        rig.wait_ready(5.0)
        while True:
            s = rig.latest_synced()
            if s is None:
                time.sleep(0.05)
                continue
            imgs = s.policy_images(IMAGE_SIZE)
            yield Sample(state=list(state), images=[imgs["observation/image"], imgs["observation/wrist_image"]],
                         label=f"rig seq {s.seq}", meta={"skew_ms": round(s.skew_ms, 1)})
    finally:
        rig.stop()


# --------------------------------------------------------------------------- the run


def mapping_preflight(health: dict[str, Any], checkpoint: str | None) -> dict[str, Any]:
    """What the server says it serves vs what the YAM needs (7-dim joint_pos_abs, 6 joints + gripper)."""
    act = health.get("active") or {}
    spec = act.get("spec") or {}
    out = {"active_checkpoint_uri": health.get("active_checkpoint_uri"), "spec": spec, "problems": []}
    if checkpoint and checkpoint not in BASE_ALIASES and health.get("active_checkpoint_uri") \
            and health["active_checkpoint_uri"].rstrip("/") != checkpoint.rstrip("/").removesuffix("/params"):
        out["problems"].append(f"active checkpoint is {health.get('active_checkpoint_uri')}, not {checkpoint} "
                               "(requests will fail with checkpoint_mismatch; pass --activate)")
    if spec:
        if spec.get("action_space") != "joint_pos_abs":
            out["problems"].append(f"action_space {spec.get('action_space')!r}: base pi0.5 (DROID layout); its actions "
                                   "are meaningless on the YAM. Only a fine-tuned YAM checkpoint gives joint targets.")
        else:
            for k, want in (("state_dim", 7), ("action_dim", 7), ("joint_dims", 6)):
                if spec.get(k) != want:
                    out["problems"].append(f"{k}={spec.get(k)} (YAM needs {want})")
    out["yam_ok"] = not out["problems"] and spec.get("action_space") == "joint_pos_abs"
    return out


def run(samples: Iterator[Sample] | Any, client: Any, checker: ChunkChecker, *, prompt: str | None,
        checkpoint_uri: str | None, calls: int, warmup: int = 1, warmup_timeout_s: float = 120.0,
        timeout_s: float = 10.0, interval_s: float = 0.0, log: Any = None) -> dict[str, Any]:
    """Pull ``calls`` samples, query the policy for each, check each chunk. Executes nothing."""
    rows: list[dict[str, Any]] = []
    replies: list[PolicyReply] = []
    it = iter(samples) if not hasattr(samples, "sample") else None
    for i in range(calls + warmup):
        s = next(it, None) if it is not None else samples.sample()
        if s is None:
            break
        p = prompt or s.meta.get("task") or "do the task"
        obs = Observation(images=s.images, state=s.state, prompt=p, t_obs=time.monotonic(), step=i,
                          checkpoint_uri=checkpoint_uri, timeout_s=warmup_timeout_s if i < warmup else timeout_s)
        r = client.infer_blocking(obs, timeout=(warmup_timeout_s if i < warmup else timeout_s) + 5)
        row: dict[str, Any] = {"i": i, "warmup": i < warmup, "label": s.label, "state": [round(v, 4) for v in s.state],
                               "prompt": p, "rtt_ms": round(r.rtt_ms, 1), "timing": r.timing, **{"meta": s.meta}}
        if r.ok:
            row["check"] = checker.check(r.actions, r.action_space, s.state)
        else:
            row["error"] = {"code": r.code, "error": r.error}
        if i >= warmup:
            replies.append(r)
        rows.append(row)
        if log is not None:
            log.write(json.dumps(row, default=str) + "\n")
            log.flush()
        if interval_s:
            time.sleep(interval_s)
    measured = [r for r in rows if not r["warmup"]]
    verdicts: dict[str, int] = {}
    for r in measured:
        v = r["check"]["verdict"] if "check" in r else "error"
        verdicts[v] = verdicts.get(v, 0) + 1
    lat = latency_summary(replies)
    srv = [float(r.timing.get("server_ms")) for r in replies if r.ok and "server_ms" in r.timing]
    if srv:
        lat["server_p50_ms"] = round(float(np.percentile(srv, 50)), 1)
    if replies and any(r.ok for r in replies):
        ok = [r.rtt_ms for r in replies if r.ok]
        lat["rtt_p50_under_200ms"] = bool(np.percentile(ok, 50) < 200)
    return {"calls": len(measured), "warmup_calls": len(rows) - len(measured), "verdicts": verdicts,
            "latency": lat, "envelope": checker.env.to_dict(), "rows": rows}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_robot_client.tdm122_dryrun", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, help="recorded:<episode dir> | stream:<base url> | rig[:<cameras.yaml>]")
    ap.add_argument("--state", default=None, help="fixed:ready | fixed:rest | fixed:q1,..,q6,g (overrides the source's state)")
    ap.add_argument("--backend", choices=["production", "spike", "sim", "fake"], default=None,
                    help="default $POLICY_BACKEND (or spike)")
    ap.add_argument("--url", default=None, help="override the policy URL")
    ap.add_argument("--transport", choices=["ws", "http"], default=None)
    ap.add_argument("--checkpoint", default=None, help="checkpoint URI sent with every request (production)")
    ap.add_argument("--activate", action="store_true", help="POST /policy/activate --checkpoint first (GPU load ~44 s if uncached)")
    ap.add_argument("--prompt", default=None, help="must be the training task string (default: the episode's task)")
    ap.add_argument("--calls", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=1, help="first N calls not counted in latency (cold start / JIT)")
    ap.add_argument("--every", type=int, default=10, help="recorded: one observation every N frames")
    ap.add_argument("--start", type=int, default=0, help="recorded: first frame")
    ap.add_argument("--interval", type=float, default=0.0, help="seconds between calls (live sources)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--robot-id", type=int, default=0, help="stream: robot id for the minted view token")
    ap.add_argument("--robot-name", default="")
    ap.add_argument("--token", default=None, help="stream: a stream token instead of minting one")
    ap.add_argument("--safety-config", default=None, help="default config/safety.yaml ($RDF_SAFETY_CONFIG)")
    ap.add_argument("--profile", default="yam")
    ap.add_argument("--gripper", default=os.environ.get("YAM_GRIPPER", "linear_4310"))
    ap.add_argument("--out", default=None, help="write the full JSON report here")
    ap.add_argument("--log", default=None, help="append one JSON line per call here as it happens")
    a = ap.parse_args(argv)

    checker = ChunkChecker(safety_config=a.safety_config or os.environ.get("RDF_SAFETY_CONFIG"), profile=a.profile,
                           gripper_type=a.gripper)
    fixed = parse_state(a.state)
    fake = None
    source: Any
    if a.source.startswith("recorded:"):
        gen = recorded_samples(a.source.split(":", 1)[1], every=a.every, start=a.start)
        source = (Sample(fixed, s.images, s.label, s.t, s.meta) if fixed else s for s in gen)
    elif a.source.startswith("stream:"):
        base = a.source.split(":", 1)[1]
        tok = a.token or mint_view_token(a.robot_id, a.robot_name)
        ss = StreamSource(base, tok)

        class _S:
            def sample(self) -> Sample:
                s = ss.sample()
                return Sample(fixed, s.images, s.label, s.t, s.meta) if fixed else s
        source = _S()
    elif a.source.startswith("rig"):
        cfg = a.source.split(":", 1)[1] if ":" in a.source else None
        source = rig_samples(cfg, fixed or [*YAM_READY, 1.0])
    else:
        ap.error("--source must start with recorded:, stream: or rig")

    if a.backend == "fake":
        from .fake_policy_server import FakePolicyServer, jitter_schedule

        fake = FakePolicyServer(schedule=jitter_schedule(160, 30)).start()
        pcfg = PolicyConfig(backend="spike", url=fake.url)
    else:
        pcfg = PolicyConfig.from_env(a.backend, url=a.url, transport=a.transport)
    report: dict[str, Any] = {"tdm": "TDM-122 dry run (nothing executed)", "policy": pcfg.describe(),
                              "source": a.source, "state_override": fixed, "checkpoint": a.checkpoint,
                              "fake": a.backend == "fake"}
    rc = 1
    try:
        with PolicyClient(pcfg) as pc:
            try:
                h = pc.wait_healthy(timeout=600 if a.activate else 180)
                report["health"] = {k: h.get(k) for k in ("ready", "active_checkpoint_uri", "gpu", "startup_s", "fake")}
                if a.activate and a.checkpoint:
                    report["activate"] = pc.activate(a.checkpoint)
                    h = pc.health()
                report["mapping"] = mapping_preflight(h, a.checkpoint)
            except Exception as e:  # noqa: BLE001
                report["health_error"] = f"{type(e).__name__}: {e}"[:300]
                raise
            log = open(a.log, "a") if a.log else None  # noqa: SIM115
            try:
                report.update(run(source, pc, checker, prompt=a.prompt,
                                  checkpoint_uri=a.checkpoint if pcfg.hot_swap else None, calls=a.calls,
                                  warmup=a.warmup, timeout_s=a.timeout, interval_s=a.interval, log=log))
            finally:
                if log:
                    log.close()
        rc = 2 if report.get("verdicts", {}).get("reject") else 0
    finally:
        if fake is not None:
            fake.stop()
        if hasattr(source, "close"):
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
        brief = {k: report.get(k) for k in ("calls", "verdicts", "latency", "mapping", "health", "health_error")}
        print(json.dumps(brief, indent=1, default=str))
        if a.out:
            Path(a.out).write_text(json.dumps(report, indent=1, default=str))
            print(f"full report: {a.out}", file=sys.stderr)
    return rc


__all__ = ["ChunkChecker", "Envelope", "Sample", "ShadowYamArm", "StreamSource", "mapping_preflight", "parse_state",
           "recorded_samples", "run"]

if __name__ == "__main__":
    raise SystemExit(_main())
