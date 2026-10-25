#!/usr/bin/env bash
# TDM-118: install / inspect the GeonosisAI read-only robot client on the northstar Jetson, WITHOUT sudo,
# next to the user's vrteleop (never touched). Everything lives in ~/geonosis/app, ~/.config/geonosis,
# ~/.config/systemd/user/geonosis-robot.service and ~/.local/bin/cloudflared on the Jetson.
#
#   scripts/jetson/tdm118_geonosis.sh install     # sync HEAD bundle, push env + local env, venv, cloudflared, user unit
#   scripts/jetson/tdm118_geonosis.sh env         # re-push ~/.config/geonosis/robot.env (key names printed only)
#   scripts/jetson/tdm118_geonosis.sh status      # unit state, vrteleop state, CAN links, disk
#   scripts/jetson/tdm118_geonosis.sh logs        # journalctl --user -u geonosis-robot (FOLLOW=0 to print and exit)
#   scripts/jetson/tdm118_geonosis.sh check       # offline config check on the Jetson (opens no CAN, no network)
#
# It never starts the unit: the user does that after stopping vrteleop (docs/jetson-inventory.md section 8).
# Host: JETSON_HOST / JETSON_USER from .env (northstar / northstar). Secrets travel over ssh stdin into a 0600 file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${RDF_DOTENV:-$REPO/.env}"
BUNDLE_PATHS=(services/robot-client services/common scripts/jetson)
val() { [ -f "$ENV_FILE" ] || return 0; grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
JETSON_HOST="${JETSON_HOST:-$(val JETSON_HOST)}"; JETSON_USER="${JETSON_USER:-$(val JETSON_USER)}"
[ -n "$JETSON_HOST" ] || { echo "JETSON_HOST not set (.env)" >&2; exit 2; }
TARGET="${JETSON_USER:+$JETSON_USER@}$JETSON_HOST"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$TARGET")
step() { printf '==> %s\n' "$*"; }

push_env() {
  step "push ~/.config/geonosis/robot.env (names only)"
  local content="# GeonosisAI robot env (TDM-118), pushed from $(id -un)@$(hostname -s) $(date -u +%FT%TZ)"$'\n' k src v
  # KEY:SOURCES (first non-empty wins)
  for spec in "SPACETIME_HOST:SPACETIME_HOST" "SPACETIME_DB_NAME:SPACETIME_DB_NAME" \
              "SPACETIME_TOKEN:JETSON_ROBOT_CLIENT_SPACETIME_TOKEN" "ROBOT_ID:JETSON_ROBOT_ID" \
              "ORCHESTRATOR_URL:JETSON_ORCHESTRATOR_URL" "RDF_STREAM_TOKEN_SECRET:RDF_STREAM_TOKEN_SECRET" \
              "RDF_MEDIA_STORE_URL:RDF_MEDIA_STORE_URL"; do
    k="${spec%%:*}"; v=""
    IFS='|' read -r -a srcs <<<"${spec#*:}"
    for src in "${srcs[@]}"; do v="$(val "$src")"; [ -n "$v" ] && break; done
    [ "$k" = ROBOT_ID ] && v="${v:-yam-01}"
    if [ -z "$v" ]; then
      case "$k" in SPACETIME_*|RDF_STREAM_TOKEN_SECRET) echo "MISSING $k (from ${spec#*:})" >&2; exit 3 ;; esac
      echo "    unset $k (optional)"; continue
    fi
    case "$v" in *$'\n'*) echo "$k has a newline" >&2; exit 3 ;; esac
    echo "    set   $k <- ${spec#*:}"
    content+="$k=$v"$'\n'
  done
  printf '%s' "$content" | "${SSH[@]}" 'set -e; d=$HOME/.config/geonosis; mkdir -p "$d"; chmod 700 "$d"
    t=$(mktemp "$d/.robot.env.XXXXXX"); cat >"$t"; chmod 600 "$t"; mv -f "$t" "$d/robot.env"'
}

cmd_install() {
  step "bundle committed HEAD ($(git -C "$REPO" rev-parse --short=12 HEAD)) -> $TARGET:~/geonosis/app"
  local b; b="$(mktemp -d "${TMPDIR:-/tmp}/tdm118-bundle.XXXXXX")"; trap 'rm -rf "$b"' RETURN
  git -C "$REPO" archive --format=tar HEAD -- "${BUNDLE_PATHS[@]}" | tar -x -C "$b"
  printf 'rev=%s\nsrc=head\nbuilt_at=%s\nticket=TDM-118\n' "$(git -C "$REPO" rev-parse --short=12 HEAD)" "$(date -u +%FT%TZ)" >"$b/DEPLOYED_REV"
  "${SSH[@]}" 'mkdir -p $HOME/geonosis/app'
  rsync -az --delete --exclude .venv/ --exclude __pycache__/ -e "ssh -o BatchMode=yes" "$b/" "$TARGET:geonosis/app/"
  push_env
  step "push ~/.config/geonosis/robot.local.env (non-secret, from services/robot-client/config/tdm118_northstar.env)"
  "${SSH[@]}" 'set -e; d=$HOME/.config/geonosis; mkdir -p "$d"; chmod 700 "$d"; cat >"$d/robot.local.env"; chmod 600 "$d/robot.local.env"' \
    <"$REPO/services/robot-client/config/tdm118_northstar.env"
  step "user-level setup on the Jetson (no sudo)"
  "${SSH[@]}" 'bash $HOME/geonosis/app/scripts/jetson/remote/tdm118_user_setup.sh --cloudflared'
}

cmd_status() {
  "${SSH[@]}" 'echo "== bundle"; cat $HOME/geonosis/app/DEPLOYED_REV 2>/dev/null || echo none
    echo "== units"; printf "geonosis-robot (user) enabled=%s active=%s\n" "$(systemctl --user is-enabled geonosis-robot 2>/dev/null)" "$(systemctl --user is-active geonosis-robot 2>/dev/null)"
    printf "vrteleop-rh (system)  active=%s\n" "$(systemctl is-active vrteleop-rh 2>/dev/null)"
    echo "== CAN"; ip -br link show type can; echo "== disk"; df -h / | tail -1'
}

case "${1:-}" in
  install) cmd_install ;;
  env) push_env ;;
  status) cmd_status ;;
  logs) if [ "${FOLLOW:-1}" = 0 ]; then "${SSH[@]}" "journalctl --user -u geonosis-robot -n ${LINES:-200} --no-pager -o short-iso"
        else ssh -t -o ConnectTimeout=15 "$TARGET" "journalctl --user -u geonosis-robot -n ${LINES:-100} -f -o short-iso"; fi ;;
  check) "${SSH[@]}" 'set -a; . $HOME/.config/geonosis/robot.local.env; set +a; cd $HOME/geonosis/app/services/robot-client && .venv/bin/python -m rdf_robot_client.tdm118_readonly --check' ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
