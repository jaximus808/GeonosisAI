# TDM-119: phone teleop on the real right YAM (northstar), step by step

**The user runs every step, at the arm, with the hardware e-stop in reach.** Agents never enable motors or
command motion. Rules: `docs/real-arm-safety.md` (from LARP). Hardware facts: `docs/jetson-inventory.md`.

Run every command from the laptop exactly as written. The single quotes make `~` and `&&` expand **on the
Jetson**. Commands with `-t` need a terminal: they prompt, or they follow logs.
`R=~/geonosis/app/scripts/jetson/remote` below is shorthand only: the commands spell the path out.

What runs (all installed by `scripts/jetson/tdm118_geonosis.sh install`, user-level, with no sudo and nothing of vrteleop's touched):
- `tdm119_rt.sh <step>` runs one bring-up step interactively under the user systemd manager. That gives the fork's
  CAN thread real-time priority.
- Arm: `LarpSafeYamArm` (LARP's i2rt fork 1.1.1) inside the TDM-45 SafetyLayer.
- Read-only client: `geonosis-robot.service`. Teleop client: `geonosis-robot-teleop.service`. Both are user units,
  both are disabled, and they conflict with each other.

## Speeds (all apply, the slowest wins)
| layer | value at the start | where |
|---|---|---|
| teleop EE speed | `RDF_TELEOP_MAX_LIN_SPEED` 0.10 m/s x the phone's speed_scale (default 0.5) = **5 cm/s**, 10 cm/s at 100 % | `~/.config/geonosis/teleop.env` |
| teleop rotation | 0.5 rad/s x speed_scale | same |
| SafetyLayer | `speed_scale` **0.25** x yam `max_velocity` 0.8-1.2 rad/s = 0.2-0.3 rad/s per joint (cap 0.3 until verified) | `RDF_SAFETY_SPEED_SCALE`, safety.yaml |
| LarpSafeYamArm | every setpoint <= **0.3 rad/s** per joint, a straight line in joint space from the last setpoint | `YAM_MAX_JOINT_SPEED` (can only go lower) |
| watchdog | no command for 200 ms while clutched -> hold | safety.yaml / TeleopConfig |

RTPRIO: the user manager runs with `LimitRTPRIO=20` (from `/etc/security/limits.d/90-vrteleop-rtprio.conf`), so the
units and `tdm119_rt.sh` get SCHED_FIFO 10 for the 250 Hz CAN thread. Plain ssh shells have 0. Without it, the fork
logs `Could not set SCHED_FIFO priority ... running at normal priority` and carries on. That is a warning, not a
refusal. `hold` and `jog` print `rt_threads` (it should be 1 or more).

Cameras (`services/robot-client/config/cameras.yaml`): RealSense colour nodes, YUYV 640x360 at 15 fps.
- top = exo D435 `420222071940`, by-path `...1.1.3:1.3-video-index0` (video16).
- wrist = wristR D405 `353322271521`, by-id `...Camera_405_344123071140-video-index4` (video4).
- Read the D405 firmware once with `rs-enumerate-devices -s` (step 0). LARP needs 5.15.1.55 or newer; 5.12.14.100 sends no frames.

---

## Step 0: free the hardware and preflight (no CAN traffic)
**Physical:** fold **both** arms at rest. Clear the table. Hardware e-stop in reach. When vrteleop stops it releases
its motors, so the arms go limp: support them if they are not folded.

```
ssh -t northstar@northstar 'sudo systemctl stop vrteleop-rh'
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh preflight'
ssh -t northstar@northstar 'rs-enumerate-devices -s'
```
**See:**
- `[PASS]` on: vrteleop-rh stopped, no other arm process, both flocks free, `can_right ERROR-ACTIVE` and
  `YAM_CAN_CHANNEL is can_right`.
- `[INFO] RTPRIO limit` soft and hard 20. `[INFO] safety profile in use`: repo copy, `verified: false`.
- rs-enumerate-devices lists the three cameras with their firmware.

**Abort if:** any `[FAIL]`. Notably: `can_right` is not ERROR-ACTIVE (ERROR-PASSIVE / BUS-OFF: power-cycle the arm,
don't loop clears), a vrteleop/arm process is still alive, or a lock is held. Also abort if a D405 shows firmware 5.12.14.100.

## Step 1: passive joint read (done; repeat per arm if wanted)
**Physical:** move one arm by hand during the read, then the other.
```
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh passive-read 10 --channel can_right'
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh passive-read 10 --channel can_left'
```
**See:** 2 lines/s, `motor_states` all `disabled`, `fault: null`, `frames_sent` rising. `moved` changes only on
the bus of the arm you are moving (`can_right` = right arm).
**Abort if:** a fault, `joints: null`, or the wrong arm's bus moves. Fix the mapping before step 2.

## Step 2: enable and hold the right arm, then release
**Physical:** right arm folded at rest, left arm folded (its bus is never opened). **Fingers clear of the gripper.**
One hand on the e-stop. The other hand ready under the right forearm for the release.
```
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh hold --seconds 10'
```
Type `go`. What happens:
1. `LarpSafeYamArm` checks the fork (refuses stock i2rt) and takes the `can_right` lock.
2. The fork enables motors 1 to 7 in id order and holds the **measured** pose: no motion.
3. **Gripper calibration:** no gripper limits are stored, so the fork calibrates the linear_4310. This is what
   vrteleop does at every start. The gripper alone opens and closes once at low torque while the arm holds, and
   the fork aborts if the arm drifts. That is what `YAM_ALLOW_GRIPPER_CALIBRATION=1` in `teleop.env` allows.
   Without it, connect refuses. To skip it next time, put the printed `YAM_GRIPPER_LIMITS=closed,open` in
   `~/.config/geonosis/teleop.env` and delete the ALLOW line.
4. `[PASS] enabled and holding ...` with `fork 1.1.1`, `gripper_limits`, `rt_threads >= 1`. Then 10 `[PASS] holding`
   lines with `drift_rad < 0.02`.
5. The `RELEASE` prompt: hand under the arm, press Enter. The fork damps until every joint is below 0.05 rad/s for
   0.3 s (at most 4.5 s), then disables motor by motor. **The arm goes limp.** Then `[PASS] hold step`.

**Abort (hardware e-stop) if:** the arm moves on enable beyond a millimetre-scale settle, it sags while holding, or
it buzzes or oscillates. **Stop and report, don't retry** if you see `enable refused` (motor fault, joint outside
LARP's limits, gains, lock held, stock i2rt), `fail to communicate with motor`, or `still moving, left in damping`
(support the arm; it stays in damping).
Never re-zero because of a limit error.

## Step 3: tiny jog through teleop's path, then the soft e-stop test
**Physical:** as in step 2, plus the space in front of the arm cleared to 40 cm forward and 30 cm up.
```
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh jog --ready --rest-at-end'
```
Type `go` at each prompt: enable, READY, E-STOP TEST, CLEAR, REST. Then press Enter to release.
1. **READY:** a joint-space move from the fold to `(0, 0.98, 0.93, -0.73, 0, 0)`, with the tool about 30 cm forward
   and 20 cm up, at the safety layer's 0.2 rad/s (**about 35 s**). It is needed because the folded rest has
   J2/J3 below the safe range. **See** `[PASS] ready pose`.
2. **JOG:** +2 cm along base **+x (straight forward, away from the base)** at 2 cm/s through `command_ee`
   (IK + SafetyLayer, the same path teleop uses), then back. **See** `[PASS] +2 cm x` with `moved_x_m ~ 0.020` and
   `off_axis_m < 0.005`, then `[PASS] -2 cm x (back)`.
3. **E-STOP TEST:** a slow +1 cm jog, then a soft stop 0.4 s in. This is the same `arm.stop()` the web STOP and
   `{t:"stop"}` call. **See** `[PASS] soft e-stop: latch + hold` with `stop_call_ms < 50`, `hold_ms < 300`,
   `drift_after_rad < 0.01`, `latched: true`.
4. **CLEAR:** `[PASS] cleared, holding`. **REST:** a slow move back toward the fold; J2/J3 stop 0.1 rad above their
   limits. **RELEASE:** hand under the arm, Enter; the arm drops the last ~0.1 rad into the fold. `[PASS] jog step`.

**Abort (hardware e-stop) if:** the tool moves in any direction other than forward/back on the jog, it is visibly
faster than 2 cm/s, it moves at all after the soft stop, or the READY move heads toward the table or anything nearby.
Any `[FAIL]` line: stop and report.

## Step 4: sign off, then the teleop-mode service
**4a. Sign off (the user only).** This writes `~/.config/geonosis/safety.yaml`: the repo `safety.yaml` with
`profiles.yam.verified: true`, plus who and when. That lifts the unverified 0.3 cap on the speed *ceiling*; the
start scale stays 0.25.
```
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh sign-off'
ssh -t northstar@northstar '~/geonosis/app/scripts/jetson/remote/tdm119_rt.sh verified-check'
```
Type the phrase it shows (`I verified the yam profile at the arm`). **See** `[PASS] sign-off`, then
`[PASS] yam profile verified`.

**4b. Offline config check** (opens nothing):
```
ssh northstar@northstar 'cd ~/geonosis/app/services/robot-client && set -a && . ~/.config/geonosis/robot.local.env && . ~/.config/geonosis/teleop.env && set +a && .venv/bin/python -m rdf_robot_client.tdm119_teleop --check'
```
**See:** `"verified": true`, `"speed_scale": 0.25`, `"teleop_max_lin_speed": 0.1`, `"ee_speed_at_start_m_s": 0.05`,
`"adapter_max_joint_speed": 0.3`, `"refused_jobs": ["home", "run_baseline", "run_eval"]`.

**4c. Start it.** **Physical:** right arm folded, fingers clear of the gripper (it calibrates at start), e-stop.
```
ssh northstar@northstar 'systemctl --user start geonosis-robot-teleop'
ssh -t northstar@northstar 'journalctl --user -u geonosis-robot-teleop -f -o cat'
```
How the command path turns on:
- `geonosis-robot-teleop.service` runs `rdf_robot_client.tdm119_teleop`.
- Its `ExecStartPre` guards refuse while vrteleop-rh runs or CAN is down, and **refuse unless 4a's sign-off exists**.
- Only this unit mounts `/ws/control`. The read-only `geonosis-robot` never does.
- `Restart=no`: a crashed motion client stays down.

Env (no secrets):
- `~/.config/geonosis/robot.local.env`: `YAM_CAN_CHANNEL=can_right`, `YAM_ARM_TYPE=yam`, `YAM_GRIPPER=linear_4310`,
  `YAM_KINEMATICS=poe`, `RDF_STREAM=cloudflared`, `RDF_STREAM_PORT=8091`,
  `CLOUDFLARED_BIN=/home/northstar/.local/bin/cloudflared`, `TDM118_CAMERAS=config`,
  `RDF_CAMERAS_CONFIG=.../config/cameras.yaml`.
- `~/.config/geonosis/teleop.env`: `RDF_SAFETY_CONFIG=/home/northstar/.config/geonosis/safety.yaml`,
  `RDF_SAFETY_SPEED_SCALE=0.25`, `RDF_TELEOP_MAX_LIN_SPEED=0.10`, `RDF_TELEOP_MAX_ANG_SPEED=0.5`,
  `YAM_MAX_JOINT_SPEED=0.3`, `YAM_ALLOW_GRIPPER_CALIBRATION=1`, `TDM119_ALLOW_POLICY=0`.
- `~/.config/geonosis/robot.env` (0600, pushed from the laptop): SpacetimeDB host and db, the `robot_client@northstar`
  token, `ROBOT_ID=yam-01`, the stream token secret, the media URL. `ORCHESTRATOR_URL` is unset: see step 5.

**See in the log:**
1. `guard ok`, then `[PASS] yam profile verified`.
2. The fork's enable lines, then the gripper calibration (the gripper opens and closes).
3. A cloudflared `trycloudflare.com` URL.
4. `TDM-119 teleop client running on the real arm` with a `robot_id`, `speed_scale 0.25` and `teleop_max_lin_speed 0.1`.

**Then:** on `https://geonosis.jaxonp.com` the robot `yam-01` (embodiment `i2rt_yam`, `sim: false`) shows **online**.
Its RobotView opens on **Live** with two camera panes (top = exo D435, wrist = wristR D405) through the Jetson's
tunnel, and the 3D twin matches the real arm's pose.

**Abort if:** the unit exits (`systemctl --user status geonosis-robot-teleop` shows failed): read the log, don't retry
blindly. Also abort on any arm motion at start other than the gripper calibration (hardware e-stop), or on
`fail to communicate with motor`.

## Step 5: phone teleop at 25 %
**Prerequisites:** a clean-teleop task on `yam-01` (the coordinator's routing posts it). For recording uploads,
`ORCHESTRATOR_URL` must reach the laptop orchestrator: set `JETSON_ORCHESTRATOR_URL` in the laptop `.env` to the
hybrid / `demo-public --api` URL, run `scripts/jetson/tdm118_geonosis.sh env`, then restart the unit (step 6, then 4c).
Teleop itself works without it.

**Physical:** e-stop in hand. Someone watches the arm, not the phone.

**On the phone:** claim the task, open its teleop page, hold the clutch, and make small moves.

**Check, in this order:**
1. Both video feeds are live, with the HUD latency (`worstLatencyMs`) under 500 ms. The twin tracks the arm.
2. With the clutch held, the tool follows the phone's motion at about **5 cm/s** at the default slider.
   Releasing the clutch holds the arm in place.
3. **Phone STOP** (`{t:"stop"}`): the arm latches and holds within 300 ms. The robot shows `estop`, and the log says
   `SOFT E-STOP from /control`. To clear, a human at the arm runs:
   `ssh northstar@northstar 'systemctl --user kill -s USR1 geonosis-robot-teleop'`.
4. **Dashboard STOP** (soft_estop through SpacetimeDB): the same latch, and the log line carries
   `issue_to_stop_ms` (should be under 300). Clear it the same way.
5. Network drop or stale feed (turn the phone's Wi-Fi off mid-clutch): the arm holds within about 200 ms (watchdog).
6. Recording: start, then stop with success. An episode appears for the task (needs ORCHESTRATOR_URL).
7. Log hygiene:
   `ssh northstar@northstar 'journalctl --user -u geonosis-robot-teleop -n 300 --no-pager -o cat | grep -E "limit_hit|clamp|reject|SOFT E-STOP|fail to communicate|fault|watchdog"'`
   Expect a few clamps at the workspace or joint edges, no `reject` storms, and no `fail to communicate`.

**Abort (hardware e-stop) if:** the arm moves faster than about 5-10 cm/s, moves without the clutch, keeps moving
after STOP or after a clutch release, buzzes, or the video freezes while the arm moves.

## Step 6: switch back to vrteleop
**Physical:** drive the arm back near the fold (or have a hand ready to support it). Hand under the forearm.
```
ssh northstar@northstar 'systemctl --user stop geonosis-robot-teleop'
```
The LARP release runs: damp until settled (at most 4.5 s), then disable. **The arm goes limp:** fold it by hand.
If the log says it was still moving and stayed in damping, support it and wait; power-cycle the arm if needed.
```
ssh -t northstar@northstar 'sudo systemctl start vrteleop-rh'
ssh northstar@northstar 'systemctl is-active vrteleop-rh; journalctl -u vrteleop-rh -n 40 --no-pager | grep -E "connected on|opened|Streaming"'
```
**See:** `active`, `right YAM connected on can_right`, `left YAM connected on can_left`, three `camera ... opened`,
and `Streaming`. vrteleop calibrates its grippers again at its own start.
