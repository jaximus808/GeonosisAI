# TDM-49 command loop: Jetson bring-up checklist

This was built and tested on a laptop with the simulated arm (`--sim`). Nobody was at the arm. Do these steps with a human at the table and the physical e-stop in reach.

## Prereqs
- [ ] TDM-46 `adapters/yam.py` (`YamArm`) has landed. Until then `rdf-robot-client` (the default `--adapter yam`) exits with "adapter 'yam' is not available".
- [ ] TDM-45 `rdf_robot_client/safety.py` (`SafetyLayer`) has landed (it has). Before any motion, walk through `docs/safety-jetson-checklist.md` with the physical e-stop in hand: the YAM limits in `config/safety.yaml` are unverified placeholders.
- [ ] `.env` on the Jetson has `SPACETIME_HOST`, `SPACETIME_DB_NAME` and `SPACETIME_TOKEN`. The token must be a **`robot_client` identity** (`POST /v1/identity`, then `register_service_identity(..., "robot_client", ...)`), not the owner token. Also set `ROBOT_ID` to the robot name, e.g. `yam-office-1`.

## Laptop dry run (no arm needed)
```
cd services/robot-client
uv run --with pytest pytest tests/test_tdm49_units.py tests/test_tdm49_loop.py -q   # local SpacetimeDB on :3049
uv run rdf-robot-client --sim --host http://127.0.0.1:3049 --db <db> --token-file <robot_client.token>
```

## On the Jetson
1. [ ] Run `uv run rdf-robot-client` and watch for log lines `registered` and then `ready`. On the dashboard, `robot.status` should read `online`.
2. [ ] **E-stop first, before any motion:** issue `soft_estop`. The arm holds, the log shows `SOFT E-STOP: arm stopped` with `issue_to_stop_ms`, and status goes to `estop`. Clear it at the terminal by typing `clear` + Enter, or with `pkill -USR1 -f rdf-robot-client`.
3. [ ] Issue `home` from the dashboard. The arm moves slowly (25 % of max velocity) and status goes `busy` → `online`. The `robot.state_changed` events show `idle→homing→idle`.
4. [ ] Once TDM-50/51 exist: issue `run_eval`, then `soft_estop` mid-rollout. The arm must stop at once, and the log `issue_to_stop_ms` should be under 200. `run_eval` is left `preempted` and is **not** restarted after you clear the e-stop.
5. [ ] Issue `pause_for_takeover` mid-rollout and check that the arm holds and status reads `paused`. Then issue `resume_policy` and check that the arm continues.

## Behaviour to know
- Typing `estop` at the terminal latches the same soft stop locally. `status` prints a state snapshot.
- If the robot row says `estop` at boot, the client starts `locked`. Clear it by hand.
- At boot, commands left `acked` by a previous run are failed ("restarted"). Commands still `issued` but older than 120 s are failed as stale (`--max-command-age-s`).
- If the SpacetimeDB link is down for more than 2 s, a rollout holds the arm until the link is back.

## Teleop over `/ws/control` (TDM-52) [HIL]
Built and tested on the laptop only (SimArm + safety layer + synthetic cameras + a scripted browser): `tests/test_tdm52_teleop.py` (clutch drive, 200 ms gap hold, stop latch, token expiry 4001, lease revoke 4004, takeover -> handback episode that passes `review_checks`) and `spikes/tdm-52-teleop/e2e_loop.py` (the real command loop on a local SpacetimeDB: `start_teleop`, scripted drive, `stop_teleop`). **Nobody has driven the real arm this way yet.** A human at the table with the physical e-stop in hand does this:

Prereqs: the camera checklist (`docs/cameras-jetson-checklist.md` sections 7-8: cloudflared, `RDF_STREAM_TOKEN_SECRET` in `.env`) and `docs/safety-jetson-checklist.md` (workspace box + table floor measured, so the teleop clamp is right).

1. [ ] Start `uv run rdf-robot-client`. The log shows `stream + teleop up` with a `control_url` (`wss://<x>.trycloudflare.com/ws/control`), and `robot.camera_config_json.stream.control_url` has it. With `RDF_STREAM=none` it is `ws://<jetson-ip>:8091/ws/control` on the LAN. With `RDF_STREAM=off`, or the secret missing, the log says `stream + teleop NOT started` and `start_teleop` just holds the arm.
2. [ ] **Scripted first, tiny motion.** Set `RDF_TELEOP_ALLOW_OPS=1` for this test only (it lets an ops token drive without a task lease), restart, issue `start_teleop` from the dashboard, then on the laptop:
   `uv run python -m rdf_common.stream_token mint --robot-name <ROBOT_ID> --sub ops:<you> --ttl 300` and
   `uv run python -m rdf_robot_client.teleop probe <control_url> --token <t> --pattern square --size 0.01 --speed-scale 0.3`.
   The tool point traces a 1 cm square in the base XY plane and returns. If the motion is in the wrong axis or direction, STOP and note it: the base frame of `YamArm.fk` disagrees with what the browser assumes.
3. [ ] **Clutch release holds:** during the probe, kill it with Ctrl-C mid-square. The arm must stop within ~200 ms (`gap_holds` +1 in the state, safety `watchdog_holds` may also count) and stay still.
4. [ ] **Stop latches:** run the probe with `--stop-at-end` (or press STOP on the page). The arm stops, the robot goes `estop`/`locked`, and every later command is ignored until you type `clear` at the Jetson terminal.
5. [ ] **Real page:** unset `RDF_TELEOP_ALLOW_OPS`, claim a `clean_teleop` task on the teleop page (TDM-56) and drive with the clutch at a low speed slider. Check that releasing the clutch holds at once, that pushing past the workspace box slides along the wall (`workspace_clamps` goes up) and never crosses the table floor, and that the gripper slider opens and closes the gripper.
6. [ ] **Lease loss:** while you drive, release or cancel the task from the dashboard. Within ~2 s the page shows lease lost (close 4004) and the arm holds.
7. [ ] **Clean demo:** `record_start`, pick up and place the object, `record_stop(success)`. The episode has `control_source = human` rows and passes the reviewer.
8. [ ] **Takeover (with TDM-69):** mid-rollout `pause_for_takeover`, take over on the page, recover, hand back. The policy resumes from where you left the arm, and the episode has `takeover_ts < handback_ts` and passes the reviewer.
9. [ ] Tune it if it feels wrong: `TeleopConfig` in `teleop.py` (`max_lin_speed` 0.25 m/s, `max_ang_speed` 1.5 rad/s at speed 1.0, `smoothing_tau_s` 0.08, `cmd_timeout_s` 0.2). The safety layer's joint caps still apply on top (unverified profile: speed scale capped at 0.3).
