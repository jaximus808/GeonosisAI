# YAM v1 + linear_4310 meshes for the web twin (TDM-97)

Source: i2rt-robotics/i2rt@120c3c8 (`i2rt/robot_models/arm/yam/v1`, `i2rt/robot_models/gripper/linear_4310`), MIT, see LICENSE.

Built by `spikes/tdm-97/build_yam_assets.py` from the SDK's own `combine_arm_and_gripper_xml(YAM, LINEAR_4310)`
model compiled in MuJoCo: each mesh geom's pose is baked into its body frame, then decimated
(~35% of the triangles, floor 1500). The kinematic tree is in
`apps/web/src/components/robotview/yamModel.ts`.

| node | tris (SDK) | tris (web) |
|---|---:|---:|
| base__base | 8754 | 4718 |
| link1__link1 | 6172 | 2160 |
| link2__link2 | 29914 | 10468 |
| link3__link3 | 30150 | 10552 |
| link4__link4 | 3708 | 1498 |
| link5__link5 | 1136 | 1136 |
| gripper__gripper | 8000 | 7466 |
| tip_left__tip_left | 8000 | 4172 |
| tip_right__tip_right | 8000 | 4468 |
| wrist_cam_mount__d405_wrist | 9390 | 5598 |
