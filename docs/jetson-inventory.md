# Jetson inventory: `northstar` (TDM-117)

Taken read-only over Tailscale SSH (`ssh northstar@northstar`) on Sat Oct 24 2026, ~18:05 PDT, while the user's
`vrteleop-rh.service` was running and in use (teleop engage/disengage events in its journal that afternoon).
Nothing on the Jetson was changed, and no CAN bus or camera was opened. Camera and CAN facts come from sysfs,
udev, `lsusb`, the vrteleop journal and its `robot.toml` (read only). Items marked **VERIFY** need vrteleop
stopped first.

## 1. The board

| | |
|---|---|
| Model | NVIDIA Jetson Orin Nano Developer Kit (TNSPEC 3767-300, "Super"), aarch64 |
| L4T | R39.2.0 (`nvidia-l4t-core 39.2.0-20260601`), kernel 6.8.12-1021-tegra |
| OS | Ubuntu 24.04.4 LTS |
| CPU / RAM | 6 cores, 7.3 GiB RAM, **no swap**. Power mode `15W` (nvpmodel id 0) |
| GPU / CUDA | Ampere (ga10b), driver 595.78, **CUDA 13.2** (from `nvidia-smi`). Only the `nvidia-l4t-cuda*` runtime packages are installed: no `/usr/local/cuda`, no `nvcc`, no cuDNN/TensorRT. Fine for us: the Jetson does no inference |
| Python | system 3.12.3; uv 0.12.3 in `~/.local/bin`; a uv-managed **CPython 3.11.15** already exists in `~/.local/share/uv/python` (our client needs 3.11) |
| Toolchain | gcc/g++, cmake, make, git, rsync present (enough to build ruckig's sdist for the I2RT SDK) |
| Media tools | `ffmpeg 8.0.1-nvidia1`, `gstreamer1.0-tools 1.24.2`, `v4l-utils 1.26.1`, `can-utils 2023.03`; **librealsense 2.58.3** built in `/usr/local/lib` with `rs-enumerate-devices` in `/usr/local/bin` |
| Not installed | `cloudflared` (TDM-120 needs it), system `python3-opencv` / `pyrealsense2` |
| sudo | **needs a password** (no NOPASSWD). User `northstar` is in `video`, `dialout`, `plugdev`, `adm`, `render`, `i2c`, `gpio` already |
| systemd --user | works, and **linger is on** (`Linger=yes`), so a user unit survives logout and starts at boot |
| Clock | NTP active and synchronised; Jetson `Date` equals the maincloud `Date` header to the second |
| Tailscale | Jetson `100.105.223.36` (`northstar.tail4e5abb.ts.net`); the laptop `jaxons-macbook-air` is `100.66.171.21`, **direct** (LAN 10.2.1.135) |

### Disk
| mount | size | used | free |
|---|---|---|---|
| `/` (`mmcblk0p1`, the only data partition) | 28 G | 21 G (81%) | **5.0 G** |
| `/boot/efi` | 63 M | 110 K | 63 M |

There is no other data mount (no NVMe, no USB disk). Big existing items: `~/i2rt` 1.1 G, `~/.local/share/uv` 699 M,
many old venvs (`lerobot_env`, `molmo-client-venv`, `rebot_env`, `OLD_VRTeleop`, ...) and a running Docker daemon.
None of it is ours to clean. **Budget for GeonosisAI: under ~1 GB** (one venv with numpy, OpenCV headless, MuJoCo,
mink, the I2RT SDK; plus cloudflared ~40 MB). No Docker images.

## 2. CAN: two YAM arms (bimanual)

| interface | driver | USB path | adapter serial (gs_usb, `1d50:606f`) | bitrate | arm |
|---|---|---|---|---|---|
| `can_right` | gs_usb | `1-2.1.1` | `00280030594E501820313332` | 1 Mbit/s, UP | **right YAM** |
| `can_left` | gs_usb | `1-2.3` | `0066003D594E501820313332` | 1 Mbit/s, UP | **left YAM** |
| `can1` | mttcan (on-board) | `c310000.mttcan` | - | DOWN | unused |

- The names come from the user's `/etc/udev/rules.d/80-vrteleop-can.rules` (by adapter serial), and
  `vrteleop-can.service` brings both links up at 1 Mbit/s. **It is a separate unit from `vrteleop-rh`**, so stopping
  `vrteleop-rh` leaves both buses UP. GeonosisAI should **not** install our own `rdf-can.service` or CAN udev rule:
  the user's ones already do exactly this.
- The `robot.toml` comment line for `can_left` shows an older adapter serial (`003F005A...`); the udev rule and the
  live interface use `0066003D...`, so the left adapter was swapped at some point. The interface name is what matters.
- vrteleop's log: "right YAM connected on can_right", "left YAM connected on can_left", 7 motors each
  (6 joints + gripper), gripper `linear_4310`, `gravity_comp = false`, and `gripper_limits: None,
  enable_gripper_calibration: True`, i.e. **the gripper calibration sweep runs on every connect** (see §6).

## 3. Cameras: three RealSense

| role (vrteleop name) | model | RealSense serial (robot.toml) | USB port | V4L2 nodes | USB serial in `/dev/v4l/by-id` |
|---|---|---|---|---|---|
| `exo` (overhead / scene; depth on) | **D435** (`8086:0b07`) | `420222071940` | `1-1.1.3` (5 Gbps) | `video12`-`15` (if 1.0), `video16`-`17` (if 1.3) | `344223026503` |
| `wristR` (right wrist) | **D405** (`8086:0b5b`) | `353322271521` | `1-1.1.2` (5 Gbps) | `video0`-`5` | `344123071140` |
| `wristL` (left wrist) | **D405** (`8086:0b5b`) | `260322276368` | `1-1.2.2` (5 Gbps) | `video6`-`11` | `260323072101` |

- The RealSense serial (what `rs-enumerate-devices` and `pyrealsense2` use) is not the USB serial in `by-id`.
- **wristR to `video0-5` is inferred**, not read: vrteleop logged "camera 'wristR' failed (frame deadline exceeded) ...
  restart ... opened" at 16:36:53-59 today, and `video0-5` (port `1-1.1.2`) are the only nodes re-created at 16:36.
  `wristL` is therefore `video6-11`. **VERIFY** with `rs-enumerate-devices -s` once vrteleop is stopped.
- vrteleop opens them through `pyrealsense2` (2.58.4 wheel in its own venv) as `bgr8`, with the cv2 camera backend
  (no GStreamer python bindings). Exo depth is spooled at z16 640x360 15 fps.
- `~/camera_roles.json` lists two other serials (`352122273466`, `352122273139`): it is stale, ignore it.
- Which V4L2 index is the colour stream (needed if we stay on the `v4l2` camera source) is **VERIFY**: on a D435 it is
  normally the interface-3 node (`video16`), on a D405 one of `video0/2/4`. `v4l2-ctl --list-formats-ext -d /dev/videoN`
  tells. RealSense UVC colour is YUYV, **not MJPG**, so `config/cameras.yaml`'s `fourcc: MJPG` will not apply.
  All three cameras are on the 5 Gbps hub, so YUYV 640x480@30 fits.
- The robust option on this box is **`pyrealsense2` by serial** rather than raw V4L2 paths. Our `cameras.py` has no
  `realsense` source yet (sources: v4l2, gstreamer, webcam, file, synthetic, mujoco). That is TDM-120's call.

### Can the Orin Nano CPU stream two 640x480 JPEG feeds at 15-20 fps?
Yes, comfortably. Measured on the Jetson itself with OpenCV 4.11 `imencode` (1 thread, camera-like 640x480 frames,
`nice 19`, while vrteleop was using ~45% of all 6 cores at 883-1267 MHz):

| JPEG quality | encode time | size | 2 cams x 20 fps |
|---|---|---|---|
| 70 | 2.55 ms/frame | 14 KiB | ~10% of one core |
| 80 | 2.62 ms/frame | 21 KiB | ~10% of one core |

Add YUYV->BGR conversion and librealsense capture (well under one core for two cams), and our stream config
downscales to 424 px wide before encoding anyway. Bandwidth: 2 x 20 fps x ~20 KiB = ~0.8 MB/s up the tunnel.
vrteleop itself runs three cameras plus two arm processes on this box today at ~50% total CPU.

## 4. The YAM SDK (`i2rt`) on the Jetson

| location | i2rt | notes |
|---|---|---|
| `~/i2rt` (git, remote `github.com/Northstar-Robotics-AI/i2rt`, a **fork**) | 1.1.2 | HEAD `26133c7` (2026-09-06, "potentially i2rt clean?"). Its own `.venv` (py3.12) has i2rt 1.1.2, mujoco 3.10, numpy 2.2.6, python-can 4.5.0, opencv-python 4.11; **no mink, no ruckig** installed in that venv |
| `~/vrteleop_env_rh` (vrteleop's venv, **do not touch**) | 1.1.1 | py3.12, mujoco 3.14, pyrealsense2 2.58.4, opencv-headless 4.11, numpy 1.26.4 |
| `~/lerobot_env` | 1.1.0 | py3.12, pyrealsense2 2.58.4 |
| `~/vrteleop-rh/i2rt-minimal` | - | vrteleop's vendored copy (do not touch) |

Against `yam-sdk-notes.md` / `jetson-bootstrap-checklist.md`:
- We pin upstream `i2rt-robotics/i2rt@120c3c8` (pyproject 1.1.2) into **our own** venv under `~/rdf/app`, built with
  `scikit-build-core<0.10` for ruckig. That needs network to GitHub and the toolchain (both present). None of the
  existing installs is reusable: wrong Python (3.12 vs our `>=3.11,<3.12`) and the user's own envs.
- The arm hardware matches the notes: YAM v1, DM motors on CAN, gripper `linear_4310` on id 7, 1 Mbit/s.
- `YAM_CAN_CHANNEL` must be **`can_right`** (or `can_left`), not the default `can0`.

## 5. Network from the Jetson

| target | result |
|---|---|
| `https://maincloud.spacetimedb.com/v1/ping` | 200, 0.25 s (TLS 0.17 s) |
| `https://jaxonp808--rdf-media.modal.run/healthz` | 200, 3.2 s (cold Modal container) |
| `https://jaxonp808--rdf-training-policy-web.us-west.modal.run/health` (`POLICY_SERVER_URL`) | **404**, 0.14 s: reachable, but the TDM-41 policy app is not deployed at that URL (same as the laptop saw; `make policy-status`) |
| `https://jaxonp808--rdf-s05b-policy-policy-web.us-west.modal.run/` (spike `POLICY_ENDPOINT_URL`) | TLS OK, no answer in 20 s (scaled to zero; cold start ~33 s) |
| `https://geonosis.jaxonp.com/api/tdm110/backend` | 200, 0.3 s |
| `https://api.trycloudflare.com/` | 405 (reachable: cloudflared quick tunnels will work) |
| laptop over Tailscale | `100.66.171.21`, direct path. `JETSON_ORCHESTRATOR_URL` could be `http://100.66.171.21:<port>` **only if** the orchestrator listens on the tailnet; today it binds 127.0.0.1, so use the `make demo-public --api` / hybrid API tunnel URL instead |

## 6. What's missing / risks before GeonosisAI can run on the real arm

1. **vrteleop owns the CAN buses and all three cameras.** The user must stop it for every GeonosisAI session.
2. **Gripper calibration sweep = motion on connect.** `linear_4310` with no `YAM_GRIPPER_LIMITS` makes the SDK drive the
   gripper both ways on every connect (vrteleop does exactly this). Our first `YamArm.connect()` must happen with the
   user at the e-stop (TDM-119). After it, record `YAM_GRIPPER_LIMITS=closed,open` so later connects skip it.
   Any `YamArm.connect()` also enables the motors in PD hold, so even a "read joints" via the SDK is not motion-free.
   Passive `candump can_right` (listen-only) is the only zero-risk CAN check.
3. **No robot-client install yet** (`~/rdf`, `/etc/rdf` absent). `make jetson-bootstrap` needs root (apt, `/etc/rdf`,
   system units, udev) and sudo needs a password; most of its apt list is already installed. A user-level install
   (`systemctl --user`, env file under `~/.config/rdf/`) avoids sudo entirely (TDM-118).
4. Our `rdf-can.service` and CAN udev rules would fight `vrteleop-can.service`: skip them.
5. `config/cameras.yaml` is a placeholder (`/dev/video0`/`video2`, MJPG): needs the RealSense colour nodes or a
   `pyrealsense2` source (TDM-120).
6. `cloudflared` is not installed (user-level binary in `~/.local/bin` is enough, no sudo).
7. No `robot_client` SpacetimeDB identity yet (`ROBOT_CLIENT_SPACETIME_TOKEN` unset): minting writes to maincloud, so it
   is the user's call (`scripts/jetson/jetson.sh mint-identity --yes`).
8. The TDM-41 policy server URL 404s; the Jetson can reach Modal, so this is a deploy question, not a network one.
9. Safety profile `yam` stays `verified: false` until TDM-119's on-arm checklist.

## 7. Proposal: which arm first

**The right arm** (`can_right`), with **`exo` (D435, `420222071940`)** as our `top` camera and **`wristR`
(D405, `353322271521`)** as our `wrist` camera. Reasons: one arm keeps us inside the single-arm adapter and action
space (`yam.ACTION_SPACE`, 7-dim) that everything was built and trained against; the right side is vrteleop's
primary arm (`right` is listed first, and it is the "rightfirst" side in the user's molmo test logs); and `wristR` is the
camera that has been actively used today. The left arm stays unpowered in software (we never open `can_left`).

Robot-client settings that follow from this:
```
YAM_CAN_CHANNEL=can_right
YAM_GRIPPER=linear_4310
JETSON_ROBOT_ID=yam-northstar-right     # proposal; any non-"test." name
```

## 8. Exact commands for the user

Before a GeonosisAI session (on the Jetson, or `ssh -t northstar@northstar '<cmd>'` from the laptop):
```
sudo systemctl stop vrteleop-rh
```
`vrteleop-can.service` keeps both CAN links up; nothing else needs stopping. Check the cameras are free:
`fuser -v /dev/video* 2>&1 | grep -v '^$' || echo free`.

After the session, give the hardware back to vrteleop (stop ours first so the cameras and CAN are free):
```
# stop GeonosisAI's robot client (command from TDM-118), then:
sudo systemctl start vrteleop-rh
```
`vrteleop-rh` is `enabled`, so it also comes back on its own at the next reboot.

## 9. GeonosisAI on northstar (TDM-118 / TDM-120): what is installed and how to run it

Rules: `docs/real-arm-safety.md` (from LARP). Everything is user-level, with no sudo and nothing of vrteleop's touched.
Installed by `scripts/jetson/tdm118_geonosis.sh install` from committed HEAD:

| what | where |
|---|---|
| code bundle | `~/geonosis/app` (robot-client, common, scripts/jetson) |
| venv (py3.11, 316 MB) + **LARP's i2rt fork 1.1.1** | `~/geonosis/app/services/robot-client/.venv`; fork copied from `~/arc-dist-0.3.2/arc-dist/i2rt-minimal` to `~/geonosis/i2rt-larp-1.1.1` (sources sha256 `1819a1b2...` match `vrteleop_env_rh`) |
| env | `~/.config/geonosis/robot.env` (0600: SpacetimeDB host/db, the `robot_client@northstar` token, `ROBOT_ID=yam-01`, stream secret, media URL) and `robot.local.env` (`YAM_CAN_CHANNEL=can_right`, cameras, stream) |
| tunnel | `~/.local/bin/cloudflared` |
| service | `~/.config/systemd/user/geonosis-robot.service`, **disabled**; runs `rdf_robot_client.tdm118_readonly` (read-only arm, `/ws/state` + `/ws/cameras`, no `/ws/control`). Its `ExecStartPre` guard refuses while `vrteleop-rh` is active |

D405 firmware: not readable while vrteleop holds the cameras. USB `bcdDevice` differs between the two D405s:
wristR (USB serial 344123071140) is `0x50ce`, while wristL (260323072101) and the D435 are `0x50f1`. Read the real
version with `rs-enumerate-devices -s` once vrteleop is stopped. LARP: 5.12.14.100 opens but sends no frames.

### The user's commands

Run them on the Jetson, or from the laptop with `ssh -t northstar@northstar '<cmd>'`.

1. Free the hardware: `sudo systemctl stop vrteleop-rh`
2. Check (agent can run): `pgrep -af "vrteleop|yam_robot_relay" || echo none`; `flock -n /run/lock/vrteleop-arm-can_right.lock true && echo lock free`; `ip -details link show can_right | grep -o "ERROR-[A-Z]*"` (want `ERROR-ACTIVE`)
3. **First hardware step, passive joint read only** (right arm, 10 s, DM refresh-status queries, no enable, no SpacetimeDB):
   `cd ~/geonosis/app/services/robot-client && set -a && . ~/.config/geonosis/robot.local.env && set +a && .venv/bin/python -m rdf_robot_client.tdm118_readonly --passive-read 10`
   (move the right arm by hand during it: `moved` should change only for that arm)
4. Read-only client (yam-01 online, live joints on `/ws/state`, cameras): `systemctl --user start geonosis-robot`, then logs: `journalctl --user -u geonosis-robot -f`
5. Back to vrteleop: `systemctl --user stop geonosis-robot && sudo systemctl start vrteleop-rh`
