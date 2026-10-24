#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2015  # remote snippets are single-quoted on purpose; A && B || C is used for messages
# TDM-44: one-time (and re-runnable) system setup on the Jetson. Run as root via sudo; normally
# invoked by `make jetson-bootstrap` from the laptop, which syncs the bundle first.
#
#   sudo bash bootstrap.sh --user <login> --app-dir /home/<login>/rdf/app [--uv-version X.Y.Z] [--dry-run]
#
# Idempotent: every step checks current state and only changes what differs. --dry-run only probes
# (no root needed) and prints what it would change.
# Verified in an arm64 Ubuntu 22.04 container (scripts/jetson/test/); NOT yet on real L4T. Steps
# that only make sense on hardware (modprobe, udev trigger, systemd) are skipped when absent.
set -euo pipefail

RDF_USER=""; APP_DIR=""; UV_VERSION=""; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) RDF_USER="$2"; shift ;;
    --app-dir) APP_DIR="$2"; shift ;;
    --uv-version) UV_VERSION="$2"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$RDF_USER" ] && [ -n "$APP_DIR" ] || { echo "usage: bootstrap.sh --user U --app-dir DIR [--uv-version V] [--dry-run]" >&2; exit 2; }
REMOTE_DIR="$APP_DIR/scripts/jetson/remote"
PY_VERSION=3.11
ETC=/etc/rdf
CHANGED=0

say()  { printf '  [bootstrap] %s\n' "$*"; }
warn() { printf '  [bootstrap] WARN %s\n' "$*" >&2; }
act()  {  # act "description" cmd...: run (or, in dry-run, describe) a change
  local d="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then say "would: $d"; else say "$d"; "$@"; fi
  CHANGED=1
}
as_user() { if [ "$(id -un)" = "$RDF_USER" ]; then "$@"; else sudo -u "$RDF_USER" -H "$@"; fi; }
have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then echo "run as root (sudo)" >&2; exit 2; fi
id "$RDF_USER" >/dev/null 2>&1 || { echo "user $RDF_USER does not exist" >&2; exit 2; }
USER_HOME="$(getent passwd "$RDF_USER" | cut -d: -f6)"

# ---------- 1. preflight: L4T / JetPack / OS / Python ----------
ARCH="$(uname -m)"
L4T="none"; JETPACK="unknown"
if [ -f /etc/nv_tegra_release ]; then
  # "# R36 (release), REVISION: 3.0, ..." -> 36.3.0
  L4T="$(sed -n 's/^# R\([0-9]*\) (release), REVISION: \([0-9.]*\).*/\1.\2/p' /etc/nv_tegra_release | head -n1)"
  case "$L4T" in
    36.*) JETPACK="6.x" ;;
    35.*) JETPACK="5.1.x" ;;
    34.*) JETPACK="5.0.x" ;;
    32.*) JETPACK="4.x (too old: no Python 3.11 wheels for some deps; upgrade advised)" ;;
  esac
fi
DPKG_L4T="$(dpkg-query -W -f='${Version}' nvidia-l4t-core 2>/dev/null || true)"
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
SYS_PY="$(python3 -V 2>/dev/null || echo 'python3 missing')"
say "arch=$ARCH  L4T=$L4T${DPKG_L4T:+ (nvidia-l4t-core $DPKG_L4T)}  JetPack=$JETPACK  OS=${PRETTY_NAME:-?}  system $SYS_PY"
[ "$ARCH" = aarch64 ] || warn "not aarch64: this is not a Jetson (fine for the container test)"
[ "$L4T" = none ] && warn "no /etc/nv_tegra_release: not L4T (container stand-in?). Jetson-specific bits will be skipped."
say "Python $PY_VERSION comes from uv (managed interpreter), independent of the system python3"
FREE_MB="$(df -Pm "$USER_HOME" | awk 'NR==2{print $4}')"
[ "${FREE_MB:-0}" -lt 3000 ] && warn "only ${FREE_MB} MB free in $USER_HOME (venv + I2RT need ~2-3 GB)"

# ---------- 2. apt packages ----------
APT_PKGS=(ca-certificates curl git rsync iproute2 kmod usbutils udev
          v4l-utils ffmpeg can-utils
          gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav
          libglib2.0-0 libgl1    # libgl1/libglib: some OpenCV wheels still dlopen them
          build-essential cmake) # I2RT SDK: ruckig 0.15.3 is sdist-only and builds from source (yam-sdk-notes.md §1)
MISSING=()
for p in "${APT_PKGS[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || MISSING+=("$p")
done
if [ ${#MISSING[@]} -eq 0 ]; then say "apt: all ${#APT_PKGS[@]} packages present"
else
  act "apt install ${MISSING[*]}" bash -c 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq --no-install-recommends "$@" >/dev/null' _ "${MISSING[@]}"
fi

# ---------- 3. groups (cameras, serial/USB-CAN adapters) ----------
for g in video dialout plugdev; do
  getent group "$g" >/dev/null || act "create group $g" groupadd --system "$g"
  if id -nG "$RDF_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then :; else
    act "add $RDF_USER to group $g (log out/in for interactive shells; the service gets it via SupplementaryGroups)" usermod -aG "$g" "$RDF_USER"
  fi
done

# ---------- 4. CAN kernel modules (YAM arm over SocketCAN) ----------
MODS_FILE=/etc/modules-load.d/rdf-can.conf
MODS_WANT=$'# TDM-44: SocketCAN for the I2RT YAM arm. gs_usb = USB-CAN adapters (candleLight/CANable);\n# mttcan = the Jetson\'s on-board CAN controller (needs a transceiver). TO VERIFY which one the YAM uses.\ncan\ncan_raw\ngs_usb\nmttcan'
if [ "$(cat "$MODS_FILE" 2>/dev/null || true)" != "$MODS_WANT" ]; then
  act "write $MODS_FILE" bash -c 'printf "%s\n" "$1" >"$2"' _ "$MODS_WANT" "$MODS_FILE"
fi
if [ -d "/lib/modules/$(uname -r)" ]; then
  for m in can can_raw gs_usb mttcan; do
    lsmod 2>/dev/null | awk '{print $1}' | grep -qx "$m" && continue
    modinfo "$m" >/dev/null 2>&1 || { say "module $m not available in this kernel (ok if unused)"; continue; }
    act "modprobe $m" modprobe "$m" || warn "modprobe $m failed"
  done
else say "no /lib/modules/$(uname -r): skipping modprobe (container)"; fi

# ---------- 5. directories ----------
# /etc/rdf: root-owned, group = the robot user (setgid, 2770), so `make jetson-env` can write the
# env file over ssh without sudo while other users cannot read it.
if [ "$(stat -c '%U:%G %a' "$ETC" 2>/dev/null || true)" != "root:$RDF_USER 2770" ]; then
  act "create $ETC (root:$RDF_USER 2770)" install -d -m 2770 -o root -g "$RDF_USER" "$ETC"
fi
LOCAL_ENV="$ETC/robot-client.local.env"
if [ ! -f "$LOCAL_ENV" ]; then
  # Jetson-local, non-secret settings. Created once; never overwritten (edit it on the Jetson).
  act "create $LOCAL_ENV (Jetson-local settings, edit in place)" bash -c 'cat >"$1" <<EOF
# Jetson-local robot-client settings (TDM-44). Created once by bootstrap.sh; edit here, it is never overwritten.
# Secrets and shared config live in robot-client.env (pushed from the laptop by make jetson-env).
RDF_ENV_FILE=/etc/rdf/robot-client.env
# Camera config (TDM-47). Leave unset to use services/robot-client/config/cameras.yaml.
#RDF_CAMERAS_CONFIG=/etc/rdf/cameras.yaml
# CAN interfaces brought up by rdf-can.service, space-separated. TO VERIFY against the I2RT SDK docs
# (the SDK may expect a specific name such as can0 or a udev-renamed one like can_follow).
RDF_CAN_IFACES=can0
RDF_CAN_BITRATE=1000000
RDF_CAN_TXQUEUELEN=1000
# YAM adapter (TDM-46, services/robot-client/docs/yam-sdk-notes.md §8). The channel must match RDF_CAN_IFACES.
YAM_CAN_CHANNEL=can0
# TO VERIFY at the arm: gripper model from its label, calibration result, smoothing choice (CP-1).
#YAM_GRIPPER=linear_4310
#YAM_GRIPPER_LIMITS=
#YAM_SMOOTHING=1
PYTHONUNBUFFERED=1
EOF
chown root:"$2" "$1"; chmod 640 "$1"' _ "$LOCAL_ENV" "$RDF_USER"
fi

# ---------- 6. uv + Python 3.11 (as the robot user) ----------
UV="$USER_HOME/.local/bin/uv"
have_uv="$( [ -x "$UV" ] && "$UV" --version 2>/dev/null | awk '{print $2}' || true)"
if [ -z "$have_uv" ] || { [ -n "$UV_VERSION" ] && [ "$have_uv" != "$UV_VERSION" ]; }; then
  url="https://astral.sh/uv/${UV_VERSION:+$UV_VERSION/}install.sh"
  act "install uv ${UV_VERSION:-latest} for $RDF_USER (had: ${have_uv:-none}) from $url" \
    as_user env UV_NO_MODIFY_PATH=1 sh -c "curl -LsSf '$url' | sh -s -- --quiet"
else say "uv $have_uv present"; fi
if [ "$DRY_RUN" = 0 ] || [ -x "$UV" ]; then
  if [ -x "$UV" ] && as_user "$UV" python find "$PY_VERSION" >/dev/null 2>&1; then say "Python $PY_VERSION present (uv)"
  else act "uv python install $PY_VERSION" as_user "$UV" python install "$PY_VERSION"; fi
else say "would: uv python install $PY_VERSION"; fi
# login shells get uv on PATH
PROFILE_D=/etc/profile.d/rdf-uv.sh
PROFILE_WANT='# TDM-44: uv for the robot user
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac'
[ "$(cat "$PROFILE_D" 2>/dev/null || true)" = "$PROFILE_WANT" ] || act "write $PROFILE_D" bash -c 'printf "%s\n" "$1" >"$2"' _ "$PROFILE_WANT" "$PROFILE_D"

# ---------- 7. files from the bundle: udev rules, systemd units, CLI ----------
# install_file SRC DEST MODE: copy only if content differs (rendering @USER@/@APP@/@HOME@)
install_file() {
  local src="$1" dest="$2" mode="$3" tmp
  if [ ! -f "$src" ]; then say "would install $dest (bundle not synced yet: $src)"; return 0; fi
  tmp="$(mktemp)"
  sed -e "s#@USER@#$RDF_USER#g" -e "s#@APP@#$APP_DIR#g" -e "s#@HOME@#$USER_HOME#g" "$src" >"$tmp"
  if cmp -s "$tmp" "$dest" 2>/dev/null; then rm -f "$tmp"; return 1; fi
  if [ "$DRY_RUN" = 1 ]; then say "would install $dest"; rm -f "$tmp"; CHANGED=1; return 0; fi
  install -D -m "$mode" "$tmp" "$dest"; rm -f "$tmp"; say "installed $dest"; CHANGED=1; return 0
}

if install_file "$REMOTE_DIR/99-rdf-robot.rules" /etc/udev/rules.d/99-rdf-robot.rules 644; then
  if [ "$DRY_RUN" = 0 ] && command -v udevadm >/dev/null && [ -d /run/udev ]; then
    udevadm control --reload-rules && udevadm trigger --subsystem-match=video4linux --subsystem-match=net --subsystem-match=tty || warn "udevadm reload failed"
  else say "udev not running here: rules take effect on next boot/replug"; fi
fi
install_file "$REMOTE_DIR/robot-client" /usr/local/bin/robot-client 755 || say "/usr/local/bin/robot-client up to date"
install_file "$REMOTE_DIR/can-up.sh" /usr/local/lib/rdf/can-up.sh 755 || true

UNITS_CHANGED=0
install_file "$REMOTE_DIR/robot-client.service.in" /etc/systemd/system/robot-client.service 644 && UNITS_CHANGED=1
install_file "$REMOTE_DIR/rdf-can.service" /etc/systemd/system/rdf-can.service 644 && UNITS_CHANGED=1

# journald: keep logs across reboots, bounded
JD=/etc/systemd/journald.conf.d/rdf.conf
JD_WANT=$'# TDM-44: persistent, bounded journal for robot-client logs\n[Journal]\nStorage=persistent\nSystemMaxUse=500M\nMaxRetentionSec=14day'
if [ "$(cat "$JD" 2>/dev/null || true)" != "$JD_WANT" ]; then
  act "write $JD (persistent journal, 500 MB cap)" bash -c 'mkdir -p "$(dirname "$2")"; printf "%s\n" "$1" >"$2"' _ "$JD_WANT" "$JD"
  [ "$DRY_RUN" = 0 ] && have_systemd && { mkdir -p /var/log/journal; systemctl restart systemd-journald || warn "journald restart failed"; }
fi
# let the robot user read the journal without sudo (make jetson-logs)
id -nG "$RDF_USER" | tr ' ' '\n' | grep -qx systemd-journal || { getent group systemd-journal >/dev/null && act "add $RDF_USER to systemd-journal" usermod -aG systemd-journal "$RDF_USER"; } || true

if have_systemd; then
  if [ "$UNITS_CHANGED" = 1 ] && [ "$DRY_RUN" = 0 ]; then systemctl daemon-reload; fi
  if [ "$(systemctl is-enabled rdf-can 2>/dev/null || true)" != enabled ]; then act "enable rdf-can.service (CAN up at boot)" systemctl enable rdf-can.service; fi
  # robot-client itself is enabled/started by deploy.sh once the venv and adapter are ready.
else
  say "systemd is not running (container stand-in): units installed, enable/start skipped"
fi

# ---------- 8. narrow sudo for deploy (systemctl on our two units only) ----------
SUDOERS=/etc/sudoers.d/rdf-robot-client
SUDO_WANT="# TDM-44: let $RDF_USER manage the robot-client units without a password (make jetson-deploy)
$RDF_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start robot-client, /usr/bin/systemctl stop robot-client, /usr/bin/systemctl restart robot-client, /usr/bin/systemctl enable robot-client, /usr/bin/systemctl disable robot-client, /usr/bin/systemctl enable --now robot-client, /usr/bin/systemctl disable --now robot-client, /usr/bin/systemctl restart rdf-can, /usr/bin/systemctl daemon-reload"
if [ "$(cat "$SUDOERS" 2>/dev/null || true)" != "$SUDO_WANT" ]; then
  if [ "$DRY_RUN" = 1 ]; then say "would write $SUDOERS (systemctl on robot-client/rdf-can only)"; CHANGED=1
  else
    t="$(mktemp)"; printf '%s\n' "$SUDO_WANT" >"$t"
    if visudo -cf "$t" >/dev/null; then install -m 440 "$t" "$SUDOERS"; say "wrote $SUDOERS"; CHANGED=1; else warn "sudoers snippet failed visudo; skipped"; fi
    rm -f "$t"
  fi
fi

[ "$CHANGED" = 0 ] && say "nothing to change: system already set up" || true
if [ "$DRY_RUN" = 1 ]; then say "done (dry run: nothing was changed)"; else say "done"; fi
