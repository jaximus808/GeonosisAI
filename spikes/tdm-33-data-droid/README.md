# TDM-33: Data Droid proof (local Factory Droid, throwaway SpacetimeDB, labelled fixtures)

The production code:
- `services/factory/src/rdf_factory/droids/data.py` holds the brief, the bounds and the launcher.
- `services/training/src/rdf_training/data_droid_tools.py` holds the droid's only tool.
- `services/orchestrator/src/rdf_orchestrator/handlers/data_droid.py` is the flywheel hook.
- `spacetime/spacetimedb/src/reducers/data_droid.ts` defines `set_dataset_lerobot_uri`.

```bash
cd services/factory && uv run python ../../spikes/tdm-33-data-droid/prove_data_droid.py              # real droid (credits)
cd services/factory && TDM33_DRY=1 uv run python ../../spikes/tdm-33-data-droid/prove_data_droid.py  # tools only, no droid
```

The script never touches maincloud. It publishes HEAD's module, from a clean worktree, to `127.0.0.1:3033` (db `tdm33local`). It then creates 7 fixture episodes, all marked `fixture: true`, each with an exo and a wrist mp4. Before the droid starts, the fixture reviewer accepts 3 of them (one is a recovery whose row has no `takeover_ts`), rejects 1 and leaves 1 pending.

## Result (2026-10-24, gpt-5.4-mini/low, local mode)

| | |
|---|---|
| agent_session | 1 (`data_droid`), Factory session `a2eb2d52-870c-47a2-b325-dbc0b8d56be1` |
| turn 1 (start, 28 s, 9,089 credits) | The droid ran `inspect` and `validate` (3 accepted) and wrote `notes.md` and `resume_payload.json`. It ended with `WAITING_FOR dataset:accepted_count>=5`, so the row went to `waiting_for`. |
| (between turns) | The reviewer accepts 2 more episodes and `accepted_count` reaches 5. The proof then calls `FactoryClient.resume` with the text the TDM-27 bridge would send (the bridge process itself is not run here). |
| turn 2 (native resume, 60 s, 9,487 credits) | The droid ran `inspect`, `validate`, `assemble`, `verify`, then wrote the dataset card `README.md`, then ran `publish` and replied `DONE`. |
| total | 18,576 credits, 2 turns. The earlier attempt (7,685 credits, `proof_attempt1_result.json`) was refused a shell heredoc (bounds held), then ended without a status line. The brief now says to write files with file tools and to put the status line last. |
| dataset | `services/factory/work/datasets/lerobot/rdf/ticket-1` (gitignored). Copies are here: `manifest.json`, `dataset_card.md`. |
| manifest ids | `[1, 2, 3, 6, 7]`, which equals the accepted ids. Rejected episode 4 and pending episode 5 are listed as excluded with their reason. |
| takeover cut | Episode 3 has no `takeover_ts`, so the cut falls at the first human tick: 60 frames before it and 15 after handback are cut. Episode 7 has 40 frames cut before `takeover_ts`. `verify` confirms that frame 0's state equals the source state at the takeover tick (`no_frames_before_takeover: true`). |
| openpi loader | `LeRobotDataset` at lerobot@0cf8648 (openpi's pin; TDM-39 venv) loads 5 episodes, 380 frames at 30 fps. Images are `[3,240,320]` x2, state is `[7]`, action is `[50,7]` (S-05a chunks). |
| SpacetimeDB | `dataset.lerobot_uri` is set, and one `dataset.assembled` event lists the 5 ids. There are 36 `droid.*` event_log rows for the session (12 tool_call, 12 tool_result, 6 assistant_text, 2 turn_started, 2 turn_completed, 1 waiting_for, 1 session_started). `agent_session` status went running → waiting_for → resuming/running → done. |

Two gaps:
- The droid ended turn 2 with `DONE` rather than the brief's final park (`WAITING_FOR data_plan:iteration>=1`). The orchestrator hook covers this: a `done` session is not "live", so the next time the ticket enters `review` a fresh Data Droid starts.
- The modal backend (`assemble --backend modal`, for `modal-vol://rdf-media` episodes) is written but not proven. `verify` only reads a local dataset.
