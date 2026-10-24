# GeonosisAI: ticket in, deployed robot out

Devpost write-up. Supersedes `devpost-draft.md`.

Rules for whoever pastes this into Devpost:
- Every number below comes from a commit message, a committed proof script or `docs/MORNING.md`.
- Anything that ran only in simulation, on the fake robot or on labelled fixture data says so in the same sentence.
- Fill every `[TODO after real run: ...]` from the real-arm ticket before submitting (`grep -n "TODO after real run" docs/submission/*.md`). If the real run did not produce a number, delete the line rather than estimate it.

**Tagline:** Plug in your robot, tell it the job, and the factory gets it deployed.

**Live site:** https://geonosis.jaxonp.com (landing page at `/`, contributors join at `/join`, operators log in at `/ops/login`).

**Repo:** `[TODO: GitHub URL once pushed]`

**Prize tracks:** Cal Hacks overall, Most Technically Advanced, Best Hardware Hack, Factory "Best Use of the Factory Droid SDK".

---

## Inspiration

Getting a robot to do a new task reliably is a deployment project, not a model problem. Today engineers do it on site for weeks: run the policy, watch it fail, collect more demos, retrain, repeat. microagi raised a $55.5M seed to do that loop per customer site. Deft Robotics does it with engineers and human oversight.

The expensive mistake in that loop is collecting the wrong data. Most teams record more generic clean demos. Northstar Robotics' study found that 50 recovery demonstrations beat 50 extra clean demonstrations, 78% vs 64% success. The step that matters is deciding *which* data fixes *this* robot's failures, and nobody automates it.

We wanted a foundry that makes that call from measured failures, buys exactly that data from people on their phones, trains, and proves the fix with an eval. The name is a nod to the droid foundry on Geonosis, but for real robots.

## What it does

You register a robot and file a ticket: "Put the cube in the bin, 80% reliable, $50.00 budget." The ticket then moves down the line:

1. **Baseline.** The robot runs the task 10 times on its current policy. Every trial is recorded with its outcome.
2. **Diagnose.** Claude groups the failed trials by why they failed, from trial notes, durations and sampled camera frames.
3. **Plan data.** The planner reads the clusters, the success rate with its Wilson 95% interval and the budget, and picks `recovery_takeover` (a person takes over the paused robot mid-failure) or `clean_teleop` (a full demonstration), plus an amount. Its reasoning is logged verbatim and must quote a failure cluster. A human approves the spend.
4. **Collect.** Paid tasks go out. Each new task gets a share card with a QR code posted to Discord (and Bluesky, Telegram, X when configured), and is texted to opted-in contributors over iMessage ("Reply YES to claim"). Every link carries a signed invite for that one task. Contributors claim on their phones, see the cameras, drive the arm in the browser and hand back.
5. **Review.** Deterministic checks (takeover and handback timestamps, jerk, frame drops, length) plus a Claude vision judgement. Rejected episodes show the reason and are not paid. Accepted ones reserve a payout (Stripe test mode).
6. **Train.** A π0.5 LoRA fine-tune on one H100 on Modal, from the accepted episodes converted to LeRobot v2.1.
7. **Eval.** The new checkpoint runs the same 10-trial eval. The ticket shows the rate with its interval.
8. **Ship or loop.** If the rate clears the target, the checkpoint is marked deployed and the report is saved. If not, the line loops back to plan more data.

Every row lives in SpacetimeDB and shows live: the ticket page, the planner's decision card, tasks, episodes with video, reviews, payouts, training and eval runs, and an append-only event log with a chain-hash seal.

What you can open today:
- `/` the public landing page with four live numbers.
- `/factory` the factory floor: every active ticket as a lane of eight agent stations. Factory Droids ride an overhead rail to their station, sleep there showing their `WAITING_FOR` predicate, and show "woke on `<table>:<field>`" when a SpacetimeDB update wakes them.
- `/tickets/[id]` the ticket, its 3D scene, the decision trail and "Watch the robot".
- `/live` a projector view: robot, the line, before/after with intervals, the planner decision, agent wake-ups, the join QR.
- `/practice` drive a browser copy of the robot in the ticket's scene before you are allowed to claim arm control.

### Sim robot or real arm, per ticket

A ticket runs on its robot's mode: the **MuJoCo [SIM] YAM** or the **real I2RT YAM on a Jetson**. The operator can switch a ticket between them. Sim and real numbers are never pooled: success rates, ship decisions and before/after use only evals on the ticket's current mode, and sim episodes go into training only when the ticket asks for them.

The sim robot is the official I2RT YAM model in MuJoCo at 500 Hz, with friction-only grasping, behind the same safety layer as the real arm. Claude generates a 3D scene for each new ticket (table, objects, a goal region and a success predicate such as `inside(cube, bin)`). The operator deploys it, and the sim hot-swaps to that scene. The three.js twin in the browser draws the same scene from top, side and wrist cameras.

## What is real and what is simulated

| Area | Status |
|---|---|
| π0.5 LoRA fine-tune | **Real**, on Modal H100, through the production code path. A 200-step smoke on fixture data: 483 s, loss 0.045 to 0.0014. A 200-step run on public real-YAM data (`allenai/24112025-yam-01`, left arm, 2 episodes): 460 s, loss 0.043 to 0.0072. Both checkpoints load and serve in the policy server. |
| Policy server | **Real** on H100 (ephemeral `modal serve`): bearer auth, hot-swap between cached checkpoints, round trip p50 157 ms / p95 183 ms over a websocket (41 ms of that is GPU). |
| Sim policy | **[SIM] scripted IK pick-and-place, not π0.5.** It plans from the scene's geometry and adds per-episode perception noise by checkpoint. Calibrated offline over 40 episodes: base about 65%, "fine-tuned" about 97.5%. In sim, the before/after gap comes from that calibration, not from learning. Sim runs prove the plumbing, not the policy. |
| Training in `make demo` | **Stub by default**, labelled `[STUB] no GPU` everywhere it appears (no Modal credit spent). `--real-modal` launches real H100 training. |
| Seeded evals | Optional `--seed-evals` for fast run-throughs on sim robots only: every trial note starts `[SEEDED] not run`. A real robot ignores it and runs. |
| SpacetimeDB | **Real**, maincloud database `robo-factory-a706j`: 23 tables, reducers that enforce the hard rules, append-only event log. |
| Orchestrator, planner, clustering, reviewer, dispatcher | **Real code, real Claude calls.** Proven on the sim robot and on labelled fixture failures. |
| Factory Droids | **Real** Factory sessions through local `droid-sdk`: Onboarding, Data and Training Droids, woken by SpacetimeDB updates. Cloud computers are implemented, not proven live. |
| Teleop | **Proven on the sim arm:** a clean demo and a DAgger takeover/handback, each claimed, driven, recorded, accepted by the reviewer, test payout reserved. |
| Job drops | **Live on Discord** (the bot posts to one configured channel; `RDF_DROP_MODE=live` on the backend). Bluesky, Telegram and X are built and need keys. Links only work while the backend's public URL is up. |
| Job texts (Inkbox iMessage) | Built and proven in dry run (opt-in, offer, signed YES webhook, claim link). Live sending is in progress: the one-time Inkbox setup and the Vercel deploy that serves the `/join` card are waiting on a human. |
| Payments | Stripe Connect **test mode**. No real money moves. |
| Real YAM arm | `[TODO after real run: what ran on the arm, in one line]` |

Real-arm results (fill from the real ticket, or delete the line):
- Baseline: `[TODO after real run: k/10, base π0.5, rate with 95% interval]`
- After the foundry's fine-tune: `[TODO after real run: k/10, checkpoint URI, rate with 95% interval]`
- Planner decision: `[TODO after real run: data type, amount, one-line quote of the reasoning, backend and model]`
- Data bought: `[TODO after real run: N accepted of M submitted, by type; resets paid]`
- Cost: `[TODO after real run: payouts $X.XX (test mode) + GPU $Y.YY = $Z.ZZ of $B.BB]`
- Training: `[TODO after real run: steps, wall time, final loss]`
- Event-log seal: `[TODO after real run: row count and rdf-evlog-chain-v1 hash from scripts/verify_event_log.py]`

## How we built it

- **SpacetimeDB on maincloud is the coordination bus.** Every service, agent, robot and phone reads and writes the same tables. Reducers make the dangerous actions atomic: one arm-control lease per robot (`claim_task`, 60 s, heartbeat every 30 s), a budget guard on every payout, and `append_event`, which only service identities can call and nothing can update or delete.
- **Factory Droids that sleep on the database.** A droid ends its turn with `WAITING_FOR dataset:accepted_count>=5` and goes idle at no cost. The orchestrator's resume bridge re-evaluates the predicate on every row change, claims the wake-up atomically (`agent_resume`) and resumes the same Factory session with what changed. If the session is gone, it starts a new one from a saved `resume_payload`. In the data-droid proof, an accepted review woke the parked droid in 597 ms. Each droid runs with `autonomy=off`, one allow-listed tool, a write root and a turn cap.
- **Orchestrator (Python, FastAPI).** It subscribes to SpacetimeDB over a raw WebSocket client (no maintained Python SDK exists for 2.x) and runs the line as a state machine: baseline, diagnose, plan, dispatch, review, train, eval, ship.
- **One switch for every Claude call.** `RDF_LLM_BACKEND` = `api` | `droid` | `claude_code`, read on every call. Local heavy testing runs on Claude Code, compute tests on Factory credits, the demo on the API. Production and cloud deploys are locked to `api`: startup fails otherwise and the switch endpoint answers 403. Every call logs backend, model, latency, tokens and a cost estimate, and the decision trail names the model behind each decision. On the planner fixture, one call each: api 9.7 s, claude_code 13.5 s, droid 21.0 s. All three chose `recovery_takeover x 5`.
- **Hybrid deploy.** The web app runs on Vercel at https://geonosis.jaxonp.com. The orchestrator and the robot stay on a laptop behind two cloudflared quick tunnels whose URLs change on every start. `make demo-hybrid` publishes the current URL as an `orchestrator.endpoint` row in SpacetimeDB. Vercel's server routes read the newest row and check an HMAC proof from that URL before they send it the shared secret. Restarting the backend never needs a redeploy. No backend key (Anthropic, Modal, Factory, Stripe, the SpacetimeDB owner token) is on Vercel.
- **Robot client (Python, Jetson or MuJoCo).** One `ArmAdapter` interface with a real YAM adapter (I2RT SDK, IK, 200 Hz smoothing servo) and the MuJoCo sim. A safety layer sits in front of every setpoint: joint limits, velocity and acceleration caps, a workspace box, a 200 ms watchdog and a latched soft e-stop. Also: synced cameras, an episode recorder (per-camera H.264 plus `actions.parquet` on one clock), an eval trial runner, DAgger interventions (pause on failure, route a takeover, hand back) and a teleop executor behind the lease.
- **Per-ticket scenes.** On `ticket.created`, Claude writes a scene spec with structured output. `rdf_common.scene_spec` validates it (reach, gripper width, overlaps, spawn vs goal). The operator deploys it, the sim robot hot-swaps, and the browser twin draws the same JSON.
- **Web (Next.js, three.js).** The GeonosisAI Foundry theme, the landing page, the factory floor (SVG and DOM, 60 fps in headless Chromium), a 3D twin built from the SDK's own MuJoCo model, phone pages for teleop, takeover, reset and practice, the marketplace, earnings, events and the printable report.
- **Training and policy (Modal).** Episode to LeRobot v2.1 conversion, π0.5 LoRA on 1x H100 (about 49 minutes and $3–4 for 2,000 steps), and a policy server that hot-swaps checkpoints by URI, so the live A/B flips with no cold start.
- **Job drops and texts.** Share cards are rendered in Python with Pillow, with a MuJoCo render of the ticket's scene. Posts follow their task: a claimed Discord post is edited to "Claimed", finished tasks are deleted, reopened ones are reposted. Texts go through Inkbox iMessage, and the first YES claims the task through the same `claim_task` reducer.
- **Claude connector (remote MCP).** `create_ticket`, `ticket_status`, `approve_spend`, `get_report`, `list_robots`.

The repo has 200+ commits from a team of coding agents working one shared Tandem ticket board, with a decisions log that records a revert plan for every call made while the human was asleep.

## Challenges we ran into

- **No arm overnight.** Nobody was at the YAM, so we built a physics sim of the same arm behind the same adapter interface and safety layer, and made the mode a per-robot fact that every number is labelled with. That decision kept the sim from leaking into real results.
- **A scripted sim policy that looped forever.** On one ticket the sim policy went 0/6: its release height sat below its own "just grasped, lift" band, so it lowered, lifted and lowered until the 45 s limit. It now plans from the scene's real geometry. Offline: 4/10 base and 10/10 "fine-tuned" after the fix, against 0/10 before. These are [SIM] scripted numbers.
- **Droids that must wait for hours.** A Mission cannot block on an external event, so we built the wait-for protocol and resume bridge on plain sessions. We also found that droid-sdk's default autonomy ran edits without asking our permission handler, so our bounds only hold with `autonomy=off`. We changed the default.
- **Remote inference at 30 Hz.** A 157 ms round trip is about 5 control steps. We run 50-step action chunks asynchronously, re-query every 10 steps, skip the stale prefix on arrival and crossfade over 3 steps.
- **A backend whose URL changes every start.** Quick tunnels get a new URL every run. Publishing the URL into SpacetimeDB, with an HMAC proof, let Vercel find it without a redeploy and without trusting a forged row.
- **Ten trials is a small sample.** At 10/10 the Wilson lower bound is 0.72, so a CI-based rule can never clear 80%. We ship on the point estimate and always show the interval.
- **Many agents, one checkout.** Stale shared git indexes reverted other tickets' work three times. A pre-commit hook now refuses deletions and rollbacks, and every commit uses an explicit pathspec.

## Accomplishments that we're proud of

- **The full line runs with the real orchestrator** on the fake robot and a throwaway SpacetimeDB: prompt, ticket, baseline, Claude clustering, Claude planner, paid tasks, review, train, eval, ship, report. `make test-loop DRIVER=orchestrator` passes 6/6, including a 2-iteration run.
- **Agents wake on database writes.** A parked data droid woke 597 ms after an accepted review, on the same Factory session. The Training Droid chained two waits on one session: queued, hit a forced OOM, woke, halved the batch, re-queued, finished, wrote a loss-curve note (3 turns, 19,215 credits, fake Modal backend).
- **Real π0.5 LoRA fine-tunes on H100**, including one on public real-YAM data, through the same code path the ticket uses.
- **A decision trail you can audit.** `GET /tickets/{id}/decision` joins the eval run, failed trials, clusters, the planner's verbatim reasoning, the rejected alternative, the human approval, tasks, episodes, the training run and the after-eval, all by id.
- **Hybrid deploy with nothing sensitive on the edge.** The public site runs on Vercel and talks to a laptop it finds through SpacetimeDB.
- **Every fake says so.** `[SIM]`, `[STUB] no GPU`, `[SEEDED] not run`, `[FAKE]` and `test.*` labels appear on rows, badges, reports and the factory floor.

## What we learned

- The hard part of "AI deploys your robot" is not the model. It is the plumbing that touches the physical world: who resets the cube, who takes over when it drops, how a GPU job wakes an agent, how a payout waits on a review.
- Agents need to be able to sleep. A session parked on a database predicate costs nothing and resumes with full context. That turned Factory Droids from a coding tool into long-running workers inside the loop.
- Guardrails belong in code, not prompts. The planner must quote a cluster. A payout cannot exceed the approved cents. `append_event` cannot be undone. A droid's only command is one allow-listed tool.
- A simulator is only useful if nobody can mistake it for the real thing. Label by the row's own robot, never pool sim with real.

## What's next

- Run the real ticket on the YAM and fill in the numbers above.
- Turn on live job texts and live posts for the event (the setup steps are written down).
- Move droids from local `droid-sdk` to Factory cloud computers once the repo has a remote.
- Phone-motion (WebXR) clutch control on Android. Today's teleop is keyboard, gamepad and on-screen sticks.
- More data types in the planner (UMI, phone clips, autonomous rollouts), once the two core types are proven on hardware.
- Open tickets automatically from production failures, and score contributors on accepted episodes.

## Built with

Python, FastAPI, httpx, pyarrow, OpenCV, Pillow, TypeScript, Next.js, React, three.js, react-three-fiber, Tailwind CSS, SpacetimeDB (maincloud, TypeScript module), Claude API (structured output, vision), Claude Code, Factory Droid SDK and Sessions API, Modal (H100 fine-tune, policy server, volumes), openpi π0.5 with LoRA, LeRobot v2.1, MuJoCo, I2RT YAM SDK, NVIDIA Jetson, Vercel, cloudflared, Stripe Connect (test mode), Discord API, Inkbox (iMessage), Web Push (VAPID), MCP (Streamable HTTP), Docker Compose, uv, pnpm.
