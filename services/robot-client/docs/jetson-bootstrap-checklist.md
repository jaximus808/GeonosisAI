# Jetson bootstrap checklist (TDM-44)

This takes a fresh Jetson to a running `robot-client` systemd service with **one command from the laptop**. Code: `scripts/jetson/`. It was built and tested overnight with no Jetson available: shellcheck, the dry run, and a full run against an arm64 Ubuntu 22.04 container with systemd (`scripts/jetson/test/run_docker_test.sh`). **None of it has run on real L4T yet.** Items marked TO VERIFY are guesses.

## 0. Before you start (laptop)
- [ ] Get passwordless ssh working: `ssh-copy-id <user>@<jetson-ip>`, then check that `ssh <user>@<jetson-ip> true` doesn't prompt.
- [ ] Optional: put `JETSON_HOST=` and `JETSON_USER=` in `.env`, so you can leave them off every `make` call.
- [ ] Recommended: give the Jetson its own SpacetimeDB identity instead of the owner token. Run `scripts/jetson/jetson.sh mint-identity`, which only shows a plan; add `--yes` to actually run it. With `--yes` it:
  - creates an identity
  - allow-lists it as `robot_client` with the owner token
  - stores it in `.env` as `ROBOT_CLIENT_SPACETIME_TOKEN`
  Without it, `jetson-env` falls back to the owner token and warns. To refuse that fallback, set `JETSON_REQUIRE_SERVICE_TOKEN=1`.
- [ ] Set `JETSON_ORCHESTRATOR_URL` in `.env` to an address the Jetson can reach: the laptop's LAN IP and port, or a tunnel URL. It's needed for episode uploads, because the orchestrator signs them. If you leave it blank, the Jetson uses `http://localhost:8000`, which is the Jetson itself.
- [ ] Optional: set `JETSON_ROBOT_ID` (default `yam-office-1`).
- [ ] Look at the plan first. Nothing is written anywhere: `make jetson-bootstrap JETSON_HOST=<ip> JETSON_USER=<user> DRY_RUN=1`

## 1. The one command
```
make jetson-bootstrap JETSON_HOST=<ip> JETSON_USER=<user>
```
If sudo needs a password, run this from a real terminal so it can prompt. Bootstrap then installs a narrow NOPASSWD rule, scoped to `systemctl` on the two units, which later deploys use. The command runs these steps, and it is safe to re-run: each step checks the current state first.

1. **Preflight.** Checks ssh, then prints L4T/JetPack (from `/etc/nv_tegra_release` and `nvidia-l4t-core`), Ubuntu, system python3 and the sudo mode.
2. **Sync.** Builds a bundle from **committed HEAD** with `git archive` (set `JETSON_SRC=worktree` to ship uncommitted changes instead) and rsyncs it to `~/rdf/app` over ssh. The bundle contains `services/robot-client`, `services/common`, `scripts/jetson` and `bench_client.py`. There's no GitHub remote yet. Stale files are deleted; `.venv` is kept. `~/rdf/app/DEPLOYED_REV` records the revision.
3. **`remote/bootstrap.sh`** (runs as root):
   - apt: `v4l-utils ffmpeg can-utils gstreamer1.0-*` plus a few basics (only the missing ones)
   - groups `video`/`dialout`/`plugdev`
   - the CAN modules `can can_raw gs_usb mttcan`
   - uv, at the laptop's version, plus Python 3.11 (managed by uv; the system python is untouched)
   - `/etc/rdf/`, with `robot-client.local.env`. This file is created once and holds Jetson-local settings like the CAN interface and bitrate. Edit it in place on the Jetson.
   - the udev rules
   - `robot-client.service` and `rdf-can.service`
   - a persistent journald drop-in, capped at 500 MB
   - the `/usr/local/bin/robot-client` CLI
4. **Env push.** Writes `/etc/rdf/robot-client.env` (0600) from `.env`, with only the keys the client needs:
   - `SPACETIME_HOST`, `SPACETIME_DB_NAME` and `SPACETIME_TOKEN`
   - `ROBOT_ID` and `ORCHESTRATOR_URL`
   - `POLICY_ENDPOINT_URL`, `POLICY_SERVER_URL` and `POLICY_SERVER_TOKEN`
   - `RDF_MEDIA_STORE_URL`, `RDF_STREAM_TOKEN_SECRET` and `JETSON_WS_PORT`

   Only key names are printed. Values travel over ssh stdin, and a push with no changes does nothing.
5. **`remote/deploy.sh`:**
   - `uv sync --locked --inexact` of the robot-client venv, with `rdf-common` from `../common`
   - clones the I2RT SDK (`github.com/i2rt-robotics/i2rt`, pinned to TDM-46's tested commit `120c3c8`; override with `ARGS="--i2rt-ref <sha>"`). It then installs it editable into the venv with `--build-constraint scikit-build-core<0.10`, because `ruckig 0.15.3` is sdist-only (`yam-sdk-notes.md` §1). Bootstrap installs `build-essential` and `cmake` for that build. Check: `import i2rt, mink, mujoco`.
   - import checks (also reports whether OpenCV was built with GStreamer)
   - picks the service mode (see below) and restarts the service **only if something changed**
6. **Connectivity check** (see section 3).

## 2. Service modes (`/etc/rdf/robot-client.mode`; it persists across deploys)
| mode | what runs |
|---|---|
| `off` (default on a fresh Jetson) | The unit is installed but disabled. Bootstrap **never** starts arm control on its own: a human picks `yam` after the SDK-alone bring-up in `yam-sdk-notes.md` §8 (steps 1–6). |
| `auto` | `yam` once the YAM adapter (TDM-46), safety layer (TDM-45) and I2RT SDK all import; otherwise `off` |
| `yam` | `rdf-robot-client`: the real arm, with cameras from `config/cameras.yaml` |
| `sim` | `--sim --name test.jetson-sim-<host>`. A no-arm smoke test: it proves the service, the env and the heartbeat from the Jetson. Robots named `test.*` are ignored by auto-selection. |

To switch modes: `make jetson-deploy JETSON_SERVICE_MODE=yam` (or `sim`/`auto`/`off`). `yam` refuses to start, and stays `off`, if the YAM stack doesn't import.

The unit uses `Restart=on-failure` with `RestartSec=3`, capped at 20 starts per 10 min. Logs go to journald, and `SIGTERM` stops the arm cleanly.

## 3. Connectivity check (`make jetson-check`, or `robot-client check` on the Jetson)
- [ ] **clock:** skew against the SpacetimeDB `Date` header should be under 2 s. If not, run `sudo timedatectl set-ntp true`.
- [ ] **dns+tcp** for each host. Firewall problems show up here first.
- [ ] **spacetime:** `/v1/ping`, an authenticated `SELECT * FROM robot`, and a **WebSocket subscription**, which is the client's real path and the one office proxies tend to break.
- [ ] **policy:** runs `spikes/tdm-37-latency/bench_client.py` (http, 20 calls; add `ARGS=--policy-ws` for the websocket rollout path). This **wakes a Modal GPU container** (~33 s cold). The laptop baseline is ws p50 157 / p95 183 ms; PASS needs p95 < 250 ms. The results are copied back to `spikes/tdm-37-latency/results/jetson-*.json`, which is the Jetson benchmark TDM-37 asked for. To skip this check: `make jetson-check ARGS=--no-policy`.
- [ ] **media:** `RDF_MEDIA_STORE_URL/healthz`. When the orchestrator is reachable, it also does a real signed upload: `POST /uploads/sign` → PUT 1 KiB → the sha256 must match. The test object lands under `tickets/tdm44-probe/`.
- [ ] **orchestrator:** `/healthz`. This is a WARN rather than a FAIL, because the command loop only needs SpacetimeDB.
- Note: `POLICY_SERVER_URL/health` returned **404** from the laptop tonight. Check that the TDM-41 server is deployed (`make policy-status`).

## 4. Hardware bits (TO VERIFY on the real Jetson + YAM)
- [ ] **CAN** (same as `yam-sdk-notes.md` §8 step 1; `rdf-can.service` runs `ip link set <if> up type can bitrate 1000000`): run `lsusb` and `ip -details link show type can`. Check which adapter the YAM uses: a USB-CAN `gs_usb`, usually `1d50:606f`, or the on-board `mttcan`. Then check the interface name the I2RT SDK opens. Set `RDF_CAN_IFACES`, `RDF_CAN_BITRATE` (default 1 Mbit/s) and the adapter's `YAM_CAN_CHANNEL` (same name; also `YAM_GRIPPER` once known) in `/etc/rdf/robot-client.local.env`, run `sudo systemctl restart rdf-can`, and confirm with `robot-client can`. `candump can0` should show motor traffic once the arm is powered.
- [ ] **udev:** fill in the `TO_VERIFY_*` values in `scripts/jetson/remote/99-rdf-robot.rules`:
  - camera VID/PID/serial (or USB port paths, for identical cameras): run `udevadm info -a -n /dev/video0 | grep -E 'idVendor|idProduct|serial|KERNELS'`
  - the CAN adapter serial
  Then re-run `make jetson-bootstrap`: it reinstalls only the changed rules and reloads udev. Check that `ls -l /dev/rdf-*` shows `rdf-cam-top` and `rdf-cam-wrist`, then point `config/cameras.yaml` `device:` at them.
- [ ] **Cameras:** follow `cameras-jetson-checklist.md`, using `robot-client cameras-list` and `robot-client cameras-check --seconds 10 -v`.
- [ ] **Arm:** follow `yam-sdk-notes.md` §8 (SDK alone first), then `make jetson-deploy JETSON_SERVICE_MODE=yam`, then `robot-loop-jetson-checklist.md` (e-stop first). `robot-client home` and `robot-client record-test` are placeholders until TDM-46 and TDM-48 land; they print what to do instead.
- [ ] Repeat everything on the spare Jetson with `JETSON_HOST=<spare-ip> JETSON_ROBOT_ID=<name>`.

## 5. Done when (ticket)
- [ ] `make jetson-status` shows `robot-client enabled=enabled active=active`.
- [ ] On the dashboard, the `robot` row for `yam-office-1` shows `online` with a fresh heartbeat. Before the arm is brought up, `JETSON_SERVICE_MODE=sim` proves the same path as `test.jetson-sim-<host>`.

## 6. Day-to-day
| command | what it does |
|---|---|
| `make jetson-deploy` | Code update: sync HEAD, `uv sync`, then restart only if something changed. `ARGS=--restart` forces a restart. **Don't deploy during an eval trial.** |
| `make jetson-env` | Re-push the env after editing `.env`, then `make jetson-deploy` to apply it. |
| `make jetson-status` | Revision, mode, unit state, devices, CAN links, disk. |
| `make jetson-logs` | `journalctl -f`. Add `FOLLOW=0 LINES=500` to print and exit instead. |
| `make jetson-check` | The connectivity check. `ARGS=--no-policy` skips the GPU wake. |
| `make jetson-ssh` | A shell on the Jetson. |
| `robot-client run` | On the Jetson: stops the service and runs the client in the foreground, so you can type `clear`/`status`/`estop`. |

If bootstrap's system files change (udev rules, units, the CLI), re-run `make jetson-bootstrap`. It is idempotent, and only the changed files get reinstalled.
