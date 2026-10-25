#!/usr/bin/env bash
# TDM-118: user-level (no sudo) setup of the GeonosisAI robot client on the northstar Jetson.
# Run by scripts/jetson/tdm118_geonosis.sh install after the bundle is synced to ~/geonosis/app.
#   bash tdm118_user_setup.sh [--cloudflared] [--dry-run]
# * venv: uv sync of services/robot-client (Python 3.11, uv-managed; the system python is untouched)
# * cloudflared (optional): the static arm64 binary into ~/.local/bin (for the /ws/state tunnel)
# * geonosis-robot.service into ~/.config/systemd/user/, daemon-reload, NOT enabled, NOT started
# Never touches vrteleop, CAN, the cameras, /etc or anything outside ~/geonosis, ~/.config, ~/.local/bin.
set -euo pipefail
CLOUDFLARED=0; DRY=0
for a in "$@"; do case "$a" in --cloudflared) CLOUDFLARED=1 ;; --dry-run) DRY=1 ;; *) echo "unknown arg $a" >&2; exit 2 ;; esac; done
APP="$HOME/geonosis/app"; RC="$APP/services/robot-client"; UNIT_DIR="$HOME/.config/systemd/user"
CFG="$HOME/.config/geonosis"
CF_VER="${CLOUDFLARED_VERSION:-latest}"   # a release tag, or latest
export PATH="$HOME/.local/bin:$PATH" UV_PYTHON=3.11 UV_PYTHON_PREFERENCE=only-managed
say() { printf '  [geonosis] %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "would: $*"; else "$@"; fi; }

[ -f "$APP/DEPLOYED_REV" ] || { echo "no bundle at $APP" >&2; exit 3; }
say "bundle: $(tr '\n' ' ' <"$APP/DEPLOYED_REV")"
say "disk before: $(df -h / | awk 'NR==2{print $4" free ("$5" used)"}')"

# 1. venv (no I2RT SDK: the read-only adapter speaks SocketCAN through the stdlib; TDM-119 adds the SDK)
if [ "$DRY" = 1 ]; then say "would: uv sync --locked --inexact in $RC"; else
  (cd "$RC" && { uv sync --locked --inexact --quiet 2>/dev/null || { say "uv.lock stale for this bundle: syncing without --locked"; uv sync --inexact --quiet; }; })
  "$RC/.venv/bin/python" -c 'import rdf_robot_client.tdm118_readonly, rdf_robot_client.stream, cv2, numpy, websockets; print("  [geonosis] imports ok")'
  say "venv: $("$RC/.venv/bin/python" -V) $(du -sh "$RC/.venv" | cut -f1)"
fi

# 2. cloudflared (user binary, no package install)
if [ "$CLOUDFLARED" = 1 ]; then
  if command -v cloudflared >/dev/null && { [ "$CF_VER" = latest ] || cloudflared --version 2>/dev/null | grep -q "$CF_VER"; }; then
    say "cloudflared present: $(cloudflared --version 2>&1 | head -1)"
  else
    run mkdir -p "$HOME/.local/bin"
    if [ "$CF_VER" = latest ]; then url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    else url="https://github.com/cloudflare/cloudflared/releases/download/$CF_VER/cloudflared-linux-arm64"; fi
    if [ "$DRY" = 1 ]; then say "would: download $url -> ~/.local/bin/cloudflared"; else
      t="$(mktemp "$HOME/.local/bin/.cloudflared.XXXXXX")"
      curl -fsSL --retry 3 -o "$t" "$url" && chmod 755 "$t" && mv -f "$t" "$HOME/.local/bin/cloudflared"
      say "cloudflared: $("$HOME/.local/bin/cloudflared" --version 2>&1 | head -1)"
    fi
  fi
fi

# 3. config dir + the user unit (disabled)
run mkdir -p "$CFG" "$UNIT_DIR"; run chmod 700 "$CFG"
[ -f "$CFG/robot.env" ] || say "WARN $CFG/robot.env missing (push it: scripts/jetson/tdm118_geonosis.sh env)"
[ -f "$CFG/robot.local.env" ] || say "WARN $CFG/robot.local.env missing (pushed by tdm118_geonosis.sh install)"
run chmod 755 "$APP/scripts/jetson/remote/tdm118_guard.sh"
if ! cmp -s "$APP/scripts/jetson/remote/geonosis-robot.service" "$UNIT_DIR/geonosis-robot.service"; then
  run install -m 644 "$APP/scripts/jetson/remote/geonosis-robot.service" "$UNIT_DIR/geonosis-robot.service"
  run systemctl --user daemon-reload
  say "installed $UNIT_DIR/geonosis-robot.service (disabled; start it by hand after stopping vrteleop-rh)"
else say "geonosis-robot.service unchanged"; fi
say "unit: enabled=$(systemctl --user is-enabled geonosis-robot 2>/dev/null || true) active=$(systemctl --user is-active geonosis-robot 2>/dev/null || true)"
say "disk after: $(df -h / | awk 'NR==2{print $4" free ("$5" used)"}')"
