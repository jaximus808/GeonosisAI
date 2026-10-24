# rdf_factory: Factory Droid client (TDM-30)

`FactoryClient` starts, messages and resumes Factory Droid sessions, and mirrors every one of them into SpacetimeDB:
- the `agent_session` row holds the status, `current_step` and `factory_session_ids`;
- `event_log` gets one `droid.*` row per step.

The design follows the S-04 decision (TDM-29): Protocol A (native resume), with Protocol B (restart with `resume_payload`) as the fallback.

## Modes

| `FACTORY_MODE` | Runs | Resume | Notes |
|---|---|---|---|
| `local` (default tonight) | `droid-sdk` drives a `droid` subprocess on this machine, with its cwd set to the repo checkout (`FACTORY_REPO_DIR`) | `droid_sdk.Session.resume(id)`, from any process | Each turn opens the droid process and closes it again, so a parked session costs nothing. File edits are allowed only inside the session workdir `services/factory/work/sessions/<agent_session_id>/` (gitignored) unless `write_roots` widens that. `Execute` is off unless `allow_commands` lists command prefixes, and shell metacharacters are always refused. |
| `cloud` | Factory Sessions API (`api.factory.ai/api/v0`) on a Droid Computer created from `FACTORY_ENVIRONMENT_ID`, or reused from `FACTORY_COMPUTER_ID` or the cached `work/state/cloud_computer.json` | `POST /sessions/{id}/messages` to the same session | Retries the transient 404 on the first message after `POST /sessions`. Retries 429/5xx/transport errors with backoff and honours `Retry-After`. A session `GET` 404 or a computer in `error` raises `SessionGone`, which triggers Protocol B. Each agent works in its own dir, `/home/factory-user/agents/<id>`, because sessions on one computer share a disk. Events come from polling `GET /sessions/{id}/messages` every 3 s. |

Cloud is ready but unproven live. Its unit tests use `httpx.MockTransport`. Before switching to cloud, push the repo and add it to the Environment.

## API (`from rdf_factory import FactoryClient`)

```python
async with FactoryClient() as fc:            # mode from FACTORY_MODE; SPACETIME_* from env
    r = await fc.start_session(role, brief, env=None, repo_ref="", ticket_id=0, name="",
                               write_roots=None, allow_commands=None)      # -> TurnResult
    r = await fc.send(agent_session_id, text)                  # another turn, no resume claim
    r = await fc.resume(agent_session_id, text, reason="", claim=True)     # resume-bridge entry
    r = await fc.restart_with_payload(agent_session_id, text="", resume_payload=None)  # force protocol B
    await fc.finish(agent_session_id, "done" | "failed")
    async for ev in fc.stream_events(agent_session_id, until_turn_end=True): ...   # live events
    unsubscribe = fc.subscribe(agent_session_id_or_None, callback)
```

- **Return value.** Each call returns after the droid's turn ends. `TurnResult` holds `agent_session_id`, `factory_session_id`, `protocol` (`start|send|native_resume|restart`), `status`, `ok`, `text`, `turn`, `waiting_for`, `resume_payload`, `credits`, `credits_total`, `seconds`, `error` and `factory_session_ids`.
- **Roles.** `onboarding_droid`, `data_droid` or `training_droid`; `planner`, `reviewer` and `dispatcher` are also accepted.
- **CLI.** `uv run rdf-factory start|send|resume|restart|finish|show ...`. Run `--help` for details. Events print live, and the TurnResult prints as JSON.

### Resume bridge (TDM-27) contract

The bridge calls **`await fc.resume(agent_session_id, "<what changed>")`**:

1. **Claim.** If the row is `waiting_for` or `paused`, `resume` calls `agent_resume(id, "", reason)`, the atomic wake-up claim. If another bridge already won, it raises `ResumeConflict`.
   - If the bridge already claimed itself (status is `resuming`), pass nothing extra: resume skips the claim.
   - It raises `ResumeConflict` for `running`, `done`, `failed` and `starting`.
2. **Protocol A.** It sends `[resume-bridge] <text>` to the same Factory session.
3. **Protocol B.** Used when that session is gone (local: "Session not found"; cloud: 404 or computer error).
   - A new session is seeded with the droid contract, `resume_payload_json` (or the workdir's `resume_payload.json`) and a snapshot of the row.
   - Its id is reported with `upsert_agent_session` and appended to `factory_session_ids_json`.
   - The `droid.session_restarted` event records the previous id.

`resume` returns when the resumed turn ends. The status is then `waiting_for` again, `done`, `paused` or `failed`. The bridge does not need to write status itself.

### Droid contract (prepended to the first message)

The droid must end every turn with exactly one final line:
- `DONE`: the session goes to `done`.
- `WAITING_FOR <table>:<field><op><value>`, e.g. `WAITING_FOR dataset:accepted_count>=5`: the session goes to `waiting_for`. The predicate is stored as `{"table","field","op","value","ticket_id","raw"}`. A JSON object after `WAITING_FOR` is used verbatim.

Before a `WAITING_FOR` line, the droid writes `resume_payload.json` in its workdir and prints `RESUME_PAYLOAD {...}`. That payload becomes `resume_payload_json`.

A turn with neither line leaves the session `paused` (idle, can be messaged). Every follow-up message (`send`, `resume`) repeats a one-line reminder of the rule.

## What lands in SpacetimeDB

| When | Reducer |
|---|---|
| `start_session` | `upsert_agent_session(0, role, "", "starting", "starting: <brief>", name, ticket_id)` |
| Factory session id known | `upsert_agent_session(id, role, <factory id>, "running", "Factory session started (...)")` |
| Each step (see `STEP_KINDS`) | `agent_step(id, <event text>)`. While a turn runs with no new step, a heartbeat `agent_step(id, "<last step> · still working (Ns into turn T)")` fires at least every `heartbeat_s` = 20 s. |
| Each logged event | `append_event("droid:<role minus _droid>", "droid.<kind>", <event json>)` |
| Turn ends | `agent_wait_for(id, predicate, payload)`, or `upsert_agent_session(..., "done"/"paused"/"failed", ...)` |
| Resume | `agent_resume(id, "", reason)`, then protocol A or B as above |

The caller needs the `factory` service role (`register_service_identity`) or the owner token. Reducer transport errors and 5xx are retried. A failed step or event write is logged and never kills a droid turn. A failed status transition is logged at error level.

## Event shape (v1): what the agents panel (TDM-35/76) consumes

Each event has the same JSON shape everywhere:
- in-process (`stream_events`, `subscribe`, `on_event`);
- in `work/state/<agent_session_id>.events.jsonl`;
- as `event_log.payload_json` for the logged kinds.

```json
{"v": 1, "kind": "tool_call", "agent_session_id": 1, "role": "data_droid", "mode": "local",
 "factory_session_id": "63583e50-…", "turn": 1, "seq": 4, "ts_us": 1792827973000000,
 "text": "Read services/common/pyproject.toml", "tool": "Read",
 "data": {"tool_use_id": "call_…", "input": {"file_path": "…"}}}
```

`text` is a one-line summary (300 characters or fewer) that the panel can show as is. `seq` increases per agent_session within one client process. `turn` counts turns across restarts.

| kind | logged to event_log | moves current_step | `data` |
|---|---|---|---|
| `session_started` | yes | yes | model, cwd, workdir, protocol |
| `session_restarted` | yes | yes | previous_factory_session_id, model, cwd, workdir |
| `turn_started` | yes | yes | protocol, prompt_preview |
| `tool_call` | yes | yes | tool_use_id, input (long strings cut to 600 characters) |
| `tool_result` | yes | no | tool_use_id, is_error, preview |
| `assistant_text` | yes | yes (`says: …`) | chars, message_id, full_text (up to 4 KB) |
| `permission_denied` | yes | yes | reasons |
| `waiting_for` | yes | yes (`waiting for …`) | predicate, resume_payload |
| `turn_completed` | yes | via status | subtype, success, status, credits, credits_total, seconds, tokens, final_text, error |
| `error` | yes | yes | error_type, detail |
| `thinking`, `state`, `usage` | no (in-process and jsonl only) | no | state / token counters |

**Panel recipe:**
- Subscribe to `agent_session` for the live status and current_step.
- Subscribe to `event_log` and keep rows whose `type` starts with `droid.`, grouped by `payload.agent_session_id`, for the step history.
- Credits per session come from `turn_completed.data.credits_total`.
- The reducer-side rows (`agent.session_created`, `agent.step`, `agent.waiting_for`, `agent.resumed`) are logged under the caller's actor (`factory`).
- Or use the TDM-35 feed below, which does all of this already.

## Agents panel feed (TDM-35): AgentFeed v1

One normalised, ordered stream per agent. It merges `agent_session` rows, the `droid.*` rows above (reused unchanged), the `agent.*` reducer echoes, the resume bridge's `agent.resume_*` rows, and the orchestrator's own agents, which exist only as event_log rows. There are two implementations of the same normaliser, and a golden test keeps them identical (`services/orchestrator/tests/test_tdm35_agents_feed.py` runs the TS file under Node):

| Where | Use | Code |
|---|---|---|
| Browser (live, SpacetimeDB subscriptions only) | `const { feed, ready, mode, error } = useAgentFeed({ ticketId?, limit = 20, replay?: true \| 'auto', replaySpeed? })` | `apps/web/src/lib/agentfeed/useAgentFeed.ts`, pure part in `normalize.ts` |
| Orchestrator HTTP | `GET /agents?ticket_id=&limit=20&replay=0\|1\|auto` | `services/orchestrator/src/rdf_orchestrator/agents_feed.py`, `agents_api.py` |
| Raw recording | `GET /agents/replay/source` → `{sessions, events}` that the hook plays back | same |

```ts
AgentFeed = { v: 1, replay: boolean, generated_at_us, agents: AgentView[], replay_source?, replay_reason? }
AgentView = { key: 'session:<id>' | 'service:<name>', kind: 'droid' | 'session' | 'service', label, role,
  agent_session_id | null, ticket_id | null, status, current_step, last_update_us,
  factory_session_id | null, factory_session_ids: string[], waiting_for,
  usage: { credits_total, turns, input_tokens, output_tokens, seconds } | null,
  replay: boolean, event_count, events: FeedEvent[] /* oldest -> newest, last `limit` */ }
FeedEvent = { id: 'ev:<event_log id>' | 'rec:<agent_session_id>:<line>', agent_key, ts_us,
  kind: 'started'|'step'|'tool_call'|'tool_result'|'message'|'waiting'|'resumed'|'done'|'failed'|'error'|'info',
  type /* source type, e.g. droid.tool_call */, actor, text /* one line, <=300 chars */, tool, ticket_id, turn,
  is_error, replay, data /* droid: DroidEvent.data + droid_kind, role, seq, mode, factory_session_id; else the payload */ }
```

- **Agent order:** droids first, then other agent_sessions, then services. Within each group, the most recently active comes first.
- **Event order:** `(ts_us, id)`. Droid events use the droid's own `ts_us`, because `seq` restarts in each client process.
- **Droid kinds:**
  - `session_started` and `session_restarted` map to `started`.
  - `turn_started` maps to `step`.
  - `assistant_text` maps to `message`.
  - `permission_denied` and `error` map to `error`.
  - `waiting_for` maps to `waiting`.
  - `turn_completed` maps to `done` or `failed` from its status, and to `info` otherwise.
  - `thinking`, `state` and `usage` are dropped.
- **Duplicates:** for a droid session, `agent.step`, `agent.waiting_for` and `agent.session_updated` repeat the droid.* rows, so they are dropped. `agent.resumed` and `agent.session_created` stay.
- **Service agents** are matched by type prefix first, then by actor:
  - `clustering`: `diagnose.*` and `planner.failure_cluster`.
  - `planner`: `planner.*`.
  - `reviewer`: `review.*`.
  - `qa`: `qa.*`.
  - `dispatcher`: `dispatch.*`.
  - `eval`: `eval.*`.
  - `flywheel`: `flywheel.*`.
  - Rows from `human:*`, `robot:*` and `contributor:*` are skipped, as are `test.`, `infra.`, `spike.` and `probe.` rows.
  - A type ending `.error`, `.blocked`, `_failed` or `_error` is an error.
  - Service status is `error` if its last row is an error, `active` if it logged something in the last 5 min, and `idle` otherwise.
- **Usage** comes from `droid.turn_completed`: credits are the max of `credits_total` and the sum of `credits` (cloud runs report `factoryCredits`), plus the summed tokens and seconds. No jsonl file is needed.
- **Replay:** every agent and event is flagged `replay: true`, as is `feed.replay`, and the panel must show a "replay" label.
  - The recording comes from `services/orchestrator/fixtures/tdm35_replay/*.events.jsonl` (TDM-30's proof run), or from `RDF_AGENTS_REPLAY_DIR`. For example, set it to `services/factory/work/state` to replay tonight's local runs.
  - `replay=auto` (HTTP) or `replay: 'auto'` (hook) uses the live feed, and switches to the recording while SpacetimeDB is unreachable.
  - `replaySpeed: 4` plays the recording back at 4x and loops, with timestamps rebased to now.
- **Live proof** (throwaway local SpacetimeDB, no credits): `cd services/orchestrator && uv run python ../../spikes/tdm-35/live_proof.py`. It replays the recording through `Mirror`, and a Node subscriber (`spikes/tdm-35/subscribe_feed.mjs`, using the real bindings and `normalize.ts`) builds the feed from subscriptions. The script checks that this feed matches `GET /agents`.

## Config (`.env`)

- `FACTORY_API_KEY`: required, never printed.
- `FACTORY_MODE`: `local` (default) or `cloud`.
- `FACTORY_ENVIRONMENT_ID`: defaults to `173ec531-…`.
- `FACTORY_COMPUTER_ID`: optional.
- `FACTORY_MODEL`: defaults to `gpt-5.4-mini`.
- `FACTORY_REASONING`: defaults to `low`.
- `FACTORY_REPO_DIR`, `FACTORY_STATE_DIR`, `FACTORY_WORK_ROOT`: optional overrides.
- `SPACETIME_HOST`, `SPACETIME_DB_NAME`, `SPACETIME_TOKEN`: as for every service.

## Proof run (2026-10-24, local mode, throwaway SpacetimeDB on 127.0.0.1:3030)

Local SpacetimeDB was set up from a clean HEAD worktree: module published as `tdm30-local`, with a fresh identity registered as `factory`. One `data_droid`, agent_session 1, Factory session `63583e50-59b4-4305-b908-dd80bf7c8d71`, model gpt-5.4-mini/low:

1. **`start`** (22 s, 8,068 credits)
   - The droid read the six `services/*/pyproject.toml` files.
   - It wrote `work/sessions/1/services_inventory.md` (a table of service, package, description, deps) and `resume_payload.json`.
   - It ended with `WAITING_FOR dataset:accepted_count>=5`, and the row went to `waiting_for`.
2. **`resume 1 "..."`** (8 s, 1,665 credits): run in a new process.
   - `agent_resume` claimed the wake-up, then Protocol A continued the same Factory session id.
   - The droid appended an "Episode consumers" section from memory, without re-reading the manifests.
   - The turn ended without the final line, so the row went to `paused`. That gap is why the reminder was added.
3. **`send 1 "..."`** (6 s, 1,158 credits): the droid checked the file and replied `DONE`, and the row went to `done`.

Totals: 10,891 credits over 3 turns. `event_log` grew to 73 rows. A 2 s SQL poll saw `current_step` change 14 times. Status went running → waiting_for → resuming/running → paused → running → done.

`Session.resume` on an unknown id raises `SessionGone`, which is the local protocol-B trigger. That was checked with no credits spent.
