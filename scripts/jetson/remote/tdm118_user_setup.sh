#!/usr/bin/env bash
# TDM-118: user-level (no sudo) setup of the GeonosisAI robot client on the northstar Jetson.
# Run by scripts/jetson/tdm118_geonosis.sh install after the bundle is synced to ~/geonosis/app.
#   bash tdm118_user_setup.sh [--cloudflared] [--i2rt-larp SRC_DIR] [--dry-run]
# * venv: uv sync of services/robot-client (Python 3.11, uv-managed; the system python is untouched)
# * --i2rt-larp SRC_DIR: LARP's patched i2rt fork (docs/real-arm-safety.md), COPIED read-only from SRC_DIR
#   (e.g. ~/arc-dist-0.3.2/arc-dist/i2rt-minimal, what vrteleop_env_rh was installed from) to
#   ~/geonosis/i2rt-larp-<version>, its sources hash-checked against LARP's installed copy, then installed
#   into our venv. LARP's own files and venv are only read.
# * cloudflared (optional): the static arm64 binary into ~/.local/bin (for the /ws/state tunnel)
# * geonosis-robot.service into ~/.config/systemd/user/, daemon-reload, NOT enabled, NOT started
# Never touches vrteleop, CAN, the cameras, /etc or anything outside ~/geonosis, ~/.config, ~/.local/bin.
set -euo pipefail
CLOUDFLARED=0; DRY=0; I2RT_LARP=""
while [ $# -gt 0 ]; do case "$1" in --cloudflared) CLOUDFLARED=1 ;; --dry-run) DRY=1 ;; --i2rt-larp) I2RT_LARP="$2"; shift ;;
  *) echo "unknown arg $1" >&2; exit 2 ;; esac; shift; done
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

# 1b. LARP's i2rt fork (copy, verify, install). Never stock i2rt on the real arm.
if [ -n "$I2RT_LARP" ]; then
  [ -f "$I2RT_LARP/pyproject.toml" ] && [ -d "$I2RT_LARP/i2rt" ] || { echo "not an i2rt source dir: $I2RT_LARP" >&2; exit 3; }
  ver="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$I2RT_LARP/pyproject.toml" | head -1)"
  src_hash() { (cd "$1" && find i2rt -name '*.py' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1); }
  want="$(src_hash "$I2RT_LARP")"
  larp_site="$(ls -d "$HOME"/vrteleop_env_rh/lib/python3*/site-packages 2>/dev/null | head -1)"
  if [ -n "$larp_site" ] && [ -d "$larp_site/i2rt" ]; then
    have="$(src_hash "$larp_site")"
    [ "$have" = "$want" ] && say "i2rt fork $ver: sources match LARP's installed copy ($want)" \
      || { echo "i2rt sources in $I2RT_LARP differ from LARP's installed copy ($want vs $have); refusing" >&2; exit 3; }
  fi
  dst="$HOME/geonosis/i2rt-larp-$ver"
  if [ "$DRY" = 1 ]; then say "would: copy $I2RT_LARP -> $dst and uv pip install it"; else
    if [ ! -f "$dst/.src-sha256" ] || [ "$(cat "$dst/.src-sha256")" != "$want" ]; then
      rm -rf "$dst"; mkdir -p "$dst"; cp -a "$I2RT_LARP/." "$dst/"; printf '%s\n' "$want" >"$dst/.src-sha256"
    fi
    if [ "$("$RC/.venv/bin/python" -c 'import importlib.metadata as m; print(m.version("i2rt"))' 2>/dev/null)" != "$ver" ] \
       || [ "$(cat "$RC/.venv/.i2rt-larp" 2>/dev/null)" != "$want" ]; then
      uv pip install --quiet --python "$RC/.venv/bin/python" "$dst" && printf '%s\n' "$want" >"$RC/.venv/.i2rt-larp"
    fi
    "$RC/.venv/bin/python" -c 'import importlib.metadata as m, inspect; from i2rt.robots.motor_chain_robot import MotorChainRobot as R; assert "release_motors" in inspect.signature(R.close).parameters; print("  [geonosis] i2rt", m.version("i2rt"), "(LARP fork) ok")'
  fi
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
