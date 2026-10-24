# Devpost draft: Robot Deployment Factory

Status: DRAFT. Every number below comes from the git log or a committed proof script. Anything proven only in simulation, on a fake robot or on labelled fixture data is marked as such. `[TODO after real run: …]` placeholders must be filled from the real-arm ticket before submitting. Do not submit with a placeholder left in.

**One-liner:** Plug in your robot, tell it the job, and the factory gets it deployed.

---

## Inspiration

Getting a robot to do a new task reliably is a deployment project, not a model problem. Today engineers do it on site for weeks: run the policy, watch it fail, collect more data, retrain, repeat. Companies like microagi ($55.5M seed) and Deft Robotics sell exactly that loop, staffed with people.

The step that matters most is the one nobody automates: deciding *what kind of data* will fix the failures. Northstar Robotics found that 50 recovery demonstrations beat 50 extra clean demonstrations (78% vs 64% success). We wanted an agent that makes that call from real failure evidence, buys exactly that data from people, reviews it, trains, and proves the fix with evals.

## What it does

You register a robot and open a ticket like "Put the cup in the bin, 80% reliable, $50 budget." The factory then:

1. **Onboards** the robot. A Factory Droid validates the arm adapter, camera rig and safety profile, and writes a success check and a reset routine.
2. **Runs a baseline** with the base π0.5 policy, N trials, each one recorded with an operator-confirmed outcome.
3. **Diagnoses** the failures. Claude clusters the failed trials from notes, durations and sampled camera frames.
4. **Picks the data type.** The planner reads the clusters, the success rate with its Wilson interval and the budget, and decides between `recovery_takeover` (a person takes over the paused robot mid-failure) and `clean_teleop` (a full demonstration). It logs its reasoning verbatim, and the reasoning must quote a failure cluster.
5. **Buys that data.** After a human approves the spend, the dispatcher posts paid tasks. Contributors claim them on their phones, drive the arm over a camera stream, and get paid (Stripe test mode) once a reviewer accepts the episode. Resets between trials are paid tasks too.
6. **Reviews** every episode with deterministic checks (takeover/handback timestamps, jerk, frame drops, length) plus a Claude vision judgement. Rejections show their reason and are not paid.
7. **Trains** a π0.5 LoRA fine-tune on Modal (H100) from the accepted episodes, converted to LeRobot v2.1.
8. **Evaluates** the new checkpoint with the same trial runner, then ships it or loops with more data.
9. **Reports** what was bought, why, what it cost, and the success rate before and after, with every link in the decision chain joined to real rows by id.

Everything is stored in SpacetimeDB and shown live on the dashboard: the ticket, the planner's decision card, tasks, episodes with video, reviews, payouts, training and eval runs, an agents panel with the droids' steps, and an append-only event log with a tamper-check seal.

## How we built it

- **SpacetimeDB (Maincloud, TypeScript module)** is the source of truth: 20 tables, reducers that enforce the hard rules (atomic `claim_task` arm-control lease, a budget guard on every payout, `append_event` that only services can call and that nothing can update or delete).
- **Orchestrator (Python, FastAPI)** subscribes to SpacetimeDB over a raw WebSocket client and runs the flywheel state machine: diagnose, plan, dispatch, review, train, evaluate, ship. Claude (structured output) does clustering, the data-type decision, episode review and LLM QA, with bounded retries.
- **Factory Droids** do the engineering work: an Onboarding Droid (adapter config, onboarding checks, success check, reset routine), a Data Droid (LeRobot dataset assembly) and a Training Droid (dataset sanity checks, justified hyperparameters, launch, OOM retry, loss-curve note). Each runs with `autonomy=off`, one allow-listed tool, `write_roots` and a turn limit.
- **SpacetimeDB-driven Droid resume bridge.** A droid ends its turn with `WAITING_FOR dataset:accepted_count>=5` and its session goes idle at zero cost. The orchestrator stores the predicate, re-evaluates it on every row change, claims the wake-up atomically (`agent_resume`), and resumes the *same* Factory session with a message describing what changed. If the session is gone, it restarts a new one from a saved `resume_payload`. Droids can wait hours for humans and GPUs without burning credits or being babysat.
- **Robot client (Python, Jetson)**: I2RT YAM adapter (EE target to IK to joint commands, 200 Hz smoothing servo), a safety layer in front of every setpoint (joint limits, velocity and acceleration caps, workspace box, watchdog, latched soft e-stop), synced wrist and top cameras, an episode recorder that writes per-camera H.264 plus `actions.parquet` on one clock, an eval trial runner, DAgger-style interventions (pause on failure, route a takeover task, hand back), and a teleop executor behind the task lease.
- **Training and policy (Modal)**: episode to LeRobot v2.1 conversion, π0.5 LoRA fine-tune on 1x H100, a policy server that hot-swaps checkpoints by URI (so the live A/B flips with no cold start), and GPU cost accounting against the ticket budget.
- **Web (Next.js)**: dashboard, phone pages for teleop, takeover and reset, marketplace, join QR, earnings, events timeline, agents panel, printable report. JPEG-over-WebSocket camera streaming through a cloudflared tunnel, with ack pacing for bounded latency.
- **Claude connector (remote MCP)**: `create_ticket`, `ticket_status`, `approve_spend`, `get_report`, `list_robots`.
- **Payments**: Stripe Connect test mode, transfers only after an accepted review, idempotent per episode.
- **Media store**: a Modal Volume behind a signed-URL endpoint; the Jetson and browsers upload with presigned PUTs, training jobs mount the same volume read-only, the dashboard plays clips from presigned GETs.

The repo has 153 commits as of this draft, across a team of coding agents working from one shared ticket board, with a decisions log for every call made while the human was asleep.

## Challenges

- **No maintained Python SDK for SpacetimeDB 2.x.** We wrote a small raw-WebSocket subscriber (`v1.json` subprotocol) plus HTTP reducer calls instead of a TypeScript sidecar.
- **Droids that wait.** A Mission cannot block for hours on an external event, so we built the wait-for protocol and resume bridge on top of plain sessions. Droid-sdk's default autonomy (`low`) ran "read-only" commands and file edits without asking the permission handler, so our bounds only hold with `autonomy=off`; we found that live and changed the default.
- **Remote inference at 30 Hz.** Measured policy round trip on H100 was p50 157 ms / p95 183 ms (laptop proxy, 100 calls), about 5 control steps. We execute 50-step action chunks asynchronously, re-query every 10 steps, skip the stale prefix on arrival and crossfade.
- **Small-sample statistics.** With 10 trials the Wilson 95% lower bound is at most 0.72 even at 10/10, so a CI-based ship rule can never clear an 80% target. We ship on the point estimate and always display the interval.
- **Parallel agents on one checkout.** Stale shared indexes reverted other tickets' files twice. We added a pre-commit guard and a pathspec-only commit rule.
- **Keeping the arm safe when nobody is at it.** The YAM safety profile is marked unverified; until a human runs the on-arm checklist, real-hardware speed is capped at 0.3 of the limits and the Onboarding Droid never connects to hardware.

## Accomplishments that we're proud of

Proven, with the proof scripts in the repo:

- **The resume bridge works on a real droid.** A data droid parked on `dataset:accepted_count>=1`; a manual `submit_review(accepted)` woke it through the bridge in 597 ms on the same Factory session (2 turns, 5,168 credits).
- **The Training Droid loop, end to end (with a fake Modal backend, no GPU):** inspect, write a justified config, queue, park; the run failed with a forced OOM; the bridge woke it; it halved the batch and re-queued; the retry finished; it analysed the loss curve and wrote a training note. 3 turns on one Factory session, 19,215 credits, 85 s wall.
- **The Onboarding Droid on a simulated YAM:** adapter config, contract check through the safety layer, camera check, tighten-only safety overrides, 41/41 IK round trips (0.044 mm), success check, reset routine, report. 1 turn, 19,564 credits, 0 permission denials. Nothing touched hardware.
- **Real π0.5 LoRA fine-tunes ran on H100** through the production code path: a 200-step smoke on a fixture (483 s, loss 0.045 to 0.0014) and one on public real-YAM data (`allenai/24112025-yam-01`, left arm, 2 episodes, 460 s, loss 0.043 to 0.0072). Both load in the policy server.
- **Full loop on the fake stack:** `make test-loop` runs a throwaway SpacetimeDB, a fake robot and a stub trainer through ticket, baseline, diagnose, plan, dispatch, fake contributor, review, training, eval and report. The teleop integration proof ran a clean demo and a recovery takeover on the simulated arm, both accepted, with test payouts reserved.
- **A decision trail that judges can audit.** `GET /tickets/{id}/decision` joins eval run, failed trials, clusters, the planner's verbatim reasoning, the rejected alternative, the human's approval, the tasks, the episodes bought, the training run and the after-eval, all by id, and checks whether the chain ran without a human.
- **An event log nobody can edit.** Append-only by construction, service-only writes, a chain hash and tamper watch on the `/events` page.

Real-arm results:

- Baseline on the YAM: `[TODO after real run: k/10 successes, base π0.5]`
- After the factory's fine-tune: `[TODO after real run: k/10 successes, checkpoint URI]`
- Planner decision: `[TODO after real run: recovery_takeover or clean_teleop, amount, one-line quote of the reasoning]`
- Data bought: `[TODO after real run: N episodes accepted / M submitted, by type; resets paid]`
- Cost: `[TODO after real run: payouts $X (test mode) + GPU $Y estimate = $Z of the $B budget]`
- Training: `[TODO after real run: steps, wall time, final loss]`

## What we learned

- The hard part of "AI deploys your robot" is not the model. It is the plumbing that touches the physical world: who resets the cup, who takes over when it drops, how a GPU job wakes an agent, how a payout waits on a review.
- Agents need to be able to sleep. A session that parks on a database predicate costs nothing and resumes with full context; that turned Factory Droids from "a coding tool" into long-running engineers inside the loop.
- Guardrails belong in code, not prompts. The planner's output must quote a cluster; a payout cannot exceed the plan's approved cents; `append_event` cannot be undone; the droid's only command is one allow-listed tool. The model adds judgement and a written rationale on top.
- Every fake must say so. Fake robot rows are tagged `[FAKE]`, sim recordings `fixture: true`, test robots `test.*`, and the seed script refuses the production database.

## What's next

- Run the real ticket on the YAM and fill in the numbers above.
- Move droids from local `droid-sdk` to Factory cloud computers once the repo has a remote.
- Phone-motion (WebXR) clutch control on Android; today's phone teleop is gamepad, keyboard and on-screen sticks.
- More data types in the planner (UMI, phone clips, autonomous rollouts), once the two core types are proven.
- Auto-open tickets from production failures; contributor quality scores; real payouts.

## Built with

Python (FastAPI, httpx, pyarrow, OpenCV), TypeScript (Next.js, React), SpacetimeDB (Maincloud, TypeScript module), Claude API (structured output and vision), Factory Droid SDK and Sessions API, Modal (H100 fine-tune, policy server, media volume), openpi π0.5 with LoRA, LeRobot v2.1, I2RT YAM SDK on an NVIDIA Jetson, Stripe Connect (test mode), Web Push (VAPID), cloudflared, MCP (Streamable HTTP), Docker Compose, uv, pnpm.

---

## Honesty notes for whoever submits this

- Nothing has run on the physical arm yet as of this draft. The YAM adapter has only run in the I2RT MuJoCo simulator; the safety limits are placeholders marked `TO VERIFY ON ARM`.
- Droid proofs ran locally through `droid-sdk`, not on Factory cloud computers (cloud is implemented and unit-tested, unproven live).
- Planner and clustering proofs used labelled fixture failures, not real-arm failures. The real run must produce the real decision.
- Stripe: the budget guard, transfer idempotency and webhook checks pass in tests; a real test-mode transfer is blocked on the Connect liability acknowledgement (morning question 8).
- Camera-stream latency (p50 38-40 ms through the tunnel) was measured laptop to laptop with synthetic cameras, not from the Jetson.
- The deployed `rdf-training` Modal app still serves stubs until `make deploy-modal` runs; the real fine-tune and policy server ran through ephemeral `modal run`.
