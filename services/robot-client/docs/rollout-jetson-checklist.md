# TDM-50 policy rollout: checklist for the first real-arm run [HIL]

Nobody has run this on the YAM yet. Everything below was tested on the laptop with SimArm, the
TDM-45 safety layer, synthetic cameras, a local fake policy server and one real call to the Modal
spike endpoint. **Someone must be at the arm with the physical e-stop in reach for every step.**

The base pi0.5 model's actions mean nothing for the YAM. With the spike endpoint (`droid_base`
output) the rollout reads them as joint velocities, scaled by 0.25 and integrated from the observed
joints. The arm will wander slowly. That is expected. On this run we are checking the control
path, not the task.

## 0. Before you start
- [ ] The TDM-46 YAM adapter and the TDM-45 safety layer are on the Jetson. `build_arm` refuses the
      YAM without the safety layer.
- [ ] `config/safety.yaml`: the workspace box and speed caps fit the table. Start with `speed 0.3`.
- [ ] Cameras: `python -m rdf_robot_client.cameras check --seconds 5` shows both cameras in sync.
- [ ] `.env` on the Jetson has `POLICY_ENDPOINT_URL` (spike). For TDM-41, set
      `POLICY_BACKEND=production`, `POLICY_SERVER_URL` and `POLICY_SERVER_TOKEN`.
- [ ] Laptop: `uv run --with modal python spikes/tdm-37-latency/warm.py up` (production:
      `make policy-up`). Wait about a minute.

## 1. Dry run with no motion that matters (about 2 min)
```
cd services/robot-client
uv run python -m rdf_robot_client.rollout --adapter yam --cameras config --home-first \
    --prompt "<task prompt>" --seconds 5 --out /tmp/tdm50-dry.json
```
- [ ] It prints `achieved_hz` ≈ 30, `latency.rtt_p95_ms` < 250 and `executor.errors` = {}.
- [ ] Press Enter: the episode stops and the arm holds. Then try the physical e-stop.
- [ ] Unplug the Jetson's network for 2 s in a second run. The arm must HOLD (`holds` gets a
      `stale_chunk` / `policy_error` entry). After 4 failed calls the episode ends
      `policy_error` with the arm held.

## 2. A recorded episode through the command loop
Start the client as usual (`rdf-robot-client`), then issue the command from the dashboard or
orchestrator:
`issue_robot_command(robot_id, "run_baseline", '{"prompt": "<task>", "timeout_s": 30}', ticket_id)`
- [ ] `result_json.rollout` shows `ended`, `timing.achieved_hz`, `latency.rtt_p95_ms`,
      `executor.skips` (expect 5–6) and `holds`.
- [ ] `result_json.recording.recorded` is true once TDM-48 puts a `recorder` in `ctx.services`.
      Until then the episode is not recorded and the result says so.
- [ ] Note the achieved Hz and p95 latency on TDM-50 / TDM-53. These are the "done when" numbers.

## 3. After the session
- [ ] `uv run --with modal python spikes/tdm-37-latency/warm.py down` (production: `make policy-down`).

## Knobs (`ExecConfig` in rollout.py, or payload `mode`)
- `mode=sync`: the S-05b v0 fallback. It runs 25 steps, then holds while one query is answered.
  Use it if async misbehaves.
- `base_velocity_scale` (0.25) and `speed_fraction` (0.5 of the arm's max velocity per step) are
  the rollout's own caps. The safety layer adds its own caps on top.
- `request_timeout_s` (1.0 s, `PolicyConfig`): an unanswered call counts as dropped after this
  long. The socket is then reopened.
