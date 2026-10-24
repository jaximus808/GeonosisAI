# Pitch script

Supersedes `pitch-script.md`. Two versions: 3 minutes with stage cues, and 1 minute. Judge Q&A answers are in `qa.md`.

Rules:
- Do not say a `[TODO after real run: ...]` number until it is real. If the real run did not produce it, use the sim fallback line and say "simulation" out loud.
- Say "test mode" whenever money comes up, and "simulation" whenever the sim robot is on screen.
- The sim policy is a scripted stand-in. Never call it π0.5 on stage.
- Target about 430 spoken words for 3 minutes. Run it twice against a timer. If you run long, cut the "under the hood" paragraph first.

## Setup before you start

- Tabs, in order: `/factory`, the ticket page `/tickets/[id]` with the decision card in view, `/live` on the projector, `/marketplace` on a spare phone.
- Both checkpoints preloaded on the policy server (`POST /policy/activate {checkpoint_uri, preload_only:true}`), so the A/B swap is instant.
- Arm homed, object on its start mark, physical e-stop in the operator's hand.
- A Discord post or the `/join` QR on screen, so a judge can scan it.
- The before/after video queued, in case the arm misbehaves.
- Check `https://geonosis.jaxonp.com/api/tdm110/backend` returns `{"ok": true}` (the laptop backend is reachable).

## 3-minute version

**[0:00–0:20] The problem. Face the judges. Nothing on screen yet.**

Getting a robot to do a new task reliably is not a model problem. It is a deployment project. Engineers sit on site for weeks: run the policy, watch it fail, collect more data, retrain, repeat. microagi raised a fifty-five million dollar seed to do that with people. We built the foundry that does it with agents. It's the droid foundry from Geonosis, but for real robots.

**[0:20–0:40] The ticket. Point at the ticket page.**

You plug in a robot and file a ticket: "Put the cube in the bin, 80 percent reliable, 50 dollar budget." The foundry runs a baseline. Ten trials on the current policy. `[TODO after real run: "It got k out of 10."]`

**[0:40–1:10] The decision. Point at the decision card. Slow down: this is the key moment.**

Here is the part that matters. The foundry does not just ask for more data. Claude groups the failures by why they happened, and the planner decides *which kind* of data fixes them. A recovery takeover, where a person grabs the paused robot mid-failure. Or a clean demonstration from scratch. Northstar found fifty recovery demos beat fifty clean ones, 78 to 64 percent. Our planner makes that call from the measured failures, and logs its reasoning verbatim. `[TODO after real run: read the first sentence of the planner's reasoning.]`

**[1:10–1:35] The marketplace. Hold up a phone. Point at the QR.**

Then it buys exactly that data. Every task goes out as a post with a QR code and as a text: reply YES to claim. Scan it now. **[To a judge:]** You just became the supply. You get one task, you see the cameras, you drive the arm from your browser, you hand back. A reviewer checks the episode on numbers, not vibes, and pays it. Test mode.

**[1:35–2:15] The proof. Turn to the arm, or to `/live`.**

Then a π0.5 fine-tune runs on an H100, and the new checkpoint runs the same eval. Here is the base policy. **[Operator: activate "before", 3 trials. Say each outcome.]** Now the foundry's checkpoint, hot-swapped. **[Activate "after", 3 trials.]** `[TODO after real run: "Ten-trial eval: k out of 10 before, k out of 10 after."]`

Sim fallback, if the arm is not used: "This is our MuJoCo simulation of the same arm, with a scripted stand-in policy. It proves the line runs end to end. The real π0.5 fine-tunes ran separately on H100."

**[2:15–2:40] Why a deployment company buys this. Face the judges.**

Could a deployment company do this themselves? They can, and today they do. But every new task means standing up a data operation: teleop, operators, QA, training, evals. Most of that money goes to the wrong data. We measure where the robot fails, buy exactly the data that fixes it, retrain, and prove it with a real eval. They want "robot does task X at 90 percent." We sell that outcome, not the data.

**[2:40–3:00] Under the hood and close. Point at `/factory`.**

Under the hood, SpacetimeDB is the bus. Claude diagnoses and plans. Factory Droids do the engineering, and they sleep on a database predicate until a row changes, so they can wait hours for a person or a GPU for free. Watch one wake up. Robots have no CI. This is it.

**[3:00] Stop.**

## 1-minute version

About 150 words. Use it for the expo floor or when a judge walks up mid-demo.

Getting a robot to do a new task reliably takes weeks of engineers on site: run it, watch it fail, collect data, retrain. GeonosisAI does that loop with agents.

You file a ticket: "cube in the bin, 80 percent, 50 dollars." The foundry runs a baseline, Claude groups the failures, and the planner decides which data fixes them: recovery takeovers or clean demos. That choice is the product. Recovery data beat clean data 78 to 64 in Northstar's study.

Then it buys exactly that data from people on their phones. Scan this QR and you get a paid job, test mode. It reviews every episode, fine-tunes π0.5 on an H100 and proves the fix with a real eval. `[TODO after real run: "k out of 10 before, k out of 10 after."]`

Deployment companies want "robot does task X at 90 percent." We sell that outcome, not the data.

## Timing fallbacks

- Long at 1:35: skip the reviewer sentence and go straight to the arm.
- Arm will not start: "This is the recorded run from this morning." Play the video and keep saying the counts.
- Nobody scans the QR: open `/marketplace` on the spare phone, hand it to a judge and claim there.
- Backend offline (the site says "backend offline"): switch to the video and the printed report. Do not show the sim as if it were the arm.
