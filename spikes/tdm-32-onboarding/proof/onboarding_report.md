Robot: test.tdm32-yam-sim.
Mode: sim.
Task: [SIM] pick up the red cup and put it in the grey bin.
Adapter: yam.
Adapter config: sim: true, arm_type: yam, gripper_type: linear_4310, kinematics: auto.
Safety profile: yam, safety_overrides: {}.
Why: this is the registered sim_yam robot, and the draft config matches the simulated arm and gripper.
Why: no override was needed, because the onboarding check already ran at speed_scale 0.25 with the yam profile.
Adapter version: yam-tdm46-i2rt-sim@120c3c8.
DOF: 6.
Joint names: joint1, joint2, joint3, joint4, joint5, joint6.
Home joints: 0.0, 0.98, 0.93, -0.73, 0.0, 0.0.
Home tool xyz: 0.3013, -0.0, 0.2016.
Joint lower limits: -2.61799, 0.0, 0.0, -1.69297, -1.5708, -2.0944.
Joint upper limits: 3.14159, 3.66519, 3.14159, 1.5708, 1.5708, 2.0944.
Contract live steps: connect x2; read_state/status; home via SafetyLayer (14.1s, arrived=True); command_ee 2 cm (streamed 1 s); gripper + hold; soft e-stop latch + clear; disconnect x2.
Contract home_err_rad: 0.0.
Contract ee_move_err_m: 0.0.
Contract estop_return_ms: 0.01.
Camera capture: top 91 frames, 0 dropped, 0.0% drop, 30.24 fps; wrist 90 frames, 0 dropped, 0.0% drop, 29.91 fps.
Camera sync: skew_ms_p50 7.49, skew_ms_p95 9.81, out_of_sync 0.
Camera warnings: top /dev/video0 can swap on reboot; wrist /dev/video2 can swap on reboot.
Safety profile + home tool point: verified false, tightened [], home_tool_xyz 0.3013, -0.0, 0.2016.
Safety warning: joints [3, 4, 5] use a layer cap above the adapter max_velocity, but the layer takes the lower value.
IK success rate: 1.0.
IK errors: pos_err_max_mm 0.0438, rot_err_max_mrad 0.6865, ik_ms_p50 0.55, ik_ms_p95 1.57.
Warnings mean the system is safe but not hardware-verified, and the camera device paths are index-based rather than by-id paths.
Success check: operator, top camera, red cup fully inside grey bin, 60 s limit.
Reset routine: home_joints above, gripper open, speed_fraction 0.25, then put the red cup back upright and keep the grey bin in place.
hil_required: yes.
Human still must verify the top camera scene and handle the real arm if it is ever moved out of simulation.
