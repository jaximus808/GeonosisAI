# Jetson camera checklist (TDM-47)

Use this checklist the first time the wrist and top (exo/scene) cameras are plugged into the Jetson. Code: `src/rdf_robot_client/cameras.py`, `camera_v4l2.py` and `camera_stream.py`. Config: `config/cameras.yaml`. You don't need the arm for this. Each step takes a few minutes.

Run all commands from `services/robot-client/` after `uv sync`.

## 0. Laptop sanity (already done on Oct 24)
- `uv run python -m rdf_robot_client.cameras check --synthetic` passed: 300/300 frames per camera, 0 drops, skew p95 9.5 ms.
- `uv run python -m rdf_robot_client.cameras check --webcam 0` passed on the MacBook webcam: 30 fps, 0 drops.

## 1. Packages
- [ ] `sudo apt install v4l-utils`. This gives you `v4l2-ctl`, which the config uses to set exposure and flicker controls. Without it the controls are skipped with a warning.
- [ ] `uv sync`. This installs the pip `opencv-python-headless` (4.x, aarch64 wheel) with the V4L2 backend. That backend is enough for UVC cameras over MJPG.
  - The pip wheel has **no GStreamer**. You need GStreamer only for the `nvv4l2decoder` pipeline in `cameras.yaml` (hardware JPEG decode). For that, use JetPack's system OpenCV (`uv venv --system-site-packages`, then remove the pip wheel). Don't bother unless CPU is the bottleneck.
- [ ] `sudo usermod -aG video $USER`, then log in again. Without it, opening `/dev/video*` fails with "permission denied".

## 2. Device discovery
- [ ] Plug in both cameras and run `uv run python -m rdf_robot_client.cameras list` (add `--formats` for modes and controls).
- [ ] Each UVC camera shows up as **two** nodes. `[capture]` (by-id `...-video-index0`) is the one to use. `[metadata]` (`-index1`) cannot stream.
- [ ] Find out which is which. Unplug one camera and list again, or run `serve` (step 5) and look.
- [ ] In `config/cameras.yaml`, set `device:` for `top` and `wrist` to stable paths. Don't use `/dev/videoN`, because the numbers change across reboots and replugs.
  - `/dev/v4l/by-id/usb-<vendor>_<model>_<serial>-video-index0` follows the camera, if the two have different serials.
  - **Two identical cameras with no serial** get the same by-id name. Use `/dev/v4l/by-path/platform-...-usb-0:1.2:1.0-video-index0` (per USB port) instead, and label the ports.
  - Globs work, e.g. `/dev/v4l/by-id/*Arducam*-video-index0`. The first match in sorted order wins.
- [ ] Check that the camera can do 640x480 MJPG at 30 fps: `v4l2-ctl -d <dev> --list-formats-ext | grep -A3 MJPG`.

## 3. V4L2 settings (expected values)
| setting | value | why |
|---|---|---|
| pixel format | `MJPG` | Two 640x480@30 YUYV streams are about 300 Mbit/s and don't fit on one USB 2 bus. MJPG is about 10–25 Mbit/s each. |
| size / fps | 640x480 @ 30 | The S-05a recipe. Training and policy resize to 224 px anyway. |
| `exposure_dynamic_framerate` (old kernels: `exposure_auto_priority`) | `0` | Otherwise auto-exposure halves the frame rate to 15 fps in dim light. This is the most common cause of "drops". |
| `power_line_frequency` | `2` (60 Hz) | Avoids flicker banding under US office lights. |
| `auto_exposure` (old: `exposure_auto`) | leave on (3), or `1` with `exposure_time_absolute` fixed | Use a fixed exposure only if lighting changes confuse the policy. With a fixed exposure, keep it under 333 (33 ms) or fps drops. |
| `white_balance_automatic` | optional `0` with a fixed `white_balance_temperature` | Gives consistent color between demo recording and eval. |

- [ ] The config applies these with `v4l2-ctl` after opening the camera. If a control fails, a warning shows up in `stats()["cameras"][cam]["control_problems"]`. Both old and new kernel control names are tried.
- [ ] Check what stuck: `v4l2-ctl -d <dev> --list-ctrls`.

## 4. Capture test (this is the done-when)
- [ ] `uv run python -m rdf_robot_client.cameras check --seconds 10 -v`
- [ ] Expect each camera to show **30 ± 0.5 fps** and **< 1 % dropped**. Sync should show **skew p95 < 20 ms** (the two cams free-run, so expect p50 of about 0–17 ms) and `PASS`. The exit code is 0 on pass and 1 on fail.
- [ ] Check `ts`. The `"ts_source"` in the JSON report should be `driver` (V4L2 buffer timestamps, CLOCK_MONOTONIC). If it shows `host`, that still works, but expect a few ms of extra jitter.
- [ ] Open the stitched image it prints (`/tmp/rdf-cameras-check/cameras-check-<time>.jpg`; `--out DIR` changes the location). There are also raw full-res PNGs and the 224-px policy view for each camera.
  - **Orientation:** in `top`, the table should be at the bottom and the robot base in a sensible place. In `wrist`, the gripper fingers should be at the bottom of the frame. If not, fix `rotate:` (0/90/180/270, clockwise) and `flip:` in the config, then rerun. The recorder stores frames exactly as shown here, so fix this **before** recording any demos.
- If it fails:
  - fps around 15: see `exposure_dynamic_framerate` above, or add light.
  - Drops on both cameras: both are on one hub or bus. Try separate USB ports (`lsusb -t`), make sure MJPG is in use, and try lower-power hubs.
  - `could not open`: wrong node (metadata `-index1`), the `video` group is missing, or another process holds the camera (`fuser /dev/video*`).
  - Skew high but no drops: one camera isn't really running at 30 fps. Check `fps_negotiated` in the report.

## 5. Browser feed (server-side source for TDM-55)
- [ ] `uv run python -m rdf_robot_client.cameras serve --host 0.0.0.0 --port 8090`
- [ ] From the laptop on the same LAN, open `http://<jetson-ip>:8090/`. It shows both cameras at about 15 fps, 424 px wide.
  - `/stream/{top,wrist,both}.mjpg` are the MJPEG streams.
  - `/snapshot/<cam>.jpg` returns a single frame.
  - `/stats` returns JSON (fps, drops, skew, health).
- [ ] Check the CPU load (`tegrastats` / `htop`) while streaming and capturing. Capture and decode of 2× 640x480 MJPG should use well under one core.

## 6. Hand-off to the other robot-client tickets
- TDM-49 command loop: `rig = CameraRig.from_config(); rig.start()`. Put `rig.healthy()` and `rig.stats()` into the heartbeat. Wire the `rdf-robot-client cameras-check` subcommand to `cameras.main(["check", *args])`.
- TDM-48 recorder: `rig.add_synced_callback(fn)` or `rig.subscribe_synced()`. One `SyncedFrames` arrives per top-camera frame (30 Hz), with `.frames[name].image` (full-res BGR), `.t_ns` (monotonic) and `.in_sync`. Stamp robot state with `time.monotonic_ns()` so it lines up.
- Rollout (TDM-50): `rig.latest_synced().policy_images()` returns 224x224 RGB under the S-05a keys `observation/image` and `observation/wrist_image`.
