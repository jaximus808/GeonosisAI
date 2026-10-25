# Real YAM arms on northstar: safety rules (from LARP)

These rules come from a read-only study of the user's LARP runtime (`~/northstar/LARP`), which drives these
same arms as `vrteleop-rh.service`. The arms are **already zeroed**. Breaking that is the main risk. Every
GeonosisAI change that touches the real arms must follow this page. Source paths are relative to `~/northstar/LARP`.

## Use LARP's i2rt fork, not stock `i2rt@120c3c8`

Stock 120c3c8, which GeonosisAI pinned for the sim and FK, is unsafe on these arms:

- `get_yam_robot` runs `check_motor_types=True, check_motor_config=True`. It can rewrite the CTRL_MODE register and
  **save it to motor flash** (`motor_check.py:612-650`).
- The first command after enable is torque-only with `kp=kd=0` (`dm_driver.py:488-492`), and gripper calibration
  sends torques with no gains on the arm joints (`utils.py:833`). Either can make **the arm sag or drop**.
- It defaults to `zero_gravity_mode=True` (backdrivable). Its J4 kp is 10, and its joint limits and gripper mount
  geometry differ from LARP's.

LARP vendors a patched fork in `i2rt/`: version 1.1.0 locally, and 1.1.1 in dists 0.2.8 and later. It never
writes registers, holds the measured pose on enable, and aborts calibration on drift. **The GeonosisAI `yam` adapter
on the Jetson must import that fork.** Use the version already installed for LARP on the Jetson, or `pip install` the
same `i2rt/` into the GeonosisAI venv. Stock 120c3c8 stays for the MuJoCo model and FK only.

## Never

1. Call `save_zero_position` (DM `0xFE`), `motor_config_tool/set_zero.py` or `set_timeout.py`, or run any
   register write or flash save (`0x55`).
2. Run stock 120c3c8 `get_yam_robot` on the real arms.
3. Run the Jetson's `~/zero_left_arm.py` / `~/zero_right_arm.py`. Their contents are unknown.
4. Re-zero because of a joint-limit error. The i2rt message suggests it; find the cause instead.
5. Run two programs on one CAN bus. That includes `vrteleop doctor`, which sends `0xFD` and can drop a live arm.
6. Send a large step, a raw first policy action, or a fixed home pose without a ramp. Step changes after a gap
   caused CAN comms failures and **arm collapse** in LARP (`examples/yam_teleop/molmoact_policy_client.py:236-266`).
7. Disable (`0xFD`) a moving or extended arm.
8. Loop `clean_error` (`0xFB`), or turn auto-recovery on.

## Do

1. **Take over the buses only after the user stops LARP.** The user runs `sudo systemctl stop vrteleop-rh`
   (and `vrteleop-relay` if present). Then confirm with `pgrep -af "vrteleop|yam_robot_relay|arm"` that nothing is
   left, and that the locks `/run/lock/vrteleop-arm-can_*.lock` are free (`flock -n`). The GeonosisAI client
   takes the **same flock** for its bus, so the two can never overlap. Agents never stop LARP.
2. **Set up the buses.** Use `can_right` (right arm) and `can_left` (left arm) at 1 Mbit/s SocketCAN, set by
   `vrteleop-can.service`. Check that both are `ERROR-ACTIVE`. `can1` is the Jetson's built-in port and is unused.
   Confirm which side is which while nothing else runs: read joints passively and move one arm by hand.
3. **Prepare the arms before enabling.** Park them folded at rest, inside the joint limits, hands off. The gripper
   calibrates at startup unless `gripper_limits_override` is passed. Read the joints passively first.
4. **Enable the way LARP does.** Enable motors 1 to 7 in id order (3 ms apart), with
   `enable_auto_recovery=False` and `zero_gravity_mode=False`. Refuse to start on any motor fault or out-of-limit
   joint. **Hold the measured pose first**, then ramp to any target in joint space, slowly (LARP's policy
   client uses at least 15 steps; ours caps at about 0.3 rad/s).
5. **Reuse LARP's values** (`i2rt/i2rt/robots/config/yam.yml`, `linear_4310.yml`, `robot_models/arm/yam/yam.xml`):
   - motors: ids 0x01–0x03 are DM4340 (J1–J3), 0x04–0x06 are DM4310 (J4–J6), 0x07 is the gripper `linear_4310`
   - feedback mode p16, all directions +1
   - `kp [80,80,80,40,10,10]`, `kd [5,5,5,1.5,1.5,1.5]`
   - `gravity_comp_factor [1.0,1.1,1.1,1.2,1.0,1.0]`, `grav_comp_kd [0.1,0.1,0.1,0.3,0.05,0.05]`
   - `coulomb_friction [0.3,0.3,0.3,0.06,0.06,0.06]`
   - gripper kp 20, kd 0.5, force limit 50 N, slide 0–0.0475 m
   - joint limits in rad:

     | J1 | J2 | J3 | J4 | J5 | J6 |
     |---|---|---|---|---|---|
     | −2.61799..3.05433 | 0..3.65 | 0..3.66519 | −1.5708..1.5708 | −1.5708..1.5708 | −2.0944..2.0944 |

     i2rt clips commands at these limits ±0.15 and aborts at ±0.25. The GeonosisAI safety box must sit
     **inside** them.
   - It is an error if gravity torque exceeds 25 Nm.
6. **Run the 250 Hz control thread at real-time priority** (SCHED_FIFO 10; needs CAP_SYS_NICE or `LimitRTPRIO`).
   Otherwise the GIL starves it and causes "fail to communicate with motor N".
7. **Shut down the way LARP does.** Switch to damping only (`kp=0`, `kd=max(kd, grav_comp_kd)`), wait until
   every joint is under 0.05 rad/s for 0.3 s (at most 4.5 s), then disable (`0xFD`) and close CAN. If it is
   still moving after 4.5 s, stay enabled in damping. **After a settled release the arms go limp**, so fold or
   support them first. Upstream `close()` is unsafe: it does not do this.
8. **After any crash, expect latched fault `0xD`.** Power-cycle the arms rather than looping clears.

## Cameras

- **Overhead (exo):** a RealSense D435, serial `420222071940`. This comes from an August LARP launch script
  marked VERIFY.
- **Wrists:** two D405s. Their serials are only on the Jetson, in `~/vrteleop-rh/robot.toml`; read that file
  read-only.
- **Format:** LARP runs 640x360, opens at 30 fps bgr8, and sends exo at 15 fps and wrists at 8 fps.
- **D405 firmware:** 5.12.14.100 opens but delivers no frames. 5.15.1.55 is needed.

## GeonosisAI's own layers on top

GeonosisAI adds these above the rules here:

- **Teleop:** it is relative deltas from the measured pose at clutch-down, not absolute positions
  (`services/robot-client/src/rdf_robot_client/teleop.py`).
- **TDM-45 SafetyLayer:** joint, velocity and acceleration caps; jump rejection at 0.3 rad; a workspace box;
  a 200 ms watchdog.
- **Motion only after sign-off:** no motion until the user signs off TDM-119 with the hardware e-stop in hand.
