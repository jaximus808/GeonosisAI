# I2RT YAM model assets (vendored for the [SIM] MuJoCo robot)

Source: https://github.com/i2rt-robotics/i2rt at commit `120c3c8` (release v1.3.6), MIT License (`LICENSE`
in this folder, copyright I2RT Robotics). Only the files the simulator needs are copied, unchanged:

| here | upstream |
|---|---|
| `assets/yam_v1/{base,link1..link5}.stl` | `i2rt/robot_models/arm/yam/v1/assets/` |
| `assets/linear_4310/{gripper,tip_left,tip_right}.stl` | `i2rt/robot_models/gripper/linear_4310/assets/` |
| `assets/station/d405_wrist_linear_4310.stl` | `i2rt/robot_models/station/assets/` |
| `yam_linear_4310.xml` | output of the SDK's `i2rt.robots.utils.combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310)` at that commit, with the absolute mesh paths rewritten to `assets/...` |

Everything the simulation adds (actuators, gravity compensation, finger pad contacts, the wrist camera
mount, the table and objects) is generated at load time by `adapters/mujoco_scene.py` from a scene spec
(`../scenes/*.json`); these files stay byte-for-byte as upstream ships them. To refresh: clone the SDK at
the new commit, re-run `combine_arm_and_gripper_xml`, copy the meshes, and re-run `tests/test_mujoco_sim.py`
plus `tests/test_tdm46_yam.py` (PoE vs MuJoCo FK must still agree).
