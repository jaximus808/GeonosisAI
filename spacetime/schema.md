# SpacetimeDB schema (TDM-8)

The source is `spacetimedb/src/schema.ts`. The allowed enum values and defaults are in `spacetimedb/src/enums.ts`, and the `ctx.sender` helpers are in `spacetimedb/src/auth.ts`. Everything is published to maincloud `robo-factory-a706j`.

## Conventions

- **IDs:** every `id` is a u64 auto-inc and starts at 1. A u64 foreign key of `0` means "none". Indexed FKs use this sentinel, not `option`, so clients can filter on them.
- **Enums:** stored as plain strings. Reducers must validate them with `oneOf(field, value, ENUM)` from `enums.ts`.
- **Column suffixes:** `*_json` is JSON text, `*_uri` is an object-storage pointer, `*_cents` is integer US cents.
- **Indexes:** single-column btree indexes cover every `ticket_id`, `robot_id`, `episode_id` and `status`, plus `eval_run_id` and `task.assignee_id`. The index accessor is the column name, e.g. `ctx.db.task.status.filter('open')`.
- **Visibility:** every table is **public** (the dashboard, phones and Jetson subscribe to them) except `service_identity`. No public table holds secrets: Stripe `acct_`/`tr_` ids are references, not credentials.

## Tables

| Table | Vis. | One line |
|---|---|---|
| `service_identity` | private | Allow-list of reducer callers: identity (PK), role ∈ `SERVICE_ROLES` (owner, orchestrator, reviewer, payments, robot_client, factory, mcp, web), label, added_at. |
| `robot` | public | A registered arm: name, embodiment, status (offline/online/busy/paused/estop), adapter_version, camera_config_json, client_identity (Jetson), last_heartbeat. |
| `ticket` | public | A job for one robot. Fields: task_description, target_success, budget_cents (hard cap), approved_cents (sum of approved spend), spent_cents (reserved + paid), status (flywheel stage), dataset_id, iteration, deployed_checkpoint_uri, created_by. |
| `run_settings` | public | One row per ticket (PK ticket_id). Fields: eval_trials (10), success_threshold (0.8), require_ci_low (false), budget_cents, target_episodes, max_iterations, hyperparams_json `{lr, steps, batch_size, lora_rank, prompt}`. |
| `dataset` | public | The per-ticket training dataset (ticket_id unique): accepted_count, total_count, lerobot_uri. |
| `failure_cluster` | public | A diagnosed failure type for a ticket iteration: label, description, count, example_episode_ids. |
| `data_plan` | public | The planner's decision: data_type (clean_teleop/recovery_takeover/purchased), amount, unit and estimated cost, reasoning, failure_cluster_ids, status, plus the `approve_spend` fields approved_cents/by/at. |
| `task` | public | A paid marketplace task: type (clean_teleop/recovery_takeover/reset), status (open/claimed/submitted/completed/expired/cancelled), price_cents, assignee_id (contributor, 0 = none), claimed_at, lease_expires_at, completed_at, robot_id, data_plan_id. |
| `contributor` | public | A person or dataset seller. Fields: identity, kind (contributor/dataset_seller), name, devices, online, stripe_account_id (acct_…), payouts_enabled, total_paid_cents. |
| `episode` | public | One recording. Fields: kind (baseline/eval/clean_teleop/recovery_takeover/purchased), review_status (pending/accepted/rejected/not_reviewed), storage_uri, cameras_json, actions_uri, frame_count, duration_ms, takeover_ts, handback_ts, success, checkpoint_uri, task/contributor/offer FKs. |
| `review` | public | The reviewer verdict on an episode: accepted, reason, metrics_json, reviewer, ticket_id. |
| `qa_result` | public | One deterministic check on an episode: check_name, passed, detail_json. |
| `payment` | public | A payout row, created by `reserve_payment` before Stripe is called. Fields: ticket_id, contributor_id, episode_id (0 for dataset batches), dataset_offer_id, batch_ref, amount_cents, provider (`stripe_test`), provider_ref (tr_…), status (pending/paid/failed/reversed/blocked_budget/awaiting_onboarding), error. |
| `dataset_offer` | public | An offer to buy episodes from a seller (contributor.id). Fields: price_per_episode_cents, needed_count, ingested_count, accepted_count, status, source_uri. |
| `training_run` | public | A Modal fine-tune: iteration, episode_ids, status (queued/running/done/failed/cancelled), modal_call_id, base_checkpoint_uri, checkpoint_uri, hyperparams_json, metrics_json, gpu_cost_cents, error, start/finish times. |
| `eval_run` | public | An N-trial eval of a checkpoint: kind (baseline/candidate/ab_live), status, trials, completed, successes, ci_low/ci_high (Wilson 95%), training_run_id, robot_id. |
| `eval_trial` | public | One trial: eval_run_id, trial_index, success, duration_ms, episode_id, failure_note. |
| `agent_session` | public | A long-running agent (planner, reviewer, dispatcher or a droid). Fields: status (starting/running/paused/waiting_for/resuming/done/failed), current_step, factory_session_id plus factory_session_ids_json, waiting_for_json (pause signal and predicate), resume_payload_json, ticket_id. |
| `robot_command` | public | The command bus to the Jetson. Fields: robot_id, ticket_id, seq (per-robot 1, 2, 3...), type (run_baseline/run_eval/start_teleop/stop_teleop/pause_for_takeover/resume_policy/handback/reset_request/load_checkpoint/home/stop/soft_estop), payload_json, status (issued/acked/done/failed/preempted), issued_by, result_json, issued/acked/finished times. |
| `event_log` | public | An append-only evidence log: id, ts, actor, type, payload_json. The columns are unchanged from the placeholder module. Never edit or delete rows. |

## Reducers in this ticket

The reducer tickets (TDM-9..15) add the rest. Each one goes in its own file and is re-exported from `index.ts`.

- **`init`:** on a fresh DB, seeds the publisher as `owner` in `service_identity` and logs `infra.module_init`.
- **`bootstrap_owner()`:** seeds the owner row once, for the hard-coded owner identity only. Maincloud predates `init`, so this ran once on 2026-10-24.
- **`register_service_identity(identity, role, label)`** and **`revoke_service_identity(identity)`:** owner-only. Both log to `event_log`.
- **`append_event(actor, type, payload_json)`:** service identities only since TDM-15 (anonymous callers get `unauthorized: not a service identity`). See the event_log section below.

## event_log is append-only (TDM-15, `src/event_log.ts`)

- **One write path.** `logEvent(ctx, actor, type, payload)` is the only `ctx.db.eventLog.insert` in the module. Each ticket's `tdmNLog` helper delegates to it. No reducer updates or deletes `event_log` rows. `payload` is either a JSON string, kept verbatim, or a value that gets JSON-encoded.
- **`append_event` checks**, beyond `requireService`:
  - `actor` must look like `kind[:qualifier...]`: a lowercase kind, no spaces, at most 128 chars.
  - `type` must be lowercase and dotted, like `area.event`.
  - `payload_json` must be valid JSON. `""` is stored as `{}`. The limit is 64 KB.
  - A non-owner may not log as another service role, or as `spacetime`.
- **Actor convention:** `orchestrator`, `planner`, `reviewer`, `dispatcher`, `droid:<role>`, `robot:<robot_id>`, `human:<contributor_id>`, `human:via_mcp`. Reducers also log as their caller's service role, as `contributor:<id>`, as `seller`, and as `spacetime`. Only the shape is enforced, so older actors such as `orchestrator.diagnose` and `fake-robot:<id>` still pass.
- **Verify:** `python3 -I scripts/verify_event_log.py` prints the row count, the id/ts range and the `rdf-evlog-chain-v1` chain hash, which is the same as the /events seal. `--save cp.json` / `--check cp.json` / `--expect N:HASH` prove the first N rows are unchanged. The `static` mode audits the module source: no update/delete path, inserts only in `event_log.ts`, and every exported reducer reaches `logEvent`.

## Agent-session and robot-command reducers (TDM-13, `reducers/agent_robot.ts`)

Callers: agent reducers need the `orchestrator` or `factory` role (owner always passes). Positional HTTP args are in the order shown.

- **`upsert_agent_session(id, role, factory_session_id, status, current_step, name, ticket_id)`:** `id` 0 creates the row. On update, `""`/`0` leave name, ticket_id and factory_session_id unchanged. A new factory_session_id is appended to `factory_session_ids_json`. It cannot enter `waiting_for` or `resuming` (use the next two), and leaving `waiting_for` clears `waiting_for_json`.
- **`agent_wait_for(session_id, predicate_json, resume_payload_json)`:** sets status `waiting_for` and stores the predicate verbatim in `waiting_for_json`, e.g. `{"table":"dataset","ticket_id":1,"field":"accepted_count","op":">=","value":5}`. Compound predicates use `{"all":[...]}` or `{"any":[...]}`; `op` is one of `== != > >= < <=`. `resume_payload_json` (`""` = none) is the state handed to a restarted session.
- **`agent_resume(session_id, new_factory_session_id, reason)`:** the resume bridge's atomic wake-up claim. It succeeds only from `waiting_for` or `paused`, so a second bridge loses. It sets status `resuming` and clears `waiting_for_json`. **Native pause/resume:** pass `""`, then message the same `factory_session_id`. **End-and-restart:** pass the new Factory session id. It becomes `factory_session_id`, is appended to `factory_session_ids_json` under the same `agent_session.id`, and `resume_payload_json` stays on the row to seed it. A bridge can also claim with `""` first, start the new session, and then report its id with `upsert_agent_session`.
- **`agent_step(session_id, step_text)`:** updates current_step and last_update. A `starting` or `resuming` session moves to `running`. Refused once the session is `done` or `failed`.
- **`issue_robot_command(robot_id, type, payload_json, ticket_id)`:**
  - Who can call it: the owner, `orchestrator` or `factory`; or the contributor holding a claimed, unexpired task on that robot. A claimant may only send start_teleop, stop_teleop, pause_for_takeover, resume_policy, handback or soft_estop, and the ticket_id comes from the task.
  - Each command gets the next per-robot `seq`.
  - "Job" types (run_baseline, run_eval, start_teleop, reset_request, home, load_checkpoint) are refused while another job is open (issued or acked) on the robot.
  - Every non-e-stop command is refused while a soft_estop is open or `robot.status = estop`.
  - **`soft_estop` is never blocked.** Any service role may send it, as may the claimant. It skips every busy and latch check, is accepted even for an unknown robot_id, and preempts every other open command on the robot (status `preempted`, with `result_json.preempted_by`).
- **`ack_robot_command(id)`**, **`complete_robot_command(id, result_json)`**, **`fail_robot_command(id, error)`:** only the robot's bound `client_identity` may call these, or a `robot_client` role if the robot has no binding. Ack moves issued to acked; repeating an ack is a no-op, and acking a preempted command is refused, so it must not run. Complete needs an acked command. Fail works from issued or acked.
- **event_log types:** agent.session_created, agent.session_updated, agent.waiting_for, agent.resumed (with mode native or restart), agent.step, robot.command_issued, robot.soft_estop (with the preempted list), robot.command_acked, robot.command_done and robot.command_failed.

## Auth hooks (`auth.ts`)

- `requireService(ctx)`: the caller must be on the allow-list.
- `requireRole(ctx, 'orchestrator', 'reviewer')`: the caller must have one of the listed roles; owner always passes.
- `requireOwner(ctx)`: the caller must be the owner.

`claim_task` is the exception: contributors call it as themselves, so it should match `contributor.identity` against `ctx.sender` instead of using the allow-list.

To give a service its own identity:

1. `curl -X POST https://maincloud.spacetimedb.com/v1/identity` returns `{identity, token}`.
2. As the owner, call `register_service_identity`. Over HTTP, pass the identity as a 1-element tuple: `[["0x<hex>"], "orchestrator", "orchestrator@laptop"]`. The form `{"__identity__":"0x<hex>"}` also works; a bare hex string is rejected.

## Ticket, run-settings and robot reducers (TDM-9, `src/reducers/tickets.ts`)

Each call that changes state appends one `event_log` row; `actor` is the caller's service role. Tests: `spacetime/tests/test_tdm9_ticket_reducers.py` (local server only).

| Reducer (HTTP args in order) | Who may call | Notes |
|---|---|---|
| `register_robot(name, embodiment, adapter_version, camera_config_json)` | orchestrator, robot_client, factory | Upsert by `name`, so the Jetson can call it on every boot. Starts `offline`. A robot_client caller is bound as `client_identity`. Logs `robot.registered` or `robot.updated`. |
| `robot_heartbeat(robot_id, status)` | robot_client (its own robot only), orchestrator | Sets `status` (ROBOT_STATUSES) and `last_heartbeat`. The first robot_client to heartbeat an unbound robot is bound to it. Logs `robot.status_changed` **only when the status changes or the client binds**, so steady heartbeats don't flood the log. |
| `create_ticket(robot_id, task_description, target_success, budget_cents, settings_overrides_json)` | orchestrator, mcp, web | `target_success` in (0, 1]. Inserts `ticket` (status `created`, iteration 0, `created_by` = role), its `dataset` row and its `run_settings` row. Settings are `DEFAULT_RUN_SETTINGS`, except `success_threshold` defaults to `target_success`; then overrides (`""` or a JSON object with the `update_run_settings` keys, minus `budget_cents`) are applied. Logs `ticket.created` with `ticket_id`; reducers return nothing, so read the new id from that event or the `ticket` table. |
| `update_run_settings(ticket_id, patch_json)` | orchestrator, mcp, web | Keys: `eval_trials` (int 1–50), `success_threshold` (0–1), `require_ci_low` (bool), `budget_cents` (int ≥ 0 and ≥ the ticket's approved/spent cents; also written to `ticket.budget_cents`), `target_episodes` (1–10000), `max_iterations` (1–20, > current iteration), `hyperparams` (object merged into the current one: `lr`, `steps`, `batch_size`, `lora_rank`, `prompt`), `data_type_override` (TDM-20: `null`/`""` = the planner decides, else `clean_teleop`\|`recovery_takeover`; stored in `run_settings.data_type_override`, `''` = none). Unknown keys are rejected. HTTP front end: `PATCH`/`GET /tickets/{id}/settings` on the orchestrator (`run_settings_api.py`). Frozen once the ticket is shipped/failed/cancelled. Logs `ticket.run_settings_updated` with the patch, before and after. |
| `set_ticket_status(ticket_id, status, note)` | orchestrator; mcp/web may only set `cancelled` | Allowed: `created→onboarding\|baseline`, `onboarding→baseline`, `baseline→diagnose`, `diagnose→plan_data`, `plan_data→dispatch`, `dispatch→review`, `review→train\|dispatch`, `train→eval`, `eval→shipped\|diagnose\|plan_data\|dispatch`, and any non-terminal status → `failed\|cancelled`. `shipped`, `failed` and `cancelled` are terminal. Looping back from `eval` increments `ticket.iteration` and is refused once `iteration + 1 >= max_iterations`. Logs `ticket.status_changed` {from, to, note, iteration}. |

Status names: the TDM-9 brief's names map onto the TDM-8 enum as diagnosing=`diagnose`, planning=`plan_data`, collecting=`dispatch`, reviewing=`review`, training=`train`, evaluating=`eval`. Two edges go beyond the brief: `created→baseline` lets a robot that is already onboarded skip onboarding, and `review→dispatch` reopens collection when rejections leave the set short. To make the chain strict again, edit `TDM9_TICKET_TRANSITIONS`.

## Dataset-offer and QA reducers (TDM-14, `src/reducers/dataset_offer.ts`)

The rule: a seller is paid only for `target_episodes − (episodes already uploaded or accepted for the ticket)`, and only for purchased episodes that both QA layers accepted. The cap is enforced in the reducers. Reducers are serializable transactions, and open offers **reserve** their unfilled slots, so concurrent offers can never promise more than the target.

- **live episode:** a `clean_teleop`, `recovery_takeover` or `purchased` episode that is not rejected. An episode is rejected if `review_status = rejected`, if any of its latest `qa_result` checks failed, or if its latest `review` is not accepted.
- **committed(ticket)** = max(live episodes, `dataset.accepted_count`) + Σ over open offers (`proposed|accepted|ingesting|qa`) of (`needed_count` − that offer's live episodes).

| Reducer | Caller | Does |
|---|---|---|
| `create_dataset_offer(ticket_id, seller_id, price_per_episode_cents, source_uri?)` | orchestrator, mcp, web | `needed_count = max(0, target_episodes − committed)`. Rejects when 0, when the seller isn't `dataset_seller`, or when the ticket is closed. Status `proposed`. |
| `accept_dataset_offer(offer_id)` | orchestrator, mcp, web, or the seller's own identity | `proposed → accepted`. |
| `ingest_purchased_episode(offer_id, storage_uri, actions_uri, cameras_json, frame_count, duration_ms, success?)` | orchestrator, factory, web, or the seller | Inserts `episode(kind=purchased, review_status=pending)` and bumps `ingested_count` and `dataset.total_count`. Rejected when the offer already has `needed_count` live episodes ("is full: 3/3"), or when the ticket is already at target (other uploads landed). A rejected delivery frees its slot for a replacement, so `ingested_count` counts every delivery. Status becomes `ingesting`, then `qa` when full. |
| `record_qa_result(episode_id, check_name, passed, detail_json)` | reviewer, orchestrator | One QA check row. The latest row per `check_name` wins. Query per episode with `SELECT * FROM qa_result WHERE episode_id = N`. |
| `finalize_offer(offer_id)` | orchestrator, reviewer, payments | Refused while any delivered episode still lacks a verdict. `accepted_count = min(needed_count, episodes with ≥1 QA check, all latest checks passed AND an accepted review)`. Status `done` (the "settled" state). Logs `payout_cents`. Then call `reserve_dataset_payment(offer_id, batch_ref)` (TDM-11). |
| `cancel_dataset_offer(offer_id, reason)` | orchestrator, mcp, web | Open offer → `cancelled`, which releases its reserved slots. |

Local test (44 checks, including target 5 with 2 accepted → `needed_count` 3, and the 4th ingest rejected): `spikes/tdm-14-dataset-offer/local_test.sh`.

## Contributor identity and presence (TDM-60, `src/reducers/contributors.ts`)

A contributor **is** a SpacetimeDB connection identity: the browser connects anonymously, the server issues a token, and `apps/web` keeps it in localStorage (`lib/spacetime/provider.tsx`), so a refresh reconnects as the same identity and finds the same row. Never the owner token: service identities are refused. No passwords.

Tables added (TDM-60): `contributor_presence` (public; contributor_id PK, last_seen, page), `contributor_contact` (**private**; contributor_id PK, email, updated_at — PII for Stripe onboarding, owner SQL only), `contributor_presence_sweep` (private schedule row, seeded by the first heartbeat).

| Reducer (HTTP args in order) | Who may call | Notes |
|---|---|---|
| `register_contributor(name, email, devices, kind, source_ticket_id)` | any **non-service** identity, as itself | Upsert by `ctx.sender`: one row per identity. `name` `""` → `guest-<6 hex of identity>`; trimmed, ≤ 64. `email` `""` keeps the stored one; else validated, lowercased, written only to `contributor_contact`. `devices` ≤ 8 of `[a-z0-9_]{1,32}`, deduped (`phone`, `android_webxr`, `laptop`, `leader_arm`). `kind` ∈ CONTRIBUTOR_KINDS, fixed at creation. `source_ticket_id` (0 = none) is the ticket whose QR brought them (event only). Sets `online`. Logs `contributor.registered` / `contributor.updated` (`has_email`, never the email). |
| `contributor_heartbeat(page)` | the contributor itself | Every 30 s from marketplace pages. Moves `contributor_presence.last_seen`; logs `contributor.online` **only on the offline→online transition**. |
| `contributor_offline()` | the contributor itself | Tab closed (pagehide). Logs `contributor.offline` {reason: left} on transition. |
| `sweep_contributor_presence` (scheduled, 30 s) | the scheduler (harmless if called: it only applies the time rule) | Online contributors with no heartbeat for 75 s go offline; logs `contributor.offline` {reason: stale}, actor `spacetime`. |

`stripe_account_id` / `payouts_enabled` are set by TDM-63's `set_contributor_payout_account`. Tests (45 checks, incl. the sweep; local server only): `spacetime/tests/test_tdm60_contributor_reducers.py`.
