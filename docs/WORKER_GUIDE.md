# Worker guide: read before starting a ticket

Shared facts for every agent working tickets on Tandem canvas `HL4RZKEQ`. `CONTEXT.md` is the spec. Where this guide and CONTEXT.md disagree, this guide wins: it reflects the user's later decisions.

## Overrides to CONTEXT.md
- **GPU work runs on Modal**, not Vultr/RunPod. The Modal CLI is logged in on this laptop.
- **The robot computer is a Jetson** running `services/robot-client/` (CONTEXT.md calls it robot-bridge) with the I2RT YAM SDK. Hardware is one YAM arm and a spare Jetson.
- **Evals are ~10 trials**, not 20. We need only a few clean demos (π0.5 plus 3–5 demos on an easy task).
- **Entry points** are the website, or the Claude connector (remote MCP).
- **Our backend** (`services/orchestrator/`) owns ticket start, run settings, the Modal integration and all cross-agent orchestration. Factory Droids are only for the long-running agents.
- **Fly.io deploy is on hold** until the user adds Fly secrets (morning of Sat Oct 24). Run everything locally.

## Secrets
- They live in `.env` (gitignored). **Never print, cat, echo or log values**, and never put them in code, commits or board notes.
- To check which keys are set: `./scripts/check-env.sh`, which prints only set/MISSING.
- If you add a key, append it to `.env.example` with a blank value.
- Set: ANTHROPIC_API_KEY, FACTORY_API_KEY, STRIPE_SECRET_KEY (sk_test_), STRIPE_PUBLISHABLE_KEY, HF_TOKEN, MODAL_TOKEN_ID/SECRET, SPACETIME_HOST/DB_NAME/TOKEN.
- Not set: JETSON_HOST/USER, FLY_API_TOKEN, WANDB_API_KEY, NGROK_AUTHTOKEN, STRIPE_WEBHOOK_SECRET.

## SpacetimeDB (already set up; see `spacetime/README.md`)
- Maincloud host is `https://maincloud.spacetimedb.com`. The database is `robo-factory-a706j`. The module is TypeScript in `spacetime/spacetimedb/src/`, built with CLI v2.13 and the npm `spacetimedb@2.13` client.
- Publish with `cd spacetime && spacetime publish -s maincloud robo-factory-a706j --yes=remote,migrate`. **Don't pass `--delete-data`** unless your ticket says so.
- HTTP reducer call: `POST /v1/database/<db>/call/<reducer>`. Send `Authorization: Bearer $SPACETIME_TOKEN` and `Content-Type: application/json`, with a JSON array of args in order.
- SQL: `POST /v1/database/<db>/sql`, with the raw SQL as the body.
- TS bindings use camelCase (`conn.db.eventLog`, `conn.reducers.appendEvent`).
- **Security:**
  - Anonymous callers can invoke any reducer and read any public table. Important reducers (claim_task, payments, reviews, robot commands) must check `ctx.sender` against an allow-list of service identities. Keep sensitive tables private.
  - `SPACETIME_TOKEN` is the owner token. Per-service identities come from `POST /v1/identity`.

## Repo layout (from TDM-1)
- **TS:** pnpm workspace with `apps/web`, `apps/mcp` and `apps/mobile`, named `@rdf/*`.
- **Python:** one uv project each (Python 3.11) under `services/{orchestrator,factory,training,robot-client}`, plus `services/payments` (Stripe).
- **Make targets:** `make install`, `make dev`, `make env-check`.
- **Spike code:** `spikes/<ticket>/`.

## Working in the shared checkout
- Several workers run at the same time. Stay inside your ticket's surface. Commit by explicit path, never `git add -A`.
- If you need a dependency in a shared manifest (a root `package.json`, or a service's `pyproject.toml`), make one targeted edit and re-run install. Don't reformat other people's code.
- **Nobody is at the arm tonight.** For [HIL] or Jetson work, build against a fake or simulated robot, and leave a ready-to-run script plus a checklist for the human.
- Need the user's answer? Add it to `docs/morning-questions.md` and keep going with the best assumption.
- Made a decision with lasting impact? Add a line to `docs/decisions.md`: the decision, why, and how to revert quickly.
- **Never fake** eval numbers, success rates or planner decisions. Label any mock as mocked in the UI.
- Never edit `event_log` rows. It is append-only.
- No real money: Stripe stays in test mode.

## Python ↔ SpacetimeDB (TDM-7 decision)
- Python talks straight to the database over the WebSocket JSON protocol (`v1.json.spacetimedb`), with no sidecar. The reference client is `spikes/tdm-7-spacetime/stdb_ws.py` (only dependency: `websockets`), and TDM-16 turns it into the shared helper.
- Reuse one keep-alive `httpx.Client` for HTTP reducer calls (~120 ms), or call reducers over the open socket. One-off requests cost 1–3 s in TLS handshakes.
- Rows from other clients come in as positional arrays. The helper maps them to snake_case dicts using `/schema`.

## SpacetimeDB reducer tickets (TDM-9..15): working together on one module
- The schema is in `spacetime/spacetimedb/src/schema.ts` (and `enums.ts`); per-table notes are in `spacetime/schema.md`. Auth helpers are in `auth.ts`: `requireService`, `requireRole`, `requireOwner`. A foreign key of 0 means "none". Identity args over HTTP are `["0x<hex>"]`.
- Put your reducers in **your own file** (`src/reducers/<area>.ts`) and add **one** re-export line to `src/index.ts`. Re-read `index.ts` right before you edit it, and check afterwards that the other tickets' lines are still there.
- Change `schema.ts` only if your brief needs a new column or index. Keep it to one targeted edit, and say so in your result.
- **Test locally first:** start a throwaway server on your own port (`spacetime start --listen-addr 127.0.0.1:<30xx>` with its own data dir under `spikes/` or `$TMPDIR`) and publish there.
- **Publishing to maincloud is serialized.** Take the lock with `mkdir spacetime/.publish-lock` (retry every 10 s if it exists). Then run `npx tsc --noEmit` for the whole module; if another ticket's half-written file breaks the build, wait and retry rather than editing it. Publish with `--delete-data=never --yes=remote,migrate`, then `rmdir spacetime/.publish-lock`. Adding a column needs `--yes=remote,migrate,break-clients`. That drops connected clients once, so do it only when your brief needs the column. After any schema change, rerun bindings generation (TDM-16's command / `apps/web` `gen:bindings`). Never leave the lock behind.
- Every state-changing reducer appends an `event_log` row (actor, type, payload_json). Important reducers check `ctx.sender` with the auth helpers.
- The **one-time event_log wipe is held** until right before the Saturday-night real run (TDM-72). Until then, test rows on maincloud are fine. Tag them `test.*`.

## Orchestrator runtime (TDM-18)
- `services/orchestrator/src/rdf_orchestrator/bus.py` registers handlers per table. Examples: `@on_enter('training_run', where=lambda r: r.status=='queued')`, and `on_insert`/`on_update`/`on_delete`. Rows allow attribute access.
- The bus resyncs after a reconnect, so handlers must be idempotent.
- Call reducers with `stdb.call_reducer(...)`, not ad-hoc HTTP. Log through `log.py`, which tags `ticket_id` automatically.
- Put new handlers in `handlers/<area>.py`, register them where `handlers/event_log_tap.py` is registered, and add tests under `tests/`.
- Run: `cd services/orchestrator && uv run rdf-orchestrator --port 8000`. Health: `/healthz`.

## Temp files
- Keep scratch files in `spikes/<ticket>/tmp/` (gitignored by `tmp/`) or in a ticket-named dir under `$TMPDIR`. Never use a shared scratchpad, and never use generic names like `probe.py`.

## Reducers live on maincloud (check `spacetime/schema.md` for args and auth)
- TDM-9 tickets: `register_robot`, `robot_heartbeat`, `create_ticket` (the new id is in the `ticket.created` event), `update_run_settings`, `set_ticket_status`. Status names come from `enums.ts`: created→onboard→baseline→diagnose→plan_data→dispatch→review→train→eval→shipped/failed/cancelled.
- TDM-12 training/eval: `queue_training_run`, `set_training_status`, `start_eval_run`, `record_eval_trial`, `finalize_eval_run` (matches `stats.EvalRunFinalizer`), `record_failure_cluster`, `record_data_plan` (insert-only).
- TDM-13 agents/robot: `upsert_agent_session`, `agent_wait_for(session_id, predicate_json, resume_payload_json)`, `agent_resume` (only one caller can win it), `agent_step`, `issue_robot_command(robot_id, type, payload_json, ticket_id)` plus ack/complete/fail. `soft_estop` is always accepted.
- TDM-10 marketplace: `create_task(ticket_id, type, price_cents, payload_json)`, `claim_task(task_id, contributor_id)` (atomic), `heartbeat_claim`, `release_task`, `complete_task(task_id, result_json)`, `cancel_task(task_id, reason)`, `expire_task_leases()`. Arm-control leases are 60 s (heartbeat every 30 s) and reset leases 10 min. One live control lease per robot. `(task_id, claimed_at)` is the lease token.
- TDM-11 episodes/payments: `create_episode(...)`, `submit_review(episode_id, accepted, reason, metrics_json)` (an accept reserves the payout), `reserve_payment(episode_id)`, `reserve_task_payment(task_id)` (resets), `reserve_dataset_payment(offer_id, batch_ref)` (call after TDM-14's `finalize_offer`), `mark_payment_paid/failed/awaiting_onboarding/reversed`, `approve_spend(ticket_id, data_plan_id, approved_cents, approved_by)`. Payments over budget are stored as `blocked_budget`.
- Arg encoding over HTTP: optional args are `{"some":x}` / `{"none":[]}`, and timestamps are `{"__timestamp_micros_since_unix_epoch__":n}`. The shared `rdf_common.spacetime_client` handles this for you.

## Shared clients (TDM-16), so use these and don't roll your own
- Python: `rdf_common.spacetime_client` provides `SpacetimeClient`/`AsyncSpacetimeClient` (`call_reducer`, `sql`, `from_settings`) and `subscribe(...)`. Typed dataclasses and reducer wrappers are in `rdf_common.spacetime_tables`.
- TS: `@rdf/spacetime-bindings`. Regenerate everything with `pnpm gen:bindings` from the repo root, after publishing.

## Committing in the shared checkout (IMPORTANT)
- **Always commit with an explicit pathspec: `git commit -m "..." -- <your paths>`.** A plain `git commit` takes whatever *anyone* has staged, and that has already put one ticket's files under another ticket's message. Don't leave files staged between steps.

## Training recipe (TDM-36 decision; full details in the "S-05a decision" note on the Architecture tab)
- π0.5 LoRA (`gemma_2b_lora` + `gemma_300m_lora`) on 1× H100, using openpi `215abfb`. Batch 32, action horizon 50, 224 px images. Joint actions are trained as deltas; the gripper stays absolute.
- **Our datasets must be LeRobot format v2.1** (openpi pins that LeRobot version).
- 2,000 steps takes ~49 min end to end and costs ~$3–4, whether we have 5 or 10 episodes. Fixed overhead is ~4.5 min per run. Checkpoints are 9.5 GB.
- The working spike is `spikes/tdm-36-finetune/` (Volume `rdf-s05a-checkpoints`). Production code goes in `services/training/`.

## Running the orchestrator (IMPORTANT)
- **Don't leave a full orchestrator process running against maincloud.** Its handlers (diagnose, planner, dispatcher, payouts) act on maincloud rows, so two copies would duplicate Claude calls, tasks and payments. Run full-app tests against a local throwaway SpacetimeDB. Against maincloud, run only your own router or handler in isolation, and stop it when you're done.
- The one real maincloud orchestrator gets started by the planner/orchestrator session for the real run.
- Port 8000 on this laptop is taken by an unrelated project. Use another port.
- Maincloud already holds test robot `test.tdm19-yam` (id 1) and ticket 1. Names starting `test.` are ignored by robot auto-selection.
- **Committing only your hunks of a shared file** (with a temporary `GIT_INDEX_FILE`): build that index from the *current* HEAD immediately before you commit (`GIT_INDEX_FILE=$t git read-tree HEAD`), apply only your hunks, commit, and delete it. A stale private index already reverted another ticket's files once tonight. **After every commit, check `git show --stat HEAD`:** no deletions, and only your files. If anything else shows up, fix it immediately with a follow-up commit and say so in your result.

## Episode media storage (TDM-4 decision)
- Media lives on Modal Volume `rdf-media`, behind a signed-URL app at `https://jaxonp808--rdf-media.modal.run`. Modal jobs mount the same Volume at `/media`, with no copy.
- In Python, use `services/orchestrator/src/rdf_orchestrator/storage.py`: `get_backend()` → `signed_put(key, content_type)`, `signed_get(uri_or_key)`, `put(key, bytes|Path)`, `list(prefix)`. Build keys with `episode_key(ticket, episode, filename)`, which gives `tickets/{t}/episodes/{e}/{file}`. URIs look like `modal-vol://rdf-media/<key>`.
- Over HTTP, through the orchestrator: `POST /uploads/sign`, `POST /uploads/sign-batch`, `GET /media/sign?uri=`, `GET /media/list?ticket_id=&episode_id=`.
- The Modal app must not use `from __future__ import annotations`, because FastAPI then misreads `Request`.

## Full-loop test harness (TDM-28): use it to prove your work end to end
- `make test-loop` runs a scripted driver. `make test-loop DRIVER=orchestrator` runs the real orchestrator, and skips until the TDM-22 flywheel is registered in `rdf_orchestrator.handlers`.
- `make fake-stack ARGS="-- --success-prob 0.5"` boots everything and leaves it running for manual testing. It prints the `SPACETIME_*` lines to point your service at.
- Everything runs on a throwaway local SpacetimeDB at 127.0.0.1:3028 (db `tdm28loop`), with data under `$TMPDIR/tdm-28/`. It's wiped on every boot and never touches maincloud.
- The fake robot is `rdf-robot-client --fake`. Every fake output is tagged `[FAKE]` / `fake: true`, and its payload keys are documented in the `fake.py` docstring.
- The orchestrator driver makes **real** Anthropic calls on fake data (diagnose and planner). That's fine for tests, but run it on purpose, not in a loop.

## Policy inference (TDM-37 decision; details in the "S-05b decision" note)
- **Remote action chunks, run asynchronously.** The arm runs at 30 Hz, and each call returns a 50-step chunk (1.67 s). Re-query every 10 steps (~3 Hz) over a websocket while the current chunk keeps executing. On arrival, skip the first round(latency/33 ms) actions (~5–6) and blend over 3 steps. If no new chunk has arrived by step 40, hold the pose. The simple fallback: run 25 steps, then make a blocking query.
- **Latency on H100:** p50 157 ms, p95 183 ms (websocket), of which 41 ms is GPU. Cold start is ~33 s.
- **Spike endpoint:** `POLICY_ENDPOINT_URL` (Modal app `rdf-s05b-policy`, us-west, H100→A100→L40S). It scales to zero; warm it with `spikes/tdm-37-latency/warm.py up` and stop it with `down`. **It has no auth yet.**
- The base π0.5 actions don't mean anything for the YAM until the fine-tuned config defines the YAM state/action mapping.

## Publishing to maincloud from a clean checkout (preferred)
- The shared working tree often holds other tickets' half-finished reducer files. Publish from a clean worktree of HEAD instead:
  - `git worktree add --detach $TMPDIR/<ticket>-pub HEAD`
  - symlink `spacetime/spacetimedb/node_modules` into it
  - `npx tsc --noEmit`
  - take the lock
  - `spacetime publish` from `$TMPDIR/<ticket>-pub/spacetime`
  - release the lock and remove the worktree
- That means **commit your reducer first, then publish**.
- If a publish is refused by a permission prompt, release the lock and note it in your result; the orchestrator session will publish.

## Modal training app (TDM-38)
- The app `rdf-training` is in `services/training/modal_app.py`. Deploy with `make deploy-modal`, and refresh the Modal Secret `rdf-training` from `.env` with `make modal-secrets` (it prints key names only).
- **Functions:**
  - `convert(ticket_id, episode_manifest)` returns `modal-vol://rdf-datasets/lerobot/<repo_id>`.
  - `finetune(ticket_id, dataset_uri, hyperparams)` returns `{checkpoint_uri, metrics}`. It's a stub until TDM-40.
  - The `Policy` class has `health()` and `infer(obs)`, and is a stub until TDM-41.
- **Volumes:** `rdf-checkpoints` at `/checkpoints` (pi05_base is cached in `openpi-cache/`; outputs go to `runs/` and `metrics/`), `rdf-datasets` at `/datasets`, and `rdf-media` mounted read-only at `/media`.
- **Calling from Python (Modal 1.6):** `modal.Function.from_name("rdf-training","finetune").spawn(...)`, then poll `modal.FunctionCall.from_id(id).get(timeout=0)`, which raises `TimeoutError` while the job is still running. `Function.lookup` doesn't exist.
