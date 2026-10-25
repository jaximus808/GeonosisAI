#!/usr/bin/env bash
# TDM-118: install / inspect the GeonosisAI read-only robot client on the northstar Jetson, WITHOUT sudo,
# next to the user's vrteleop (never touched). Everything lives in ~/geonosis/app, ~/.config/geonosis,
# ~/.config/systemd/user/geonosis-robot.service and ~/.local/bin/cloudflared on the Jetson.
#
#   scripts/jetson/tdm118_geonosis.sh install     # sync HEAD bundle, push env + local env, venv, cloudflared, user unit
#   scripts/jetson/tdm118_geonosis.sh env         # re-push ~/.config/geonosis/robot.env (key names printed only)
#   scripts/jetson/tdm118_geonosis.sh sync        # code only: HEAD bundle -> ~/geonosis/app (no venv/unit changes);
#                                                 # refuses while geonosis-robot-teleop runs (FORCE=1 overrides)
#   scripts/jetson/tdm118_geonosis.sh status      # unit state, vrteleop state, CAN links, disk
#   scripts/jetson/tdm118_geonosis.sh logs        # journalctl --user -u geonosis-robot (FOLLOW=0 to print and exit)
#   scripts/jetson/tdm118_geonosis.sh check       # offline config check on the Jetson (opens no CAN, no network)
#   scripts/jetson/tdm118_geonosis.sh policy on   # TDM-126: TDM119_ALLOW_POLICY=1 in ~/.config/geonosis/teleop.env
#   scripts/jetson/tdm118_geonosis.sh policy off  #   (=0); takes effect at the next start of geonosis-robot-teleop
#
# It never starts the unit: the user does that after stopping vrteleop (docs/jetson-inventory.md section 8).
# Host: JETSON_HOST / JETSON_USER from .env (northstar / northstar). Secrets travel over ssh stdin into a 0600 file.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${RDF_DOTENV:-$REPO/.env}"
BUNDLE_PATHS=(services/robot-client services/common scripts/jetson)
val() { [ -f "$ENV_FILE" ] || return 0; { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }
JETSON_HOST="${JETSON_HOST:-$(val JETSON_HOST)}"; JETSON_USER="${JETSON_USER:-$(val JETSON_USER)}"
[ -n "$JETSON_HOST" ] || { echo "JETSON_HOST not set (.env)" >&2; exit 2; }
TARGET="${JETSON_USER:+$JETSON_USER@}$JETSON_HOST"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$TARGET")
step() { printf '==> %s\n' "$*"; }

push_env() {
  step "push ~/.config/geonosis/robot.env (names only)"
  local content="# GeonosisAI robot env (TDM-118), pushed from $(id -un)@$(hostname -s) $(date -u +%FT%TZ)"$'\n' k src v pushed=""
  # KEY:SOURCES (first non-empty wins)
  for spec in "SPACETIME_HOST:SPACETIME_HOST" "SPACETIME_DB_NAME:SPACETIME_DB_NAME" \
              "SPACETIME_TOKEN:JETSON_ROBOT_CLIENT_SPACETIME_TOKEN" "ROBOT_ID:JETSON_ROBOT_ID" \
              "ORCHESTRATOR_URL:JETSON_ORCHESTRATOR_URL" "RDF_STREAM_TOKEN_SECRET:RDF_STREAM_TOKEN_SECRET" \
              "RDF_MEDIA_STORE_URL:RDF_MEDIA_STORE_URL" "RDF_EDGE_SECRET:RDF_EDGE_SECRET" \
              "POLICY_SERVER_URL:JETSON_POLICY_SERVER_URL|POLICY_SERVER_URL" "POLICY_SERVER_TOKEN:POLICY_SERVER_TOKEN" \
              "POLICY_BACKEND:JETSON_POLICY_BACKEND"; do
    k="${spec%%:*}"; v=""
    IFS='|' read -r -a srcs <<<"${spec#*:}"
    for src in "${srcs[@]}"; do v="$(val "$src")"; [ -n "$v" ] && break; done
    [ "$k" = ROBOT_ID ] && v="${v:-yam-01}"
    # TDM-126: the Jetson always talks to the production policy server (never the laptop's sim/spike backend)
    if [ "$k" = POLICY_BACKEND ] && [ -z "$v" ] && [ -n "$(val POLICY_SERVER_TOKEN)" ]; then v=production; fi
    if [ -z "$v" ]; then
      case "$k" in SPACETIME_*|RDF_STREAM_TOKEN_SECRET) echo "MISSING $k (from ${spec#*:})" >&2; exit 3 ;; esac
      echo "    unset $k (optional)"; continue
    fi
    case "$v" in *$'\n'*) echo "$k has a newline" >&2; exit 3 ;; esac
    echo "    set   $k <- ${spec#*:}$([ "$k" = POLICY_BACKEND ] && [ -z "$(val JETSON_POLICY_BACKEND)" ] && echo ' (default: production, POLICY_SERVER_TOKEN is set)')"
    content+="$k=$v"$'\n'
    pushed+=" $k"
  done
  # recording uploads: RDF_EDGE_SECRET lets the robot discover make demo-hybrid's tunnel URL (orch_endpoint.py);
  # an explicit ORCHESTRATOR_URL turns discovery off
  case "$pushed" in
    *" ORCHESTRATOR_URL"*) echo "    note  ORCHESTRATOR_URL is set: it overrides discovery (unset JETSON_ORCHESTRATOR_URL to discover)" ;;
    *" RDF_EDGE_SECRET"*) echo "    note  uploads find the orchestrator by discovery (event_log orchestrator.endpoint + edge proof)" ;;
    *) echo "    WARN  neither ORCHESTRATOR_URL nor RDF_EDGE_SECRET: recordings will not upload" >&2 ;;
  esac
  printf '%s' "$content" | "${SSH[@]}" 'set -e; d=$HOME/.config/geonosis; mkdir -p "$d"; chmod 700 "$d"
    t=$(mktemp "$d/.robot.env.XXXXXX"); cat >"$t"; chmod 600 "$t"; mv -f "$t" "$d/robot.env"'
}

sync_bundle() {
  step "bundle committed HEAD ($(git -C "$REPO" rev-parse --short=12 HEAD)) -> $TARGET:~/geonosis/app"
  local b; b="$(mktemp -d "${TMPDIR:-/tmp}/tdm118-bundle.XXXXXX")"; trap 'rm -rf "$b"' RETURN
  git -C "$REPO" archive --format=tar HEAD -- "${BUNDLE_PATHS[@]}" | tar -x -C "$b"
  printf 'rev=%s\nsrc=head\nbuilt_at=%s\nticket=TDM-118\n' "$(git -C "$REPO" rev-parse --short=12 HEAD)" "$(date -u +%FT%TZ)" >"$b/DEPLOYED_REV"
  "${SSH[@]}" 'mkdir -p $HOME/geonosis/app'
  rsync -az --delete --exclude .venv/ --exclude __pycache__/ -e "ssh -o BatchMode=yes" "$b/" "$TARGET:geonosis/app/"
}

cmd_sync() {
  # never swap code under a running motion client (it imports modules lazily); stop it first (runbook step 6)
  local active; active="$("${SSH[@]}" 'systemctl --user is-active geonosis-robot-teleop 2>/dev/null || true')"
  if [ "$active" = active ] && [ "${FORCE:-0}" != 1 ]; then
    echo "geonosis-robot-teleop is running: stop it first (docs/tdm119-teleop-runbook.md step 6), then sync" >&2; exit 4
  fi
  sync_bundle
}

cmd_install() {
  sync_bundle
  push_env
  step "push ~/.config/geonosis/robot.local.env (non-secret, from services/robot-client/config/tdm118_northstar.env)"
  "${SSH[@]}" 'set -e; d=$HOME/.config/geonosis; mkdir -p "$d"; chmod 700 "$d"; cat >"$d/robot.local.env"; chmod 600 "$d/robot.local.env"' \
    <"$REPO/services/robot-client/config/tdm118_northstar.env"
  step "push ~/.config/geonosis/teleop.env (non-secret, from services/robot-client/config/tdm119_teleop.env)"
  "${SSH[@]}" 'set -e; d=$HOME/.config/geonosis; cat >"$d/teleop.env"; chmod 600 "$d/teleop.env"' \
    <"$REPO/services/robot-client/config/tdm119_teleop.env"
  step "user-level setup on the Jetson (no sudo)"
  # LARP's i2rt fork (docs/real-arm-safety.md): copied from the dist vrteleop_env_rh was installed from
  "${SSH[@]}" "bash \$HOME/geonosis/app/scripts/jetson/remote/tdm118_user_setup.sh --cloudflared --i2rt-larp ${I2RT_LARP_SRC:-/home/northstar/arc-dist-0.3.2/arc-dist/i2rt-minimal}"
}

cmd_status() {
  "${SSH[@]}" 'echo "== bundle"; cat $HOME/geonosis/app/DEPLOYED_REV 2>/dev/null || echo none
    echo "== units"; printf "geonosis-robot (user) enabled=%s active=%s\n" "$(systemctl --user is-enabled geonosis-robot 2>/dev/null)" "$(systemctl --user is-active geonosis-robot 2>/dev/null)"
    printf "vrteleop-rh (system)  active=%s\n" "$(systemctl is-active vrteleop-rh 2>/dev/null)"
    echo "== CAN"; ip -br link show type can; echo "== disk"; df -h / | tail -1'
}

cmd_policy() {
  local want
  case "${1:-}" in on) want=1 ;; off) want=0 ;; *) echo "usage: $0 policy on|off" >&2; exit 2 ;; esac
  step "TDM119_ALLOW_POLICY=$want in ~/.config/geonosis/teleop.env (non-secret)"
  "${SSH[@]}" "set -e; f=\$HOME/.config/geonosis/teleop.env; test -f \"\$f\" || { echo 'no teleop.env: run install first' >&2; exit 3; }
    if grep -q '^TDM119_ALLOW_POLICY=' \"\$f\"; then t=\$(mktemp \"\$f.XXXXXX\"); sed 's/^TDM119_ALLOW_POLICY=.*/TDM119_ALLOW_POLICY=$want/' \"\$f\" >\"\$t\"; chmod 600 \"\$t\"; mv -f \"\$t\" \"\$f\"
    else echo 'TDM119_ALLOW_POLICY=$want' >>\"\$f\"; fi
    grep '^TDM119_ALLOW_POLICY=' \"\$f\"; printf 'geonosis-robot-teleop: %s (restart it for the change to apply)\\n' \"\$(systemctl --user is-active geonosis-robot-teleop 2>/dev/null || true)\""
}

case "${1:-}" in
  install) cmd_install ;;
  policy) cmd_policy "${2:-}" ;;
  sync) cmd_sync ;;
  env) push_env ;;
  status) cmd_status ;;
  logs) if [ "${FOLLOW:-1}" = 0 ]; then "${SSH[@]}" "journalctl --user -u geonosis-robot -n ${LINES:-200} --no-pager -o short-iso"
        else ssh -t -o ConnectTimeout=15 "$TARGET" "journalctl --user -u geonosis-robot -n ${LINES:-100} -f -o short-iso"; fi ;;
  check) "${SSH[@]}" 'set -a; . $HOME/.config/geonosis/robot.local.env; set +a; cd $HOME/geonosis/app/services/robot-client && .venv/bin/python -m rdf_robot_client.tdm118_readonly --check' ;;
  *) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
