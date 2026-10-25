"""TDM-50: policy rollout. Real-time 30 Hz executor of remote pi0.5 action chunks.

Scheme (S-05b decision, docs/WORKER_GUIDE.md "Policy inference"):
  * The control loop runs at the arm's rate (30 Hz) inside ``JobContext.run_control_loop``, so
    every target goes through ``ctx.arm`` (the TDM-45 safety-wrapped adapter) and is recorded as a
    ``control_source='policy'`` ControlTick (TDM-48 recorder listens with ``add_tick_listener``).
  * Each policy call returns a 50-step chunk. Every 10 steps the loop samples an observation
    (2 x 224 px images from ``SyncedFrames.policy_images()`` + joints/gripper + prompt) and
    submits it to the async websocket client (``policy_client.PolicyClient``) while the current
    chunk keeps executing.
  * When a chunk arrives with latency L (observation sample -> first use) the first
    ``round(L / 33 ms)`` actions are skipped (they are in the past) and the switch is crossfaded
    over 3 steps from the old trajectory to the new one.
  * If no fresh chunk has arrived by index 40 of the current chunk, the arm HOLDS its last target
    (and the episode is flagged). The same hold covers a network loss, a dropped reply
    (no answer within ``request_timeout_s``) and a server error. Errors are retried right away;
    after more than ``max_retries`` (3) consecutive failures the episode ends with
    ``ended='policy_error'``, the arm held.
  * A pause (pause_for_takeover, waiting_reset, link loss) holds the arm in run_control_loop; the
    executor notices the time gap on resume, drops the stale chunk and re-queries.
  * ``mode='sync'`` is the S-05b v0 fallback: execute 25 steps, then hold while one query is answered.
  * TDM-99: while an episode runs, ``ctx.services["chunk_preview"]`` is ``ChunkExecutor.preview``
    (the remaining targets of the chunk being executed, downsampled to 10 rows of joints + gripper),
    which the ``/ws/state`` feed (stream.LiveStateSampler) publishes for the HUD ghost. Removed at the end.

Public API (TDM-51's trial runner builds on this)::

    from rdf_robot_client.rollout import run_episode
    result = run_episode(ctx, prompt, max_steps=900, success_check=lambda step, st: ...,
                         timeout_s=None, checkpoint_uri=None)
    result.ended   # success | max_steps | max_seconds | policy_error | finish_requested
    result.to_dict()  # timing (achieved Hz, period jitter), stitching stats, latency p50/p95, holds

    PolicyRollout(ctx).run_episode(prompt, max_steps, success_check)   # same, bound to ctx

Job handlers registered here (``@job_handler``):
  load_checkpoint  wait for /health, then activate the checkpoint (production: POST /policy/activate;
                   spike: base only) and run one warm-up inference.
  run_baseline     ONE policy episode: payload {prompt?, max_steps?, timeout_s?, checkpoint_uri?, mode?}.
                   Interim until TDM-51 registers its trial runner for run_baseline / run_eval.

Action mapping (``action_space`` from the server):
  joint_pos_abs  (TDM-41 fine-tuned YAM checkpoints) absolute 6 joints + gripper, used as is.
  droid_base / spike (base pi0.5, DROID layout, 8-d) MEANINGLESS on the YAM: interpreted as joint
                 velocities (first 6 dims, scaled by ``base_velocity_scale``) integrated from the
                 observed joints, gripper = 1 - last dim. Only for control-path tests and baselines.

Standalone (no SpacetimeDB; the human's arm checklist uses this)::

    uv run python -m rdf_robot_client.rollout --sim --fake-policy --seconds 10
    uv run python -m rdf_robot_client.rollout --adapter yam --cameras config --prompt "..." --seconds 20
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from . import handlers as _default_handlers  # noqa: F401 - defaults first, so the handlers below win
from .adapters.base import ArmState, JointTarget
from .jobs import JobContext, job_handler
from .policy_client import (
    IMAGE_KEYS,
    Observation,
    PolicyClient,
    PolicyClientError,
    PolicyConfig,
    PolicyReply,
    latency_summary,
)
from .state import RobotState

SuccessCheck = Callable[[int, ArmState], bool]


# --------------------------------------------------------------------------- executor config + stats


@dataclass(frozen=True)
class ExecConfig:
    mode: str = "async"  # async (S-05b) | sync (v0 fallback: 25 steps, then a blocking query)
    period_s: float = 1.0 / 30
    requery_every: int = 10
    blend_steps: int = 3
    hold_at: int = 40  # chunk index: no fresh chunk by then -> hold
    sync_steps: int = 25
    max_retries: int = 3  # consecutive failed calls tolerated (each one retried at once)
    gap_reset_s: float = 0.2  # a gap this long between ticks (pause) invalidates the chunk
    speed_fraction: float = 0.5  # per-step joint change cap, fraction of the arm's max velocity
    # TDM-122 / docs/real-arm-safety.md rule 6 (LARP: a step change after a gap caused CAN failures and arm
    # collapse): policy joint targets never move faster than this, whatever the safety scale allows, and
    # every move out of a hold (first chunk, after a pause, after a stale-chunk hold) starts from the
    # MEASURED pose with the per-tick cap ramping up linearly over ``soft_start_ticks`` ticks.
    max_speed_rad_s: float = 0.3
    soft_start_ticks: int = 15
    base_velocity_scale: float = 0.25  # droid_base: fraction of the predicted joint velocity used
    dof: int = 6
    skip_latency: bool = True  # False only in tests, to show what stitching without the skip does


@dataclass
class ExecStats:
    ticks: int = 0
    requests: int = 0
    chunks_received: int = 0
    chunks_used: int = 0
    stale_discarded: int = 0  # arrived with skip >= hold_at
    errors: dict[str, int] = field(default_factory=dict)
    skips: list[int] = field(default_factory=list)
    blends: int = 0
    hold_ticks: int = 0
    holds: list[dict[str, Any]] = field(default_factory=list)  # {start, end, reason}
    gap_resets: int = 0
    max_jump_rad: float = 0.0  # largest per-step joint target change BEFORE the speed cap
    switch_jumps: list[float] = field(default_factory=list)  # max joint step over each switch + blend window (no hold before)
    rate_limited_ticks: int = 0
    aborted: str | None = None

    def summary(self) -> dict[str, Any]:
        d = asdict(self)
        sk = self.skips
        d["skips"] = {"n": len(sk), "min": min(sk) if sk else None, "max": max(sk) if sk else None,
                      "mean": round(float(np.mean(sk)), 2) if sk else None}
        d["holds"] = self.holds[:50]
        d["hold_events"] = len(self.holds)
        d["max_jump_rad"] = round(self.max_jump_rad, 5)
        sj = self.switch_jumps
        d["switch_jumps"] = {"n": len(sj), "max_rad": round(max(sj), 5) if sj else None,
                             "mean_rad": round(float(np.mean(sj)), 5) if sj else None}
        return d


# --------------------------------------------------------------------------- action mapping


def map_actions(actions: np.ndarray, action_space: str | None, anchor: np.ndarray, cfg: ExecConfig,
                max_velocity: np.ndarray) -> np.ndarray:
    """Server actions [H, D] -> robot targets [H, dof + 1] (joints rad, gripper 0..1)."""
    dof = cfg.dof
    h = actions.shape[0]
    out = np.zeros((h, dof + 1))
    if action_space == "joint_pos_abs" or (action_space is None and actions.shape[1] == dof + 1):
        out[:, :dof] = actions[:, :dof]
        out[:, dof] = np.clip(actions[:, dof], 0.0, 1.0) if actions.shape[1] > dof else anchor[dof]
        return out
    # droid_base (spike / base pi0.5): joint velocities + gripper position (1 = closed in DROID)
    vel = np.clip(actions[:, :dof] * cfg.base_velocity_scale, -max_velocity, max_velocity)
    out[:, :dof] = anchor[:dof] + np.cumsum(vel, axis=0) * cfg.period_s
    out[:, dof] = 1.0 - np.clip(actions[:, -1], 0.0, 1.0)
    return out


# --------------------------------------------------------------------------- chunk executor (pure, clock passed in)


class ChunkExecutor:
    """Turns async policy replies into one target per control tick. No I/O, no sleeping.

    ``client`` needs ``submit(Observation) -> id|None``, ``poll() -> [PolicyReply]`` and ``busy``;
    ``observe(step, now, arm_state) -> Observation`` samples images/state. ``tick(step, now,
    arm_state)`` returns the target (joints + gripper as an array) or None when the episode must end
    (``stats.aborted`` says why). Deterministic for a given sequence of replies and clock values.
    """

    def __init__(self, client: Any, observe: Callable[[int, float, ArmState], Observation], cfg: ExecConfig,
                 max_velocity: tuple[float, ...]) -> None:
        self.client = client
        self.observe = observe
        self.cfg = cfg
        self.vmax = np.asarray(max_velocity[: cfg.dof], dtype=float)
        self.stats = ExecStats()
        self.chunk: np.ndarray | None = None  # [H, dof+1] targets
        self.pos = 0  # index into chunk for THIS tick
        self.blend_from: np.ndarray | None = None  # old trajectory [k, dof+1] for the crossfade
        self.blend_k = 0
        self.last_target: np.ndarray | None = None
        self.last_raw: np.ndarray | None = None
        self.last_submit_step: int | None = None
        self.retry_now = True  # submit on the first tick
        self.consecutive_errors = 0
        self.last_now: float | None = None
        self._hold: dict[str, Any] | None = None
        self._obs_states: dict[int, np.ndarray] = {}  # req id -> observed state (velocity anchor)
        self._win, self._win_max = 0, 0.0  # switch window: the switch tick + the blend ticks
        self.phase: str | None = None  # TDM-114: phase label of the newest policy reply (None = server sends none)
        self._moving_ticks = 0  # TDM-122: ticks since the last hold (soft start)

    # ---- helpers
    def _hold_reason(self) -> str:
        if self.consecutive_errors:
            return "policy_error"
        if self.chunk is None:
            return "no_chunk" if self.stats.chunks_used == 0 else "awaiting_chunk"
        return "stale_chunk" if self.cfg.mode == "async" else "sync_query"

    def _start_hold(self, step: int) -> None:
        if self._hold is None:
            self._hold = {"start": step, "end": step, "reason": self._hold_reason()}
            self.stats.holds.append(self._hold)
        self._hold["end"] = step
        self.stats.hold_ticks += 1

    def _end_hold(self) -> None:
        self._hold = None

    # ---- TDM-99: the plan the arm is executing, for the HUD ghost (``/ws/state`` chunk_preview)
    def preview(self, points: int = 10, now: float | None = None) -> list[list[float]] | None:
        """Downsampled remaining targets of the chunk being executed: ``points`` rows of joints (rad) +
        gripper (0..1), from the next tick's target to the chunk's last step (always included).

        None when nothing is planned: no chunk yet, the chunk ran out (holding), or the loop has not
        ticked for ``gap_reset_s`` (paused for a takeover / reset: the chunk describes the past).
        Called from another thread (the state feed): reads one snapshot of (chunk, pos), never mutates.
        """
        chunk, pos, last = self.chunk, self.pos, self.last_now
        if chunk is None or last is None:
            return None
        now = time.monotonic() if now is None else now
        if now - last > self.cfg.gap_reset_s:
            return None
        n = len(chunk)
        if pos >= n:
            return None
        idx = np.unique(np.linspace(pos, n - 1, num=max(2, int(points))).round().astype(int))
        return [[float(v) for v in chunk[i]] for i in idx]

    def _accept(self, r: PolicyReply, step: int, now: float, st: ArmState) -> None:
        self.stats.chunks_received += 1
        anchor = self._obs_states.pop(r.req_id, np.asarray(st.state_vector(), dtype=float))
        targets = map_actions(r.actions, r.action_space, anchor, self.cfg, self.vmax)  # type: ignore[arg-type]
        if self.cfg.mode == "sync" or not self.cfg.skip_latency:
            skip = 0  # sync: the arm held still while this query was answered
        else:
            skip = int(round((now - r.t_obs) / self.cfg.period_s))
        if skip >= min(self.cfg.hold_at, len(targets)):
            self.stats.stale_discarded += 1
            return
        # crossfade source: what the old chunk would have commanded next, else the held target
        if self.chunk is not None and self.pos < len(self.chunk) and self.last_target is not None:
            self.blend_from = self.chunk[self.pos: self.pos + self.cfg.blend_steps]
        elif self.last_target is not None:
            self.blend_from = np.repeat(self.last_target[None], self.cfg.blend_steps, axis=0)
        else:
            self.blend_from = None
        self.blend_k = 0
        self.chunk, self.pos = targets, skip
        if self._hold is None and self.last_target is not None:  # a switch while moving (not out of a hold)
            self._win, self._win_max = self.cfg.blend_steps + 1, 0.0
        else:
            self._win = 0
        self.stats.chunks_used += 1
        self.stats.skips.append(skip)

    # ---- one control tick
    def tick(self, step: int, now: float, st: ArmState) -> np.ndarray | None:
        cfg = self.cfg
        self.stats.ticks += 1
        if self.last_now is not None and now - self.last_now > cfg.gap_reset_s:
            # TDM-122: the arm may have moved during the pause (a human takeover drives it). Re-anchor on the
            # MEASURED pose, so the hold and the rate-limited ramp into the next chunk start where the arm is,
            # instead of commanding the pre-pause target (a jump the safety layer would reject, then abort on).
            self.last_target = None
            self.last_raw = None
            self.blend_from = None
        if self.last_now is not None and now - self.last_now > cfg.gap_reset_s and self.chunk is not None:
            self.chunk = None  # paused (takeover / reset / link): the chunk describes the past
            self.stats.gap_resets += 1
            self.retry_now = True
            self.client.poll()  # drop anything that landed during the pause
        self.last_now = now

        for r in self.client.poll():
            if r.ok:
                self.consecutive_errors = 0
                self.phase = getattr(r, "phase", None)
                self._accept(r, step, now, st)
            else:
                self.consecutive_errors += 1
                self.stats.errors[r.code or "error"] = self.stats.errors.get(r.code or "error", 0) + 1
                self._obs_states.pop(r.req_id, None)
                self.retry_now = True
                if self.consecutive_errors > cfg.max_retries:
                    self.stats.aborted = f"policy_error: {r.code}: {(r.error or '')[:200]}"
                    return None

        # decide this tick's target
        limit = min(cfg.hold_at if cfg.mode == "async" else cfg.sync_steps, len(self.chunk) if self.chunk is not None else 0)
        if self.chunk is None or self.pos >= limit:
            if self.chunk is not None and cfg.mode == "sync" and self.pos >= limit:
                self.chunk = None  # executed its 25 steps: wait for the next one
                self.retry_now = True
            self._start_hold(step)
            self._moving_ticks = 0
            target = self.last_target if self.last_target is not None else np.asarray(st.state_vector(), dtype=float)
            raw = target
        else:
            self._end_hold()
            raw = self.chunk[self.pos].copy()
            if self.blend_from is not None and self.blend_k < min(cfg.blend_steps, len(self.blend_from)):
                w = (self.blend_k + 1) / (cfg.blend_steps + 1)
                raw = w * raw + (1 - w) * self.blend_from[self.blend_k]
                self.blend_k += 1
                self.stats.blends += 1
            self.pos += 1
            target = raw
            if self.last_target is None:  # never start from a stale/unknown anchor: the measured pose
                self.last_target = np.asarray(st.state_vector(), dtype=float)
            if self.last_target is not None:
                d = raw[: cfg.dof] - self.last_target[: cfg.dof]
                # seam in the PLANNED trajectory (raw vs last raw); equals d unless the rate limit is lagging
                seam = raw[: cfg.dof] - (self.last_raw if self.last_raw is not None else self.last_target)[: cfg.dof]
                self.stats.max_jump_rad = max(self.stats.max_jump_rad, float(np.max(np.abs(seam))))
                if self._win > 0:
                    self._win_max = max(self._win_max, float(np.max(np.abs(seam))))
                    self._win -= 1
                    if self._win == 0:
                        self.stats.switch_jumps.append(self._win_max)
                ramp = min(1.0, (self._moving_ticks + 1) / max(1, cfg.soft_start_ticks))
                cap = np.minimum(self.vmax * cfg.speed_fraction, cfg.max_speed_rad_s) * cfg.period_s * ramp
                self._moving_ticks += 1
                if np.any(np.abs(d) > cap + 1e-12):
                    self.stats.rate_limited_ticks += 1
                    target = raw.copy()
                    target[: cfg.dof] = self.last_target[: cfg.dof] + np.clip(d, -cap, cap)
        self.last_raw = raw
        self.last_target = np.asarray(target, dtype=float)

        # re-query
        if cfg.mode == "async":
            due = self.retry_now or self.last_submit_step is None or step - self.last_submit_step >= cfg.requery_every
        else:
            due = self.retry_now or self.chunk is None
        if due and not self.client.busy:
            obs = self.observe(step, now, st)
            rid = self.client.submit(obs)
            if rid is not None:
                self._obs_states[rid] = np.asarray(obs.state, dtype=float)[: cfg.dof + 1]
                self.stats.requests += 1
                self.last_submit_step = step
                self.retry_now = False
        return self.last_target


# --------------------------------------------------------------------------- observation sampling


def _blank_images() -> list[np.ndarray]:
    return [np.full((224, 224, 3), 96, np.uint8), np.full((224, 224, 3), 160, np.uint8)]


class Observer:
    """Samples [base, wrist] 224 px RGB from the CameraRig + the arm state."""

    def __init__(self, cameras: Any, prompt: str, checkpoint_uri: str | None, allow_blank: bool = False) -> None:
        self.cameras = cameras
        self.prompt = prompt
        self.checkpoint_uri = checkpoint_uri
        self.allow_blank = allow_blank
        self.blank_used = 0
        self.stale_frames = 0

    def images(self) -> list[np.ndarray]:
        s = self.cameras.latest_synced() if self.cameras is not None else None
        if s is None:
            if not self.allow_blank:
                raise PolicyClientError("no_cameras", "no synced camera frames (CameraRig not running?)")
            self.blank_used += 1
            return _blank_images()
        if (time.monotonic_ns() - s.t_ns) > 200e6:
            self.stale_frames += 1
        imgs = s.policy_images()
        missing = [k for k in IMAGE_KEYS if k not in imgs]
        if missing:
            raise PolicyClientError("no_cameras", f"camera frames missing policy keys {missing}; got {sorted(imgs)}")
        return [imgs[k] for k in IMAGE_KEYS]

    def __call__(self, step: int, now: float, st: ArmState) -> Observation:
        return Observation(images=self.images(), state=st.state_vector(), prompt=self.prompt, t_obs=now, step=step,
                           checkpoint_uri=self.checkpoint_uri)


# --------------------------------------------------------------------------- the episode


@dataclass
class RolloutResult:
    ended: str
    steps: int
    seconds: float
    success: bool | None
    prompt: str
    checkpoint_uri: str | None
    policy: dict[str, Any]
    timing: dict[str, Any]
    executor: dict[str, Any]
    latency: dict[str, Any]
    flags: list[str]
    observer: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.ended not in ("policy_error",)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _timing(stamps: list[float], period: float, overruns: int) -> dict[str, Any]:
    if len(stamps) < 3:
        return {"ticks": len(stamps)}
    dt = np.diff(np.asarray(stamps)) * 1000
    per = period * 1000
    err = np.abs(dt - per)
    return {
        "ticks": len(stamps), "target_hz": round(1 / period, 2),
        "achieved_hz": round((len(stamps) - 1) / (stamps[-1] - stamps[0]), 2),
        "period_ms_mean": round(float(dt.mean()), 2), "period_ms_std": round(float(dt.std()), 2),
        "period_ms_p95": round(float(np.percentile(dt, 95)), 2), "period_ms_max": round(float(dt.max()), 2),
        "jitter_ms_p95": round(float(np.percentile(err, 95)), 2), "jitter_ms_max": round(float(err.max()), 2),
        "late_ticks_over_5ms": int((dt > per + 5).sum()), "overruns": overruns,
    }


# ---- TDM-114: live policy phase for /ws/state (stream.LiveStateSampler publishes it as ``policy``)
POLICY_PHASE_SERVICE = "policy_phase"  # callable -> {phase, trial_index, trials, purpose, eval_run_id}
POLICY_TRIAL_SERVICE = "policy_trial"  # dict set by the eval trial runner for the trial it is on


def policy_phase_service(services: dict[str, Any], phase: Callable[[], str | None]) -> Callable[[], dict[str, Any]]:
    """The ``policy_phase`` callable for one episode: the newest reply's phase label (None when the server sends
    none, e.g. real pi0.5) plus the trial context in ``services['policy_trial']`` (None outside a trial runner)."""

    def read() -> dict[str, Any]:
        trial = services.get(POLICY_TRIAL_SERVICE) or {}
        return {"phase": phase(), "trial_index": trial.get("trial_index"), "trials": trial.get("trials"),
                "purpose": trial.get("purpose"), "eval_run_id": trial.get("eval_run_id")}

    return read


def resolve_prompt(ctx: JobContext, prompt: str | None) -> str:
    p = (prompt or ctx.payload.get("prompt") or ctx.payload.get("task_prompt") or "").strip()
    if p:
        return p
    tid = getattr(ctx.command, "ticket_id", 0)
    if tid:
        try:
            rows = ctx.sql(f"SELECT task_description FROM ticket WHERE id = {int(tid)}")
            if rows and rows[0].get("task_description"):
                return str(rows[0]["task_description"]).strip()
        except Exception:  # noqa: BLE001
            pass
    raise ValueError("rollout needs a prompt (payload.prompt or the ticket's task_description)")


def get_policy(ctx: JobContext, config: PolicyConfig | None = None) -> PolicyClient:
    """The shared PolicyClient in ``ctx.services['policy']`` (built from env on first use)."""
    pc = ctx.services.get("policy")
    if pc is None:
        pc = PolicyClient(config or PolicyConfig.from_env()).start()
        ctx.services["policy"] = pc
    return pc


class PolicyRollout:
    def __init__(self, ctx: JobContext, policy: PolicyClient | None = None, exec_config: ExecConfig | None = None,
                 checkpoint_uri: str | None = None, allow_blank_images: bool | None = None) -> None:
        self.ctx = ctx
        self.policy = policy or get_policy(ctx)
        arm = ctx.arm
        self.cfg = exec_config or ExecConfig(period_s=1.0 / arm.control_rate_hz, dof=arm.dof)
        self.checkpoint_uri = checkpoint_uri
        sim = "sim" in str(arm.describe().get("adapter", "")) or bool(arm.describe().get("sim"))
        self.allow_blank = sim if allow_blank_images is None else allow_blank_images

    def warm(self, prompt: str, timeout_s: float = 90.0) -> PolicyReply:
        """Health check + one blocking inference (JIT / connection warm-up) before the arm moves."""
        self.policy.wait_healthy(timeout=timeout_s)
        obs = Observer(self.ctx.services.get("cameras"), prompt, self._wire_uri(), self.allow_blank)
        st = self.ctx.arm.read_state()
        o = obs(0, time.monotonic(), st)
        o.timeout_s = timeout_s
        r = self.policy.infer_blocking(o, timeout=timeout_s + 5)
        if not r.ok:
            raise PolicyClientError(r.code or "error", f"warm-up inference failed: {r.error}")
        return r

    def _wire_uri(self) -> str | None:
        return self.checkpoint_uri if self.policy.config.hot_swap else None

    def run_episode(self, prompt: str, max_steps: int | None = None, success_check: SuccessCheck | None = None, *,
                    timeout_s: float | None = None, warm: bool = True, success_every: int = 1) -> RolloutResult:
        ctx = self.ctx
        if ctx.state is not RobotState.ROLLOUT:
            ctx.enter(RobotState.ROLLOUT, f"policy rollout: {prompt[:60]}")
        flags: list[str] = []
        if warm:
            try:
                self.warm(prompt)
            except Exception as e:  # noqa: BLE001 - the loop's retry logic decides; record it
                flags.append(f"warmup_failed: {e}"[:200])
        observer = Observer(ctx.services.get("cameras"), prompt, self._wire_uri(), self.allow_blank)
        ex = ChunkExecutor(self.policy, observer, self.cfg, ctx.arm.limits.max_velocity)
        n_log0 = len(self.policy.log)
        self.policy.poll()  # nothing from before this episode
        stamps: list[float] = []
        tick_ms: list[float] = []  # executor compute per tick (incl. observation sampling)
        state = {"success": None, "ended": None}
        dof = self.cfg.dof
        ctx.event("rollout.started", prompt=prompt, checkpoint_uri=self.checkpoint_uri, mode=self.cfg.mode,
                  policy=self.policy.config.describe())

        def step(i: int, st: ArmState) -> JointTarget | None:
            now = time.monotonic()
            stamps.append(now)
            if success_check is not None and i % max(1, success_every) == 0 and i > 0:
                if success_check(i, st):
                    state["success"], state["ended"] = True, "success"
                    return None
            try:
                tgt = ex.tick(i, now, st)
            except PolicyClientError as e:  # camera failure while sampling
                ex.stats.aborted = f"{e.code}: {e}"
                tgt = None
            tick_ms.append((time.monotonic() - now) * 1000)
            if tgt is None:
                state["ended"] = "policy_error"
                return None
            return JointTarget(tuple(float(v) for v in tgt[:dof]), gripper=float(np.clip(tgt[dof], 0.0, 1.0)))

        # TDM-99: the state feed (stream.LiveStateSampler) publishes this as chunk_preview while it is ours
        preview = ex.preview
        ctx.services["chunk_preview"] = preview
        # TDM-114: the policy's phase (+ the trial the eval runner is on) for the same feed, same lifecycle
        phase_fn = policy_phase_service(ctx.services, lambda: ex.phase)
        ctx.services[POLICY_PHASE_SERVICE] = phase_fn
        t0 = time.monotonic()
        try:
            stats = ctx.run_control_loop(step, max_steps=max_steps, max_seconds=timeout_s,
                                         active_state=RobotState.ROLLOUT, control_source="policy")
        finally:
            if ctx.services.get("chunk_preview") is preview:
                ctx.services.pop("chunk_preview", None)
            if ctx.services.get(POLICY_PHASE_SERVICE) is phase_fn:
                ctx.services.pop(POLICY_PHASE_SERVICE, None)
            ctx.arm.hold()
        ended = state["ended"] or stats.ended
        if ex.stats.hold_ticks:
            flags.append("held")
        if any(h["reason"] == "stale_chunk" for h in ex.stats.holds):
            flags.append("stale_chunk_hold")
        if ex.stats.errors:
            flags.append("policy_errors")
        if observer.blank_used:
            flags.append("blank_images")
        if success_check is not None and state["success"] is None:
            state["success"] = False
        result = RolloutResult(
            ended=ended, steps=stats.steps, seconds=round(time.monotonic() - t0, 3), success=state["success"],
            prompt=prompt, checkpoint_uri=self.checkpoint_uri, policy=self.policy.config.describe(),
            timing={**_timing(stamps, self.cfg.period_s, stats.overruns), "paused_s": round(stats.paused_seconds, 3),
                    "tick_ms_p95": round(float(np.percentile(tick_ms, 95)), 3) if tick_ms else None,
                    "tick_ms_max": round(max(tick_ms), 3) if tick_ms else None},
            executor={**ex.stats.summary(), "mode": self.cfg.mode}, latency=latency_summary(self.policy.log[n_log0:]),
            flags=flags, observer={"blank_images": observer.blank_used, "stale_frames": observer.stale_frames},
        )
        ctx.event("rollout.ended", ended=ended, steps=result.steps, success=result.success,
                  achieved_hz=result.timing.get("achieved_hz"), rtt_p95_ms=result.latency.get("rtt_p95_ms"),
                  hold_ticks=ex.stats.hold_ticks, errors=ex.stats.errors, flags=flags)
        return result


def run_episode(ctx: JobContext, prompt: str, max_steps: int | None = None, success_check: SuccessCheck | None = None,
                **kw: Any) -> RolloutResult:
    """One policy episode on ``ctx.arm`` (see module doc). kw: timeout_s, checkpoint_uri, mode, policy, warm."""
    policy = kw.pop("policy", None)
    checkpoint_uri = kw.pop("checkpoint_uri", None)
    mode = kw.pop("mode", None)
    exec_config = kw.pop("exec_config", None)
    if exec_config is None and mode:
        exec_config = ExecConfig(mode=mode, period_s=1.0 / ctx.arm.control_rate_hz, dof=ctx.arm.dof)
    r = PolicyRollout(ctx, policy=policy, exec_config=exec_config, checkpoint_uri=checkpoint_uri,
                      allow_blank_images=kw.pop("allow_blank_images", None))
    return r.run_episode(prompt, max_steps, success_check, **kw)


# --------------------------------------------------------------------------- job handlers


@job_handler("load_checkpoint")
def load_checkpoint(ctx: JobContext) -> dict[str, Any]:
    uri = ctx.payload.get("checkpoint_uri") or "base"
    pc = get_policy(ctx)
    health = pc.wait_healthy(timeout=float(ctx.payload.get("timeout_s") or 120))
    act = pc.activate(uri)
    ctx.client.checkpoint_uri = str(uri)
    return {"checkpoint_uri": uri, "loaded": True, "policy": pc.config.describe(), "activate": act,
            "health": {k: health.get(k) for k in ("ok", "ready", "gpu", "active_checkpoint_uri") if k in health}}


@job_handler("run_baseline")
def rollout_episode_job(ctx: JobContext) -> dict[str, Any]:
    """One policy episode (interim run_baseline until TDM-51's trial runner replaces it)."""
    p = ctx.payload
    prompt = resolve_prompt(ctx, None)
    uri = p.get("checkpoint_uri") or getattr(ctx.client, "checkpoint_uri", None)
    if uri and get_policy(ctx).config.hot_swap:
        get_policy(ctx).activate(uri)
    max_steps = int(p["max_steps"]) if p.get("max_steps") else None
    if p.get("timeout_s"):
        timeout_s: float | None = float(p["timeout_s"])
    else:
        timeout_s = None if max_steps else 30.0
    rec = ctx.services.get("recorder")  # TDM-48 Recorder (main.py builds it when cameras + uploads are set up)
    recording: dict[str, Any] = {"recorded": False, "reason": "no recorder service"}
    if rec is not None:
        key = rec.start(ctx.command.ticket_id, "baseline", checkpoint_uri=uri, note=f"policy rollout: {prompt[:80]}")
        recording = {"recorded": True, "episode_key": key}
    res = None
    try:
        res = run_episode(ctx, prompt, max_steps=max_steps, timeout_s=timeout_s, checkpoint_uri=uri, mode=p.get("mode"))
    finally:
        if rec is not None and rec.recording:
            ep = rec.stop(success=res.success if res else None,
                          note=f"rollout ended={res.ended if res else 'error'} flags={res.flags if res else []}")
            recording["local_dir"] = str(getattr(ep, "dir", ""))
    ctx.result.update(rollout=res.to_dict(), recording=recording)
    return {"rollout": res.to_dict(), "recording": recording,
            "note": "single policy episode (TDM-50); eval trials are TDM-51"}


# --------------------------------------------------------------------------- standalone runner (no SpacetimeDB)


class StandaloneRobot:
    """Just enough of RobotClient for a JobContext without SpacetimeDB (CLI, tests).

    Uses RobotClient's own record_command / control_tick so ticks are identical to the real loop.
    """

    from .loop import RobotClient as _RC

    record_command = _RC.record_command
    control_tick = _RC.control_tick

    def __init__(self, arm: Any, services: dict[str, Any] | None = None) -> None:
        from .state import StateMachine

        self.arm = arm
        self.sm = StateMachine()
        self.services: dict[str, Any] = services or {}
        self.checkpoint_uri: str | None = None
        self._last_cmd = None
        self._tick_listeners: list[Any] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.link_up = True

    def link_ok(self) -> bool:
        return self.link_up

    def event(self, type_: str, **payload: Any) -> None:
        self.events.append((type_, payload))

    def add_tick_listener(self, fn: Any) -> None:
        self._tick_listeners.append(fn)

    def remove_tick_listener(self, fn: Any) -> None:
        if fn in self._tick_listeners:
            self._tick_listeners.remove(fn)

    def sql(self, q: str) -> list[dict[str, Any]]:
        return []

    def context(self, payload: dict[str, Any] | None = None, type_: str = "run_baseline") -> JobContext:
        from .commands import Command

        cmd = Command(id=0, seq=0, robot_id=0, ticket_id=0, type=type_, payload=payload or {}, status="acked",
                      issued_by="standalone", issued_at_us=None)
        return JobContext(self, cmd)  # type: ignore[arg-type]


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_robot_client.rollout",
                                 description="TDM-50: one policy episode without SpacetimeDB")
    ap.add_argument("--sim", action="store_true", help="SimArm (default adapter yam)")
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--cameras", choices=["off", "synthetic", "config"], default=None)
    ap.add_argument("--prompt", default="pick up the cube and put it in the bowl")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--mode", choices=["async", "sync"], default="async")
    ap.add_argument("--backend", choices=["spike", "production", "sim"], default=None, help="default $POLICY_BACKEND or spike")
    ap.add_argument("--url", default=None, help="override the policy URL (e.g. a local fake)")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--fake-policy", action="store_true", help="start the local fake policy server (laptop tests)")
    ap.add_argument("--fake-latency-ms", type=float, default=160)
    ap.add_argument("--fake-jitter-ms", type=float, default=30)
    ap.add_argument("--fake-drop-prob", type=float, default=0.0)
    ap.add_argument("--home-first", action="store_true", help="move to home before the episode")
    ap.add_argument("--out", default=None, help="write the result JSON here")
    ap.add_argument("--unsafe-no-safety-layer", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    adapter = "sim" if a.sim else (a.adapter or "yam")
    cams_mode = a.cameras or ("synthetic" if adapter == "sim" else "config")

    from .main import build_arm

    arm = build_arm(adapter, safety=not a.unsafe_no_safety_layer)
    fake = None
    services: dict[str, Any] = {}
    try:
        if a.fake_policy:
            from .fake_policy_server import FakePolicyServer, jitter_schedule

            fake = FakePolicyServer(schedule=jitter_schedule(a.fake_latency_ms, a.fake_jitter_ms, a.fake_drop_prob)).start()
            cfg = PolicyConfig(backend="spike", url=fake.url)
        else:
            cfg = PolicyConfig.from_env(a.backend, url=a.url)
        if cams_mode != "off":
            from .cameras import CameraRig

            rig = CameraRig.from_config(synthetic=cams_mode == "synthetic")
            rig.start()
            rig.wait_ready(5.0)
            services["cameras"] = rig
        arm.connect()
        robot = StandaloneRobot(arm, services)
        ctx = robot.context({"prompt": a.prompt})
        stop = threading.Event()
        if sys.stdin and sys.stdin.isatty():
            def _keys() -> None:
                sys.stdin.readline()
                ctx.finish_requested.set()  # Enter = operator stop (arm holds)
            threading.Thread(target=_keys, daemon=True).start()
            print("Enter = stop the episode (arm holds). Physical e-stop stays in reach.", file=sys.stderr)
        with PolicyClient(cfg) as pc:
            if a.home_first:
                ctx.enter(RobotState.HOMING, "home")
                arm.home(should_abort=ctx.should_abort)
                ctx.enter(RobotState.IDLE, "homed")
            res = run_episode(ctx, a.prompt, timeout_s=a.seconds, policy=pc, checkpoint_uri=a.checkpoint,
                              mode=a.mode, allow_blank_images=cams_mode == "off")
        stop.set()
        out = res.to_dict()
        print(json.dumps(out, indent=1, default=str))
        if a.out:
            with open(a.out, "w") as f:
                json.dump(out, f, indent=1, default=str)
        return 0 if res.ok else 2
    finally:
        try:
            arm.hold()
            arm.disconnect()
        except Exception:  # noqa: BLE001
            pass
        if services.get("cameras") is not None:
            services["cameras"].stop()
        if fake is not None:
            fake.stop()


__all__ = ["ChunkExecutor", "ExecConfig", "ExecStats", "Observer", "PolicyRollout", "RolloutResult",
           "StandaloneRobot", "get_policy", "load_checkpoint", "map_actions", "resolve_prompt", "rollout_episode_job",
           "run_episode"]

if __name__ == "__main__":
    raise SystemExit(_main())
