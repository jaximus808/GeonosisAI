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

## Re-run with `autonomy="off"` (commit 7831121; the current `proof_result.json`)

Commit 7831121 made the FactoryClient default `autonomy="off"`, so every file edit and command now goes through `write_roots` / `allow_commands`. The proof was re-run under that setting:

- agent_session 1, Factory session `179d38c6-437b-4b23-93e9-f4981fcb8e00`, 3 turns, 27,089 credits, **0 permission denials**.
- The droid used only the five tool commands, Read/LS, and ApplyPatch inside its write roots (notes.md, resume_payload.json, the card).
- **Turn 1** (9,485 credits) parked on `dataset:accepted_count>=5`.
- **Turn 2** (native resume, 16,604 credits) ran assemble, verify, the card and publish. It wrote the next-iteration `WAITING_FOR data_plan:iteration>=1`, but glued it onto the previous sentence with no newline (`...I'll stop.WAITING_FOR ...`). `parse_waiting_for` only reads a line that starts with it, so the row went to `paused`.
- **Turn 3:** one bounded nudge (`send`, 1,000 credits), and the droid replied `DONE`.
- Same outputs as above:
  - manifest `[1, 2, 3, 6, 7]` equals the accepted ids;
  - cuts of 60 and 40 frames, with `no_frames_before_takeover` true;
  - LeRobotDataset loads 5 episodes / 380 frames;
  - `dataset.lerobot_uri` set and one `dataset.assembled` event;
  - 61 `droid.*` rows.
- The previous run's result is kept as `proof_autonomy_low_result.json`.

Two gaps:
- The droid ended turn 2 with `DONE` rather than the brief's final park (`WAITING_FOR data_plan:iteration>=1`). The orchestrator hook covers this: a `done` session is not "live", so the next time the ticket enters `review` a fresh Data Droid starts.
- The modal backend (`assemble --backend modal`, for `modal-vol://rdf-media` episodes) is written but not proven. `verify` only reads a local dataset.
