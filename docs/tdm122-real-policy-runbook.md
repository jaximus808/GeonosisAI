# TDM-122: real pi0.5 policy path, user runbook

Everything below is for **you** to run. Agents did not deploy and did not start any GPU. The laptop work
(mapping tests, the dry-run harness) cost $0.

## Costs (Modal list prices, `services/training/config/modal_rates.json`)

| What | Resources | $/hour | Typical |
|---|---|---|---|
| `modal deploy` | builds the image on CPU (layers are cached) | ~$0 | one time |
| Policy server up (`make policy-up` until `make policy-down`) | 1x H100 + 4 CPU + 32 GiB | **$4.39** (H100 alone $3.95) | 15 min session: **~$1.10** |
| First load of an uncached checkpoint | inside the session above | | ~44 s: ~$0.05 |
| Dry run, 30 calls | inside the session above | | <1 min: ~$0.07 |
| **The one live fine-tune (TDM-124)** | 1x H100 + 16 CPU + 96 GiB = **$5.47/h** | | **2,000 steps: ~300 s fixed + 2,000 x 1.19–1.31 s = 45–49 min = $4.10–4.50** (H100 alone: $2.95–3.25) |

The fine-tune line uses the real-YAM smoke run's 1.19 s/step (`smoke-yam.log`) and TDM-40's estimate of 1.31 s/step
plus 300 s overhead. It is under $10 at any step count up to ~4,500. The policy server must be **down** while
nobody uses it: it bills about $0.07 a minute.

## 1. Deploy the Modal app (once)

From a clean worktree of HEAD (the shared checkout has other tickets' half-finished files):

```bash
git worktree add --detach $TMPDIR/tdm122-modal HEAD
cd $TMPDIR/tdm122-modal
make modal-secrets          # refresh Modal Secret rdf-training from .env (POLICY_SERVER_TOKEN, HF_TOKEN, ...; names only printed)
cd services/training && uv run --with 'modal>=1.6.0' modal deploy modal_app.py
cd - && git worktree remove --force $TMPDIR/tdm122-modal
```

The deploy itself starts no GPU (`min_containers=0`). The policy URL is
`https://jaxonp808--rdf-training-policy-web.us-west.modal.run`. Put it in `.env` as `POLICY_SERVER_URL`, and set
`POLICY_BACKEND=production` (the laptop `.env` has neither yet; the client defaults to that URL anyway).

## 2. Warm the policy server (GPU billing starts here)

```bash
make policy-up              # min_containers=1, waits for GET /health; cold start ~35 s, comes up on base pi0.5
make policy-status          # URL, autoscaler intent, running containers (does not wake it)
```

## 3. Preload the checkpoints (no swap, so the live A/B flips instantly)

```bash
cd services/training
# base pi0.5 is loaded at startup; this makes sure it is cached:
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py activate base --preload
# the fine-tuned checkpoint TDM-124 produced (its training_run.checkpoint_uri), ~44 s the first time:
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py activate modal-vol://rdf-checkpoints/runs/ticket-<t>/<run>/<step> --preload
# make one active (cache hit = milliseconds):
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py activate modal-vol://rdf-checkpoints/runs/ticket-<t>/<run>/<step>
uv run --with 'modal>=1.6.0' --with httpx python scripts/policy_ctl.py health
```

The cache holds 2 checkpoints (`POLICY_CACHE_SIZE`). `health` must show the fine-tuned spec as
`single_arm`, `state_dim 7`, `action_dim 7`, `joint_dims 6`, `action_space joint_pos_abs`. Base pi0.5 shows
`droid_base` (8-dim, meaningless on the YAM: baseline only).

## 4. Dry run from the Jetson (nothing moves)

Prerequisites: the robot client is deployed (TDM-118), `POLICY_SERVER_TOKEN` and `RDF_STREAM_TOKEN_SECRET` are in
the Jetson `.env`, and the client is running **idle** with its stream server up (TDM-120). The harness only reads
`/ws/state` and `/snapshot/<cam>.jpg` with a `view` token, and computes and checks actions. It never connects
to the arm or sends a command.

```bash
ssh northstar@northstar
cd ~/rdf/app/services/robot-client
POLICY_BACKEND=production uv run python -m rdf_robot_client.tdm122_dryrun \
  --source stream:http://127.0.0.1:8091 --robot-id <robot id> \
  --checkpoint modal-vol://rdf-checkpoints/runs/ticket-<t>/<run>/<step> \
  --prompt "<the exact task string the fine-tune trained on>" \
  --calls 30 --interval 0.3 --out ~/tdm122-dryrun.json --log ~/tdm122-dryrun.jsonl
```

- **The stream server is down or the robot client is stopped:** use the cameras directly and give a state:
  `--source rig --state fixed:ready`, or `fixed:q1,...,q6,g`.
- **The active checkpoint differs:** add `--activate` to make `--checkpoint` active first. Otherwise every request
  fails with `checkpoint_mismatch`, by design.
- **Base pi0.5 instead:** use `--checkpoint base`. Every chunk then reports `unmapped`, as expected.
- **Laptop, at $0:** `--source recorded:<episode dir> --backend fake` runs against the local [FAKE] server, for
  any episode in our format.

What to look at, in the printed summary and in `~/tdm122-dryrun.json` → `rows[].check`:

- **`mapping.yam_ok: true`:** the server serves a 7-dim `joint_pos_abs` YAM checkpoint.
- **`verdicts`:**
  - `reject` means the raw chunk would be refused: a jump of more than `max_jump_rad` (0.3) from the observed
    pose, NaN, the wrong shape, or a safety rejection on replay.
  - `clamp` means the safety layer would alter it: speed, joint range or workspace.
  - `ok` means neither.
  - Exit code 2 means at least one chunk was a `reject`.
- **`static`:** joint range (safety and LARP hard limits), distance from the observed pose, peak speed vs the cap,
  tool point vs the workspace box and table floor, and gripper range.
- **`latency`:** `rtt_p50_ms`, `rtt_p95_ms`, `server_p50_ms`, and `rtt_p50_under_200ms`. The warm-up call is
  excluded. Put the real p50/p95 into `docs/submission/devpost.md`.

## 5. Cool down (stop billing)

```bash
make policy-down            # min_containers=0 AND stops the running container now
make policy-status          # expect no running containers
```

## Mapping findings (offline, against the real allenai/24112025-yam-01 data and the real smoke checkpoint)

- **Joint order:** OK. LeRobot `left_joint_0..5` is our `joint1..joint6` (SDK order), with the gripper last, in
  both state and action (`[*YAM_JOINT_NAMES, "gripper"]` and `ArmState.state_vector()`).
- **Units:** radians, inside the YAM SDK ranges. Signs agree with our ready pose (J2/J3 positive, J4 negative).
- **Gripper:** normalised 0 = closed .. 1 = open (i2rt `[closed, open]` → [0, 1]). It is absolute in both training
  and output (the checkpoint's action stats for the gripper equal its state stats; the joint actions are deltas
  with mean ≈ 0). The rollout passes it through without inversion.
- **Chunking:** 50 steps at 30 fps. Joint deltas in training become absolute targets through openpi's
  `AbsoluteActions`, so the reply is `joint_pos_abs` [50, 7]. The rollout re-queries every 10 steps and holds at 40.
- **Checkpoint:** the real smoke checkpoint's `rdf_policy.json` resolves to the 7-dim YAM spec, with or without the
  sidecar.
- **Cameras:**
  - The policy takes `observation/image` (the exo or top camera) and `observation/wrist_image`, both 224x224
    RGB letterboxed.
  - The northstar Jetson has 1 D435 (overhead/exo) and 2 D405 (wrists). Their color nodes are YUYV only. The
    by-id `...-video-index4` nodes are video4, video10 and video16.
  - The committed `config/cameras.yaml` points at `/dev/video0` (a D405 **depth Z16** node) and `/dev/video2`
    (an **IR** node), with `MJPG`. That cannot work. **TDM-120 must switch to the color nodes, `fourcc: YUYV`,
    640x360.** LARP and the allenai data both use 640x360 (16:9).
  - Training and live inference must use the same resolution. Both go through the one `cameras.yaml`.
- **Prompt:** the server uses the request's prompt as given. The dry run defaults to the episode's task string;
  live, send the exact string the fine-tune trained on.
- **LoRA rank:** the server builds openpi's default LoRA (rank 16) and ignores `rdf_policy.json`'s `lora_rank`.
  Keep TDM-124 at the default `lora_rank: 16`, or the checkpoint won't load.
- **Base pi0.5 (DROID layout):** `build_obs` passes our gripper (0 = closed) as DROID's `gripper_position`
  (1 = closed) without inverting it, while `map_actions` does invert the output. This only matters for base-model
  baselines, which are meaningless on the YAM anyway. It is noted, not fixed: fixing it means a server change and
  a redeploy.

## Jump safety (docs/real-arm-safety.md rule 6)

- **Policy rollouts:** `ChunkExecutor` holds the measured pose until the first chunk arrives, then moves toward the
  chunk at most `min(0.5 x the live joint cap, 0.3 rad/s)` per joint (`ExecConfig.max_speed_rad_s`). After every
  hold (the first chunk, a pause or takeover, a stale-chunk hold) that per-tick cap ramps up linearly over
  `soft_start_ticks` = 15 ticks. There is no position step. A 1 rad gap ramps at ≤ 0.1 rad/s at today's unverified
  0.25 scale, with no `rejected_jump` (test).
- **Fixed in TDM-122:** after a pause the executor used to re-anchor on its pre-pause target. If a takeover had
  moved the arm, it commanded that old pose: a jump the layer rejects and then aborts the episode on. It now
  re-anchors on the measured pose (test).
- **Home:** `SafetyLayer.move_joints` starts at the measured joints. It uses a smoothstep path whose peak speed is
  ≤ 0.25 x the live cap (0.05 rad/s at scale 0.25), so 1 rad takes ~35 s (test).
- **Joint limits:** `safety.yaml`'s `yam` profile now intersects LARP's limits. Stock URDF J4 lower (−1.693) had
  put the safe range at −1.593, outside LARP's −1.5708. The dry run also checks every target against LARP's limits
  directly.
- **Gripper:** not rate-limited, the same as teleop.
