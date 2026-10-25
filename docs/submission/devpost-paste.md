## Inspiration

Two things.

**Agents that pay people.** I saw a YC company where AI agents pay people to do the things the agent can't do itself. That stuck with me, because robotics has the clearest version of that problem: the thing an agent can't do is *collect robot data*. It can't reset a cube, take over a stuck arm, or record a clean demo. A person with a phone can, and a lot of people will do it for $5.

**I spent this whole week doing evals.** Getting a robot to actually do something is painful. You run the policy, watch it fail, guess why, collect more demos, retrain, run the eval again. Most of my time went to manual steps that could have been automated: setting up runs, resetting the scene, tracking which trial failed and why, converting data, kicking off training, and the small mistakes in between that cost an hour each.

Underneath both is cost. Robot data is expensive, and so is the infrastructure to collect, review and train on it. Data companies also aren't set up to collect data for *your* task on *your* robot. But someone will give you a few good episodes for five bucks. Scale that out and you have a usable dataset.

So I built GeonosisAI: a factory where agents run the robot deployment loop end to end and buy the exact data the robot needs from people on their phones.

## What it does

You register a robot and file a ticket: *"Put the cube in the bin, 80% reliable, $50 budget."* The ticket moves down a line of agent stations:

1. **Baseline.** The robot runs the task on its current policy. Every trial is recorded with its outcome.
2. **Diagnose.** Claude groups the failed trials by *why* they failed, from trial notes, timings and camera frames.
3. **Plan data.** The planner reads the failure clusters, the success rate and the budget, and decides what kind of data fixes them: **recovery takeovers** (the robot pauses mid-failure and a person takes over) or **clean demos**, and how many. Its reasoning has to quote a failure cluster, and a human approves the spend.
4. **Collect.** Paid tasks go out as job drops on Discord and as texts ("Reply YES to claim"). A contributor claims a task on their phone, sees the robot's cameras, drives the arm from the browser and hands control back.
5. **Review.** Deterministic checks (timestamps, jerk, dropped frames, length) plus a Claude vision review. Rejected episodes show why. Accepted ones get paid (Stripe test mode).
6. **Train.** A π0.5 LoRA fine-tune on an H100 on Modal, from the accepted episodes converted to LeRobot format.
7. **Eval and ship.** The new checkpoint runs the same eval. If it clears the target it ships with a report. If not, the line loops back and buys more data.

Everything is live on the web app: a factory floor where each ticket is a lane of agent stations, the planner's decision, every episode with video, every payout, and an append-only event log you can audit.

Success rates always show a Wilson 95% interval, because with ~10 trials the point estimate alone lies:

$$\frac{\hat p + \frac{z^2}{2n} \pm z\sqrt{\frac{\hat p(1-\hat p)}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}$$

At 10/10 the lower bound is only 0.72.

## How I built it

- **SpacetimeDB is the bus.** Every service, agent, robot and phone reads and writes the same tables on maincloud. Reducers keep the risky actions atomic: one arm-control lease per robot, a budget check on every payout, and an event log that nothing can edit or delete.
- **Factory Droids that sleep on the database.** A droid ends its turn with something like `WAITING_FOR dataset:accepted_count>=5` and goes idle for free. A resume bridge re-checks that predicate on every row change and wakes the *same* session with what changed. In testing, an accepted review woke a parked data droid in 597 ms.
- **Orchestrator (Python, FastAPI)** runs the line as a state machine: baseline, diagnose, plan, dispatch, review, train, eval, ship. Every Claude call goes through one switch (API, Claude Code, or Factory Droid backends), and each decision records which model made it.
- **Robot client** for the real **I2RT YAM on an NVIDIA Jetson**, plus a **MuJoCo sim of the same arm** behind the same interface. A safety layer sits in front of every command: joint limits, velocity and acceleration caps, a workspace box, a watchdog and a soft e-stop. It records synced cameras and actions on one clock.
- **Modal** for π0.5 LoRA training on H100 and a policy server that hot-swaps checkpoints. Inference is remote, so the robot runs 50-step action chunks asynchronously and crossfades between them to hide the ~157 ms round trip.
- **Next.js + three.js** web app on Vercel: the factory floor, a 3D twin of the robot, phone teleop pages, the marketplace, earnings and a printable report. The backend runs on my laptop behind tunnels whose URLs change every start, so it publishes its URL into SpacetimeDB with an HMAC proof and Vercel finds it from there. No backend keys live on Vercel.
- **Claude** writes a 3D scene for each new ticket, clusters failures, plans the data buy, reviews episodes, and judges real-arm trials from the live cameras.
- I built it with a team of coding agents working off one shared task board, which was its own lesson in coordination.

## Challenges I ran into

- **No arm overnight.** Nobody could be next to the YAM all night, so I built a physics sim of the same arm behind the same adapter and safety layer, and made "sim or real" a fact on every row. Sim and real numbers are never mixed.
- **Droids that have to wait for hours.** Agents normally run and finish. I needed them to park on a database condition and resume later with full context, so I built the wait-for protocol and resume bridge myself.
- **Remote inference at control rate.** A 157 ms round trip is several control steps. Async action chunks, dropping the stale prefix and crossfading made it smooth.
- **A real arm is not a sim.** Gravity pulls joints off their targets, the Jetson's ffmpeg has no H.264 encoder, the arm has to fold back to rest safely after every trial. Each one cost hours.
- **Ten trials is a small sample.** A strict confidence-interval rule can never certify 80% from 10 trials, so the factory ships on the point estimate and always shows the interval.
- **Many agents, one repo.** Agents with stale git state reverted each other's work more than once. A pre-commit hook now refuses silent deletions.

## What I learned

- The hard part of "AI deploys your robot" isn't the model. It's the plumbing that touches the physical world: who resets the cube, who takes over when it drops, how a GPU job wakes an agent, how a payout waits on a review.
- Agents need to be able to sleep. A session parked on a database condition costs nothing and comes back with full context. That turns a coding agent into a long-running worker.
- Guardrails belong in code, not prompts. The planner must cite a failure, a payout can't exceed the approved budget, the event log can't be rewritten.
- A sim is only useful if nobody can mistake it for the real thing. Everything simulated or stubbed is labelled `[SIM]` or `[STUB]` wherever it shows up.

## What's next

- Run more real-arm tickets end to end and publish the before/after numbers.
- More data types for the planner: phone video, UMI-style handheld demos, autonomous rollouts.
- Open tickets automatically from failures in production, and rank contributors by accepted episodes.
