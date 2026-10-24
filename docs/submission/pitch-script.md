# Pitch script: 3 minutes + 2 minutes Q&A

> Superseded by `pitch.md` and `qa.md` (TDM-87, Sat Oct 24). Kept for history; edit the new file.

DRAFT. Follows the pitch reference in `CONTEXT.md`. Stage cues are in `[brackets]`. Numbers in `[TODO …]` come from the real run; do not say any of them until they are real. Target about 420 spoken words for the 3 minutes. Practise with the timer; cut the "how" paragraph first if you run long.

## Setup before you start

- Dashboard open on the ticket page, decision card visible. Agents panel and `/events` in other tabs.
- Both checkpoints preloaded on the policy server (`make policy-up`, then activate "after" with `preload_only` for "before" so the swap is instant).
- Arm homed, cup at the start mark, physical e-stop in your hand or the second person's.
- One judge's phone has scanned `/join` and opted in to push. Fallback: the `/marketplace` page open on your own phone.
- The before/after video queued in case the arm misbehaves.

## Script

**[0:00, face the judges, nothing on the arm yet]**

Getting a robot to do a new task reliably is not a model problem. It is a deployment project. Engineers sit on site for weeks: run the policy, watch it fail, collect more data, retrain, repeat. microagi raised a fifty-five million dollar seed to do exactly that, with people. We built the factory that does it with agents.

**[0:20, point at the ticket page]**

You plug in a robot and write a ticket: "Put the cup in the bin, 80 percent reliable, 50 dollar budget." The factory runs a baseline, and the base policy fails. `[TODO after real run: "k out of 10."]`

**[0:35, point at the decision card. This is the key moment, slow down.]**

Here is the part that matters. The factory does not just ask for more data. It diagnoses the failures into clusters, and then decides *which kind* of data fixes them: a recovery takeover, where a person grabs control of the paused robot mid-failure, or a clean demonstration from scratch. Northstar's study showed fifty recovery demos beat fifty clean ones, 78 to 64 percent. Our planner made that call on real failure evidence, and its reasoning is logged, verbatim, with a timestamp. `[TODO after real run: read the first sentence of the planner's reasoning aloud.]`

**[1:05, point at the tasks list and the phone]**

Then it buys exactly that data. Paid tasks go to people's phones. They claim one, see the cameras, drive the arm, hand back. A reviewer checks every episode, measurable things, not vibes: takeover and handback timestamps, jerk, frame drops, plus a vision judgement. Accepted episodes get paid, in Stripe test mode. Rejected ones show why. Then a fine-tune runs on an H100, the new checkpoint is evaluated with the same trial runner, and the report shows what was bought, why, what it cost, and before versus after.

**[1:35, turn to the arm]**

Let's run it live. This is the base policy. **[Second person: activate "before", start trial 1. Narrate briefly while it runs: three trials each, same cup, same mark.]** `[TODO after real run: expected behaviour of the base policy, one phrase.]`

Now the factory's checkpoint. **[Activate "after", no cold start, the server hot-swaps. Run three trials. Say the count as each finishes.]**

**[2:20, judge's phone. Second person: flag a failure or request a reset from the robot page.]**

One more thing. The robot just paused. Someone's phone just got paged. **[To the judge:]** That's you. Tap it. You are now the person the factory hired to reset the cup. Two dollars, test mode, paid after the robot confirms it can continue.

**[2:40, close]**

Under the hood: SpacetimeDB is the source of truth, Claude does the diagnosis and the data decision, and Factory Droids do the engineering: onboarding, dataset assembly, training. The droids sleep on a database predicate and our bridge wakes them when the world changes, so they can wait hours for a human or a GPU for free. Factory can write the code for a robot. It cannot get the data, run the tests, or reset the cup. We built the parts of the factory that touch the physical world. Robots have no CI. This is it.

**[3:00, stop.]**

## Timing fallbacks

- Running long at 1:35: skip the reviewer sentence, go straight to the arm.
- Arm fails to start: "This is the recorded run from this morning," play the video, keep narrating the counts.
- Phone page does not arrive: open `/marketplace` on the judge's phone (or yours) and claim the reset there; the task is already live in SpacetimeDB.

## Q&A crib (2 minutes)

Answer short. Say "simulation" and "test mode" out loud whenever it applies.

**Why not just use a Droid / Claude Code / Cursor?**
A coding agent can write the robot adapter and the training config, and ours do. It cannot collect episodes, run physical evals, or get a cup reset at 3 a.m. We built the loop around the agent: the data decision, the paid-task marketplace, the review gate, the trial runner, and a way for the agent to wait on all of that without a person watching. The Droids are one worker inside the factory, not the product.

**Is any of this real or is it mocked?**
Real: the SpacetimeDB schema and reducers on Maincloud, the orchestrator flywheel, the planner and clustering with Claude, the resume bridge woken by a real database write, three Droid roles run as real Factory sessions, π0.5 LoRA fine-tunes that ran on H100, the policy server, the teleop and recording path, and the event log. Proven only in simulation or with fixtures as of Saturday night: the YAM adapter (MuJoCo), the Onboarding Droid (simulated arm), the Training Droid (fake Modal backend), the planner's decision (fixture failures), camera latency (synthetic cameras). `[TODO after real run: update this list; the baseline and after evals, the planner decision and the fine-tune should move to "real".]` Mocked or deferred by design: iPhone WebXR, contributor quality scores, real-money payouts (Stripe test mode), Cartesia escalation.

**How do you keep the arm safe when a stranger drives it from a phone?**
Every setpoint goes through a safety layer: joint limits with margin, velocity and acceleration caps, a workspace box and table floor, NaN and jump rejection, a 200 ms watchdog that holds the arm if commands stop, and a latched soft e-stop that a subscription callback fires in a couple of milliseconds. Teleop deltas are clutch-relative, low-passed and rate-limited before IK. Exclusive control is a 60-second lease in the database; a dead tab loses the arm within a minute and a revoke reaches the robot in under two seconds. There is a physical e-stop at the table. Until a human verifies the limits on the real arm, speed is capped at 0.3.

**What does a ticket cost?**
Example prices: reset about $2, recovery takeover about $5 per accepted episode, clean demo about $10. A 2,000-step LoRA fine-tune on an H100 is about 49 minutes and $3-4 regardless of 5 or 10 episodes; GPU time is estimated from Modal's rates and counted against the ticket budget alongside payouts. The real ticket: `[TODO after real run: payouts + GPU = total vs budget]`.

**How does the planner decide, and can it be wrong?**
Claude with structured output reads the failure clusters (phase, recoverable mid-episode or not), the success rate with its Wilson interval, the budget and the run settings, and picks a type and an amount. Code guardrails sit on top: zero successes with no approach forces clean demos, late failures push toward recovery, the amount is bounded by budget, and the reasoning must quote a cluster label. A human approves the spend before any task is posted, and can override the data type in run settings. Yes, it can be wrong; the loop is designed to run another iteration if the after-eval misses the target.

**Why SpacetimeDB?**
One source of truth with live subscriptions for the dashboard, the phones, the robot and the orchestrator, and reducers that make the dangerous actions atomic: one arm-control lease per robot, a budget guard on every payout, an append-only event log that only services can write. The droid resume bridge is just a predicate over those tables.

**How are Factory Droids used? Why not Missions?**
Three roles: Onboarding, Data, Training. Each runs with `autonomy=off`, a single allow-listed tool, a write root and a turn cap, and mirrors every step into SpacetimeDB so the agents panel shows it. A Mission cannot block for hours on an external event, so a droid ends its turn with `WAITING_FOR dataset:accepted_count>=5`, goes idle at zero cost, and our bridge resumes the same session when the row changes (597 ms in the live proof), or restarts from a saved payload if the session is gone.

**Who are the competitors?**
Axis Robotics and Labelbox Terra sell robot data. microagi and Deft Robotics deploy with engineers and human oversight. We deliver a working robot, and we decide which data to buy; the data vendors are suppliers to us (the planner can also buy an existing dataset instead of paying for new demos when one fits).

**Ten trials is a small sample.**
Yes. We always show the Wilson 95% interval next to the point estimate; with ten trials the lower bound cannot exceed 0.72, so the ship rule uses the point estimate by default and the interval is on the report. More trials is a budget setting.

**What did the agents do overnight without you?**
`[TODO after real run: open /events, point at the seal and the timestamps of the planner decision, the dispatch, the training run and the eval. The decision-trail endpoint checks whether any owner-token action happened before the chain.]` As of this draft: 153 commits, a decisions log with a revert plan for every call made while the human was asleep.

**Why π0.5 and LoRA?**
Base π0.5 gives a reasonable prior; LoRA on one H100 fits the hackathon budget and time. The fine-tuned checkpoint defines the YAM state and action mapping; the base model's outputs are only used for the baseline, with velocities scaled down.
