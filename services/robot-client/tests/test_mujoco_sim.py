"""[SIM] MuJoCo YAM: scene specs, physics adapter, scripted sim policy, practice sessions.

Needs the ``sim`` extra (mujoco); skipped without it (CI's plain ``uv sync`` has no mujoco).
"""
from __future__ import annotations

import json
import math
import types

import numpy as np
import pytest

pytest.importorskip("mujoco")

from rdf_robot_client.adapters import make_adapter  # noqa: E402
from rdf_robot_client.adapters.mujoco_scene import build_scene_xml, load_scene  # noqa: E402
from rdf_robot_client.adapters.mujoco_sim import MujocoSimArm, find_sim  # noqa: E402
from rdf_robot_client.sim_policy import PROFILES, ScriptedPickPlace, profile_for  # noqa: E402


def test_cube_plate_scene_loads_and_builds():
    sc = load_scene("cube_plate")
    assert sc.target == "cube" and sc.container == "plate"
    assert set(sc.objects) == {"cube", "plate", "full_plate", "filler"}
    assert sc.resting_z("cube") == pytest.approx(0.02)
    assert sc.place_top_z() == pytest.approx(0.012)
    xml = build_scene_xml(sc)
    assert 'name="cube_free"' in xml and 'name="wrist"' in xml and 'name="top"' in xml


def test_scene_spec_is_data_not_code():
    spec = json.loads(json.dumps(load_scene("cube_plate").spec))
    spec["objects"][0].update(primitive="cylinder", radius=0.02, height=0.05)
    spec["objects"][0].pop("size")
    spec["objects"].append({"id": "ball", "primitive": "sphere", "radius": 0.02, "dynamic": True, "pose": {"pos": [0.4, 0.1]}})
    sc = load_scene(spec)
    assert sc.objects["ball"]["pose"]["pos"][2] == pytest.approx(0.02)  # z defaults to resting on the table
    sim = MujocoSimArm(scene=sc, seed=1, realtime=False)
    st = sim.scene_state()
    assert set(st["objects"]) == {"cube", "plate", "full_plate", "filler", "ball"}
    assert st["objects"]["cube"]["primitive"] == "cylinder"
    with pytest.raises(ValueError):  # SceneError, or TDM-104's SceneSpecError (both ValueError)
        load_scene({"objects": [{"id": "x", "primitive": "torus"}]})
    with pytest.raises(ValueError):
        load_scene({**spec, "success": {"type": "object_in", "object": "nope", "target": "plate"}})


def test_reset_scene_randomizes_inside_the_region():
    sim = MujocoSimArm(seed=3, realtime=False)
    reg = sim.scene.regions["cube_spawn"]
    seen = set()
    for _ in range(5):
        out = sim.reset_scene()
        x, y, _yaw = out["objects"]["cube"]
        assert reg["x"][0] <= x <= reg["x"][1] and reg["y"][0] <= y <= reg["y"][1]
        seen.add((x, y))
        assert out["cube"]["x"] == x  # back-compat key
    assert len(seen) == 5
    sim.step_for(0.3)
    assert sim.privileged_state()["pick"]["xyz"][2] == pytest.approx(0.02, abs=0.002)


def test_scene_state_generic_and_back_compat_through_the_state_contract():
    from rdf_robot_client.stream import scene_for_contract

    sim = MujocoSimArm(seed=1, realtime=False)
    out = scene_for_contract(sim.scene_state())
    assert {"cube", "plate", "objects"} <= set(out)
    assert set(out["objects"]) == {"cube", "plate", "full_plate", "filler"}
    assert len(out["objects"]["cube"]["pos"]) == 3 and len(out["objects"]["cube"]["quat"]) == 4


def test_adapter_through_safety_layer_with_physics_thread():
    from rdf_robot_client.main import build_arm

    arm = build_arm("mujoco")
    assert find_sim(arm) is not None and arm.describe()["sim"] is True and arm.embodiment == "sim_yam"
    arm.connect()
    try:
        assert arm.home(speed_fraction=0.8)
        st = arm.read_state()
        assert st.ee_pose.xyz[2] == pytest.approx(0.2016, abs=0.01)
        arm.stop()
        from rdf_robot_client.adapters import ArmStopped

        with pytest.raises(ArmStopped):
            arm.command_joints(st.joints)
        arm.clear_stop()
        assert find_sim(arm).status().detail["physics_hz"] > 150
    finally:
        arm.disconnect()


def _episode(sim: MujocoSimArm, pol: ScriptedPickPlace, profile, seconds: float = 25.0) -> dict:
    chunk, pos = None, 0
    for step in range(int(seconds * 30)):
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


def test_scripted_policy_picks_and_places_with_real_physics():
    sim = MujocoSimArm(seed=11, realtime=False)
    pol = ScriptedPickPlace()
    sim.reset_scene()
    st = _episode(sim, pol, PROFILES["finetuned"])
    assert st["success"], st


def test_profiles_by_checkpoint():
    assert profile_for("gs://openpi-assets/checkpoints/pi05_base").name == "baseline"
    assert profile_for(None).name == "baseline"
    assert profile_for("fake://tdm28/checkpoints/run-3").name == "finetuned"
    assert PROFILES["baseline"].pos_sigma_m > PROFILES["finetuned"].pos_sigma_m


def test_missed_grasp_is_a_failure():
    sim = MujocoSimArm(seed=2, realtime=False)
    sim.reset_scene()
    # close the gripper on nothing, low over the table, far from the cube
    from rdf_robot_client.sim_policy import tool_pose

    q = sim.kin.ik(tool_pose(0.35, 0.10, 0.03, math.pi / 2), sim.home_joints)
    with sim._lock:
        sim._seg_to = np.array(q)
        sim._gripper_cmd = 1.0
    sim.step_for(1.5)
    with sim._lock:
        sim._gripper_cmd = 0.0
    sim.step_for(1.0)
    assert sim.task_status()["failure"].startswith("missed grasp")


def test_sim_policy_server_answers_in_the_tdm41_format():
    from rdf_robot_client.sim_policy_server import SimPolicy

    sim = MujocoSimArm(seed=4, realtime=False)
    sim.reset_scene()
    pol = SimPolicy("http://unused", latency_ms=0, jitter_ms=0)

    class _Http:
        def get(self, url):
            return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: sim.privileged_state())

    pol.http = _Http()
    s = sim.privileged_state()
    out = pol.answer({"state": [*s["joints"], s["gripper"]], "images": [], "prompt": "p"})
    assert out["action_space"] == "joint_pos_abs" and len(out["actions"]) == 50 and len(out["actions"][0]) == 7
    assert out["sim"] is True and out["profile"] == "baseline"
    act = pol.activate({"checkpoint_uri": "fake://tdm28/checkpoints/run-9"})
    assert act["profile"] == "finetuned" and "NOT pi0.5" in act["policy"]
    mism = pol.answer({"state": out["actions"][0], "checkpoint_uri": "gs://other"})
    assert mism["code"] == "checkpoint_mismatch"


def test_practice_tokens_only_on_practice_enabled_hubs():
    from rdf_common.stream_token import StreamClaims

    from rdf_robot_client.teleop import TeleopConfig, TeleopHub, _OpsGuard

    claims = StreamClaims(robot_id=1, robot_name="sim", task_id=0, sub="practice:operator", iat=0, exp=9e9, cams=None)
    client = types.SimpleNamespace(services={}, http=None, sql=lambda q: [])
    on = TeleopHub(client, config=TeleopConfig(allow_practice=True))
    assert isinstance(on.make_guard(claims), _OpsGuard)
    off = TeleopHub(client, config=TeleopConfig(allow_practice=False))
    with pytest.raises(Exception):  # falls through to the task-lease guard, which needs a SpacetimeDB client
        off.make_guard(claims)


def test_make_adapter_mujoco():
    arm = make_adapter("mujoco", seed=1, realtime=False)
    assert arm.name == "mujoco" and arm.scene.name == "cube_plate"
