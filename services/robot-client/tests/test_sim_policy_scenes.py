"""[SIM] The scripted policy works on any valid generated pick-and-place scene, not only cube_plate.

Ticket 3 ("Pick up the cube and place it in the bin.") got a TDM-104 scene whose bin was a goal region on
the table. The planner's release height (table + half + 2.2 cm) sat below its "just grasped: lift" band, and
that test ("tool near the object", always true while holding it) won over the target: lower -> lift -> lower
until the 45 s limit. Fixtures: ``tests/fixtures/scenes/cube_bin.json`` (the generator's own output: a plate
named bin) and ``cube_bin_region.json`` (the region form). Needs the ``sim`` extra (mujoco).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("mujoco")

from rdf_common import scene_spec as ss  # noqa: E402
from rdf_robot_client.adapters.mujoco_sim import MujocoSimArm  # noqa: E402
from rdf_robot_client.sim_policy import PROFILES, ScriptedPickPlace, task_geometry, tool_pose  # noqa: E402

SCENES = Path(__file__).parent / "fixtures" / "scenes"
CUBE_BIN = str(SCENES / "cube_bin.json")
CUBE_BIN_REGION = str(SCENES / "cube_bin_region.json")


def _episode(sim: MujocoSimArm, pol: ScriptedPickPlace, profile, seconds: float = 30.0) -> dict:
    chunk, pos = None, 0
    for _ in range(int(seconds * 30)):
        st = sim.task_status()
        if st["success"] or st["failure"]:
            return st
        if chunk is None or pos >= 10:
            s = sim.privileged_state()
            chunk, _ = pol.plan([*s["joints"], s["gripper"]], s, profile)
            pos = 0
        a = chunk[pos]
        pos += 1
        with sim._lock:
            sim._seg_to = np.array(a[:6])
            sim._gripper_cmd = float(a[6])
        sim.step_for(1 / 30)
    return {"success": False, "failure": "timeout"}


def _rate(scene: str, profile: str, n: int, seed: int = 1) -> int:
    sim = MujocoSimArm(scene=scene, seed=seed, realtime=False)
    pol = ScriptedPickPlace()
    ok = 0
    for _ in range(n):
        sim._init_state()
        sim.reset_scene()
        ok += bool(_episode(sim, pol, PROFILES[profile])["success"])
    return ok


def test_fixtures_are_valid_generator_specs():
    for f in (CUBE_BIN, CUBE_BIN_REGION):
        spec = ss.validate(json.loads(Path(f).read_text()))
        assert ss.describe_success(spec) == "inside(cube, bin)"


@pytest.mark.parametrize("scene, kind, floor", [("cube_plate", "container", 0.012), (CUBE_BIN, "container", 0.012),
                                                (CUBE_BIN_REGION, "region", 0.0)])
def test_geometry_comes_from_the_spec(scene, kind, floor):
    sim = MujocoSimArm(scene=scene, seed=3, realtime=False)
    geo = task_geometry(sim.privileged_state())
    assert geo.pick_id == "cube" and geo.place_kind == kind
    assert geo.pick_half == pytest.approx(0.02) and geo.pick_width == pytest.approx(0.04)
    assert geo.floor_z == pytest.approx(floor, abs=1e-6)
    assert geo.grasp_z == pytest.approx(0.022, abs=0.002)  # the cube's centre (+2 mm)
    assert geo.place_z == pytest.approx(floor + 0.042, abs=1e-6)  # just above the floor it lands on
    assert geo.carry_z >= geo.wall_z + geo.pick_half + 0.03  # the carried cube clears the rim / walls
    target = sim.scene.container or sim.scene.success["region"]
    assert geo.place_id == target


def test_geometry_scales_with_the_object_and_the_target():
    """A tall cup: higher grasp and approach. A stack target: release just above the box's top face. A wall
    near the goal (a tall post beside the bin): the carry height clears it."""
    cup = {"version": 1, "task": "Put the cup in the bin.",
           "objects": [{"id": "cup", "primitive": "cylinder", "radius": 0.03, "height": 0.08, "pose": {"pos": [0.32, -0.13]},
                        "randomize": {"region": "spawn"}},
                       {"id": "bin", "primitive": "plate", "radius": 0.09, "height": 0.012, "pose": {"pos": [0.33, 0.15]}},
                       {"id": "post", "primitive": "box", "size": [0.04, 0.04, 0.12], "dynamic": False,
                        "pose": {"pos": [0.33, 0.28]}}],
           "regions": {"spawn": {"x": [0.27, 0.37], "y": [-0.2, -0.06]}},
           "success": {"type": "object_in", "object": "cup", "target": "bin"}}
    geo = task_geometry({"spec": ss.validate(cup), "objects": {}})
    assert geo.grasp_z == pytest.approx(0.042) and geo.pre_z == pytest.approx(0.13)
    assert geo.wall_z == pytest.approx(0.12) and geo.carry_z == pytest.approx(0.12 + 0.04 + 0.032)
    assert geo.place_z == pytest.approx(0.012 + 0.04 + 0.022)
    stack = json.loads(json.dumps(ss.CUBE_PLATE))
    stack["objects"] = [o for o in stack["objects"] if o["id"] in ("cube",)] + [
        {"id": "blue_block", "primitive": "box", "size": [0.05, 0.05, 0.04], "pose": {"pos": [0.33, 0.12]}}]
    stack["success"] = {"type": "object_on", "object": "cube", "target": "blue_block"}
    g2 = task_geometry({"spec": ss.validate(stack), "objects": {}})
    assert g2.place_kind == "surface" and g2.floor_z == pytest.approx(0.04) and g2.place_z == pytest.approx(0.082)


def test_held_over_a_region_target_releases_instead_of_lifting():
    """The live loop: holding the cube over the region bin, tool at 4 cm (the region's release height is
    4.2 cm, under the old 5 cm lift band) -> release, never lift."""
    sim = MujocoSimArm(scene=CUBE_BIN_REGION, seed=4, realtime=False)
    pol = ScriptedPickPlace()
    s = sim.privileged_state()
    geo = task_geometry(s)
    assert geo.place_z < geo.lift_below  # the geometry that looped the old planner
    px, py = geo.place_xy
    q = pol.kin.ik(tool_pose(px, py, 0.040, np.arctan2(py, px) + np.pi / 2), np.array(sim.home_joints))
    s = {**s, "joints": list(q), "gripper": 0.42, "task": {**s["task"], "held": True, "success": False},
         "scene_seq": 10_000}
    for prof in PROFILES.values():  # whatever the perceived target offset (<= ~3 sigma), never "lift"
        _, info = pol.plan([*q, 0.42], s, prof)
        assert info["phase"] in ("release", "lower"), info
    _, info = pol.plan([*q, 0.42], s, PROFILES["finetuned"])
    assert info["geometry"]["place_kind"] == "region"


@pytest.mark.parametrize("scene", ["cube_plate", CUBE_BIN, CUBE_BIN_REGION])
def test_finetuned_policy_completes_each_scene_with_real_physics(scene):
    sim = MujocoSimArm(scene=scene, seed=11, realtime=False)
    sim.reset_scene()
    st = _episode(sim, ScriptedPickPlace(), PROFILES["finetuned"])
    assert st["success"], st
    assert st["target_label"] == ("the plate" if scene == "cube_plate" else "the bin")


def test_calibration_holds_on_the_bin_scenes():
    """Same noise model on every scene: base checkpoint misses a fair share, the fine-tuned one almost never.
    (Full numbers: spikes/mujoco-sim/calibrate_sim_policy.py --episodes 40 --scene ...)"""
    for scene in (CUBE_BIN, CUBE_BIN_REGION):
        base, tuned = _rate(scene, "baseline", 10), _rate(scene, "finetuned", 10)
        assert 3 <= base <= 9, (scene, base)
        assert tuned >= 9, (scene, tuned)


def test_failure_notes_name_the_spec_object_and_target():
    sim = MujocoSimArm(scene=CUBE_BIN, seed=5, realtime=False)
    st = sim.task_status()
    assert (st["object_label"], st["relation"], st["target_label"]) == ("the red cube", "in", "the bin")
    region = MujocoSimArm(scene=CUBE_BIN_REGION, seed=5, realtime=False)
    assert region.task_status()["target_label"] == "the bin"
    plain = MujocoSimArm(scene="cube_plate", seed=5, realtime=False)  # no labels in the spec: the ids
    assert (plain.task_status()["object_label"], plain.task_status()["target_label"]) == ("the cube", "the plate")


def test_eval_auto_outcome_note_uses_the_spec_names():
    from rdf_robot_client.eval_trials import EvalTrialRunner

    sim = MujocoSimArm(scene=CUBE_BIN, seed=5, realtime=False)
    got: list[tuple] = []
    inbox = SimpleNamespace(verdict=None, submit=lambda ok, note, src, **kw: got.append((ok, note, src)))
    me = SimpleNamespace(sim_auto=True, inbox=inbox, sim_scene=sim, eval_run_id=9)
    EvalTrialRunner._sim_verdict(me, 0, SimpleNamespace(ended="max_seconds"))
    EvalTrialRunner._sim_verdict(me, 1, SimpleNamespace(ended="success_check"))
    assert got[0] == (False, "[SIM] auto outcome from sim state: the rollout hit its time limit with the red cube "
                             "not in the bin", "sim_auto")
    assert got[1][1] == "[SIM] auto outcome from sim state: the red cube is not in the bin"
    assert "plate" not in got[0][1] + got[1][1]
    # a missed grasp ended the rollout early, then the gripper re-opened: the note keeps the real reason
    me._sim_failure = "missed grasp: gripper closed on nothing"
    EvalTrialRunner._sim_verdict(me, 2, SimpleNamespace(ended="finish_requested"))
    assert got[2][1] == "[SIM] auto outcome from sim state: missed grasp: gripper closed on nothing"
