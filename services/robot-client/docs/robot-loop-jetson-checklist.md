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
