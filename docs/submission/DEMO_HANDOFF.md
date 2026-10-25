# GeonosisAI demo handoff (for a fresh agent)

You are picking up the GeonosisAI Cal Hacks build a few hours before the deadline. **Devpost is due Sun 2026-10-25 8:30 AM PT.** Your job:

1. get a rehearsal ticket to ship end to end;
2. help Jaxon record a 2:00 video;
3. help submit.

Read this whole file before you touch anything.

- **Script and shot list:** the Google Doc "GeonosisAI demo: recording steps", <https://docs.google.com/document/d/1971Im9GUmpjtsouOPl8xxzvKz5ABd76kx0-8fnJ74NI/edit>. Use the **Full script** tab; its cue times are current. Tab 1's start times are stale.
- **Older runbook:** `docs/submission/demo-2min-runbook.md` (two-ticket plan). The Google Doc replaces it.
- **Submission text:** `docs/submission/{devpost,pitch,qa}.md`.

## What the product does (one breath)

1. A customer files a ticket ("put the cube on the plate, 80%, $50").
2. The **sim** robot runs a baseline eval.
3. Claude diagnoses the failures and plans the data to buy.
4. The operator approves the plan.
5. A job goes out by Discord and text.
6. A person drives the **real YAM arm** from their phone and records a demo.
7. A Claude reviewer accepts it and pays.
8. The policy trains (a **STUB** on the sim path).
9. It re-evaluates on the sim and ships.
10. A report shows before and after.

The ticket is a TDM-125 **split**: evals run on `sim-yam-mujoco` (robot id 3), and data is collected on `yam-01` (robot id 4, the real right arm on the Jetson).

## Current state (as of about 01:00 PT)

**Verified working:**
- **Backend:** the hybrid deploy. The Vercel site at `https://geonosis.jaxonp.com` talks to the laptop backend over cloudflared tunnels.
- **Sim:** baseline evals, Claude diagnose and plan, and the auto-reset of sim reset tasks.
- **Real arm:** teleop of yam-01 from the phone, which the user says "works decently well".
- **Jetson code:** the Jetson runs the latest robot code (synced, boot pose logged). yam-01 is `online` with its e-stop cleared.

**Built and unit-tested, but never run on the live system:**
- **Recording on the real arm.** Commit eb7193b added the GStreamer `x264enc` encoder, because the Orin Nano's ffmpeg has no libx264. On the Jetson the pipeline was tested by hand in /tmp only.
- **Everything after recording:** upload from the Jetson through the tunnel → `create_episode` → Claude review → payout → train (STUB) → eval on the sim → ship → report.
  - **Nobody has seen this run on maincloud.** The only "shipped" tickets (5 and 6) are seeded `[Sample]` rows.
  - **This is the main risk. Rehearse it before recording.**
- **On the teleop page:** Reset arm and Exit (0577c08, e073f33).
- **Phone layout:** the mobile teleop console (c7e38cf).
- **Accounts:** phone sign-in and the account merge (11215d2, 8ae42d7, fc07ab3).
- **Defaults:** new tickets default to 3 eval trials and 1 demo per round (5243906). They only show on the live site after a web deploy.

**Waiting on the user:**
- **The web deploy.** The live site is behind HEAD; a deploy ships everything above plus the new landing hero (fa4b288).
- **Cancel TKT-0011 and TKT-0012.** Both are stuck in `dispatch` with open jobs on yam-01; the user cancels them in the cockpit (More, then Cancel ticket).

## Hard rules (do not break these)

**Real arm:**
- Follow `docs/real-arm-safety.md`.
- **Never** re-zero the arms or write motor registers. Never run `zero_*_arm.py`. Never touch vrteleop or the LARP files in `~/northstar`.
- **Never start, stop or restart `geonosis-robot-teleop`, or move the arm, yourself.** The user does that, with the e-stop in hand.
- You may read on the Jetson: `ssh northstar@northstar 'journalctl --user -u geonosis-robot-teleop ...'`, `systemctl --user status`, `ip -details link show can_right`.
- If the arm must be disabled, support it first. Stopping the service leaves it limp, or in damping mode if it is still moving.

**Maincloud database (`robo-factory-a706j`):**
- Read-only `spacetime sql -s maincloud robo-factory-a706j "SELECT ..."` is fine.
- Leave writes to the user: `spacetime call`, cancelling tickets, and module publishes.
- Never run a deploy or maincloud write that the permission system refused; give the user the command instead.

**Live ports and tunnels:**
- Don't touch 8028 (orchestrator), 3737 (local web), 8791/8792 (sim stream), 8765 or 3767, or the cloudflared tunnels.
- Run any local preview on another port, such as 3853.

**Test stacks:**
- Any test stack you start uses `RDF_DROP_MODE=dry_run`, its own `RDF_DROP_DIR`, `RDF_TEXT_MODE=dry_run` and blank `DISCORD_BOT_TOKEN`, `RDF_DROP_DISCORD_WEBHOOK` and `INKBOX_API_KEY`.
- A dry-run stack once deleted real Discord posts.

**Secrets and privacy:**
- Never print `.env` values.
- Mask phone numbers. Never text a number that hasn't opted in.

**Modal:** about $20 of credit left. No real fine-tunes unless the user asks.

**Copy:** keep UI copy plain and product-like. No "Stripe test mode" banners everywhere.

**Commits:**
- Commit on `main`. End each message with `Claude-Session: https://claude.ai/code/session_01SwbLzzhUiCJrbfavsTrUBJ`.
- The pre-commit hook blocks file deletions unless `RDF_ALLOW_REVERT=1` is set; set it only for intentional deletions.

## Commands the user runs (give them verbatim)

```
# Tab 1, backend. Add --seed-evals for a fast rehearsal; drop it for the real recording.
make demo-maincloud ARGS="--orch-port 8028 --web-port 3737 --seed-evals"

# Tab 2, tunnel (after Tab 1 prints its URLs). Restart it whenever Tab 1 restarts.
make demo-hybrid ARGS="--stack attach --orch-url http://127.0.0.1:8028 --stream-port 8791 --robot-name sim-yam-mujoco --stdb-url https://maincloud.spacetimedb.com --db robo-factory-a706j"

# Web deploy (from the repo root)
cd /Users/jaxon/coding-project/robot-deploy-fact && git push && W=$TMPDIR/geonosis-prod && git worktree add --detach $W HEAD && cd $W && npx vercel@65.2.0 link --yes --project geonosisai --scope jaximus808s-projects && npx vercel@65.2.0 deploy --prod --yes --scope jaximus808s-projects; cd - ; git worktree remove --force $W

# Backend health
curl -s https://geonosis.jaxonp.com/api/tdm110/backend     # expect "ok": true

# Jetson: sync new robot code (service must be stopped first; support the arm)
ssh northstar@northstar 'systemctl --user stop geonosis-robot-teleop'
scripts/jetson/tdm118_geonosis.sh sync
ssh northstar@northstar 'systemctl --user start geonosis-robot-teleop'

# Jetson: clear a latched e-stop after the user checks the arm
ssh northstar@northstar 'systemctl --user kill --kill-whom=main -s USR1 geonosis-robot-teleop'

# Someone else holds yam-01
cd services/orchestrator && uv run python ../../scripts/demo/release_robot_hold.py --target maincloud --robot yam-01 --yes
```

## Step 1: rehearsal (do this first, off camera)

1. **Prepare.** The user deploys the web app, cancels TKT-0011 and TKT-0012, and restarts Tab 1 with `--seed-evals`, then Tab 2.
2. **File the ticket.** The user files: *"Pick up the red cube and place it on the plate. 80% reliable, $50 budget."* Robot: Sim. Collect robot: yam-01. Under Advanced: Trials per eval 3, Target episodes 1.
3. **Collect.** The user approves the plan, claims the job on the phone, drives, presses **Record**, does the task, then **Finish (success)**.
4. **Watch every step.** Tell the user which step it's on; when something stalls, find out why and fix it.

Statuses, in order: `created → baseline → diagnose → plan_data → dispatch → review → train → eval → shipped`.

```
spacetime sql -s maincloud robo-factory-a706j "SELECT id, status FROM ticket WHERE id = <T>"
spacetime sql -s maincloud robo-factory-a706j "SELECT id, type, status, robot_id, assignee_id FROM task WHERE ticket_id = <T>"
spacetime sql -s maincloud robo-factory-a706j "SELECT id, task_id, review_status FROM episode WHERE ticket_id = <T>"
spacetime sql -s maincloud robo-factory-a706j "SELECT id, ticket_id, status FROM training_run WHERE ticket_id = <T>"
spacetime sql -s maincloud robo-factory-a706j "SELECT id, type, actor FROM event_log WHERE id > <last seen id>"   # tail the event log
ssh northstar@northstar 'journalctl --user -u geonosis-robot-teleop --since "-5 min" --no-pager | grep -v cap_ffmpeg | tail -40'
```

Watch for these:
- **First real recording.** The log should say `video encoder selected … gst-x264enc`. If **Record** stays grey, the red text under it gives the reason.
- **Episode row.** After Finish, an `episode` row with this `task_id` should appear (task_id ≠ 0; task_id 0 rows are eval-trial recordings). If none appears, check the Jetson log for upload errors to the orchestrator endpoint (`orch-endpoint` lines), then the orchestrator's log in Tab 1.
- **Review.** The reviewer should set `review_status` to accepted or rejected. If rejected, read why in event_log and record one more.
- **Train.** It should start once accepted ≥ target_episodes (1). On the sim path it is the STUB; the banner says `Modal: STUB`.
- **Eval.** The fine-tuned eval must run on **robot 3 (sim)**, never on yam-01. With 3 trials and an 80% target it needs 3/3. Seeded evals (`--seed-evals`) make this deterministic.

Fix any breakage in code and run the relevant tests:
- orchestrator: `cd services/orchestrator && uv run pytest -q`
- robot client: `cd services/robot-client && uv run --with pytest pytest -q tests`
- web: `cd apps/web && pnpm exec tsc --noEmit -p . && pnpm test`

Then commit. Robot-client fixes need a Jetson sync, which the user runs; web fixes need a deploy, which the user runs.

## Step 2: the real recording

1. **Restart without the fast flag.** The user restarts Tab 1 without `--seed-evals`, so the trials visibly run, then restarts Tab 2.
2. **Record.** Same ticket text, same settings. Follow the **Full script** tab: you on camera, landing page, new ticket, baseline, diagnose and plan, the job card and text, the phone driving the real arm, train and eval, report, title card.
3. **Edit.** Record the laptop at 1440×900 and the phone separately, then speed up the waits 2–4× in the edit.

If the live fine-tuned eval doesn't reach 3/3 on camera, use the rehearsal ticket's finished Train, Eval and Report screens for shots 7 and 8.

## Step 3: submit

1. `grep -n "TODO after real run" docs/submission/*.md README.md`. Fill these in with the real numbers, labelled as sim, or remove them.
2. Paste `docs/submission/devpost.md` into Devpost, and add the video, the GitHub repo and `geonosis.jaxonp.com`.
3. Be honest if asked:
   - **Real:** Claude diagnosis, planning and review; Discord and text jobs; phone teleop of the real YAM.
   - **Sim:** evals run in MuJoCo with a scripted stand-in policy, and training in the video is a stub. π0.5 LoRA fine-tunes were proven separately on a Modal H100.

## Where things live

| Area | Path |
|---|---|
| Orchestrator stations (bus handlers) | `services/orchestrator/src/rdf_orchestrator/handlers/` |
| Planner, evaluate, dispatcher, review, text offers | `services/orchestrator/src/rdf_orchestrator/{planner,evaluate,dispatcher,text_offers}.py` |
| Real-arm runtime on the Jetson | `services/robot-client/src/rdf_robot_client/tdm119_teleop.py`, `teleop.py` (`/ws/control`, `reset_arm`), `recorder.py`, `adapters/yam_larp.py` |
| Web cockpit | `apps/web/src/components/cockpit/`, `apps/web/src/lib/tdm116/cockpit.ts` |
| Teleop page | `apps/web/src/components/teleop/` |
| Ticket defaults | `apps/web/src/lib/tdm74/ticket.ts` (`TDM74_START`) |
| Landing hero | `apps/web/src/components/landing/HeroFoundry.tsx` |
| Jetson ops script | `scripts/jetson/tdm118_geonosis.sh` |
| Demo scripts | `scripts/demo/` (`release_robot_hold.py`, `merge_contributors.py`, `cancel_old_tickets.py`, `seed_samples.py`) |
