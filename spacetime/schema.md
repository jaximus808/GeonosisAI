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
| `agent_session` | public | A long-running agent (planner, reviewer, dispatcher or a droid). Fields: status (running/paused/waiting_for/done/failed), current_step, factory_session_id plus factory_session_ids_json, waiting_for_json (pause signal and predicate), resume_payload_json, ticket_id. |
| `robot_command` | public | The command bus to the Jetson. Fields: robot_id, ticket_id, type (run_baseline/run_eval/start_teleop/stop_teleop/pause_for_takeover/handback/reset_request/load_checkpoint/stop), payload_json, status (issued/acked/done/failed), issued_by, result_json, issued/acked/finished times. |
| `event_log` | public | An append-only evidence log: id, ts, actor, type, payload_json. The columns are unchanged from the placeholder module. Never edit or delete rows. |

## Reducers in this ticket

The reducer tickets (TDM-9..15) add the rest. Each one goes in its own file and is re-exported from `index.ts`.

- **`init`:** on a fresh DB, seeds the publisher as `owner` in `service_identity` and logs `infra.module_init`.
- **`bootstrap_owner()`:** seeds the owner row once, for the hard-coded owner identity only. Maincloud predates `init`, so this ran once on 2026-10-24.
- **`register_service_identity(identity, role, label)`** and **`revoke_service_identity(identity)`:** owner-only. Both log to `event_log`.
- **`append_event(actor, type, payload_json)`:** unchanged and still open to any caller. Gate it with `requireService` once every service has its own identity.

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
| `update_run_settings(ticket_id, patch_json)` | orchestrator, mcp, web | Keys: `eval_trials` (int 1–50), `success_threshold` (0–1), `require_ci_low` (bool), `budget_cents` (int ≥ 0 and ≥ the ticket's approved/spent cents; also written to `ticket.budget_cents`), `target_episodes` (1–10000), `max_iterations` (1–20, > current iteration), `hyperparams` (object merged into the current one: `lr`, `steps`, `batch_size`, `lora_rank`, `prompt`). Unknown keys are rejected. Frozen once the ticket is shipped/failed/cancelled. Logs `ticket.run_settings_updated` with the patch, before and after. |
| `set_ticket_status(ticket_id, status, note)` | orchestrator; mcp/web may only set `cancelled` | Allowed: `created→onboarding\|baseline`, `onboarding→baseline`, `baseline→diagnose`, `diagnose→plan_data`, `plan_data→dispatch`, `dispatch→review`, `review→train\|dispatch`, `train→eval`, `eval→shipped\|diagnose\|plan_data\|dispatch`, and any non-terminal status → `failed\|cancelled`. `shipped`, `failed` and `cancelled` are terminal. Looping back from `eval` increments `ticket.iteration` and is refused once `iteration + 1 >= max_iterations`. Logs `ticket.status_changed` {from, to, note, iteration}. |

Status names: the TDM-9 brief's names map onto the TDM-8 enum as diagnosing=`diagnose`, planning=`plan_data`, collecting=`dispatch`, reviewing=`review`, training=`train`, evaluating=`eval`. Two edges go beyond the brief: `created→baseline` lets a robot that is already onboarded skip onboarding, and `review→dispatch` reopens collection when rejections leave the set short. To make the chain strict again, edit `TDM9_TICKET_TRANSITIONS`.
