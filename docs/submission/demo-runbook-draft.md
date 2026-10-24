# Demo runbook (Sunday morning) — draft

DRAFT for the human at the arm. Hard deadline: Devpost submission Sunday Oct 25, 9:00 AM PT (repo, image, video). Pitch: 3 min + 2 min Q&A. Work backwards from 9:00.

Checklists this runbook depends on (do not duplicate them here):

- `docs/pre-run-checklist.md` — maincloud wipe, service identities, one orchestrator, web env, Jetson network path.
- `docs/morning-questions.md` — open decisions (Jetson access Q1, GitHub remote Q2, Stripe liability Q8, Modal deploy, safety limits Q15, YAM gripper Q16, trial outcome Q18).
- `services/robot-client/docs/jetson-bootstrap-checklist.md`, `cameras-jetson-checklist.md` (sections 7-8 for the stream), `safety-jetson-checklist.md`, `yam-sdk-notes.md` section 8 (SDK-alone bring-up, `demo-square`).
- `docs/tdm62-reset-hil-checklist.md` — reset task on a real phone.
- `docs/tdm65-phone-push-checklist.md` — Web Push on a real phone (iOS needs the home-screen PWA).

## 0. Timeline (adjust to when you wake up)

| When | What |
|---|---|
| T-5h | Jetson bring-up, safety checklist, cameras, stream. Set `verified: true` only after the on-arm walk-through. |
| T-4h | Real ticket on maincloud: baseline 10 trials. |
| T-3h | Planner decision, approve spend, collect data (you and anyone nearby on phones), review. |
| T-2h | Fine-tune (about 49 min for 2,000 steps; fewer steps if the Training Droid says so). Record the "before" video while it trains. |
| T-1h | After eval 10 trials. Record the "after" video. Report saved. |
| T-45m | Fill `[TODO]`s in `docs/submission/devpost-draft.md`, push to GitHub, upload video, submit Devpost. |
| T-0 | Submitted. Then rehearse the live pitch once with the timer. |

If the fine-tune is late, submit with the baseline numbers and the plan, labelled as such, and update after. Never submit a number that did not happen.

## 1. Before the run

1. Work through `docs/pre-run-checklist.md` top to bottom. In particular: wipe the maincloud event log once (`--delete-data=always`), save `scripts/verify_event_log.py --save` output, register separate service identities, exactly one orchestrator process.
2. `make deploy-modal` from a clean HEAD worktree (the deployed app serves stubs until then). Then `make policy-up` so the policy server is warm; cold start is about 33 s.
3. `make test-loop DRIVER=orchestrator` must pass on the fake stack first.
4. Jetson: `make jetson-bootstrap`, SDK bring-up per `yam-sdk-notes.md` section 8, then `make jetson-deploy JETSON_SERVICE_MODE=yam`. Confirm `YAM_CAN_CHANNEL`, `YAM_GRIPPER`. Confirm the Jetson reaches the orchestrator (`JETSON_ORCHESTRATOR_URL`) for uploads.
5. Safety: walk `safety-jetson-checklist.md` with the physical e-stop in hand. Until `verified: true`, speed is capped at 0.3.
6. Re-tune the reviewer jerk threshold (`services/orchestrator/config/review.yaml`) on the first real demo; it is calibrated on synthetic data.
7. Web: `RDF_OPERATOR_PASSCODE` set and known to operators (`/ops/login`); `NEXT_PUBLIC_WEB_PUBLIC_URL` set to the public URL so the join QR works; `NEXT_PUBLIC_ORCHESTRATOR_URL` and `RDF_ORCHESTRATOR_URL` set.
8. Stripe: complete the Connect liability acknowledgement (morning Q8), run `services/payments/smoke_test.py`, finish one test onboarding so at least one contributor can be paid in test mode.
9. Phones: at least two contributor phones joined via `/join`, push opt-in done, one of them has completed the teleop practice (`/practice`) so arm-control claims are unlocked.

## 2. The real ticket

1. Create the ticket on `/tickets/new` (or through the MCP connector, which is a nice touch for the video). Keep the task simple (morning Q4). Target 80%, budget as decided.
2. Baseline: 10 trials. Confirm each outcome from the robot terminal (`pass` / `fail <note>`) or the robot page buttons within 30 s, otherwise it is recorded as a failure. Reset the scene only when asked (reset tasks; the robot waits for `resume_policy`).
3. Watch `/tickets/[id]`: clusters, then the planner's decision card. Read the reasoning. If you disagree, the run-settings drawer can override the data type; the override is logged.
4. Approve the spend (ticket page or MCP `approve_spend`). Tasks appear on `/marketplace` and phones get paged.
5. Collect: for `recovery_takeover`, the robot pauses on a detected failure and routes a takeover; the phone claims it and drives. For `clean_teleop`, claim and record a full demo. Watch review verdicts live; rejections show a reason.
6. Training runs when enough episodes are accepted. The agents panel (`/agents`) shows the Training Droid if `RDF_TRAINING_DROID=1`.
7. After eval: 10 trials on the new checkpoint, same procedure. The ship step activates the checkpoint and saves the report (`/tickets/[id]/report`).
8. Copy the real numbers into `docs/submission/devpost-draft.md` and `pitch-script.md`. Print or screenshot the report.

## 3. Videos

**Before video (base policy, record during the fine-tune):**

- One wide shot that shows the arm, the cup, the bin and a laptop with the ticket page. Phone on a stand, landscape.
- Activate the base checkpoint on the robot page. Run 3 trials. Say the trial number and the outcome out loud each time.
- Keep the clip under 60 s. Note the eval run id so the clip matches the dashboard.

**After video (factory checkpoint):**

- Same framing, same cup, same mark. Activate the after checkpoint (hot swap). 3 trials, outcomes spoken.
- Then a 20 s screen capture: the report page (data bought, why, cost, before vs after), the decision card, `/events` with the seal, `/agents`.

Edit: before, after, dashboard. Add a title card with the one-liner and the counts. Upload early; Devpost accepts YouTube or Vimeo links.

## 4. Live A/B at the table (3 + 3 trials)

Roles: pitcher (talks), operator (robot page + e-stop), judge (phone).

1. Before the pitch: `make policy-status` shows both checkpoints loaded (`/policy/activate` with `preload_only` for the second). Arm homed, cup on the mark, e-stop in the operator's hand.
2. "Before": operator activates the base checkpoint, starts a 3-trial eval from the robot page. Pitcher narrates. Operator confirms outcomes; resets the cup by hand between trials (or lets the judge do it in step 5).
3. "After": operator activates the factory checkpoint (instant swap), starts 3 trials. Pitcher says each count.
4. If the arm hesitates at any point, stop talking about it and move to the phone moment. Do not restart a trial mid-pitch.

## 5. Judge's-phone flow (reset or takeover)

Choose the reset path for the pitch; it is the simplest and always available. Takeover is the backup if a failure happens naturally.

**Reset ($2, test mode):**

1. Before the pitch, the judge scans `/join` and opts in to push (or you keep `/marketplace` open on a phone you hand over).
2. After an after-trial, the orchestrator requests a reset. The judge's phone gets the push and opens `/reset/[taskId]`: arm-safety banner, the reset steps and reference photo, live camera.
3. Judge taps "Done, robot may continue". The task completes, the payout is reserved, `resume_policy` is sent, the next trial starts. Show the earnings page if there is time.
4. Unclaimed after 90 s: it is re-paged, then escalated to the operator; claim it from `/marketplace` yourself.

**Takeover ($5, test mode, backup):** the operator presses "Failing" on the robot page (or the detector fires). The paused robot routes a `recovery_takeover` task; the judge claims it on `/teleop/[taskId]`, holds the clutch, nudges the arm, taps hand back. Only do this with a judge who finished `/practice` or with you driving.

## 6. Fallbacks

| Failure | Do this |
|---|---|
| Modal policy server down or cold | `make policy-up` before the pitch. If `/health` stays down: play the before/after video and show the report. `make download-checkpoint TICKET=<id> WHICH=deployed` pulls the checkpoint locally as a backup, but local serving needs a GPU; treat the video as the fallback. |
| Modal fine-tune fails or runs late | The Training Droid retries once on OOM/timeout. If it fails again: submit with baseline numbers plus the plan, labelled honestly; keep collecting; update after the deadline if allowed. |
| Deployed `rdf-training` app still serving stubs | `make deploy-modal` from a clean HEAD worktree. Ephemeral `modal run` proofs do not update the deployed app. |
| Cloudflared quick tunnel flaky | On the venue LAN: `python -m rdf_robot_client.stream serve --tunnel none` and `RDF_STREAM_URL=ws://<jetson>:8091/ws/cameras` on the orchestrator. Or ngrok with `NGROK_AUTHTOKEN`. Phones fall back to 5 fps snapshot polling after 3 failed WebSocket opens on their own. |
| Jetson cannot reach the orchestrator | Tunnel the laptop's orchestrator (cloudflared or the Fly deploy); SpacetimeDB, Modal and the media store are already public. |
| Web Push does not arrive | Open `/marketplace` on the phone; the task is live there with a claim button. iPhone needs the home-screen PWA. |
| Maincloud unreachable | The agents panel switches to replay (`replay=auto`) with a visible "replay" label. For a dry run only: `make dev-local` + `make fake-stack`. Never present replay or fake data as the real run. |
| Orchestrator down during the pitch | The robot page's soft e-stop still works through the `web` identity if `RDF_WEB_SPACETIME_TOKEN` is set; otherwise use the physical e-stop. Restart the orchestrator; it resumes polling in-flight training runs from the rows. |
| Arm misbehaves | Physical e-stop first. Then the robot page soft e-stop. Do not clear a latched stop from the page; a human clears it at the arm. |
| Judge's phone claim loses the race | The loser sees "already claimed by <name>"; release the other claim from `/marketplace` or revoke from the robot page (operator) and let them claim again. |

## 7. Submission checklist

- [ ] GitHub remote created and pushed (`gh auth login`; morning Q2). Repo link in Devpost.
- [ ] Video uploaded; link works in a private window.
- [ ] Cover image: the report page or the arm with the dashboard.
- [ ] `docs/submission/devpost-draft.md` has no `[TODO` left; paste into Devpost.
- [ ] Prize tracks selected: overall, Most Technically Advanced, Best Hardware Hack, Factory Best Use of the Droid SDK. x402 only if confirmed.
- [ ] `/events` seal output saved; decision-trail endpoint screenshot saved.
- [ ] `make policy-down` after the demo.
