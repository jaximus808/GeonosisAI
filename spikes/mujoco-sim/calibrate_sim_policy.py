"""Offline calibration of the [SIM] scripted policy's noise profiles (no real time, no server).

    cd services/robot-client && uv run --extra sim python ../../spikes/mujoco-sim/calibrate_sim_policy.py --episodes 40

Runs N episodes per profile on the same N random scenes (MuJoCo physics, 30 Hz control, 10-step
re-query like the TDM-50 executor) and prints the success rate per profile.
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np

from rdf_robot_client.adapters.mujoco_sim import MujocoSimArm
from rdf_robot_client.sim_policy import PROFILES, ScriptedPickPlace


def run_episode(sim: MujocoSimArm, pol: ScriptedPickPlace, profile, seconds: float = 25.0, requery: int = 10,
                verbose: bool = False) -> dict:
    sim.hold()
    chunk, pos = None, 0
    phases = []
    for step in range(int(seconds * 30)):
        st = sim.task_status()
        if st["success"] or st["failure"]:
            return {"success": st["success"], "failure": st["failure"], "t": round(step / 30, 2), "phases": phases}
        if chunk is None or pos >= requery:
            s = sim.privileged_state()
            state = [*s["joints"], s["gripper"]]
            chunk, info = pol.plan(state, s, profile)
            pos = 0
            if not phases or phases[-1] != info["phase"]:
                phases.append(info["phase"])
        a = chunk[pos]
        pos += 1
        with sim._lock:
            sim._seg_to = np.array(a[:6])
            sim._gripper_cmd = float(a[6])
        sim.step_for(1 / 30)
    return {"success": False, "failure": "timeout", "t": seconds, "phases": phases}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    out = {}
    for name, prof in PROFILES.items():
        sim = MujocoSimArm(seed=a.seed, realtime=False)
        pol = ScriptedPickPlace()
        res = []
        t0 = time.time()
        for ep in range(a.episodes):
            sim._init_state()
            sim.reset_scene()
            r = run_episode(sim, pol, prof)
            res.append(r)
            if a.verbose:
                print(name, ep, json.dumps(r))
        ok = sum(r["success"] for r in res)
        fails: dict[str, int] = {}
        for r in res:
            if not r["success"]:
                fails[r["failure"]] = fails.get(r["failure"], 0) + 1
        ts = [r["t"] for r in res if r["success"]]
        out[name] = {"success": ok, "of": a.episodes, "rate": round(ok / a.episodes, 3), "failures": fails,
                     "mean_success_s": round(float(np.mean(ts)), 2) if ts else None, "wall_s": round(time.time() - t0, 1)}
        print(name, json.dumps(out[name]), flush=True)


if __name__ == "__main__":
    main()
