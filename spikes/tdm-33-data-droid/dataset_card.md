# Ticket 1 LeRobot v2.1 dataset

**FIXTURE WARNING:** this dataset was assembled with `--allow-fixtures` from labeled synthetic fixture episodes.

- Ticket: `1`
- Task: `put the cup in the bin`
- repo_id: `rdf/ticket-1`
- Codebase version: `v2.1`
- FPS: `30`

## Features

- `observation.images.top`: `[240, 320, 3]`
- `observation.images.wrist`: `[240, 320, 3]`
- `observation.state`: `[7]`
- `action`: `[7]`
- `action.ee_target`: `[8]`

## Episodes

| episode_id | kind | source frames | kept frames | cut before takeover | cut after handback |
|---|---:|---:|---:|---:|---:|
| 1 | clean_teleop | 75 | 75 | 0 | 0 |
| 2 | clean_teleop | 90 | 90 | 0 | 0 |
| 3 | recovery_takeover | 150 | 75 | 60 | 15 |
| 6 | clean_teleop | 60 | 60 | 0 | 0 |
| 7 | recovery_takeover | 120 | 80 | 40 | 0 |

## Excluded

- Episode 4, `clean_teleop`: `review_status=rejected (only accepted episodes train)`
- Episode 5, `recovery_takeover`: `review_status=pending (only accepted episodes train)`

## Validate warnings

- Episode 1: synthetic fixture episode (parquet metadata fixture=true): not robot data
- Episode 2: synthetic fixture episode (parquet metadata fixture=true): not robot data
- Episode 3: synthetic fixture episode (parquet metadata fixture=true): not robot data
- Episode 3: no takeover_ts on the row: the converter cuts at the first human tick (60, t=1761292802000000)
- Episode 6: synthetic fixture episode (parquet metadata fixture=true): not robot data
- Episode 7: synthetic fixture episode (parquet metadata fixture=true): not robot data

## Stats

State mean: `[0.1933, 0.2118, 0.1275, 0.1099, 0.2670, 0.5464, 0.7410]`

State std: `[0.1063, 0.1843, 0.1867, 0.1071, 0.1733, 0.1949, 0.2202]`

Action mean: `[-0.0164, -0.0973, -0.0880, 0.0029, 0.0919, 0.0972, 0.6296]`

Action std: `[0.1167, 0.0893, 0.1070, 0.1189, 0.0917, 0.1028, 0.2373]`

Norm-stats dims: `7`

## LeRobot loader

- ok: `true`
- num_episodes: `5`
- num_frames: `380`
- fps: `30`

## Takeover cuts

- Episode 3: `no_frames_before_takeover=true`
- Episode 7: `no_frames_before_takeover=true`

## Load

```bash
HF_LEROBOT_HOME=/Users/jaxon/coding-project/robot-deploy-fact/services/factory/work/datasets lerobot-dataset-info rdf/ticket-1
```
