# TDM-45 safety layer: verify on the arm, physical e-stop in hand

The safety layer (`src/rdf_robot_client/safety.py`, limits in `config/safety.yaml`) was built and tested on a laptop against `SimArm` only. **Nobody was at the arm.** The YAM numbers in `config/safety.yaml` are conservative placeholders marked `TO VERIFY ON ARM`. Until the `yam` profile says `verified: true`, the layer caps the speed scale at 0.3 on real hardware.

## The physical e-stop
- The red e-stop at the table cuts power to the arm. It is the only stop that does not depend on our software, so it comes first and overrides everything below.
- **Rule:** while anything can move the arm (the client running, `safety_check`, teleop, a policy rollout), one person stands at the table with a hand on the e-stop and nothing else to do.
- After a physical e-stop: release it, power-cycle the arm as the I2RT docs require, then restart `rdf-robot-client`. If the robot row still says `estop`, the client starts `locked`; type `clear` (or `unlock`) at its terminal.
- The software soft stop (`soft_estop` command, dashboard button, `estop` typed at the client terminal) is the second line. It latches in the safety layer **and** in the adapter. Only a human at the arm clears it: type `clear` / `unlock`, or run `pkill -USR1 -f rdf-robot-client`.

## What the layer enforces (every motion goes through `command_joints`)
| Check | Config key (profile `yam`) | On violation |
|---|---|---|
| NaN / inf in joints or gripper | (always on) | reject: drop the command and hold |
| Jump: any joint more than `max_jump_rad` from the last setpoint | `joints.max_jump_rad` (0.30) | reject: hold |
| Joint limits: the adapter's limits, tightened by the config, minus a margin | `joints.lower/upper/margin_rad` (adapter, 0.10) | clamp |
| Velocity cap per joint × speed scale | `joints.max_velocity` | clamp (the setpoint lags) |
| Acceleration cap per joint × speed scale (speeding up only; slowing down is never limited) | `joints.max_acceleration` | clamp |
| Tool point (FK) inside the workspace box and above `table_floor_z + table_clearance_m` | `workspace.*` | cut the step at the boundary |
| Gripper range | `gripper.min/max`; `max_effort` goes to the adapter if it implements `set_gripper_effort_limit` | clamp |
| Watchdog: no setpoint for `watchdog_ms` while streaming | `watchdog_ms` (200) | `hold()` |
| 30 rejects in a row | `reject.max_consecutive` | raise `SafetyViolation`: the job fails |

The speed scale starts at `speed_scale` (0.25). At the client terminal: `speed 0.2` sets it (capped), `slow on` / `slow off` toggles teleop slow mode (×0.3), `safety` prints the counters, `pose` prints joints and EE pose, and `status` shows them under `arm.detail.safety`. Every clamp or reject is logged (at most once per second per kind) and appended as a `robot.limit_hit` event (at most once per 5 s per kind, with `total` and `suppressed` counts).

Known limits of the layer: it checks only the **tool point** against the box, so the elbow or wrist can still reach the table. At a wall it stops the tool; it does not slide it along the wall.

## Laptop dry run (no arm needed)
```
cd services/robot-client
uv run --with pytest pytest tests/test_tdm45_safety.py -q
uv run python -m rdf_robot_client.safety_check --adapter sim --yes --floor-test   # 7/7 PASS
uv run python -m rdf_robot_client.safety --profile yam                            # effective YAM limits
```

## On the Jetson, with the arm (needs TDM-46 `YamArm`)
Do these in order. Stop at the first FAIL. Paste the `safety_check` output into TDM-45.

1. [ ] **E-stop works:** with the arm powered and holding, press the physical e-stop, then release it and recover. Know how long recovery takes before you test anything else.
2. [ ] **Joint limits:** for each joint, read the I2RT spec and compare with `YamArm.limits` (`python -m rdf_robot_client.safety --profile yam` prints the config; the `safety layer active` log line at connect prints the effective `joint_lower` / `joint_upper`). If the adapter limits are wrong, set `joints.lower` / `joints.upper` in `safety.yaml`. They can only tighten.
3. [ ] **Workspace box:** jog the arm by hand or with slow teleop to each face of the volume you want to allow over the table. At each one type `pose` at the client terminal and read `ee_pose.xyz`. Check that the base frame matches what you expect (+x forward, +z up). Set `workspace.x/y/z`, about 2–3 cm inside the faces you measured.
4. [ ] **Table floor:** put the tool tip on the table surface, read `ee_pose.z`, and set `workspace.table_floor_z` to that value. Keep `table_clearance_m` at 0.03 or more.
5. [ ] **Run the scripted check:** `uv run python -m rdf_robot_client.safety_check --floor-test`. It prompts before every step; type `go` only with the e-stop in hand.
   - [ ] `limits`: the start pose is inside the limits.
   - [ ] `watchdog`: `time_to_hold_ms` is 240 or less and `drift_after_hold_rad` is under 0.01. This is the "cutting the command stream mid-motion holds within 200 ms" done-condition.
   - [ ] `jump` and `nan`: `moved_rad` ≈ 0.
   - [ ] `estop`: `latched: true` and `drift_after_stop_rad` < 0.01. The arm visibly stops.
   - [ ] `floor`: the tool stops above the table. **Measure the real gap with a ruler** (it should be about `table_clearance_m`). Keep a hand on the e-stop; the elbow is not checked.
6. [ ] **Dashboard soft stop:** run `uv run rdf-robot-client`, start a slow `home` from the dashboard, and press the dashboard e-stop mid-motion. The arm stops, the log shows `SOFT E-STOP` with `issue_to_stop_ms`, and status goes to `estop`. Clear it with `clear` at the terminal.
7. [ ] **Speed caps feel right:** at `speed 0.25`, `home` from about 30° away should be slow and smooth. If anything looks fast or jerky, lower `joints.max_velocity` / `max_acceleration` rather than the speed scale.
8. [ ] **Rollout stalls:** once TDM-50 exists, check that a policy that stalls for more than 200 ms (e.g. a blocking re-query) makes the log show `safety watchdog_holds` and the arm hold, and that it resumes smoothly. If blocking queries trip the watchdog all the time, use the async chunk mode. Don't raise `watchdog_ms` above 300.
9. [ ] When all of the above pass: set `verified: true` in the `yam` profile, commit with the measured numbers in the message, and only then raise `speed_scale`.
