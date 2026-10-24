"""TDM-104 scene spec: validation, normalization and the success predicate."""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from rdf_common import scene_spec as ss

ROBOT_SCENES = Path(__file__).resolve().parents[2] / "robot-client" / "src" / "rdf_robot_client" / "sim_assets" / "scenes"

TDM104_STACK = {
    "version": 1, "name": "stack_blocks", "task": "Stack the red block on the blue block.",
    "objects": [
        {"id": "red_block", "primitive": "box", "size": [0.04, 0.04, 0.04], "color": "#d42020", "mass": 0.05,
         "dynamic": True, "pose": {"pos": [0.30, -0.12], "yaw": 0.0},
         "randomize": {"region": "red_spawn", "yaw": [-0.5, 0.5]}},
        {"id": "blue_block", "primitive": "box", "size": [0.05, 0.05, 0.05], "color": "#2040d0", "mass": 0.08,
         "dynamic": True, "pose": {"pos": [0.32, 0.14, 0.2], "yaw": 0.3}},
    ],
    "regions": {"red_spawn": {"x": [0.24, 0.36], "y": [-0.20, -0.06]}},
    "success": {"type": "object_on", "object": "red_block", "target": "blue_block"},
}


def tdm104_errors(raw) -> list[str]:
    with pytest.raises(ss.SceneSpecError) as e:
        ss.validate(raw)
    return e.value.errors


def test_default_scene_is_valid_and_matches_the_sim_file():
    d = ss.default_spec()
    assert ss.describe_success(d) == "inside(cube, plate)"
    on_disk = ss.validate(json.loads((ROBOT_SCENES / "cube_plate.json").read_text()))
    assert [o["id"] for o in on_disk["objects"]] == [o["id"] for o in d["objects"]]
    assert on_disk["success"] == d["success"]


def test_validate_is_idempotent_and_fills_defaults():
    s = ss.validate(TDM104_STACK)
    assert s == ss.validate(s)
    assert set(s["cameras"]) >= {"top", "wrist"} and s["table"]["size"] == [0.96, 0.96]
    assert s["robot"]["mount"] == {"pos": [0.0, 0.0, 0.0], "yaw": 0.0}
    assert s["objects"][0]["color"][:3] == pytest.approx([0.8314, 0.1255, 0.1255], abs=1e-3)  # hex -> RGBA
    assert s["objects"][0]["graspable"] is True  # the task object


def test_z_is_snapped_to_rest_and_logged():
    s = ss.validate(TDM104_STACK)
    red, blue = s["objects"]
    assert red["pose"]["pos"][2] == pytest.approx(0.02)  # [x, y] -> resting on the table
    assert blue["pose"]["pos"][2] == pytest.approx(0.025)  # 0.2 was in the air
    assert any("blue_block" in f for f in s["meta"]["fixes"])


def test_stacked_object_rests_on_its_support():
    raw = copy.deepcopy(TDM104_STACK)
    raw["objects"].append({"id": "lid", "primitive": "box", "size": [0.03, 0.03, 0.01], "color": "#00ff00",
                           "dynamic": True, "pose": {"pos": [0.32, 0.14, 0.9]}})
    s = ss.validate(raw)
    assert s["objects"][2]["pose"]["pos"][2] == pytest.approx(0.05 + 0.005)


@pytest.mark.parametrize("mutate, needle", [
    (lambda r: r.update(version=2), "$.version"),
    (lambda r: r.update(bogus=1), "$.bogus: unknown key"),
    (lambda r: r["objects"][0].update(primitive="torus"), "primitive"),
    (lambda r: r["objects"][0].update(size=[0.04, 0.04]), "size"),
    (lambda r: r["objects"][0].update(color="red"), "color"),
    (lambda r: r["objects"][1].update(id="red_block"), "duplicate id"),
    (lambda r: r["objects"][1].update(id="Blue Block"), "slug"),
    (lambda r: r["objects"][0]["pose"].update(pos=[0.9, 0.0]), "from the robot base"),
    (lambda r: r["objects"][0]["pose"].update(pos=[0.05, 0.0]), "from the robot base"),
    (lambda r: r["objects"][1]["pose"].update(pos=[0.30, -0.09]), "overlaps"),
    (lambda r: r["objects"][0].update(size=[0.12, 0.12, 0.04]), "the gripper needs"),
    (lambda r: r["objects"][0].update(mass=1.5), "friction grasp"),
    (lambda r: r["objects"][0].update(randomize={"region": "nowhere"}), "randomize.region"),
    (lambda r: r["success"].update(object="ghost"), "$.success.object"),
    (lambda r: r["success"].update(target="ghost"), "$.success.target"),
    (lambda r: r["success"].update(target="red_block"), "must differ"),
    (lambda r: r["success"].update(type="near"), "$.success.type"),
    (lambda r: r["success"].update(region="red_spawn"), "exactly one of target"),
    (lambda r: r.update(robot={"mount": {"pos": [0.1, 0, 0], "yaw": 0}}), "robot.mount"),
    (lambda r: r["regions"]["red_spawn"].update(x=[0.36, 0.24]), "lo < hi"),
    (lambda r: r.update(cameras={"top": {"fovy": 50}}), "world camera needs pos"),
])
def test_invalid_specs_are_refused_with_a_path(mutate, needle):
    raw = copy.deepcopy(TDM104_STACK)
    mutate(raw)
    errs = tdm104_errors(raw)
    assert any(needle in e for e in errs), errs


def test_spawn_region_overlapping_the_goal_is_refused():
    raw = copy.deepcopy(ss.CUBE_PLATE)
    raw["regions"]["cube_spawn"] = {"x": [0.25, 0.37], "y": [0.10, 0.25]}
    assert any("overlaps the goal" in e for e in tdm104_errors(raw))


def test_already_solved_scene_is_refused():
    raw = copy.deepcopy(ss.CUBE_PLATE)
    del raw["objects"][0]["randomize"]
    raw["objects"][0]["pose"]["pos"] = [0.31, 0.19]
    assert any("already true" in e for e in tdm104_errors(raw))


def test_inside_plate_predicate():
    d = ss.default_spec()
    on_plate = {"cube": {"pos": [0.32, 0.20, 0.012 + 0.02]}}
    assert ss.evaluate_success(d, on_plate)["success"]
    assert not ss.evaluate_success(d, on_plate, held={"cube"})["success"]  # still in the hand
    r = ss.evaluate_success(d, {"cube": {"pos": [0.32, 0.20, 0.10]}})  # above the plate, not resting
    assert not r["success"] and r["over"] and not r["resting"]
    r = ss.evaluate_success(d, {"cube": {"pos": [0.31 + 0.07, 0.19, 0.032]}})  # on the rim
    assert not r["success"] and not r["over"]
    assert not ss.evaluate_success(d, {"cube": {"pos": [0.47, -0.05, 0.032]}})["success"]  # the WRONG plate
    rel = copy.deepcopy(d)
    rel["success"]["released"] = False
    assert ss.evaluate_success(rel, on_plate, held={"cube"})["success"]


def test_on_top_predicate_follows_the_live_target():
    s = ss.validate(TDM104_STACK)
    assert ss.describe_success(s) == "on_top(red_block, blue_block)"
    moved = {"blue_block": {"pos": [0.25, 0.05, 0.025]}, "red_block": {"pos": [0.255, 0.055, 0.05 + 0.02]}}
    assert ss.evaluate_success(s, moved)["success"]
    assert not ss.evaluate_success(s, {**moved, "red_block": {"pos": [0.32, 0.14, 0.07]}})["success"]  # old spot
    assert not ss.evaluate_success(s, {"red_block": {"pos": [0.32, 0.14, 0.02]}})["success"]  # on the table


def test_region_predicate():
    raw = {"version": 1, "task": "Push the ball into the green zone.",
           "objects": [{"id": "ball", "primitive": "sphere", "radius": 0.025, "color": "#ff8800", "dynamic": True,
                        "pose": {"pos": [0.28, -0.12]}}],
           "regions": {"goal": {"x": [0.30, 0.42], "y": [0.08, 0.20], "visible": True, "color": "#20c040"}},
           "success": {"type": "object_in", "object": "ball", "region": "goal"}}
    s = ss.validate(raw)
    assert s["name"] == "push_the_ball_into_the_green_zon"[:32].strip("_")
    assert ss.evaluate_success(s, {"ball": {"pos": [0.36, 0.14, 0.025]}})["success"]
    assert not ss.evaluate_success(s, {"ball": {"pos": [0.36, 0.25, 0.025]}})["success"]
    assert ss.describe_success(s) == "inside(ball, goal)"


def test_sample_poses_stay_in_region_and_never_overlap():
    d = ss.default_spec()
    rng = random.Random(7)
    for _ in range(50):
        p = ss.sample_poses(d, rng)
        x, y, z = p["cube"]["pos"]
        assert 0.25 <= x <= 0.37 and -0.21 <= y <= -0.07 and z == pytest.approx(0.02)
        assert not ss.evaluate_success(d, p)["success"]


def test_from_llm_round_trip():
    ans = {"name": "stack_blocks", "task": "Stack the red block on the blue block.", "notes": "two blocks",
           "table_color": None,
           "objects": [
               {"id": "red_block", "label": "the red block", "primitive": "box", "size": [0.04, 0.04, 0.04],
                "radius": None, "height": None, "color": "#d42020", "rim_color": None, "mass": 0.05, "dynamic": True,
                "x": 0.30, "y": -0.12, "z": None, "yaw": 0.0, "spawn_region": "red_spawn", "spawn_yaw": [-0.5, 0.5]},
               {"id": "blue_block", "label": "the blue block", "primitive": "box", "size": [0.05, 0.05, 0.05],
                "radius": None, "height": None, "color": "#2040d0", "rim_color": None, "mass": 0.08, "dynamic": True,
                "x": 0.32, "y": 0.14, "z": None, "yaw": 0.3, "spawn_region": None, "spawn_yaw": None}],
           "regions": [{"name": "red_spawn", "x": [0.24, 0.36], "y": [-0.20, -0.06], "visible": False, "color": None}],
           "success": {"type": "object_on", "object": "red_block", "target": "blue_block", "region": None}}
    s = ss.validate(ss.from_llm(ans))
    assert ss.describe_success(s) == "on_top(red_block, blue_block)"
    assert s["objects"][0]["randomize"] == {"region": "red_spawn", "yaw": [-0.5, 0.5]}


def test_llm_schema_is_strict_compatible():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(ss.LLM_SCHEMA)
