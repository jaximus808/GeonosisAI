# TDM-36 / S-05a: π0.5 fine-tune recipe on Modal

openpi `215abfb` (Physical-Intelligence/openpi main, 2026-08-24). LeRobot is pinned by openpi's `uv.lock` (`huggingface/lerobot@0cf8648`, dataset format v2.1). The base checkpoint is `gs://openpi-assets/checkpoints/pi05_base/params` (12.4 GB).
Proxy dataset: `lerobot/svla_so100_pickplace`. It has one arm, a top and a wrist camera (AV1 mp4, 640x480), absolute joint actions (5 joints plus a gripper), 30 fps, 50 episodes and 19.6k frames. That is the same shape as our YAM export apart from the dimensions.

## Commands (from repo root)

```bash
modal run spikes/tdm-36-finetune/modal_finetune.py --action prepare                      # ckpt + dataset -> Volume, norm stats
modal run spikes/tdm-36-finetune/modal_finetune.py --action train --mode lora --steps 200 --gpu H100
modal run spikes/tdm-36-finetune/modal_finetune.py --action train --mode full --steps 100 --gpu H100 --no-save-to-volume
modal run spikes/tdm-36-finetune/modal_finetune.py --action infer                        # load ckpt, predict a chunk
```

Modal resources: app `rdf-s05a-finetune`, volume `rdf-s05a-checkpoints`, secret `rdf-s05a-hf` (HF_TOKEN).
Smoke checkpoint: `rdf-s05a-checkpoints:/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199` (`params/`, `assets/` with norm stats, `train_state/`).
Raw metrics JSON for each run: `rdf-s05a-checkpoints:/metrics/`.

## Measured (batch 32, action horizon 50, 224px images, 12 loader workers, 16 CPU)

| run | GPU | peak GPU mem | s/step | steps/s | first step (compile) | ckpt size / save |
|---|---|---|---|---|---|---|
| LoRA, 200 steps | H100 80GB | 24.2 GB | 1.31 | 0.77 | 75 s | 9.5 GB / 37 s |
| LoRA, 100 steps | A100-80GB | 24.2 GB | 3.05 | 0.33 | 110 s | 9.5 GB / 42 s |
| full, 100 steps | H100 80GB | 75.6 GB of 76.5 GB limit | 1.46 | 0.68 | 58 s | 44.7 GB / 51 s |

Fixed overhead per run (H100): container cold start about 6 s, imports about 15 s, data-loader init about 95 s (LeRobotDataset plus spawned video-decode workers), weight load about 25 s, JIT compile about 60–75 s, final checkpoint save about 40 s. That comes to about 4.5 min. Norm stats run CPU-only at about 80 frames/s (AV1 decode, 14 workers), so 5–10 YAM episodes take about 1–2 min.
LoRA loss went from 0.035 to 0.009 over 200 steps. Inference smoke on L40S: the policy loads in 31 s, the first call (compile) takes 18 s, warm calls take 0.12 s per 50×6 chunk. On a training frame the chunk MAE is 3.1° against 15.9° for holding still (a sanity check, not an eval).

## Decision

- **GPU: H100.** It runs 2.3× faster than A100-80GB for the same memory, which costs less overall and finishes sooner.
- **LoRA** (`gemma_2b_lora` + `gemma_300m_lora`, frozen base, no EMA). It runs at about the same speed as full fine-tuning (1.31 vs 1.46 s/step) and uses a third of the memory. Full fine-tuning on one 80 GB card leaves less than 1 GB of headroom, writes 45 GB checkpoints, and overfits more easily on 5–10 demos.
- **Time per run is set by the step count, not the episode count** (batch size is fixed). With LoRA on H100 at 1.31 s/step plus about 5 min overhead:
  - 1,000 steps: about 27 min
  - 2,000 steps: about 49 min (recommended for 5–10 eps, about 3–4.5k frames each run, so 15–20 epochs)
  - 3,000 steps: about 70 min

## Config fields the fine-tune function must generate (`rdf_s05a_config.py`)

`DatasetSpec(repo_id, base_image_key, wrist_image_key, state_key, action_key, joint_dims, action_dim, fps)`. For YAM: `yam_dataset_spec(repo_id)` gives top and wrist cameras, `joint_dims=6`, `action_dim=7`, 30 fps. The code then builds `make_train_config(mode, spec, num_train_steps, batch_size=32, action_horizon=50, ...)`. Absolute joint actions become deltas (`DeltaActions`, gripper stays absolute), and norm stats are computed per dataset into `/vol/assets/<repo_id>`.

Policy-server input keys after repack: `observation/image`, `observation/wrist_image` (uint8 HWC or float CHW), `observation/state` (float32 [state_dim]), `prompt`. Output: `actions` [50, action_dim] in absolute joint space.
