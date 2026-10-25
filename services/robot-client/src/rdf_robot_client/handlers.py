"""Default job handlers (TDM-49). Later tickets replace entries with ``@job_handler(type)``.

  home             implemented here: homing -> idle via ``arm.move_joints(home_joints)``.
  load_checkpoint  implemented here: remembers ``checkpoint_uri`` in ``client.checkpoint_uri``.
                   TDM-50 replaces it to warm the Modal policy endpoint.
  reset_request    implemented here: waiting_reset until the reset task completes or resume_policy.
  start_teleop     state only: teleop, arm held, ends on stop_teleop. TDM-52 replaces it with the
                   real teleop executor (WebSocket input -> ctx.run_control_loop).
  run_baseline     NOT implemented here (fails clearly): TDM-50 rollout + TDM-51 trial runner.
  run_eval         same. ``--sim`` registers ``sim_demo_rollout`` instead, which streams a
                   scripted motion with no policy and records no trials (says so in result_json).
"""
from __future__ import annotations

import math
from typing import Any

from .adapters.base import ArmState, JointTarget
from .jobs import JobContext, job_handler
from .state import RobotState


def _snapshot(ctx: JobContext) -> dict[str, Any]:
    st = ctx.arm.read_state()
    return {"joints": [round(v, 4) for v in st.joints], "gripper": round(st.gripper, 3), "ee_pose": st.ee_pose.to_dict()}


REAL_HOME_TOLERANCE_RAD = 0.05
REAL_HOME_SETTLE_S = 4.0


@job_handler("home")
def home(ctx: JobContext) -> dict[str, Any]:
    ctx.enter(RobotState.HOMING, "home")
    # A real arm under gravity settles a few hundredths of a radian off a commanded pose (J2/J3 carry the
    # load), so the sim's 0.01 rad / 2 s arrival check never passed on the YAM. Real arms: 0.05 rad (~3 deg)
    # within 4 s, and the full safety-layer cap (itself x the safety speed scale, and the adapter's own
    # <= 0.3 rad/s per joint) instead of a quarter of it.
    sim = "sim" in str(getattr(ctx.arm, "embodiment", "") or "")
    speed = float(ctx.payload.get("speed_fraction") or (0.25 if sim else 1.0))
    tol, settle = (0.01, 2.0) if sim else (REAL_HOME_TOLERANCE_RAD, REAL_HOME_SETTLE_S)
    arrived = ctx.arm.move_joints(ctx.arm.home_joints, speed_fraction=speed, should_abort=ctx.should_abort,
                                  tolerance=tol, settle_timeout=settle)
    ctx.check()  # aborted by e-stop/stop -> Cancelled
    if not arrived:
        raise RuntimeError("home: arm did not reach the home pose within tolerance")
    ctx.enter(RobotState.IDLE, "homed")
    return {"homed": True, **_snapshot(ctx)}


@job_handler("load_checkpoint")
def load_checkpoint(ctx: JobContext) -> dict[str, Any]:
    uri = ctx.payload.get("checkpoint_uri")
    if not uri:
        raise ValueError("load_checkpoint needs payload.checkpoint_uri")
    ctx.client.checkpoint_uri = str(uri)
    return {"checkpoint_uri": uri, "loaded": True, "note": "pointer stored; policy warm-up is TDM-50"}


@job_handler("reset_request")
def reset_request(ctx: JobContext) -> dict[str, Any]:
    from .adapters.mujoco_sim import find_sim

    sim = find_sim(ctx.arm)
    if sim is not None and ctx.payload.get("sim_reset"):  # [SIM] the simulator resets the scene itself
        out = sim.reset_scene()
        ctx.event("sim.scene_reset", via="reset_request", sim=True, note="[SIM] the simulator re-placed the objects", **out)
        return {"reset": "sim", **out}
    tid = ctx.payload.get("task_id")
    done = ctx.wait_for_reset(task_id=int(tid) if tid else None, accept_task_completion=True,
                              reason=str(ctx.payload.get("reason") or "reset_request"))
    return {"reset": done.to_dict()}


@job_handler("start_teleop")
def start_teleop(ctx: JobContext) -> dict[str, Any]:
    ctx.enter(RobotState.TELEOP, f"start_teleop {ctx.payload.get('kind') or 'clean_teleop'}")
    ctx.arm.hold()
    while not ctx.finish_requested.is_set():
        ctx.sleep(0.05)
    return {"teleop": "ended", "kind": ctx.payload.get("kind") or "clean_teleop",
            "note": "no teleop executor yet (TDM-52): the arm was held for the whole session"}


def _not_implemented(ctx: JobContext) -> dict[str, Any]:
    raise NotImplementedError(f"{ctx.command.type} needs the policy rollout (TDM-50) and eval trial runner (TDM-51)")


job_handler("run_eval")(_not_implemented)
job_handler("run_baseline")(_not_implemented)


def sim_demo_rollout(ctx: JobContext) -> dict[str, Any]:
    """``--sim`` only: a scripted 'rollout' (sine sweep around home). No policy, no eval trials.

    Payload: ``{"sim_seconds": float (default 20), "sim_reset_every_s": float?}``. With
    ``sim_reset_every_s`` it calls ``wait_for_reset`` on that period, like a trial runner would.
    """
    seconds = float(ctx.payload.get("sim_seconds") or 20.0)
    reset_every = ctx.payload.get("sim_reset_every_s")
    ctx.enter(RobotState.ROLLOUT, f"sim demo {ctx.command.type}")
    home = ctx.arm.home_joints
    ctx.arm.move_joints(home, speed_fraction=0.5, should_abort=ctx.should_abort)
    ctx.check()
    resets = 0

    def step(i: int, st: ArmState) -> JointTarget:
        t = i / ctx.arm.control_rate_hz
        q = list(home)
        q[0] += 0.5 * math.sin(2 * math.pi * 0.25 * t)
        q[1] += 0.2 * math.sin(2 * math.pi * 0.5 * t)
        return JointTarget(tuple(q), gripper=0.5 + 0.5 * math.cos(2 * math.pi * 0.2 * t))

    steps = 0
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, float(reset_every)) if reset_every else remaining
        stats = ctx.run_control_loop(step, max_seconds=chunk, active_state=RobotState.ROLLOUT)
        steps += stats.steps
        remaining -= chunk
        if reset_every and remaining > 0:
            ctx.wait_for_reset(reason="sim demo between segments")
            resets += 1
    return {"sim": True, "steps": steps, "seconds": seconds, "resets": resets,
            "note": "SIM demo motion: no policy ran and no eval trials were recorded"}


__all__ = ["home", "load_checkpoint", "reset_request", "sim_demo_rollout", "start_teleop"]
