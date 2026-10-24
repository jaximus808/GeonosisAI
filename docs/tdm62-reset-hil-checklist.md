# TDM-62 reset task flow: real-table (HIL) checklist

What is done is a SIM proof. A real eval on the YAM, with a real person at the table and a real phone, has not been run yet. This checklist is for that run.

## What the flow does

1. An eval trial ends. The robot holds in `waiting_reset` (`robot.status` = `paused`).
2. The TDM-26 dispatcher posts a $2 `reset` task and pages contributors (TDM-65 push).
3. If nobody claims it within `RDF_RESET_ESCALATE_S` (90 s), it is paged again and `dispatch.reset_escalated` {level 1} is logged.
   - At 2x that time, level 2 asks the operator to do the reset.
4. The person opens `/reset/<taskId>`. The marketplace reset cards link there too. Then they:
   - read the steps and the start photo,
   - check that the banner says **Safe to reset**,
   - claim the task (a first-time phone joins with just a first name),
   - see the live robot camera,
   - tap **Done, robot may continue**.
5. Done attaches a robot-camera snapshot (and an optional phone photo) as task evidence, then calls `complete_task`.
6. The dispatcher then:
   - calls `reserve_task_payment`, which creates a $2 `payment` row (`pending`; Stripe TEST MODE via TDM-63),
   - sends `resume_policy`.
7. The robot starts the next trial. The page shows payout -> robot released -> next trial started, with timings.

## SIM proof (2026-10-24)

`cd services/orchestrator && uv run python ../../spikes/tdm-62-reset/prove_reset_flow.py [--web 1 --hold 900]`

Setup: a local SpacetimeDB, the sim robot client (`--sim --sim-policy --cameras synthetic`, TDM-51 trial runner), the real reset API and a local media store. The bus runs only the dispatcher, the eval handlers and the reset watch, with `EVAL_SOLO_MODE=0`.

Results for a 3-trial eval:

| Check | Result |
|---|---|
| Reset tasks created | 3 of 3 (one per trial) |
| Resets completed | 3 of 3. Reset 1 was done in Chrome through a 390 px phone frame (inline join, claim, live camera, Done). Resets 2 and 3 were done by the script. |
| Escalation | Fired on a reset left unclaimed |
| Payment rows | Every reset has a $2 `payment` row, `pending` |
| `resume_policy` issued | 0.01–0.02 s after `complete_task` |
| Robot left `waiting_reset` | 0.03–0.05 s after `complete_task` |
| Next trial's `eval.trial_started` | 2.8 s after `complete_task` |
| Robot-camera photo | Stored in 50–150 ms on a LAN stream |

The 2.8 s to the next trial is almost all the TDM-51 runner homing the sim arm before the trial starts. Releasing the robot takes about 50 ms.

## Before the real run

- [ ] Orchestrator running with this commit. Check that `/resets/1` answers 404/409, not 404 "Not Found" for the route.
- [ ] `RDF_STREAM_TOKEN_SECRET` is the same on the orchestrator and the Jetson. Without it there is no live camera and no robot-camera photo, but the page still works.
- [ ] The robot publishes its stream (`robot.camera_config_json.stream.url`, cloudflared). Check the reset page shows the live camera after you claim.
- [ ] Steps come from, in order of precedence:
  - a routine saved with the CLI below,
  - the TDM-32 Onboarding Droid's `reset_routine.yaml` (`human_steps`, `operator_prompt`, `est_seconds`, published in `robot.camera_config_json.onboarding`),
  - generic steps.
- [ ] Only the CLI saves a start photo. Without one the page shows a labelled sketch. Save it with the scene set up. Either:
  - `uv run python scripts/tdm62_reset_routine.py set --robot-id <id> --step "..." --step "..." --photo start.jpg`, or
  - `--from-camera <stream http base> --robot-name <name>` to grab the photo from the robot's top camera with the scene set up.
- [ ] `RDF_RESET_ESCALATE_S` decided. Default 90 s; for a judge demo, maybe 45.
- [ ] Optional: TDM-65 push links resets to `/marketplace?task=N`. Pointing reset pushes at `/reset/N` saves a tap. That is TDM-65's `notification_for`.

## At the table (phone in hand)

- [ ] Start a real eval (≥2 trials) with `EVAL_SOLO_MODE` unset.
- [ ] After trial 1, the arm holds still. Within a few seconds a reset task appears and the phone gets the push (or sees it on `/marketplace`).
- [ ] Open `/reset/<id>` on the phone:
  - the banner is green **Safe to reset**,
  - the trial number and $2 TEST MODE are shown,
  - the steps and the start photo are readable at arm's length.
- [ ] Wait for the escalation time without claiming. The page shows "paged everyone again", and `dispatch.reset_escalated` is in the event log.
- [ ] Claim (first-time phone: name, then Claim $2). The live robot camera appears.
- [ ] **Safety:** while the robot is mid-trial (not `waiting_reset`), the page shows the red **Do not touch the arm** banner and the Done button is disabled ("Wait: the arm may move"). Open a reset page during a rollout to check this.
- [ ] Reset the scene, step back, tap **Done, robot may continue**. Measure on a stopwatch: the next trial's arm motion should start ≤ 3 s after the tap.
  - The page shows: Scene reset -> $2 payout (TEST MODE, pending then paid if TDM-63 is running) -> Robot released (resume_policy, ~0.0 s) -> Trial N started (x.x s).
- [ ] The scene photo on the done screen is the robot camera's view of the reset scene. `task.completed` in event_log has `result.photo_uris`.
- [ ] `payment` row: `batch_ref = task:<id>`, 200 cents, `pending` -> `paid` (Stripe test transfer), and the contributor's earnings page shows it.
- [ ] "I can't do it, release" puts the task back to open. Another phone can then claim it.
- [ ] After the last trial, a final reset task is still posted: it readies the scene for the next eval. Completing it sends a harmless `resume_policy` to an idle robot (`done`).

## Known limits

- The arm state comes from the robot's `robot.state_changed` events. With no events yet it falls back to `robot.status` (`paused` is treated as waiting_reset).
- If the robot camera is slow (> 1.5 s), Done goes ahead without the photo and says so. The photo never holds the robot up.
- With no CLI routine and no onboarding routine, the steps are generic and the picture is a labelled sketch, not a photo.
