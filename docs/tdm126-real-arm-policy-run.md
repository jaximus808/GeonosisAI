# TDM-126: run π0.5 on the real right YAM (`yam-01`): user runbook

**You** run everything on this page, in order, with the hardware e-stop in your hand. Agents did not deploy, did not
start the policy server and never moved the arm. `docs/real-arm-safety.md` applies in full.

**What a run is.** A run is ONE supervised rollout of one checkpoint. The arm moves for at most 30 s of wall-clock
time.
- The speed scale is ≤ 0.25. The `ChunkExecutor` holds the measured pose until the first chunk arrives. It then
  ramps out of every hold at ≤ min(0.5 × the live cap, 0.3 rad/s), with a 15-tick soft start.
- The first chunk must pass the same checks as the TDM-122 dry run (`ok` or `clamp`) before anything moves. Every
  later chunk is checked too, and a `reject` ends the run.
- These stop it at once: the web STOP or `soft_estop`, `{t:"stop"}` on /ws/control, the `stop` command, and
  Ctrl-C in the script.
- At the end the arm **holds** its pose. It is never disabled.
- Nothing is recorded, and no eval_trial row is written.

**What it runs.** For the video there are two checkpoints, both resolved by the orchestrator. The browser or script
only says `before` or `after`.

| choice | checkpoint | what it is |
|---|---|---|
| `before` | `modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199` | YAM-adapted π0.5, 200 steps on public YAM data (TDM-40 smoke) |
| `after` | the ticket's `deployed_checkpoint_uri` | our fine-tune |

Base `pi05_base` (DROID, 8-dim) is never sent to the arm.

## Costs (Modal list prices, `services/training/config/modal_rates.json`)

| What | $ |
|---|---|
| Policy server up (`make policy-up` until `make policy-down`), 1× H100 + 4 CPU + 32 GiB | **$4.39 / hour** (~$0.07 / min) |
| Typical session: preload 2 checkpoints, 2 dry runs, before + after on the arm, ~20 min | **~$1.50** |
| Uncached checkpoint load, ~44 s each | ~$0.05 |

Bring the server down as soon as you are done (step 9).

## 0. Find the ticket's checkpoints and prompt (laptop, read-only, $0)

```bash
cd services/orchestrator
uv run python ../../scripts/demo/real_arm_run.py --ticket <t> --plan
```

This prints `before`, `after` (it must be `modal-vol://rdf-checkpoints/...`, with `after_is_real: true`) and the
**exact training prompt**. Use them below.

## 1. Modal secrets, then warm the policy server (GPU billing starts here)

```bash
make modal-secrets          # Modal Secret rdf-training from .env (names only printed)
make policy-up              # min_containers=1, waits for /health (cold start ~35 s)
make policy-status
```

## 2. Preload both checkpoints

The cache holds 2, so switching between before and after is instant. The robot also activates the requested one by
itself.

```bash
cd services/training
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py activate modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199 --preload
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py activate <after uri> --preload
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py health
cd -
```

`health` must show **both** specs as `single_arm`, `state_dim 7`, `action_dim 7`, `joint_dims 6` and
`action_space joint_pos_abs`. Activate each one (without `--preload`) and run `health` to see each spec. If either one
is `droid_base`, stop here.

## 3. Push the policy env to the Jetson (names only printed)

Your laptop `.env` needs `POLICY_SERVER_TOKEN`. `POLICY_SERVER_URL` is optional: it defaults to
`https://jaxonp808--rdf-training-policy-web.us-west.modal.run`, and `JETSON_POLICY_SERVER_URL` overrides it.
`POLICY_BACKEND` is pushed as `production` whenever the token is set, and `JETSON_POLICY_BACKEND` overrides it.

```bash
scripts/jetson/tdm118_geonosis.sh env      # expect: set POLICY_SERVER_TOKEN, set POLICY_BACKEND (default: production)
```

The running service picks the new env up at its next start (step 6).

## 4. Dry run on the Jetson, nothing moves: continue only if every verdict is `ok` or `clamp`

Leave `geonosis-robot-teleop` running **idle**, as it is now, so its stream server is up.

```bash
ssh northstar@northstar
cd ~/geonosis/app/services/robot-client
set -a; . ~/.config/geonosis/robot.env; . ~/.config/geonosis/teleop.env; set +a     # values are not printed
for CK in modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199 '<after uri>'; do
  .venv/bin/python -m rdf_robot_client.tdm122_dryrun --source stream:http://127.0.0.1:8091 --robot-id <yam-01 robot id> \
    --checkpoint "$CK" --activate --prompt '<the exact training prompt>' \
    --calls 20 --interval 0.3 --out ~/tdm126-dryrun-$(basename "$CK").json || echo "REJECT for $CK: do not run it"
done
exit
```

Each summary must show `mapping.yam_ok: true`, and its `verdicts` may contain only `ok` and `clamp`. Exit code 2,
or any `reject`, means that checkpoint does not go on the arm. If the stream is down, use
`--source rig --state fixed:<the arm's joints>` instead (the TDM-122 runbook has the details).

## 5. Stop the teleop service (support the arm first), then sync

Stopping the service releases the arm: it damps until settled, then disables, and **the arm goes limp**. Fold it
or hold it first.

```bash
ssh northstar@northstar 'systemctl --user stop geonosis-robot-teleop'
scripts/jetson/tdm118_geonosis.sh sync     # HEAD bundle -> ~/geonosis/app (refuses while the service runs)
scripts/jetson/tdm118_geonosis.sh env      # again, if you skipped step 3
```

## 6. Allow policy, then start the service

```bash
scripts/jetson/tdm118_geonosis.sh policy on     # one-liner: TDM119_ALLOW_POLICY=1 in ~/.config/geonosis/teleop.env
ssh northstar@northstar 'systemctl --user start geonosis-robot-teleop'
scripts/jetson/tdm118_geonosis.sh status
```

Hands clear: the gripper calibrates once at start, while the arm holds. yam-01's robot row now advertises
`policy: true`. Without it the orchestrator refuses with `robot_no_policy`.

## 7. Run it: hands on the e-stop

From the terminal (Ctrl-C once = `stop`, the arm holds; Ctrl-C twice = the latched `soft_estop`):

```bash
cd services/orchestrator
uv run python ../../scripts/demo/real_arm_run.py --ticket <t> --checkpoint before
uv run python ../../scripts/demo/real_arm_run.py --ticket <t> --checkpoint after
```

The script talks to the orchestrator at `$ORCHESTRATOR_URL`, or `http://127.0.0.1:8028` by default (pass
`--orchestrator` if needed). It asks you to type `HANDS_ON_ESTOP`, then prints the command's progress and the
robot's result: `first_chunk_verdict`, `ended` (`time_cap` | `max_seconds` | `chunk_rejected` | ...), `seconds`,
`rtt_p50_ms` and `rtt_p95_ms`.

The script and the deployment page (`/tickets/<t>/deployment`, operator login) use the same route and the same gates.
On the page, use **Real arm: pre-trained π0.5** or **Real arm: fine-tuned**, confirm the dialog, and keep the big
STOP in view.

Refusals arrive before anything moves:
- `confirm_required`, `checkpoint_not_real` or `robot_no_policy`, from the orchestrator;
- `first_chunk_rejected`, `checkpoint_not_active` or `backend_not_production`, from the robot. The command fails
  with the reason, and event_log gets `deployment.real_run_finished` with `refused`.

After a soft e-stop, clear it at the Jetson: `ssh northstar@northstar 'systemctl --user kill -s USR1 geonosis-robot-teleop'`.

## 8. Turn policy off again

```bash
scripts/jetson/tdm118_geonosis.sh policy off
# support the arm, then restart so yam-01 is teleop-only again:
ssh northstar@northstar 'systemctl --user restart geonosis-robot-teleop'
```

## 9. Stop billing

```bash
make policy-down            # min_containers=0 and stops the running container now
make policy-status          # expect no running containers
```

## Evidence

- `deployment.real_run_started` (orchestrator) carries the choice, URI, prompt, robot, time cap and speed scale.
- `deployment.real_run_first_chunk` (robot) carries the first chunk's verdict and problems.
- `deployment.real_run_finished` (robot) carries `ok`, `ended`, seconds, steps, the verdict counts, any rejected
  chunk and the latency.
