# Judge Q&A

Short answers for the 2 minutes after the pitch. Lead with the first sentence; the rest is backup if they push. Say "simulation" and "test mode" out loud whenever they apply. Numbers marked `[TODO after real run: ...]` stay unsaid until they are real.

## The business

**Why doesn't a deployment company just do this themselves?**
They can, and today they do. But every new task means standing up a data operation: teleop, operators, QA, training, evals. Most of that money goes to the wrong data, because they collect generic demos instead of fixing the specific ways their robot fails. We measure where it fails, buy exactly the data that fixes those failures from people on their phones, retrain, and prove it with a real eval. A deployment company wants "robot does task X at 90%". We sell that outcome, not the data.

**A marketplace needs supply. Where do the contributors come from on day one?**
Most jobs are remote, so they reach anyone with a phone. Recovery takeovers and clean demos are driven from a browser over the robot's cameras. Only resets need someone on site, and the robot's own operator can do those (a sim robot resets itself). Jobs go out as Discord posts with a QR code and as iMessage texts ("Reply YES to claim"), each with a signed invite for that one task. In this demo the judge is the supply: scan the QR, get a job, drive the arm.

**Who are the competitors?**
Axis Robotics and Labelbox Terra sell robot data. microagi and Deft Robotics deploy robots with engineers on site. We deliver a working robot and decide which data to buy. Data vendors are suppliers to us: the planner can buy an existing dataset instead of paying for new demos when one fits.

**Why does the data choice matter so much?**
Because the default, more clean demos, is often the wrong buy. Northstar Robotics found 50 recovery demonstrations beat 50 extra clean demonstrations, 78% vs 64%. Recovery data teaches the robot what to do in the states it actually gets into. Our planner makes that call per ticket from the measured failure clusters, and logs why. `[TODO after real run: what it chose on the real ticket and the one-line reason.]`

**What does a ticket cost?**
Default prices: reset $2.00, recovery takeover $5.00 per accepted episode, clean demo $10.00 per accepted episode. A 2,000-step π0.5 LoRA fine-tune on one H100 is about 49 minutes and $3–4, whether it has 5 or 10 episodes. Claude calls are logged one by one with a cost estimate. Everything counts against the ticket budget, and a payout over budget is blocked by the database, not by a prompt. Example at default prices, not a measured result: 5 recovery takeovers ($25.00) plus one fine-tune (about $4.00) is about $29.00 of a $50.00 budget, plus $2.00 per reset on a real arm. The real ticket: `[TODO after real run: payouts $X.XX (test mode) + GPU $Y.YY = $Z.ZZ of $B.BB]`. All money is Stripe test mode.

## Real vs simulated

**What is real and what is simulated?**
- Real: the SpacetimeDB module on maincloud, the orchestrator, Claude clustering, planning and review, three Factory Droid roles running as real Factory sessions and woken by database writes, the π0.5 LoRA fine-tunes on H100, the policy server, the teleop and recording path, the event log, the Vercel site and the Discord drops.
- Simulated: the MuJoCo YAM and its policy, which is a scripted IK stand-in labelled `[SIM]`, not π0.5.
- In `make demo`, training is a stub labelled `[STUB] no GPU` unless it runs with `--real-modal`, and rehearsal evals can be `[SEEDED] not run`.
- Real arm: `[TODO after real run: baseline and after evals on the YAM, k/10 each]`.

**So the sim before/after numbers mean nothing?**
They prove the line runs, not that the policy learned. The scripted sim policy is calibrated to succeed about 65% on the base checkpoint and about 97% on a fine-tuned one, from 40 offline episodes each. That gap is designed. The learning claim rests on the real H100 fine-tunes and the real-arm eval, and sim numbers are never pooled with real ones.

**What has π0.5 actually done?**
Two real 200-step LoRA fine-tunes on H100 through the production code path: one on fixture data (483 s, loss 0.045 to 0.0014), one on public real-YAM data, `allenai/24112025-yam-01` (460 s, loss 0.043 to 0.0072). Both checkpoints serve from the policy server, with a 157 ms p50 round trip. `[TODO after real run: the fine-tune on episodes the foundry collected, and its eval on the arm.]`

**Why simulate at all?**
Nobody could be at the arm overnight. A MuJoCo copy of the same arm, behind the same adapter interface and safety layer, let us build and test every station without the hardware. Mode is a per-robot fact: a ticket runs on the sim or the real YAM, the operator can switch it, and every row is labelled by the robot it ran on.

**Where do the sim scenes come from?**
Claude writes one per ticket from the task text: table, objects, a goal and a success predicate like `inside(cube, bin)`. Code validates it (reach, gripper width, overlaps), the operator deploys it, the sim hot-swaps to it, and the browser twin draws the same JSON from top, side and wrist cameras.

## Teleop and safety

**Can someone really teach a robot from a phone? What about remote teleop quality?**
Yes, for the kinds of data we buy. Takeovers are short corrections from the failure state, not full demos. The phone sends clutch-relative end-effector deltas at about 30 Hz. Smoothing, speed caps and workspace limits are applied on the robot before IK. Camera frames came through the tunnel at a p50 of 38–40 ms, measured laptop to laptop with synthetic cameras. A contributor must finish an in-browser practice run before they can claim arm control. Quality is enforced after the fact: the reviewer rejects episodes for jerk, frame drops, missing takeover or handback marks, or a failed task, and rejected episodes are not paid. `[TODO after real run: camera latency from the Jetson, accepted vs rejected episodes.]`

**How do you keep the arm safe when a stranger drives it?**
Every setpoint goes through a safety layer: joint limits with margin, velocity and acceleration caps, a workspace box and table floor, and NaN and jump rejection. A 200 ms watchdog holds the arm if commands stop, and the soft e-stop latches. Exclusive control is a 60-second lease in the database, so a dead tab loses the arm within a minute. There is a physical e-stop at the table. Until a human verifies the limits on the arm, speed is capped at 0.3 of the limits.

## Data and privacy

**What data do you collect, and where does it go?**
- From the robot: its own cameras (the workspace) and joint and gripper actions. From the contributor's phone: control commands only, never the phone's camera or microphone.
- Episodes live on a private Modal volume behind time-limited signed URLs. Datasets stay on a private Modal volume and are never pushed to a public hub.
- Phone numbers for job texts live in one private file on the backend host (mode 0600). The ops page masks them to the last 4 digits, and the event log carries a keyed hash, never a number. STOP is honoured.
- Payment identity is Stripe's, in test mode.

**What can the public site see?**
- The site on Vercel holds none of the backend keys: no Anthropic, Modal, Factory, Stripe or SpacetimeDB owner token.
- It reaches the laptop only after an HMAC proof, and the owner token never reaches a browser.
- Job links grant one task, not the site. The rest of the site needs the access code or the operator login.

**Whose data is it?**
The customer's. Episodes and checkpoints are keyed by ticket. Today this is one deployment for one team. Per-customer isolation and retention controls are next.

## The stack

**Why SpacetimeDB?**
It is the one bus every part shares: orchestrator, Factory Droids, robot, phones and the Vercel site all subscribe to the same tables and get changes live. Reducers make the dangerous actions atomic: one arm-control lease per robot, a budget guard on every payout, and an append-only event log only services can write. A droid's wake-up is just a predicate over those rows, so it can wait hours for a person or a GPU without polling or a running process. Even the backend's address is a row: the laptop publishes its tunnel URL, and Vercel finds it with no redeploy. Maincloud means there is nothing to host.

**Why not just use a Droid, or Claude Code?**
A coding agent can write the robot adapter and the training config, and ours do. It cannot collect episodes, run physical evals or get a cube reset at 3 a.m. We built the loop around the agent: the data decision, the marketplace, the review gate, the trial runner, and a way for the agent to wait on all of that without anyone watching.

**How are Factory Droids used?**
Three roles: Onboarding, Data, Training. Each runs with `autonomy=off`, one allow-listed tool, a write root and a turn cap, and every step is mirrored to SpacetimeDB so `/factory` shows it. A droid ends its turn with `WAITING_FOR dataset:accepted_count>=5` and goes idle at no cost. Our bridge resumes the same session when the row changes: 597 ms after an accepted review in the data-droid proof. If the session is gone, it restarts from a saved payload.

**Which Claude model makes the decisions?**
Every Claude call goes through one switch: the API, Factory credits or local Claude Code. We used the cheaper ones for heavy testing. Production is locked to the API: it refuses to start otherwise, and the switch endpoint answers 403. Each decision on the trail names the backend and model that made it. On the planner fixture all three backends chose the same plan, `recovery_takeover x 5`.

**How does the planner decide, and can it be wrong?**
Claude, with structured output, reads the failure clusters, the success rate with its Wilson interval, the budget and the run settings. It picks a type and an amount. Code sits on top: the reasoning must quote a cluster, the amount is bounded by budget, and zero successes force clean demos. A human approves the spend and can override the type. Yes, it can be wrong. The line loops for another iteration if the after-eval misses the target.

**Ten trials is a small sample.**
Yes. We always show the Wilson 95% interval next to the rate. At 10/10 the lower bound is 0.72, so the ship rule uses the point estimate and the interval goes on the report. More trials is a budget setting.

**What ran on its own?**
Open `/events`: the append-only log with its chain-hash seal, and the timestamps of the planner decision, the dispatch, the training run and the eval. `GET /tickets/{id}/decision` checks whether the chain ran without a human action in it. `[TODO after real run: row count, rdf-evlog-chain-v1 hash, and which steps ran unattended.]`

**Why π0.5 and LoRA?**
Base π0.5 is a strong general prior. LoRA on one H100 fits the time and the budget: about 49 minutes and $3–4 per 2,000-step run. The fine-tuned checkpoint defines the YAM's state and action mapping.
