# Demo video: shot list and edit plan

For the human recording it. Length 3:00 or less (Devpost). The story: before, ticket, decision, marketplace and teleop, training, after, report. The before/after footage comes from the real-arm run tonight. Everything else can be screen capture.

## Rules for the edit

- Real-arm footage and sim footage never share a frame without a label. Put a lower-third on every sim shot: "Simulation (MuJoCo). Scripted stand-in policy, not π0.5."
- Put "Stripe test mode" on screen whenever money is visible.
- Every number on screen must be on the dashboard or report at the same time. No number goes in a caption that the page does not show.
- If the real run did not finish, use cut B below and say so in the title card. Never cut sim footage into the arm's before/after.
- Note the eval run id on each before/after clip, so the clip matches the ticket page.

## Capture setup

- Screen: 1920x1080, browser at 100% zoom, Foundry (dark) theme, operator logged in (`/ops/login`), no bookmarks bar, notifications off.
- Arm: phone on a stand, landscape, one wide shot that shows the arm, the object, the target and a laptop with the ticket page. Same framing for before and after.
- Phone screen: iOS or Android screen recording, portrait.
- Audio: voiceover recorded separately after the edit is locked. Use the pitch script (`pitch.md`) as the base.

## Shot list

Times are targets for the final cut. "Page" is the URL on https://geonosis.jaxonp.com unless noted.

| # | Time | Length | Source | Page or setup | What to capture | On-screen text |
|---|---|---|---|---|---|---|
| 1 | 0:00 | 8 s | Screen | `/` | Landing page hero ("Ticket in. Deployed robot out."), slow scroll to "How the line works". | GeonosisAI. Plug in your robot, tell it the job, and the factory gets it deployed. |
| 2 | 0:08 | 15 s | Arm camera | Real YAM, base checkpoint | 2–3 baseline trials of the base π0.5 policy failing. Say the trial number and outcome out loud. | "Before: base π0.5. `[TODO after real run: k/10]`" |
| 3 | 0:23 | 12 s | Screen | `/tickets/new` | Type the ticket prompt ("Put the cube in the bin, 80% reliable, $50 budget") and submit. Cut on the ticket page loading. | — |
| 4 | 0:35 | 10 s | Screen | `/factory` | The ticket's lane: the Baseline station running, a Factory Droid riding the rail to its station. | Every agent in the pipeline, live. |
| 5 | 0:45 | 25 s | Screen | `/tickets/[id]`, decision card | Failure clusters, then the planner's decision card: data type, amount, reasoning quoting a cluster, the rejected alternative, the backend and model badge. Zoom in on the reasoning. Hold 3 s on it. | "The decision: which data fixes these failures." Quote the first sentence of the reasoning. |
| 6 | 1:10 | 6 s | Screen | `/tickets/[id]` | Click "Approve plan". Tasks appear. | Approved by a human. |
| 7 | 1:16 | 10 s | Screen + phone | Discord channel; phone Messages | The Discord post with its share card and QR. Then the iMessage offer "Reply YES to claim" and the reply with the task link (only if live texts are on; otherwise Discord only). | Every task goes to Discord and by text. Test mode. |
| 8 | 1:26 | 25 s | Phone | `/j/<task>/<channel>` link, then `/teleop/[taskId]` | Open the invite, claim, the camera feed, drive with the on-screen sticks, take over, hand back. Then the review verdict arriving. | "A contributor drives the arm from a browser." If this is the sim robot: the sim lower-third. |
| 9 | 1:51 | 8 s | Screen | `/tickets/[id]`, episodes and reviews | One accepted episode with its video, one rejected with its reason. Payout reserved. | Rejected episodes are not paid. Stripe test mode. |
| 10 | 1:59 | 12 s | Screen | `/tickets/[id]` training run, `/factory` Train station | The training run row: Modal H100, status, steps, GPU cost. The Training Droid's step on the rail. | "π0.5 LoRA, 1x H100. `[TODO after real run: steps, minutes]`" |
| 11 | 2:11 | 20 s | Arm camera | Real YAM, fine-tuned checkpoint | Same framing as shot 2. 2–3 trials of the fine-tuned checkpoint. Say outcomes out loud. | "After: the foundry's checkpoint. `[TODO after real run: k/10]`" |
| 12 | 2:31 | 15 s | Screen | `/tickets/[id]/report` | The report: data bought, why, cost, before vs after with intervals. Slow scroll. | Numbers from the report only. |
| 13 | 2:46 | 8 s | Screen | `/events` | The event log with its chain-hash seal, showing what ran on its own. | "`[TODO after real run: N rows, hash]`" |
| 14 | 2:54 | 6 s | Title card | — | Wordmark, live link, repo link, team. | geonosis.jaxonp.com |

Total: 3:00.

### Optional inserts (use only if there is time)

- `/live` on a projector: robot, the line, before/after, agent wake-ups (5 s). Good B-roll under the voiceover of shot 5 or 12.
- Sim robot on `/robots/[id]`: the three.js twin with top, side and wrist views of the ticket's Claude-generated scene (6 s). Sim lower-third required.
- `/practice`: the in-browser practice run before a contributor can claim arm control (5 s).
- `/ops/settings`: the LLM backend chip. Not in production: the site is locked to `api`, so skip this on the live site.

## Cut B: the real run did not finish

- Replace shots 2 and 11 with the sim robot on `/robots/[id]` (twin and sim cameras), each with the sim lower-third, and a title card before shot 2: "The real-arm run is in progress. This is the MuJoCo simulation of the same arm."
- Replace shot 10's caption with the real H100 fine-tunes proven separately: "π0.5 LoRA on H100. 200 steps on public real-YAM data, 460 s, loss 0.043 to 0.0072." Show the training run row or terminal output that has those numbers.
- On sim tickets, training in `make demo` is `[STUB] no GPU` unless it ran with `--real-modal`. Keep the badge visible. Do not crop it.
- Never show `[SEEDED]` evals in the video. They are for rehearsals.

## Edit plan

1. Lay down shots 1–14 in order on the timeline at the lengths above. Cut on action (a click, a trial ending).
2. Lock picture at 3:00 or less before recording voiceover.
3. Record the voiceover from `pitch.md`, trimmed to match each shot. Speak the trial outcomes yourself; do not use text-to-speech.
4. Add lower-thirds: sim label, test-mode label, and the numbers from the report.
5. Music: none, or quiet and royalty-free. The arm's sound on shots 2 and 11 stays in.
6. Export 1080p H.264. Upload to YouTube as unlisted (or Vimeo). Check the link in a private window. Paste it into Devpost.
7. Cover image for Devpost: a frame from shot 12 (the report) or shot 4 (the factory floor).

## Before you record

- [ ] Real run finished, or cut B chosen.
- [ ] Eval run ids for before and after written down.
- [ ] Report saved for the real ticket.
- [ ] Backend reachable from the live site (`/api/tdm110/backend` returns `{"ok": true}`).
- [ ] Operator logged in; access code not visible on screen (it opens the whole site).
- [ ] Phone numbers hidden in shot 7 (crop or blur).
