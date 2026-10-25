"""TDM-126: ONE supervised rollout of a shipped, fine-tuned pi0.5 checkpoint on the REAL YAM arm.

Issued by the orchestrator's ``POST /tickets/{id}/deployment/run`` with ``{"real_arm": true, "confirm":
"HANDS_ON_ESTOP"}`` as ``run_baseline`` {purpose: "deployment_real_run", checkpoint_uri, prompt, timeout_s,
speed_scale, real_arm, confirm}. Runs only where ``TDM119_ALLOW_POLICY=1`` (the Jetson's teleop.env), with the
production policy server (``POLICY_BACKEND=production``, ``POLICY_SERVER_URL``, ``POLICY_SERVER_TOKEN``).

What it does, in order (docs/real-arm-safety.md applies in full; the user holds the hardware e-stop):

  1. Refuses unless: the env gate, the payload's real_arm + confirm, a real ``modal-vol://`` checkpoint, a
     non-empty prompt (the exact training prompt), the production backend, and a policy server whose ACTIVE
     checkpoint is that one with the 7-dim ``joint_pos_abs`` YAM spec (``tdm122_dryrun.mapping_preflight``).
     When another checkpoint is active it activates this one first (no motion; a cache hit once the user has
     preloaded both with ``policy_ctl.py activate <uri> --preload``), then checks the spec again.
  2. Caps the SafetyLayer speed scale at 0.25 (restored afterwards).
  3. Asks for the FIRST chunk from the arm's measured pose and runs the TDM-122 dry-run check on it
     (``ChunkChecker``: joint/LARP limits, jump from the observed pose, speed, workspace, floor, a replay through
     the SafetyLayer). Anything but ``ok`` / ``clamp`` refuses the run; the arm has not moved.
  4. Runs the TDM-50/122 ``ChunkExecutor`` through ``ctx.arm`` (the SafetyLayer): hold the measured pose until
     a chunk arrives, then ramp from the MEASURED pose at <= min(0.5 x live cap, 0.3 rad/s) with a 15-tick soft
     start after every hold. Approach inspired by LARP's policy client lesson (a step change after an idle gap
     preceded a CAN failure and arm collapse): never jump to a chunk's first action, ramp into it.
     Every later chunk is checked the same way; a ``reject`` ends the run (the arm holds).
  5. Hard time cap: ``timeout_s`` (default 25 s, never more than 30 s) of WALL-CLOCK time from the first
     motion tick. A pause (takeover / link loss) holds the arm and does not extend it: at the cap the run ends,
     and if it is still held in a pause 2 s later the job is cancelled.
  6. Stop: a soft e-stop (the web STOP, ``soft_estop``), ``{t:"stop"}`` on /ws/control, or the stop command all
     latch the SafetyLayer / state machine, so the next tick raises ``Cancelled`` and no further setpoint goes
     out. At the end the arm HOLDS its pose (never disabled, never released).
  7. No recording, no eval_trial row. Logs ``deployment.real_run_first_chunk`` and
     ``deployment.real_run_finished`` (also when refused) in event_log.

Gripper: 0 = closed .. 1 = open in both the checkpoint and the adapter (TDM-122 mapping), passed through.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .jobs import JobContext
from .policy_client import PolicyClientError
from .rollout import Observer, get_policy, run_episode
from .state import RobotState

REAL_RUN_PURPOSE = "deployment_real_run"
REAL_RUN_CONFIRM = "HANDS_ON_ESTOP"
REAL_RUN_DEFAULT_S = 25.0
REAL_RUN_MAX_S = 30.0
REAL_RUN_MAX_SPEED_SCALE = 0.5  # the adapter still caps every joint at <= 0.3 rad/s; 0.25 left policy motion at ~0.2 rad/s
PASS_VERDICTS = ("ok", "clamp")
CANCEL_GRACE_S = 2.0  # still held in a pause this long after the cap: cancel the job

Checker = Callable[[Any, Any, Any], dict[str, Any]]  # (actions, action_space, observed state) -> check dict


class RealRunRefused(RuntimeError):
    """The run did not start: the arm never moved."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.extra = code, message, extra


def real_checkpoint(uri: str) -> bool:
    return uri.startswith("modal-vol://rdf-checkpoints/") and "fake" not in uri and "stub-no-gpu" not in uri


def default_checker() -> Checker:
    """The TDM-122 dry-run check against the live safety config ($RDF_SAFETY_CONFIG, profile yam)."""
    from .tdm122_dryrun import ChunkChecker

    c = ChunkChecker(safety_config=os.environ.get("RDF_SAFETY_CONFIG") or None)
    return lambda actions, space, state: c.check(actions, space, state)


class CheckedPolicy:
    """Wraps the PolicyClient for the ChunkExecutor: every reply is checked against the pose it was asked
    from before the executor sees it. A rejected chunk is dropped and ``on_reject`` ends the run."""

    def __init__(self, inner: Any, checker: Checker, on_reject: Callable[[dict[str, Any]], None]) -> None:
        self._inner = inner
        self._checker = checker
        self._on_reject = on_reject
        self._states: dict[int, list[float]] = {}
        self.checked = 0
        self.verdicts: dict[str, int] = {}
        self.rejected: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:  # config, log, wait_healthy, ... from the real client
        return getattr(self._inner, name)

    @property
    def busy(self) -> bool:
        return self._inner.busy

    def submit(self, obs: Any) -> int | None:
        rid = self._inner.submit(obs)
        if rid is not None:
            self._states[rid] = list(obs.state)
        return rid

    def poll(self) -> list[Any]:
        out = []
        for r in self._inner.poll():
            state = self._states.pop(r.req_id, None)
            if not r.ok or state is None or self.rejected is not None:
                if self.rejected is None:
                    out.append(r)
                continue
            chk = self._checker(r.actions, r.action_space, state)
            v = str(chk.get("verdict"))
            self.checked += 1
            self.verdicts[v] = self.verdicts.get(v, 0) + 1
            if v not in PASS_VERDICTS:
                self.rejected = {"verdict": v, "problems": list(chk.get("problems") or [])[:5], "chunk": self.checked}
                self._on_reject(self.rejected)
                continue
            out.append(r)
        return out


def _finish(ctx: JobContext, out: dict[str, Any]) -> dict[str, Any]:
    ctx.event("deployment.real_run_finished", **{k: v for k, v in out.items() if k not in ("rollout", "note", "ticket_id")})
    return out


def ensure_active(pc: Any, uri: str) -> dict[str, Any]:
    """The policy server is up and serves ``uri`` with the 7-dim joint_pos_abs YAM spec (activating it when
    another checkpoint is active: no motion; a cache hit once both were preloaded). Raises RealRunRefused."""
    try:
        health = pc.wait_healthy(timeout=60.0)
    except Exception as e:  # noqa: BLE001
        raise RealRunRefused("policy_server_down", f"policy server not healthy: {e}") from e
    from .tdm122_dryrun import mapping_preflight

    active = str(health.get("active_checkpoint_uri") or "").rstrip("/").removesuffix("/params")
    if active != uri.rstrip("/").removesuffix("/params") and pc.config.hot_swap:
        try:
            pc.activate(uri)
            health = pc.wait_healthy(timeout=60.0)
        except Exception as e:  # noqa: BLE001
            raise RealRunRefused("activate_failed", f"could not activate {uri}: {e}") from e
    mapping = mapping_preflight(health, uri)
    if not mapping.get("yam_ok") or not health.get("active_checkpoint_uri"):
        probs = mapping.get("problems") or [f"active checkpoint is {health.get('active_checkpoint_uri')}"]
        raise RealRunRefused("checkpoint_not_active", "; ".join(probs)[:300] + " (activate it with policy_ctl.py first)")
    return mapping


def guarded_episode(ctx: JobContext, pc: Any, prompt: str, uri: str, seconds: float, *, checker: Checker | None = None,
                    speed_scale: float = REAL_RUN_MAX_SPEED_SCALE, success_check: Any = None,
                    max_steps: int | None = None, mode: str | None = None, cancel_if_held: bool = True,
                    event_prefix: str = "deployment.real_run") -> tuple[Any, dict[str, Any]]:
    """ONE real-arm policy episode under every TDM-126 guard (steps 2-6 of the module doc). Returns
    (RolloutResult, info); raises RealRunRefused (the arm never moved) when the first chunk fails its check.

    ``cancel_if_held``: also cancel the job when a pause still holds the arm 2 s after the cap (the one-off
    deployment run). Eval trials pass False: their takeover pause is an existing, human-driven state; the cap
    then ends the rollout as soon as the pause ends."""
    seconds = min(float(seconds), REAL_RUN_MAX_S)
    chk = checker or default_checker()
    arm = ctx.arm
    prev_scale = getattr(arm, "speed_scale", None)
    want = min(float(speed_scale or REAL_RUN_MAX_SPEED_SCALE), REAL_RUN_MAX_SPEED_SCALE)
    if hasattr(arm, "set_speed_scale") and prev_scale is not None:
        arm.set_speed_scale(want)  # restored to prev_scale (the teleop setting) when the run ends
    info: dict[str, Any] = {"capped": False, "rejected": None, "time_cap_s": seconds,
                            "speed_scale": getattr(arm, "speed_scale", None)}
    done = threading.Event()
    try:
        if ctx.state is not RobotState.ROLLOUT:
            ctx.enter(RobotState.ROLLOUT, f"real-arm policy run (max {seconds:.0f}s): {prompt[:50]}")
        # the first chunk, from the measured pose, before anything moves (also warms the server)
        sim = bool(arm.describe().get("sim")) or "sim" in str(arm.describe().get("adapter", ""))
        observer = Observer(ctx.services.get("cameras"), prompt, uri if pc.config.hot_swap else None, allow_blank=sim)
        st = arm.read_state()
        try:
            obs = observer(0, time.monotonic(), st)
        except PolicyClientError as e:
            raise RealRunRefused("no_cameras", str(e)) from e
        obs.timeout_s = 60.0
        r = pc.infer_blocking(obs, timeout=65.0)
        if not r.ok:
            raise RealRunRefused("first_chunk_failed", f"{r.code}: {r.error}")
        first = chk(r.actions, r.action_space, list(obs.state))
        verdict = str(first.get("verdict"))
        info["first_chunk_verdict"] = verdict
        ctx.event(f"{event_prefix}_first_chunk", verdict=verdict, problems=list(first.get("problems") or [])[:5],
                  checkpoint_uri=uri)
        if verdict not in PASS_VERDICTS:
            raise RealRunRefused("first_chunk_rejected", f"first chunk verdict {verdict}: "
                                 + "; ".join(first.get("problems") or [])[:250], verdict=verdict)
        ctx.check()

        def on_reject(rej: dict[str, Any]) -> None:
            info["rejected"] = rej
            ctx.finish_requested.set()

        guarded = CheckedPolicy(pc, chk, on_reject)

        def cap() -> None:
            if done.wait(seconds):
                return
            info["capped"] = True
            ctx.finish_requested.set()
            if cancel_if_held and not done.wait(CANCEL_GRACE_S):
                ctx.cancel.cancel("time_cap", f"real-arm run passed its {seconds:.0f}s cap while held")

        ctx.finish_requested.clear()
        threading.Thread(target=cap, name="tdm126-time-cap", daemon=True).start()
        res = run_episode(ctx, prompt, max_steps=max_steps, success_check=success_check, timeout_s=seconds,
                          checkpoint_uri=uri, policy=guarded, warm=False, mode=mode)
        info.update(chunks_checked=guarded.checked + 1, verdicts=guarded.verdicts)
        if info["rejected"]:
            info["ended"] = "chunk_rejected"
        elif info["capped"]:
            info["ended"] = "time_cap"
        else:
            info["ended"] = res.ended
        return res, info
    finally:
        done.set()
        ctx.finish_requested.clear()
        try:
            arm.hold()  # hold, never disable
        except Exception:  # noqa: BLE001 - latched by an e-stop: the layer already holds
            pass
        finally:
            if prev_scale is not None and hasattr(arm, "set_speed_scale"):
                arm.set_speed_scale(float(prev_scale))


def real_arm_run(ctx: JobContext, *, checker: Checker | None = None, policy: Any = None,
                 require_production: bool = True) -> dict[str, Any]:
    p = ctx.payload
    uri = str(p.get("checkpoint_uri") or "").strip()
    prompt = str(p.get("prompt") or "").strip()
    seconds = min(float(p.get("timeout_s") or REAL_RUN_DEFAULT_S), REAL_RUN_MAX_S)
    base = {"purpose": REAL_RUN_PURPOSE, "checkpoint_uri": uri, "prompt": prompt, "real_arm": True,
            "ticket_id": int(ctx.command.ticket_id or 0), "time_cap_s": seconds,
            "checkpoint_choice": str(p.get("checkpoint_choice") or "after")}  # before = YAM smoke, after = shipped
    ctx.result.update(base)

    def refused(e: RealRunRefused) -> RealRunRefused:
        _finish(ctx, {**base, "ok": False, "moved": False, "refused": e.code, "error": e.message[:300], **e.extra})
        return e

    try:
        if os.environ.get("TDM119_ALLOW_POLICY") != "1":
            raise RealRunRefused("policy_not_allowed", "TDM119_ALLOW_POLICY=1 is not set on this robot")
        if p.get("real_arm") is not True or p.get("confirm") != REAL_RUN_CONFIRM:
            raise RealRunRefused("not_confirmed", f"payload needs real_arm: true and confirm: {REAL_RUN_CONFIRM}")
        if not real_checkpoint(uri):
            raise RealRunRefused("checkpoint_not_real", f"not a real modal-vol://rdf-checkpoints/ checkpoint: {uri!r}")
        if not prompt:
            raise RealRunRefused("no_prompt", "the run needs the exact training prompt in payload.prompt")
        pc = policy or get_policy(ctx)
        if require_production and pc.config.backend != "production":
            raise RealRunRefused("backend_not_production",
                                 f"POLICY_BACKEND is {pc.config.backend!r}; the real arm needs production")
        ensure_active(pc, uri)
        res, info = guarded_episode(ctx, pc, prompt, uri, seconds, checker=checker,
                                    speed_scale=float(p.get("speed_scale") or REAL_RUN_MAX_SPEED_SCALE))
    except RealRunRefused as e:
        raise refused(e) from None
    except BaseException as e:  # e-stop / stop / time-cap cancel / errors: log the end, then let the loop report it
        _finish(ctx, {**base, "ok": False, "moved": True, "ended": getattr(e, "reason", type(e).__name__),
                      "error": str(e)[:300]})
        raise
    out = {**base, "ok": not info["rejected"] and res.ended != "policy_error", "moved": res.steps > 0,
           "ended": info["ended"], "seconds": round(res.seconds, 2), "steps": res.steps,
           "first_chunk_verdict": info["first_chunk_verdict"], "chunks_checked": info["chunks_checked"],
           "verdicts": info["verdicts"], "rejected": info["rejected"], "speed_scale": info["speed_scale"],
           "flags": list(res.flags), "chunks_used": res.executor.get("chunks_used"),
           "rtt_p50_ms": res.latency.get("rtt_p50_ms"), "rtt_p95_ms": res.latency.get("rtt_p95_ms"),
           "note": "real-arm deployment run: not recorded, not an eval trial; the arm holds at the end"}
    _finish(ctx, out)
    return {**out, "rollout": res.to_dict()}


__all__ = ["CheckedPolicy", "REAL_RUN_CONFIRM", "REAL_RUN_MAX_S", "REAL_RUN_PURPOSE", "RealRunRefused",
           "default_checker", "ensure_active", "guarded_episode", "real_arm_run", "real_checkpoint"]
