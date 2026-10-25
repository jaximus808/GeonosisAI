# GeonosisAI: 2-minute demo runbook

The goal is a clean 2:00 video. Nothing has to be robust, only this exact path, and every number on screen must
be honest. Keep the **SIM**, **STUB** and **SEEDED** labels visible.

---

## The trick: two tickets

| Ticket | Purpose | How |
|---|---|---|
| **A: "pre-baked"** | Footage for Train, Eval-after, Report and Shipped | Run it fully **before** recording, using `--seed-evals` so it is fast. |
| **B: "live"** | Footage for filing, baseline, diagnose, plan, Discord, phone | Record it live, with no `--seed-evals`, so the trials visibly run. |

---

## 1. Setup (once, about 5 min)

**Publish the web app** from the repo root:

```
git push && W=$TMPDIR/geonosis-prod && git worktree add --detach $W HEAD && cd $W && \
npx vercel@65.2.0 link --yes --project geonosisai --scope jaximus808s-projects && \
npx vercel@65.2.0 deploy --prod --yes --scope jaximus808s-projects; cd - ; git worktree remove --force $W
```

**Tab 1 (backend).** Start it first, then wait for the box of URLs. Use fast mode for ticket A (section 2):

```
make demo-maincloud ARGS="--orch-port 8028 --web-port 3737 --seed-evals"
```

Check that the banner says `Modal: STUB (no credits used)`.

**Tab 2 (tunnel).** Start it in a second terminal tab after Tab 1, and leave both running:

```
make demo-hybrid ARGS="--stack attach --orch-url http://127.0.0.1:8028 --stream-port 8791 \
  --robot-name sim-yam-mujoco --stdb-url https://maincloud.spacetimedb.com --db robo-factory-a706j"
```

Wait about 1 minute, then check that `geonosis.jaxonp.com/api/tdm110/backend` shows `"ok": true`.

**Browser.** Use Chrome. Log in at `geonosis.jaxonp.com/ops/login` with the passcode (`grep RDF_OPERATOR_PASSCODE .env`).
In the cockpit, cancel any old open tickets (More, then Cancel ticket) so the robot is free.

**Optional:** to show the phone text offer on camera, set `RDF_TEXT_MODE=live` in `.env` before Tab 1.

---

## 2. Pre-bake ticket A (about 10 min, not recorded)

1. Tab 1 is already in fast mode from setup.
2. **New ticket:** "Pick up the cube and place it in the empty plate.", Sim robot, $50, target 80%.
3. **Approve the plan,** then do the jobs from your phone until the plan count is met. Claim, drive, Finish – success.
4. **Let it finish:** it runs Train (**STUB**), then Eval (seeded, about 95%), then **Shipped**. Leave ticket A alone after that.
5. **Restart Tab 1 without the fast flag:** Ctrl-C, then
   `make demo-maincloud ARGS="--orch-port 8028 --web-port 3737"`.
   Then restart Tab 2 (the tunnel).

---

## 3. Shot list: 2:00 total

Record the laptop at 1440x900 and the phone separately. You can speed up waiting in the edit (2 to 4x on trials).

| # | Time | Screen and capture | Voice-over (about 180 words, read at an easy pace) |
|---|---|---|---|
| 1 | 0:00 (8 s) | **`/` landing**: Hero, "Ticket in. Deployed robot out." | *Robots need task-specific data to work. GeonosisAI is a droid foundry: you file a ticket, and it gets your robot to the reliability you need.* |
| 2 | 0:08 (8 s) | **`/tickets/new`**: Type ticket **B**'s prompt and submit. | *Here's the job: put the cube on the plate, 80% reliable, $50 budget.* |
| 3 | 0:16 (14 s) | **Cockpit, Baseline**: Trials filling in live, with a failure note ("missed grasp"). Speed this up. | *First it measures. The base policy runs ten trials on the robot, which is our MuJoCo sim of the YAM arm tonight.* |
| 4 | 0:30 (16 s) | **Cockpit, Diagnose, then Plan**: Failure cluster cards. Zoom on the planner's reasoning, then click **Approve plan**. | *Claude watches the failures and groups them by cause. The planner buys exactly the data that fixes them: recovery takeovers at the failure point, not generic demos. I approve the spend.* |
| 5 | 0:46 (8 s) | **`/factory`**: Droids at their machines and a speech bubble changing. | *Every agent is visible. Some reason with Claude; Factory Droids sleep until SpacetimeDB wakes them.* |
| 6 | 0:54 (6 s) | **Discord #job-board**: The new job card with its QR code and code. | *The jobs go out to Discord and by text.* |
| 7 | 1:00 (26 s) | ****Phone****: Scan the QR, claim, the cockpit, drive the arm, place the cube, Finish, then **Accepted** and the payout. | *Anyone with a phone can claim a job, drive the arm from the browser, and get paid once Claude's reviewer accepts the episode. Stripe test mode.* |
| 8 | 1:26 (10 s) | **Ticket **A**: Train, then Eval**: The training row (**STUB** label), then the Eval trial strip at about 9/10. | *Accepted episodes fine-tune pi zero point five. On the sim we stub the GPU; on Modal it's one H100.* |
| 9 | 1:36 (14 s) | **Ticket **A**: Report**: Before and after with confidence intervals; data bought and cost. | *Then it proves it: before and after, with error bars, and every decision logged.* |
| 10 | 1:50 (10 s) | **Title card (or real-arm clip)**: Wordmark, geonosis.jaxonp.com, the repo link. | *Ticket in. Deployed robot out. GeonosisAI.* |

**If you get a real-arm clip** (someone teleoperating the YAM from a phone), use it as shot 10's background,
or cut 3 seconds of it into shot 7: "and the same flow drives the real YAM."

---

## 4. What's real vs sim (say it plainly if asked)

- **Real:** Claude diagnosis, planning and review; Factory Droids waking on SpacetimeDB; maincloud; Discord and text jobs;
  phone teleop with recording and review; Stripe test-mode payouts; the hybrid deploy at geonosis.jaxonp.com.
  π0.5 LoRA fine-tunes were proven separately on a Modal H100.
- **Sim:** the arm is MuJoCo. Its policy is a scripted stand-in, labelled **[SIM]** and **not** π0.5. Training in the
  video is a **STUB**. Its before/after gap is by design, not learned.

---

## 5. If something breaks while recording

| Symptom | Fix |
|---|---|
| Site says "backend offline" | Restart Tab 2 and wait about 1 minute. |
| "Waiting for robot: TKT-X has it" | Click **Run this ticket now**, or cancel the other ticket. |
| Sim arm locked (e-stop) | On the robot page, click **Clear e-stop**. |
| Laptop hot or laggy while driving | Set Quality to **Smooth**, and close other 3D tabs (`/robots`, `/live`, `/factory`). |
| Episode rejected | Fine; show it ("rejected episodes aren't paid"), then do one more. |
| Discord card missing | Check `RDF_DROP_MODE=live` in `.env`; use `/ops/drops`, then **Post now**. |
| Baseline taking too long | Speed it up in the edit, or use ticket A's finished baseline. |

---

## 6. Submit (Devpost due 8:30 AM)

1. Fill in or remove the `TODO after real run` lines, using sim numbers labelled as sim:
   `grep -n "TODO after real run" docs/submission/*.md README.md`
2. Paste `docs/submission/devpost.md` into Devpost, then add the video, `github.com/jaximus808/GeonosisAI` and `geonosis.jaxonp.com`.
3. Practise `docs/submission/pitch.md` twice; the judge Q&A is in `docs/submission/qa.md`.
