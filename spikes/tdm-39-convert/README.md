# TDM-39: episodes to an openpi-ready LeRobot v2.1 dataset

The production code is `services/training/src/rdf_training/convert.py`. The labelled fixtures are in `convert_fixtures.py`, and the tests are in `services/training/tests/test_tdm39_convert.py`. This folder holds the proofs.

## Commands

```bash
cd services/training
uv run pytest tests/test_tdm39_convert.py -q                    # unit tests; the LeRobot test is skipped here
uv run python -m rdf_training.convert --fixture --out ../../spikes/tdm-39-convert/tmp/ds   # CLI on fixtures
uv run python -m rdf_training.convert --manifest m.json --out /path/to/datasets
uv run modal run ../../spikes/tdm-39-convert/modal_prove.py     # openpi image: LeRobot + openpi data loader
```

To run the LeRobot load test locally, use a venv with openpi's exact pins: lerobot@0cf8648, datasets 3.6.0, torch 2.7.1, av 17.0.0, numpy 1.26.4 and pyarrow 20.0.0.

```bash
cd spikes/tdm-39-convert/tmp && git clone https://github.com/huggingface/lerobot && git -C lerobot checkout 0cf8648
uv venv -p 3.11 lerobot-venv && VIRTUAL_ENV=$PWD/lerobot-venv uv pip install ./lerobot pytest \
  datasets==3.6.0 torch==2.7.1 torchvision==0.22.1 torchcodec==0.4.0 av==17.0.0 numpy==1.26.4 pyarrow==20.0.0 \
  huggingface-hub==0.32.3 -e ../../../services/common
cd ../../../services/training && PYTHONPATH=src ../../spikes/tdm-39-convert/tmp/lerobot-venv/bin/python -m pytest tests/test_tdm39_convert.py -q
```

## Decisions (revert by flipping the manifest field)

- **Action:** the action is the absolute joint target. It uses `joint_cmd` (the IK solution that was sent) when the episode recorded it, and otherwise the next tick's `joint_pos`. The gripper uses `gripper_cmd`. The IK target is kept as `action.ee_target` [8] for a possible EE-space policy later.
- **Takeover cut:** frames before `takeover_ts` are always cut. Frames from `handback_ts` onward, where the policy is driving again, are also cut by default, so a recovery episode trains only on the human segment. Set `keep_after_handback: true` to keep them.
- **Cameras:** `exo` or `top` maps to `observation.images.top`, and `wrist` maps to `observation.images.wrist` (S-05a keys). The S-05a openpi config needs both. A single-camera dataset converts, but it does not train with that config.
- **Fixtures:** synthetic fixture episodes (`fixture: true` in the parquet metadata) are refused unless the manifest sets `allow_fixtures: true`.
