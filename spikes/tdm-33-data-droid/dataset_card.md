# Ticket 1, LeRobot v2.1 dataset

FIXTURE: synthetic fixture episodes, not robot data.

- Ticket: 1
- Task: put the cup in the bin
- repo_id: rdf/ticket-1
- LeRobot v2.1
- fps: 30

## Features

- observation.images.top: [240, 320, 3]
- observation.images.wrist: [240, 320, 3]
- observation.state: [7]
- action: [7]
- action.ee_target: [8]

## Episodes

| episode_id | kind | source frames | kept frames | cut before takeover | cut after handback |
|---|---|---:|---:|---:|---:|
| 1 | clean_teleop | 75 | 75 | 0 | 0 |
| 2 | clean_teleop | 90 | 90 | 0 | 0 |
| 3 | recovery_takeover | 150 | 75 | 60 | 15 |
| 6 | clean_teleop | 60 | 60 | 0 | 0 |
| 7 | recovery_takeover | 120 | 80 | 40 | 0 |

## Excluded

- episode 4: review_status=rejected (only accepted episodes train)
- episode 5: review_status=pending (only accepted episodes train)

## Validate warnings

- episode 1: synthetic fixture episode (parquet metadata fixture=true): not robot data
- episode 2: synthetic fixture episode (parquet metadata fixture=true): not robot data
- episode 3: synthetic fixture episode (parquet metadata fixture=true): not robot data
- episode 3: no takeover_ts on the row: the converter cuts at the first human tick (60, t=1761292802000000)
- episode 6: synthetic fixture episode (parquet metadata fixture=true): not robot data
- episode 7: synthetic fixture episode (parquet metadata fixture=true): not robot data

## Stats

- state_mean: [0.1933, 0.2118, 0.1275, 0.1099, 0.267, 0.5464, 0.741]
- state_std: [0.1063, 0.1843, 0.1867, 0.1071, 0.1733, 0.1949, 0.2202]
- action_mean: [0.1943, 0.2084, 0.1236, 0.1098, 0.2716, 0.5522, 0.7503]
- action_std: [0.1069, 0.1863, 0.1851, 0.1064, 0.1752, 0.1933, 0.2194]
- norm-stats dims: 7

## LeRobot loader

- ok: true
- num_episodes: 5
- num_frames: 380
- fps: 30

Items:
- index 0: task `put the cup in the bin`, observation.images.top [3, 240, 320], observation.images.wrist [3, 240, 320], observation.state [7], action [50, 7], action.ee_target [8], action_is_pad [50]
- index 190: task `put the cup in the bin`, observation.images.top [3, 240, 320], observation.images.wrist [3, 240, 320], observation.state [7], action [50, 7], action.ee_target [8], action_is_pad [50]
- index 379: task `put the cup in the bin`, observation.images.top [3, 240, 320], observation.images.wrist [3, 240, 320], observation.state [7], action [50, 7], action.ee_target [8], action_is_pad [50]

## Load

`HF_LEROBOT_HOME=/Users/jaxon/coding-project/robot-deploy-fact/services/factory/work/datasets/lerobot`

`repo_id=rdf/ticket-1`
