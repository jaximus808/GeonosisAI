#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2015  # remote snippets are single-quoted on purpose; A && B || C is used for messages
# TDM-44: (re)build the robot-client venv on the Jetson and (re)start the service. Runs as the robot
# user (systemctl goes through the narrow sudoers rule bootstrap.sh installs). Normally invoked by
# `make jetson-deploy` / `make jetson-bootstrap` after the bundle is synced.
#
#   bash deploy.sh --user U --app-dir DIR [--mode auto|yam|sim|off] [--restart] [--no-i2rt]
#                  [--i2rt-ref REF] [--dry-run]
#
# Service modes (stored in /etc/rdf/robot-client.mode, kept across deploys):
#   off   (default on a fresh Jetson) unit installed but disabled. The arm is never driven until a human
#         picks yam after the SDK-alone bring-up (yam-sdk-notes.md §8 steps 1-6).
#   auto  yam when the YAM adapter (TDM-46), safety layer (TDM-45) and I2RT SDK import, else off
#   yam   the real arm: rdf-robot-client (adapter yam, cameras from config)
#   sim   smoke test with no arm: --sim --name test.jetson-sim-<host> (test.* robots are ignored by
#         auto-selection, so nothing gets dispatched to it). Proves service + heartbeat from the Jetson.
# Idempotent: the service restarts only when the bundle, venv, env or mode changed (or --restart).
set -euo pipefail

RDF_USER=""; APP_DIR=""; MODE=""; FORCE_RESTART=0; I2RT=1; DRY_RUN=0
I2RT_REPO="${I2RT_REPO:-https://github.com/i2rt-robotics/i2rt.git}"   # TDM-46: services/robot-client/docs/yam-sdk-notes.md
I2RT_REF="${I2RT_REF:-120c3c8}"   # TDM-46 tested commit; pyproject 1.1.2 vs tag v1.3.6 disagree, so pin the commit
while [ $# -gt 0 ]; do
  case "$1" in
    --user) RDF_USER="$2"; shift ;;   # accepted for symmetry with bootstrap.sh
    --app-dir) APP_DIR="$2"; shift ;;
    --mode) MODE="$2"; shift ;;
    --restart) FORCE_RESTART=1 ;;
    --no-i2rt) I2RT=0 ;;
    --i2rt-ref) I2RT_REF="$2"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done
: "$RDF_USER"
[ -n "$APP_DIR" ] || { echo "usage: deploy.sh --user U --app-dir DIR [...]" >&2; exit 2; }
case "$MODE" in ''|auto|yam|sim|off) ;; *) echo "--mode must be auto|yam|sim|off" >&2; exit 2 ;; esac

say()  { printf '  [deploy] %s\n' "$*"; }
warn() { printf '  [deploy] WARN %s\n' "$*" >&2; }
have_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null; }
sctl() { sudo /usr/bin/systemctl "$@"; }

ROOT="$(dirname "$APP_DIR")"
RC="$APP_DIR/services/robot-client"
VENV_PY="$RC/.venv/bin/python"
MODE_FILE=/etc/rdf/robot-client.mode
STATE_FILE="$ROOT/.deploy-state"
export PATH="$HOME/.local/bin:$PATH"
export UV_PYTHON=3.11 UV_PYTHON_PREFERENCE=only-managed

if [ ! -f "$APP_DIR/DEPLOYED_REV" ]; then
  if [ "$DRY_RUN" = 1 ]; then say "would: uv sync the venv, install the I2RT SDK and set the service mode once the bundle is synced"; exit 0; fi
  echo "no bundle at $APP_DIR (run make jetson-deploy from the laptop)" >&2; exit 3
fi
say "bundle: $(tr '\n' ' ' <"$APP_DIR/DEPLOYED_REV")"
command -v uv >/dev/null || { [ "$DRY_RUN" = 1 ] && { say "would: uv sync (uv not installed yet)"; exit 0; }; echo "uv missing: run make jetson-bootstrap first" >&2; exit 3; }

# ---------- 1. venv: robot-client + rdf-common (path dep ../common) ----------
if [ "$DRY_RUN" = 1 ]; then
  say "would: uv sync --locked --inexact in $RC (Python 3.11, rdf-common from ../common)"
  [ -x "$VENV_PY" ] && say "venv exists: $("$VENV_PY" -V 2>&1)" || say "venv does not exist yet"
else
  # --inexact keeps the I2RT SDK (installed below, not in uv.lock) across syncs
  (cd "$RC" && { uv sync --locked --inexact --quiet 2>/dev/null || { warn "uv.lock is stale for this bundle; syncing without --locked"; uv sync --inexact --quiet; }; })
  say "venv: $("$VENV_PY" -V 2>&1) at $RC/.venv"
fi

# ---------- 2. I2RT YAM SDK (services/robot-client/docs/yam-sdk-notes.md §1) ----------
# Not on PyPI: install the pinned git commit, editable, into the venv. Its ruckig==0.15.3 dep ships only
# an sdist that fails to build under scikit-build-core>=0.10; the SDK's own uv build-constraint is
# ignored by `uv pip install` from another project, so we pass one explicitly (needs cmake + g++,
# installed by bootstrap.sh).
I2RT_DIR="$ROOT/i2rt"
I2RT_CONSTRAINTS="$ROOT/i2rt-build-constraints.txt"
I2RT_STAMP="$ROOT/.i2rt-installed"
i2rt_resolve() {  # REF -> full commit sha in the local checkout ('' if unknown)
  git -C "$I2RT_DIR" rev-parse --verify -q "$1^{commit}" 2>/dev/null \
    || git -C "$I2RT_DIR" rev-parse --verify -q "origin/$1^{commit}" 2>/dev/null || true
}
if [ "$I2RT" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    if [ -d "$I2RT_DIR/.git" ]; then
      say "I2RT checkout at $I2RT_DIR: $(git -C "$I2RT_DIR" rev-parse --short HEAD) (want $I2RT_REF)"
      [ "$(cat "$I2RT_STAMP" 2>/dev/null || true)" = "$(i2rt_resolve "$I2RT_REF")" ] && "$VENV_PY" -c 'import i2rt' 2>/dev/null \
        && say "I2RT SDK installed at the pinned ref: nothing to do" \
        || say "would: uv pip install --build-constraint (scikit-build-core<0.10) -e $I2RT_DIR"
    else
      say "would: git clone $I2RT_REPO -> $I2RT_DIR, checkout $I2RT_REF, uv pip install --build-constraint (scikit-build-core<0.10) -e it"
    fi
  else
    if [ ! -d "$I2RT_DIR/.git" ]; then
      git clone --quiet "$I2RT_REPO" "$I2RT_DIR" || warn "I2RT clone failed (no GitHub access from this network?)"
    fi
    if [ -d "$I2RT_DIR/.git" ]; then
      want="$(i2rt_resolve "$I2RT_REF")"
      if [ -z "$want" ]; then git -C "$I2RT_DIR" fetch --quiet --tags origin || warn "git fetch of the I2RT repo failed"; want="$(i2rt_resolve "$I2RT_REF")"; fi
      if [ -z "$want" ]; then
        warn "I2RT ref '$I2RT_REF' not found in $I2RT_REPO; leaving the checkout at $(git -C "$I2RT_DIR" rev-parse --short HEAD)"
        want="$(git -C "$I2RT_DIR" rev-parse HEAD)"
      fi
      [ "$(git -C "$I2RT_DIR" rev-parse HEAD)" = "$want" ] || git -C "$I2RT_DIR" checkout --quiet --detach "$want"
      printf 'scikit-build-core<0.10\n' >"$I2RT_CONSTRAINTS"
      if [ "$(cat "$I2RT_STAMP" 2>/dev/null || true)" = "$want" ] && "$VENV_PY" -c 'import i2rt' 2>/dev/null; then
        say "I2RT SDK present at ${want:0:7} (pinned $I2RT_REF)"
      else
        say "installing the I2RT SDK at ${want:0:7} (builds ruckig from source: a few minutes on a Jetson)"
        if (cd "$RC" && uv pip install --quiet --python "$VENV_PY" --build-constraint "$I2RT_CONSTRAINTS" -e "$I2RT_DIR"); then
          printf '%s\n' "$want" >"$I2RT_STAMP"
          say "I2RT SDK installed (${want:0:7})"
        else
          rm -f "$I2RT_STAMP"
          warn "I2RT SDK install failed; the yam adapter will not load (see output above)"
        fi
      fi
      "$VENV_PY" -c 'import i2rt, mink, mujoco' 2>/dev/null && say "SDK import check ok: import i2rt, mink, mujoco" \
        || warn "SDK import check failed: import i2rt, mink, mujoco"
    fi
  fi
else say "I2RT SDK skipped (--no-i2rt)"; fi

# ---------- 3. import checks ----------
if [ -x "$VENV_PY" ]; then
  "$VENV_PY" - <<'PY' || { echo "core imports failed: the venv is broken" >&2; [ "$DRY_RUN" = 1 ] || exit 4; }
import importlib, sys
mods = ["rdf_robot_client", "rdf_robot_client.main", "rdf_common.spacetime_client", "rdf_common.settings",
        "numpy", "cv2", "yaml", "httpx", "websockets", "pyarrow"]
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append(f"{m}: {e}")
import cv2
print(f"  [deploy] imports ok: {len(mods) - len(bad)}/{len(mods)} (opencv {cv2.__version__}, gstreamer="
      f"{'YES' if 'GStreamer:                   YES' in cv2.getBuildInformation() else 'no (pip wheel)'})")
if bad:
    print("\n".join("  [deploy] FAIL " + b for b in bad)); sys.exit(1)
PY
  YAM_READY=1; YAM_WHY=""
  for m in rdf_robot_client.adapters.yam rdf_robot_client.safety i2rt; do
    "$VENV_PY" -c "import $m" 2>/dev/null || { YAM_READY=0; YAM_WHY="$YAM_WHY $m"; }
  done
else
  YAM_READY=0; YAM_WHY=" (no venv yet)"
fi
[ "$YAM_READY" = 1 ] && say "YAM stack importable (adapter TDM-46, safety TDM-45, I2RT SDK)" \
  || say "YAM stack not ready, missing:$YAM_WHY"

# ---------- 4. service mode ----------
prev_mode="$(sed -n 's/^RDF_SERVICE_MODE=//p' "$MODE_FILE" 2>/dev/null || true)"
want_mode="${MODE:-${prev_mode:-off}}"
resolved="$want_mode"
if [ "$want_mode" = auto ]; then resolved=$([ "$YAM_READY" = 1 ] && echo yam || echo off); fi
if [ "$resolved" = yam ] && [ "$YAM_READY" = 0 ]; then
  warn "mode yam requested but the YAM stack does not import; leaving the service off"; resolved=off
fi
args=""
case "$resolved" in
  sim) args="--sim --name test.jetson-sim-$(hostname -s) --cameras off" ;;
  yam) args="" ;;
esac
MODE_CONTENT="# written by deploy.sh (TDM-44); change with: make jetson-deploy JETSON_SERVICE_MODE=auto|yam|sim|off
RDF_SERVICE_MODE=$want_mode
RDF_SERVICE_RESOLVED=$resolved
RDF_ROBOT_CLIENT_ARGS=$args"
say "service mode: $want_mode -> $resolved${args:+ (args: $args)}"
if [ "$resolved" = off ]; then
  say "  service off. After the SDK-alone bring-up (yam-sdk-notes.md §8): make jetson-deploy JETSON_SERVICE_MODE=yam"
  say "  no-arm smoke test (service + heartbeat as test.jetson-sim-<host>): make jetson-deploy JETSON_SERVICE_MODE=sim"
fi

# ---------- 5. restart only if something changed ----------
state="$(
  { find "$APP_DIR" -path "$RC/.venv" -prune -o -type f ! -name DEPLOYED_REV ! -path '*/results/jetson-*' ! -path '*/__pycache__/*' -print0 \
      | sort -z | xargs -0 sha256sum
    find "$RC/.venv/lib/python3.11/site-packages" -maxdepth 1 -printf '%f %T@\n' 2>/dev/null | sort | md5sum
    cat /etc/rdf/robot-client.env /etc/rdf/robot-client.local.env 2>/dev/null | grep -v '^#' | sha256sum
    printf '%s\n' "$MODE_CONTENT" | grep -v '^#'
  } | sha256sum | cut -d' ' -f1)"
prev_state="$(cat "$STATE_FILE" 2>/dev/null || true)"

if [ "$DRY_RUN" = 1 ]; then
  [ "$state" = "$prev_state" ] && say "would: nothing changed since last deploy (no restart)" || say "would: write $MODE_FILE and restart robot-client if mode != off"
  exit 0
fi

if [ "$(cat "$MODE_FILE" 2>/dev/null || true)" != "$MODE_CONTENT" ]; then
  [ -w /etc/rdf ] || { echo "/etc/rdf not writable: run make jetson-bootstrap first" >&2; exit 3; }
  t="$(mktemp /etc/rdf/.mode.XXXXXX)"; printf '%s\n' "$MODE_CONTENT" >"$t"; chmod 640 "$t"; mv -f "$t" "$MODE_FILE"
fi

if ! have_systemd; then
  say "systemd not running (container stand-in): skipping service start"
  printf '%s\n' "$state" >"$STATE_FILE"
  exit 0
fi
[ -f /etc/rdf/robot-client.env ] || warn "/etc/rdf/robot-client.env missing: run make jetson-env (the service cannot start without it)"

if [ "$resolved" = off ]; then
  if [ "$(systemctl is-enabled robot-client 2>/dev/null || true)" = enabled ] || systemctl is-active --quiet robot-client; then
    sctl disable --now robot-client && say "robot-client stopped and disabled"
  else say "robot-client is off"; fi
  printf '%s\n' "$state" >"$STATE_FILE"
  exit 0
fi

[ "$(systemctl is-enabled robot-client 2>/dev/null || true)" = enabled ] || { sctl enable robot-client; say "robot-client enabled (starts at boot)"; }
if [ "$FORCE_RESTART" = 1 ] || [ "$state" != "$prev_state" ] || ! systemctl is-active --quiet robot-client; then
  say "restarting robot-client"
  sctl restart robot-client
  sleep 6
  if systemctl is-active --quiet robot-client; then say "robot-client is active"
  else warn "robot-client is NOT active after restart"; fi
  journalctl -u robot-client -n 20 --no-pager -o cat 2>/dev/null | sed 's/^/    | /' || true
else
  say "nothing changed since the last deploy; robot-client left running (use --restart to force)"
fi
printf '%s\n' "$state" >"$STATE_FILE"
