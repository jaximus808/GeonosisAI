"""TDM-45 on-arm safety verification, step by step. A HUMAN WITH THE PHYSICAL E-STOP runs this.

    uv run python -m rdf_robot_client.safety_check                 # Jetson: YAM adapter, prompts before every step
    uv run python -m rdf_robot_client.safety_check --adapter sim --yes   # laptop dry run (CI-safe)

It needs NO SpacetimeDB: it builds the arm exactly like the client does (``main.build_arm``, so
the SafetyLayer with config/safety.yaml) and runs small, slow motions near the current pose:

  1. limits     print the effective limits; check the current pose is inside them
  2. watchdog   stream a slow joint-1 motion, cut the stream, measure time-to-hold (<= ~220 ms)
  3. jump       send a +0.5 rad joint-1 setpoint: must be REJECTED, no motion
  4. nan        send NaN joints: must be REJECTED, no motion
  5. estop      stream a slow motion, soft stop mid-motion: must halt (<50 ms call) and latch
  6. floor      (--floor-test only) jog the tool DOWN at 1 cm/s toward the table floor: it must
                stop at table_floor_z + table_clearance_m. Hold the e-stop; watch the gap.
  7. return     move back to the start pose slowly

Each step prints PASS/FAIL with numbers. Paste the output into the TDM-45 ticket.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from typing import Any

from .adapters.base import ArmError, ArmStopped
from .safety import SafetyLayer

DT = 1.0 / 30.0
results: list[dict[str, Any]] = []


def _report(step: str, ok: bool, **fields: Any) -> None:
    results.append({"step": step, "ok": ok, **fields})
    print(f"[{'PASS' if ok else 'FAIL'}] {step} {json.dumps(fields, default=str)}", flush=True)


def _confirm(msg: str, yes: bool) -> bool:
    print(f"\n>>> {msg}", flush=True)
    if yes:
        return True
    ans = input("    e-stop in hand? type 'go' to run, anything else skips: ").strip().lower()
    return ans == "go"


def _moved(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(abs(x - y) for x, y in zip(a, b))


def _stream_slow(arm: SafetyLayer, joint: int, rad_per_s: float, seconds: float, *, stop_at: float | None = None) -> float | None:
    """Stream a slow ramp on one joint; optionally call stop() after ``stop_at`` s. Returns stop() duration."""
    q = list(arm.joint_positions())
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        if stop_at is not None and time.monotonic() - t0 >= stop_at:
            ts = time.monotonic()
            arm.stop()
            return time.monotonic() - ts
        q[joint] += rad_per_s * DT
        arm.command_joints(q)
        time.sleep(DT)
    return None


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rdf_robot_client.safety_check", description=__doc__.split("\n")[0])
    p.add_argument("--adapter", default="yam")
    p.add_argument("--yes", action="store_true", help="do not prompt (sim / CI only)")
    p.add_argument("--floor-test", action="store_true", help="also jog the tool down to the table floor")
    p.add_argument("--joint", type=int, default=1, help="1-based joint to wiggle (default 1 = base yaw)")
    a = p.parse_args(argv)
    if a.yes and a.adapter != "sim":
        p.error("--yes is only allowed with --adapter sim: a human confirms every step on the real arm")
    from .main import build_arm

    arm = build_arm(a.adapter)
    if not isinstance(arm, SafetyLayer):
        print("FAIL: build_arm did not return the SafetyLayer", file=sys.stderr)
        return 2
    j = a.joint - 1
    arm.connect()
    start = arm.joint_positions()
    try:
        # 1. limits
        cfg = arm.config
        print(json.dumps(arm.summary(), indent=2, default=str))
        st = arm.read_state()
        inside_j = arm.limits.within(st.joints)
        viol = cfg.workspace.violation(st.ee_pose.xyz)
        _report("limits", inside_j and viol == 0.0, joints=[round(v, 3) for v in st.joints],
                ee_xyz=[round(v, 3) for v in st.ee_pose.xyz], joints_inside=inside_j, workspace_violation_m=round(viol, 4),
                verified=cfg.verified, speed_scale=arm.speed_scale)
        if not inside_j or viol > 0:
            print("    the arm starts OUTSIDE the configured limits: fix config/safety.yaml (or jog the arm) first."
                  " The layer only lets it move back toward the limits.")

        # 2. watchdog
        if _confirm(f"watchdog: joint {a.joint} creeps +0.1 rad/s for 0.5 s, then the stream is cut", a.yes):
            before = arm.stats()["watchdog_holds"]
            _stream_slow(arm, j, 0.1, 0.5)
            t_cut = time.monotonic() - DT  # the last command went out one period before the loop exited
            while arm.stats()["watchdog_holds"] == before and time.monotonic() - t_cut < 1.0:
                time.sleep(0.002)
            ms = (time.monotonic() - t_cut) * 1000
            p1 = arm.joint_positions()
            time.sleep(0.3)
            drift = _moved(p1, arm.joint_positions())
            _report("watchdog", arm.stats()["watchdog_holds"] == before + 1 and ms <= cfg.watchdog_ms + 40 and drift < 0.01,
                    time_to_hold_ms=round(ms, 1), drift_after_hold_rad=round(drift, 5), watchdog_ms=cfg.watchdog_ms)

        # 3. jump
        if _confirm(f"jump: send joint {a.joint} +0.5 rad in one tick (must be rejected, arm must not move)", a.yes):
            q0 = arm.joint_positions()
            q = list(q0)
            q[j] += max(0.5, cfg.max_jump_rad + 0.05)
            before = arm.stats()["rejected_jump"]
            arm.command_joints(q)
            time.sleep(0.3)
            moved = _moved(q0, arm.joint_positions())
            _report("jump", arm.stats()["rejected_jump"] == before + 1 and moved < 0.01, moved_rad=round(moved, 5))

        # 4. nan
        if _confirm("nan: send NaN joints (must be rejected, arm must not move)", a.yes):
            q0 = arm.joint_positions()
            before = arm.stats()["rejected_nonfinite"]
            arm.command_joints([math.nan] * arm.dof)
            time.sleep(0.2)
            moved = _moved(q0, arm.joint_positions())
            _report("nan", arm.stats()["rejected_nonfinite"] == before + 1 and moved < 0.01, moved_rad=round(moved, 5))

        # 5. soft stop
        if _confirm(f"estop: joint {a.joint} creeps -0.1 rad/s, soft stop after 0.4 s", a.yes):
            call_s = _stream_slow(arm, j, -0.1, 2.0, stop_at=0.4)
            p1 = arm.joint_positions()
            time.sleep(0.3)
            drift = _moved(p1, arm.joint_positions())
            try:
                arm.command_joints(p1)
                latched = False
            except ArmStopped:
                latched = True
            _report("estop", latched and drift < 0.01 and (call_s or 1) < 0.05,
                    stop_call_ms=round((call_s or 0) * 1000, 2), drift_after_stop_rad=round(drift, 5), latched=latched)
            if _confirm("clear the soft stop (only if the arm looks fine)", a.yes):
                arm.clear_stop()

        # 6. table floor
        if a.floor_test and _confirm("floor: the TOOL moves DOWN at 1 cm/s until the layer stops it at the floor", a.yes):
            pose = arm.ee_pose()
            z_floor = cfg.workspace.lo[2]
            n = int(max(0.0, pose.xyz[2] - z_floor + 0.03) / 0.01 * 30) + 30
            for k in range(1, n):
                try:
                    arm.command_ee(pose.translated((0.0, 0.0, -0.01 * k * DT)), seed=arm.last_joint_cmd)
                except ArmError as e:
                    print(f"    IK/arm error at step {k}: {e}")
                    break
                time.sleep(DT)
            z = arm.ee_pose().xyz[2]
            _report("floor", z >= z_floor - 0.005, tool_z=round(z, 4), floor_z=z_floor,
                    clamped_workspace=arm.stats()["clamped_workspace"],
                    note="measure the real gap to the table with a ruler and write it down")

        # 7. return
        if _confirm("return: move slowly back to the start pose", a.yes):
            ok = arm.move_joints(start, speed_fraction=0.5)
            _report("return", ok, error_rad=round(_moved(start, arm.joint_positions()), 4))
    except KeyboardInterrupt:
        arm.stop()
        print("\ninterrupted: soft stop latched", file=sys.stderr)
    finally:
        print("\nsafety counters:", json.dumps(arm.stats(), default=str))
        arm.hold()
        arm.disconnect()
    bad = [r["step"] for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)}/{len(results)} steps passed" + (f"; FAILED: {bad}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(run())
