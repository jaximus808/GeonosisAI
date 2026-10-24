# TDM-48 recorder: Jetson checklist

What `rdf_robot_client/recorder.py` needs on the Jetson, and how to prove it works with the arm. Nobody has run this on the Jetson yet. Every step below was only proven on the laptop, with SimArm and synthetic cameras.

## 1. H.264 encoder
The recorder probes encoders once per process and logs `video encoder selected` with the ones it skipped and why.
- **Orin AGX / NX** (these have NVENC): check `gst-inspect-1.0 nvv4l2h264enc`. JetPack ships it. If it is present, the recorder uses `gst-nvv4l2h264enc`.
- **Orin Nano** has no hardware encoder. It falls back to `ffmpeg-libx264`, so `sudo apt install ffmpeg` is needed. Two 640x480@30 streams at `veryfast` use roughly 1–1.5 cores. If you see `queue_full` drops in `meta.json`, set `RDF_RECORDER_X264_PRESET=ultrafast`.
- To force an encoder: `RDF_RECORDER_ENCODER=ffmpeg-libx264` (or `gst-nvv4l2h264enc`, `ffmpeg-h264_nvmpi`, `pyav-libx264`, `cv2-avc1`).

## 2. Config
- `ORCHESTRATOR_URL` in `.env` points at the orchestrator, which serves `/uploads/sign-batch`. The Jetson never holds the media signing key.
- `RDF_EPISODES_DIR` (default `~/.rdf/episodes`) is the local disk queue. Allow about 10 MB per 20 s episode. Files stay after upload; prune by hand.
- The robot clock should be NTP-synced (`timedatectl`). Timestamps are monotonic with one wall-clock anchor per episode, so a slew mid-episode is harmless. A wrong wall clock, though, puts `takeover_ts` on the wrong day.

## 3. Smoke test without the arm (synthetic cameras + SimArm)
```
cd services/robot-client
uv run python -m rdf_robot_client.recorder record --seconds 20 --kind recovery_takeover --takeover-at 6 --handback-at 14
```
It should print `"review_checks_passed": true`, with every check `pass` and the encoder you expect.

## 4. With the real cameras and arm (handlers call `recorder_for(ctx)`)
1. Run `rdf-robot-client` with `--cameras config`, plus the arm.
2. Run a 20 s teleop or takeover task from the dashboard. The job handler starts and stops the recorder. Then, in `~/.rdf/episodes/<key>/`:
   - `meta.json` shows `status: recorded`.
   - `drops.queue_full` is 0.
   - `cameras.*.written` equals `frame_count`.
   - `upload.json` shows `state: done` with an `episode_id`.
3. `uv run python -m rdf_robot_client.recorder check ~/.rdf/episodes/<key>` should report every deterministic check passing. On real YAM data, re-tune `jerk` (see `services/orchestrator/config/review.yaml`).
4. On the dashboard episode page, both videos should play and the takeover/handback markers should be set.
5. Network blip test: pull the Ethernet cable during an upload. `upload.json` goes to `retrying`. Plug it back in and it reaches `done`. To recover after a crash, run `uv run python -m rdf_robot_client.recorder upload-pending`. The client also rescans on start, the first time `recorder_for` runs.
