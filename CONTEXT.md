# Robot Deployment Factory — build context

Context for the coding agents building this project at Cal Hacks 13.0. Read this whole file before writing code. Planning history and pitch notes live in the team's Google Doc (tabs "Robot Deployment Factory — v3" and "Cheat sheet"); this file is the build spec.

## Hackathon constraints

- Event: Cal Hacks 13.0, Palace of Fine Arts, SF. Hacking started Friday Oct 23, 2026.
- **Hard deadline: Sunday Oct 25, 9:00 AM PT** (Devpost submission with GitHub repo, image, and a pre-recorded or live demo video).
- All code must be written during the hackathon. Don't paste in code from earlier projects; reuse of open-source libraries is fine.
- Pitch is 3 minutes plus 2 minutes of Q&A. Judging criteria: Application, Intentionality, Functionality, Creativity, Technical Complexity.
- Target prizes: Cal Hacks overall, Most Technically Advanced, Best Hardware Hack, Factory "Best Use of the Factory Droid SDK" ($2,000). Possibly Coinbase "Best Use of x402" for payments (unconfirmed fit).

## What we are building

**One-liner:** "Plug in your robot, tell it the job, and the factory gets it deployed."

**What it does:** You give it a robot and a task. It figures out what data the robot needs, gets people to record it, runs the training and experiments, and keeps going until the robot can reliably do the job. Then it deploys it.

**User flow (website):**

1. **Onboard your robot:** connect the arm and cameras. A Droid writes the adapter and calibration.
2. **Describe the job:** a ticket like "Put cups in the bin, 80% reliable, $50 budget."
3. **Watch it work:** the dashboard shows the baseline, the data plan, paid tasks going out to people, reviews and training runs.
4. **Get a working robot:** a deployed checkpoint plus a report: what data was bought, why, what it cost, and success before and after.

**The core idea to protect:** the factory decides *what kind of data* fixes the robot's failures (recovery takeovers vs. clean teleop demos), buys exactly that from people, reviews it, trains, and proves the fix with evals. This decision is the demo's key moment.

## The demo (what must be true on Sunday morning)

1. Pre-recorded video: π0.5 out of the box fails the task on the YAM; after the factory's fine-tune it succeeds. Show trial counts.
2. The ticket and its report in the dashboard: data bought, why, cost, success before and after.
3. **Live A/B at the table:** before and after checkpoints run back to back on the arm, 3 trials each.
4. Live: a judge's phone receives a paid task (a takeover or reset) from the factory.

### Must be real (do not mock)

- Before/after evals on the real YAM with trial counts.
- A fine-tune that actually ran on episodes collected through the system.
- The planner's logged data-type decision (recovery vs. clean), with its reasoning, from real failure data.
- Tickets, tasks, episodes and the report stored in SpacetimeDB and shown live in the dashboard.
- At least one Factory Droid session doing real engineering work (e.g. writing the robot adapter, eval harness, or data conversion), visible in an "agents" panel.
- An untouched, timestamped planner/event log, to show what ran on its own overnight.

### Can be mocked, stubbed or video-only

- iPhone support (Android Chrome WebXR only is fine; say so).
- Contributor quality scores.
- Auto-opening tickets from production failures.
- Cartesia escalation call.
- Roadmap data types (UMI, phone clips, autonomous rollouts).
- Real-money payouts (Stripe **test mode** is fine).

## Architecture

```
Entry: web dashboard  +  Claude connector (remote MCP server)
            │
            ▼
SpacetimeDB  — source of truth: robots, tickets, tasks, episodes, reviews,
               training/eval runs, payments, agent sessions, event log.
               Reducers enforce atomic actions (e.g. claim_task / takeover lock).
            │
   ┌────────┼──────────────────────────┬──────────────────────────┐
   ▼        ▼                          ▼                          ▼
Orchestrator            Factory (hidden backend)        Robot bridge (robot PC)
(planner + reviewer,    Droid sessions via SDK /        YAM control (target pose → IK),
 Claude API)            Sessions API; Missions for      cameras, episode recording,
                        orchestration; cloud            teleop/takeover commands,
                        Environments for persistence    policy rollout + eval trials
                                     │
                                     ▼
                         GPU box (Vultr or RunPod): data conversion,
                         π0.5 fine-tune, checkpoint storage, policy server
            ▲
            │
Contributors' phones: task pages (takeover with clutch control, teleop, reset),
paid after the reviewer accepts the episode
```

### Components

| Component | Responsibility | Suggested stack |
|---|---|---|
| `spacetime/` | Schema, reducers, subscriptions | SpacetimeDB module (Rust or TypeScript; pick what the team knows) |
| `apps/web/` | Dashboard (ticket view, data plan, tasks, reviews, runs, report, agents panel) and phone task pages | Next.js/React + SpacetimeDB TS client |
| `apps/mcp/` | Claude connector: `create_ticket`, `ticket_status`, `approve_spend`, `get_report` | Remote MCP server (TypeScript) |
| `services/orchestrator/` | Planner and reviewer loops; creates Factory sessions; writes decisions to the event log | Python or TS + Claude API + Factory Droid SDK |
| `services/robot-bridge/` | Runs on the robot PC: arm control, cameras, recording, takeover/teleop, rollouts, eval trials | Python (I2RT YAM SDK, OpenCV) |
| `services/training/` | Convert episodes to LeRobot format, fine-tune π0.5, export checkpoints, serve policy | Python on GPU box (openpi + LeRobot) |

### Ticket lifecycle

1. **Onboard:** robot registered; a Droid writes or validates the adapter, camera config, success check and reset routine.
2. **Baseline:** run the base π0.5 policy for N trials (target 20); record each episode and outcome.
3. **Diagnose:** cluster failures into types from episodes and outcomes (e.g. "drops mid-grasp", "misses the cup").
4. **Plan the data:** the planner picks a data type (`recovery_takeover` or `clean_teleop`) and an amount within the budget, and logs its reasoning.
5. **Dispatch and pay:** tasks are created in SpacetimeDB and pushed to contributors; resets are requested between trials.
6. **Review:** each episode is checked (below). Accepted episodes are paid and added to the training set; rejected ones show the reason.
7. **Train:** the training job fine-tunes on the accepted episodes plus the base data.
8. **Evaluate:** N trials with the new checkpoint; compute success rate and binomial confidence interval.
9. **Ship or loop:** if the target is met, produce the report and mark the checkpoint deployed; otherwise plan more data.

### Task types and example prices

| Task | Who | Price (example) |
|---|---|---|
| `reset` | anyone near the robot | ~$2 |
| `recovery_takeover` | phone (clutch control) or leader arm, while the robot is paused in a failure | ~$5 per accepted episode |
| `clean_teleop` | leader arm or phone teleop, full demonstration | ~$10 per accepted episode |

Payment goes out only after the reviewer accepts the episode. Use Stripe test mode unless the team decides on Coinbase USDC.

### Reviewer checks (must be measurable, not vibes)

- Task success (operator-confirmed or success check on replay).
- For takeovers: takeover and handback timestamps present; failure frames before takeover cut.
- Motion smoothness: joint-velocity jerk below a threshold.
- Episode length and frame-drop sanity checks.
- Rejections are shown on the dashboard with a reason, and are not paid. Avoid a reviewer that approves everything.

### Phone takeover

- The robot pauses in a failure state and the factory pushes a takeover task.
- `claim_task` reducer: the first contributor to accept gets exclusive control.
- The phone shows the camera feeds and uses **clutch control**: hold a button and phone motion maps to small, scaled end-effector deltas from the current pose; release to reposition. A slider controls the gripper.
- The robot bridge applies smoothing, speed caps and workspace limits before IK. There is a physical e-stop at the table.
- Handback: the operator taps done; control returns to the policy; timestamps are logged.
- Pose tracking: WebXR on Android Chrome. iPhone Safari has no WebXR AR, so iPhone would need a native ARKit app (out of scope unless time allows). Fallback: on-screen joystick plus gyro.
- Record actions in the policy's action space: save both the IK target and the resulting joint positions.

## SpacetimeDB data model (starting point)

Adjust names freely; keep the concepts.

- `robot` (id, name, embodiment, status, adapter_version, camera_config)
- `ticket` (id, robot_id, task_description, target_success, budget_cents, spent_cents, status, created_at)
- `failure_cluster` (id, ticket_id, label, count, example_episode_ids)
- `data_plan` (id, ticket_id, data_type, amount, reasoning, created_at)
- `task` (id, ticket_id, type, status, price_cents, assignee_id, claimed_at, completed_at)
- `contributor` (id, name, devices, online, total_paid_cents)
- `episode` (id, ticket_id, task_id, contributor_id, kind, storage_uri, takeover_ts, handback_ts, success, created_at)
- `review` (id, episode_id, accepted, reason, metrics_json)
- `payment` (id, contributor_id, episode_id, amount_cents, provider, status)
- `training_run` (id, ticket_id, episode_ids, status, checkpoint_uri, started_at, finished_at, gpu_cost_cents)
- `eval_run` (id, ticket_id, checkpoint_uri, trials, successes, ci_low, ci_high)
- `agent_session` (id, role, factory_session_id, status, current_step, last_update)
- `event_log` (id, ts, actor, type, payload_json) — append-only; never edited by hand.

## Agents

| Agent | Runs as | Does |
|---|---|---|
| Planner | Claude via the Claude API | Reads failure clusters and budget; decides data type and amount; writes `data_plan` with reasoning |
| Reviewer | Claude + deterministic checks | Applies the reviewer checks; writes `review`; triggers payment |
| Onboarding Droid | Factory Droid session | Writes or validates the robot adapter, camera config, success check and reset routine |
| Data Droid | Factory Droid session | Episode conversion to LeRobot format, cutting failure frames, dataset assembly |
| Training Droid | Factory Droid session | Writes the training config, launches and monitors the fine-tune, reports the checkpoint |
| Dispatcher | Orchestrator code | Creates tasks, pages contributors, requests resets |

Factory is the hidden backend for **persistent cloud agents and orchestration**. Users never interact with Droids directly. Show Droid activity in the dashboard's agents panel so judges can see it.

## Build order

Do these in order. Don't start agent code before steps 1–2 work.

1. **Robot baseline (Friday):** YAM moving through our bridge; cameras recording; base π0.5 running on one simple task; 20 baseline trials recorded with outcomes.
   *Done when:* a script runs N trials and writes episodes plus a success rate.
2. **Training path (Friday night):** fine-tune π0.5 on a handful (~5) of recorded episodes end to end on the GPU box, and run the resulting checkpoint on the robot.
   *Done when:* a new checkpoint from our own episodes runs on the arm.
3. **SpacetimeDB and dashboard skeleton:** tables above, ticket page, live episode and task lists.
4. **One takeover path:** Android WebXR clutch control or leader arm, producing episodes with takeover/handback timestamps.
5. **Planner:** real failure clusters from baseline episodes and a logged data-type decision.
6. **Training Droid:** launches the real fine-tune job from a ticket.
7. **Reviewer and payments** (Stripe test mode).
8. **Dispatcher:** paging contributors, reset requests between trials.
9. **Claude connector, agents panel polish, report page.**
10. Stretch: Cartesia call, iPhone app, roadmap data types.

**Saturday night:** run the full ticket overnight. **Sunday morning:** record before/after videos, finalize the report, and prepare the live A/B.

## Things to verify early (don't assume)

- YAM: SDK and control interface (target pose → IK), control rate, gripper API. I2RT publishes a Python SDK; confirm the version on the arm we get.
- π0.5 fine-tuning: confirm the openpi fine-tuning recipe, the LeRobot dataset format it expects, GPU memory needs (full fine-tune vs. LoRA), and time per run on our GPU.
- Policy serving: where inference runs (GPU box over the network vs. a local GPU) and whether latency is acceptable for control.
- Factory: Droid SDK (`pip install droid-sdk` / `@factory/droid-sdk`), Sessions API, Missions (`droid exec --mission`), cloud Environments. Ask the Factory team whether a Mission can wait hours for external events and resume, and about session length, parallel sessions and credits.
- SpacetimeDB: which module language and client SDKs to use. Check whether Python services need a TS sidecar or the HTTP API to call reducers.
- Payments: Stripe test-mode payouts flow; or ask Coinbase whether USDC payouts qualify for the x402 prize.

## Rules for agents working on this repo

- Build the must-real path first; don't polish UI before steps 1–2 work.
- Never fake eval numbers, success rates or planner decisions. If something is mocked, label it as mocked in the UI.
- Never edit `event_log` rows; it is the evidence for "what ran on its own."
- Keep the robot safe: speed caps, workspace limits, and an e-stop path in the bridge before any remote control.
- Keep secrets (API keys, Stripe keys) in environment variables, never in the repo.
- Commit small and often; the repo is part of the submission.
- Keep two data types in the core loop (`recovery_takeover`, `clean_teleop`). Don't add more until the demo path works end to end.

## Pitch reference

- **Problem:** getting a robot to do a new task reliably is a deployment project, not a model problem. Engineers do it on site for weeks.
- **Market proof:** microagi raised a $55.5M seed to fine-tune robots per customer site; Deft Robotics deploys with engineers and human oversight. They use people; we use agents.
- **Why data choice matters:** Northstar Robotics' study found 50 recovery demos beat 50 extra clean demos (78% vs. 64% success).
- **Why not just a Droid:** "Factory can write the code for a robot. It can't get the data, run the tests, or reset the cup. We built the parts of the factory that touch the physical world." Robots have no CI; physical evals plus paid human resets are CI for robots.
- **Competitors (for Q&A only):** Axis Robotics and Labelbox Terra sell data; we deliver a working robot and decide which data to buy.
