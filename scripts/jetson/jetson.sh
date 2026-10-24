#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2015  # remote snippets are single-quoted on purpose; A && B || C is used for messages
# TDM-44: laptop-side driver for the Jetson that runs services/robot-client (YAM arm + cameras).
#
#   make jetson-bootstrap JETSON_HOST=10.0.0.42 [JETSON_USER=nvidia] [DRY_RUN=1]
#   make jetson-deploy | jetson-env | jetson-check | jetson-status | jetson-logs
#
# or directly:  scripts/jetson/jetson.sh <command> [--dry-run] [-- extra args]
#
# Commands
#   bootstrap  preflight -> sync bundle -> remote/bootstrap.sh (sudo) -> env -> deploy -> check
#   deploy     sync bundle (code update) -> remote/deploy.sh (uv sync, I2RT, restart service)
#   env        push only the robot client's keys from .env to /etc/rdf/robot-client.env (no values printed)
#   check      connectivity check on the Jetson (SpacetimeDB, policy latency, media upload, orchestrator)
#   status     systemd state, deployed revision, devices, CAN links
#   logs       journalctl -u robot-client (FOLLOW=0 to print and exit; LINES=200)
#   ssh        interactive shell on the Jetson
#   plan       same as `bootstrap --dry-run`
#   mint-identity  create a robot_client SpacetimeDB identity, allow-list it, store its token in .env
#                  as ROBOT_CLIENT_SPACETIME_TOKEN (writes to the database: needs --yes)
#
# Config (env or make vars; JETSON_HOST/USER/PORT also read from .env):
#   JETSON_HOST, JETSON_USER (default: your ssh config / local user), JETSON_PORT (22),
#   JETSON_SSH_KEY (optional -i), JETSON_DIR (remote dir under $HOME, default rdf),
#   JETSON_SRC=head|worktree  what to ship: committed HEAD (default) or the working tree,
#   JETSON_SERVICE_MODE=auto|yam|sim|off  service mode passed to deploy (default: keep the Jetson's),
#   JETSON_REQUIRE_SERVICE_TOKEN=1  refuse to push the owner SPACETIME_TOKEN as a fallback.
#
# Idempotent: every step checks state first; re-running bootstrap is safe. --dry-run changes
# nothing anywhere: without JETSON_HOST it is a fully offline plan, with it every remote step runs
# in probe mode (rsync -n, scripts with --dry-run, env hash compare).
# Secrets: values from .env are never printed, logged or put on a command line; they travel over
# ssh stdin straight into a 0600 file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${RDF_DOTENV:-$REPO/.env}"
ENV_EXAMPLE="$REPO/.env.example"

# Shipped to the Jetson: only what the robot client needs.
BUNDLE_PATHS=(services/robot-client services/common scripts/jetson spikes/tdm-37-latency/bench_client.py)
BUNDLE_EXCLUDES=(.venv __pycache__ .pytest_cache .ruff_cache .mypy_cache tmp node_modules '*.pyc' .DS_Store)

DRY_RUN=0
CMD=""
EXTRA=()
DEPLOY_ARGS=()

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
[ -t 1 ] || { c_red=""; c_grn=""; c_yel=""; c_dim=""; c_off=""; }
step() { printf '%s==>%s %s\n' "$c_grn" "$c_off" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%sWARN%s %s\n' "$c_yel" "$c_off" "$*" >&2; }
die()  { printf '%sERROR%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
dry()  { printf '    %s[dry-run]%s %s\n' "$c_dim" "$c_off" "$*"; }

usage() { sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ---------- .env access (never prints values) ----------

# dotenv_raw FILE KEY -> the last KEY=... value in FILE, surrounding quotes stripped ('' if absent)
dotenv_raw() {
  local file="$1" key="$2" v
  [ -f "$file" ] || return 0
  v="$(grep -E "^(export[[:space:]]+)?${key}=" "$file" | tail -n1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"
  if [[ "$v" == \"*\" && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; elif [[ "$v" == \'*\' && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; fi
  printf '%s' "$v"
}
# dotenv_get KEY: exported env wins, then .env
dotenv_get() {
  local key="$1"
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; else dotenv_raw "$ENV_FILE" "$key"; fi
}

# ---------- config ----------

load_config() {
  JETSON_HOST="${JETSON_HOST:-$(dotenv_raw "$ENV_FILE" JETSON_HOST)}"
  JETSON_USER="${JETSON_USER:-$(dotenv_raw "$ENV_FILE" JETSON_USER)}"
  JETSON_PORT="${JETSON_PORT:-$(dotenv_raw "$ENV_FILE" JETSON_PORT)}"
  JETSON_PORT="${JETSON_PORT:-22}"
  JETSON_DIR="${JETSON_DIR:-rdf}"
  JETSON_SRC="${JETSON_SRC:-head}"
  JETSON_SERVICE_MODE="${JETSON_SERVICE_MODE:-}"
  case "$JETSON_SRC" in head|worktree) ;; *) die "JETSON_SRC must be head or worktree";; esac
  case "$JETSON_SERVICE_MODE" in ''|auto|yam|sim|off) ;; *) die "JETSON_SERVICE_MODE must be auto|yam|sim|off";; esac
  TARGET="${JETSON_USER:+$JETSON_USER@}${JETSON_HOST}"
  SSH_OPTS=(-p "$JETSON_PORT" -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4
            -o StrictHostKeyChecking=accept-new
            -o ControlMaster=auto -o "ControlPath=/tmp/rdf-ssh-%C" -o ControlPersist=120)
  [ -n "${JETSON_SSH_KEY:-}" ] && SSH_OPTS+=(-i "$JETSON_SSH_KEY")
  [ -n "${JETSON_SSH_OPTS:-}" ] && read -r -a _extra <<<"$JETSON_SSH_OPTS" && SSH_OPTS+=("${_extra[@]}")
  return 0
}

have_host() { [ -n "$JETSON_HOST" ]; }
need_host() {
  have_host && return 0
  die "JETSON_HOST is not set. Pass it (make $1 JETSON_HOST=<ip> JETSON_USER=<user>) or add it to .env.
      Passwordless ssh must work first: ssh-copy-id <user>@<ip>   (docs/morning-questions.md #1)"
}

# rssh CMD...: non-interactive ssh (keys only, fails fast instead of prompting)
rssh()  { ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" "$@"; }
# rssh_tty: interactive (sudo may prompt for a password) when we have a terminal
rssh_tty() {
  if [ -t 0 ] && [ -t 1 ]; then ssh "${SSH_OPTS[@]}" -t "$TARGET" "$@"; else rssh "$@"; fi
}
rsh_quote() { [ $# -eq 0 ] || printf '%q ' "$@"; }

REMOTE_HOME=""
remote_paths() {
  [ -n "$REMOTE_HOME" ] && return 0
  REMOTE_HOME="$(rssh 'printf %s "$HOME"')" || die "ssh $TARGET failed (is passwordless ssh set up? ssh-copy-id $TARGET)"
  REMOTE_ROOT="$REMOTE_HOME/$JETSON_DIR"
  REMOTE_APP="$REMOTE_ROOT/app"
  REMOTE_USER="$(rssh 'id -un')"
}

# ---------- preflight ----------

cmd_preflight() {
  step "preflight: ssh $TARGET (port $JETSON_PORT)"
  local out
  out="$(rssh 'uname -srm; cat /etc/nv_tegra_release 2>/dev/null | head -n1 || true; . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"; command -v python3 >/dev/null && python3 -V 2>&1 || echo "python3: missing"; sudo -n true 2>/dev/null && echo "sudo: passwordless" || echo "sudo: needs password"')" \
    || die "cannot ssh to $TARGET non-interactively. Fix with: ssh-copy-id ${SSH_OPTS[*]:0:2} $TARGET"
  while IFS= read -r l; do info "$l"; done <<<"$out"
  remote_paths
  info "remote user $REMOTE_USER, app dir $REMOTE_APP"
  if grep -q 'sudo: needs password' <<<"$out" && ! { [ -t 0 ] && [ -t 1 ]; }; then
    warn "sudo needs a password and there is no terminal: run from an interactive shell, or add NOPASSWD for $REMOTE_USER"
  fi
}

# ---------- bundle ----------

BUNDLE_DIR=""
cleanup() { [ -n "$BUNDLE_DIR" ] && rm -rf "$BUNDLE_DIR"; return 0; }
trap cleanup EXIT

build_bundle() {
  BUNDLE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tdm-44-bundle.XXXXXX")"
  local rev dirty
  rev="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  if [ "$JETSON_SRC" = head ]; then
    for p in "${BUNDLE_PATHS[@]}"; do
      git -C "$REPO" cat-file -e "HEAD:$p" 2>/dev/null || die "$p is not committed at HEAD (commit it, or JETSON_SRC=worktree)"
    done
    git -C "$REPO" archive --format=tar HEAD -- "${BUNDLE_PATHS[@]}" | tar -x -C "$BUNDLE_DIR"
    dirty="no (HEAD only; uncommitted changes are NOT shipped)"
  else
    local ex=() p
    for p in "${BUNDLE_EXCLUDES[@]}"; do ex+=(--exclude "$p"); done
    (cd "$REPO" && rsync -a --relative "${ex[@]}" "${BUNDLE_PATHS[@]}" "$BUNDLE_DIR/")
    if [ -n "$(git -C "$REPO" status --porcelain -- "${BUNDLE_PATHS[@]}" 2>/dev/null)" ]; then
      dirty="yes (working tree with uncommitted changes)"
    else
      dirty="no (working tree == HEAD)"
    fi
  fi
  {
    echo "rev=$rev"
    echo "src=$JETSON_SRC"
    echo "dirty=$dirty"
    echo "built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "built_by=$(id -un)@$(hostname -s 2>/dev/null || hostname)"
  } >"$BUNDLE_DIR/DEPLOYED_REV"
  BUNDLE_FILES="$(find "$BUNDLE_DIR" -type f | wc -l | tr -d ' ')"
  info "bundle: $BUNDLE_FILES files from $JETSON_SRC @ $rev (dirty: $dirty)"
}

cmd_sync() {
  step "sync bundle -> $TARGET:$REMOTE_APP"
  build_bundle
  local rsh flags=(-az --delete --itemize-changes --exclude .venv/ --exclude __pycache__/
                   --filter 'P spikes/tdm-37-latency/results/jetson-*')
  rsh="ssh $(rsh_quote "${SSH_OPTS[@]}" -o BatchMode=yes)"
  if ! rssh 'command -v rsync >/dev/null'; then
    if [ "$DRY_RUN" = 1 ]; then dry "rsync is not installed on the Jetson: would apt-get install rsync first"; return 0; fi
    info "rsync missing on the Jetson: installing it (sudo apt-get install rsync)"
    rssh_tty 'sudo env DEBIAN_FRONTEND=noninteractive sh -c "apt-get update -qq && apt-get install -y -qq rsync >/dev/null"' \
      || die "could not install rsync on the Jetson (no network/apt?). Install it by hand: sudo apt install rsync"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    dry "rsync -n (no files written):"
    rsync "${flags[@]}" -n -e "$rsh" "$BUNDLE_DIR/" "$TARGET:$REMOTE_APP/" 2>/dev/null | sed 's/^/      /' | head -n 40 \
      || dry "remote app dir does not exist yet; everything would be copied"
    return 0
  fi
  rssh "mkdir -p $(printf %q "$REMOTE_APP")"
  local out changed
  out="$(rsync "${flags[@]}" -e "$rsh" "$BUNDLE_DIR/" "$TARGET:$REMOTE_APP/")" || die "rsync to $TARGET failed"
  changed="$(printf '%s\n' "$out" | grep -c -v -E '^(\.|$)' || true)"
  info "rsync done ($changed changed entries)"
}

# ---------- remote scripts ----------

# run_remote_script NAME SUDO(0|1) ARGS...: runs scripts/jetson/remote/NAME on the Jetson.
# Real runs use the synced copy; dry runs pipe the local copy over stdin (nothing written remotely).
run_remote_script() {
  local name="$1" use_sudo="$2"; shift 2
  local args=(--user "$REMOTE_USER" --app-dir "$REMOTE_APP" "$@")
  if [ "$DRY_RUN" = 1 ]; then
    rssh "bash -s -- $(rsh_quote "${args[@]}" --dry-run)" <"$HERE/remote/$name"
    return
  fi
  local cmd
  cmd="bash $(printf %q "$REMOTE_APP/scripts/jetson/remote/$name") $(rsh_quote "${args[@]}")"
  if [ "$use_sudo" = 1 ]; then rssh_tty "sudo $cmd"; else rssh_tty "$cmd"; fi
}

cmd_remote_bootstrap() {
  step "system setup on the Jetson (remote/bootstrap.sh, sudo)"
  local uvv
  uvv="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
  run_remote_script bootstrap.sh 1 ${uvv:+--uv-version "$uvv"}
}

cmd_remote_deploy() {
  step "deploy on the Jetson (remote/deploy.sh: uv sync, I2RT SDK, service)"
  run_remote_script deploy.sh 0 ${JETSON_SERVICE_MODE:+--mode "$JETSON_SERVICE_MODE"} ${DEPLOY_ARGS[@]+"${DEPLOY_ARGS[@]}"}
}

# ---------- env push ----------

# KEY:SOURCE[:flags]  SOURCE = .env key(s) to read, first non-empty wins ('|'-separated).
# flags: req (must be set), cfg (non-secret: falls back to .env.example), url (warn on localhost)
ENV_SPEC=(
  "SPACETIME_HOST:SPACETIME_HOST:req,cfg"
  "SPACETIME_DB_NAME:SPACETIME_DB_NAME:req,cfg"
  "SPACETIME_TOKEN:ROBOT_CLIENT_SPACETIME_TOKEN|SPACETIME_TOKEN:req"
  "ROBOT_ID:JETSON_ROBOT_ID|ROBOT_ID:cfg"
  "ORCHESTRATOR_URL:JETSON_ORCHESTRATOR_URL|ORCHESTRATOR_URL:cfg,url"
  "JETSON_WS_PORT:JETSON_WS_PORT:cfg"
  "POLICY_ENDPOINT_URL:POLICY_ENDPOINT_URL:cfg"
  "POLICY_SERVER_URL:POLICY_SERVER_URL:cfg"
  "POLICY_SERVER_TOKEN:POLICY_SERVER_TOKEN:"
  "RDF_MEDIA_STORE_URL:RDF_MEDIA_STORE_URL:cfg"
  "RDF_STREAM_TOKEN_SECRET:RDF_STREAM_TOKEN_SECRET:"
)
DEFAULT_ROBOT_ID="yam-office-1"

# Builds the env file into the variable ENV_CONTENT; prints one status line per key (names only).
build_env() {
  ENV_CONTENT="# robot-client env, pushed by scripts/jetson/jetson.sh env (TDM-44) from $(id -un)@$(hostname -s 2>/dev/null || hostname) at $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Only the keys the robot client needs. Edit on the laptop's .env and re-run make jetson-env.
"
  local spec key srcs flags src v used missing_req=0 owner_fallback=0
  for spec in "${ENV_SPEC[@]}"; do
    key="${spec%%:*}"; spec="${spec#*:}"; srcs="${spec%%:*}"; flags="${spec#*:}"
    v=""; used=""
    IFS='|' read -r -a _srcs <<<"$srcs"
    for src in "${_srcs[@]}"; do
      v="$(dotenv_get "$src")"
      if [ -n "$v" ]; then used="$src"; break; fi
    done
    if [ -z "$v" ] && [[ ",$flags," == *,cfg,* ]]; then
      v="$(dotenv_raw "$ENV_EXAMPLE" "${_srcs[${#_srcs[@]}-1]}")"; [ -n "$v" ] && used=".env.example default"
    fi
    if [ -z "$v" ] && [ "$key" = ROBOT_ID ]; then v="$DEFAULT_ROBOT_ID"; used="default"; fi
    case "$v" in *$'\n'*) die "$key: value has a newline";; esac
    if [ -z "$v" ]; then
      if [[ ",$flags," == *,req,* ]]; then printf '    %sMISSING%s  %s (required)\n' "$c_red" "$c_off" "$key"; missing_req=1
      else printf '    unset    %s (optional; code default)\n' "$key"; fi
      [ "$key" = ORCHESTRATOR_URL ] && warn "ORCHESTRATOR_URL unset: the Jetson would use http://localhost:8000 (itself). Set JETSON_ORCHESTRATOR_URL in .env to the laptop's LAN IP or tunnel URL; episode uploads need it."
      continue
    fi
    if [ "$key" = SPACETIME_TOKEN ] && [ "$used" = SPACETIME_TOKEN ]; then owner_fallback=1; fi
    if [[ ",$flags," == *,url,* ]] && [[ "$v" =~ (localhost|127\.0\.0\.1) ]]; then
      warn "$key points at localhost, which on the Jetson is the Jetson itself. Set JETSON_ORCHESTRATOR_URL in .env (laptop LAN IP or tunnel URL)."
    fi
    if [ "$key" = ROBOT_ID ] || [[ ",$flags," == *,cfg,* && "$key" != *TOKEN* ]]; then
      printf '    set      %-24s <- %s  (%s)\n' "$key" "$used" "$v"   # non-secret config: value shown
    else
      printf '    set      %-24s <- %s\n' "$key" "$used"
    fi
    ENV_CONTENT+="$key=$v"$'\n'
  done
  if [ "$owner_fallback" = 1 ]; then
    if [ "${JETSON_REQUIRE_SERVICE_TOKEN:-0}" = 1 ]; then
      die "ROBOT_CLIENT_SPACETIME_TOKEN is not set and JETSON_REQUIRE_SERVICE_TOKEN=1 (run: scripts/jetson/jetson.sh mint-identity --yes)"
    fi
    warn "SPACETIME_TOKEN on the Jetson falls back to the OWNER token. Preferred: a robot_client service identity
      (scripts/jetson/jetson.sh mint-identity --yes, then make jetson-env). See docs/pre-run-checklist.md."
  fi
  [ "$missing_req" = 0 ] || return 1
}

cmd_env() {
  step "push robot-client env -> $TARGET:/etc/rdf/robot-client.env (key names only below)"
  [ -f "$ENV_FILE" ] || die ".env not found at $ENV_FILE"
  build_env || die "required keys missing in .env (see above)"
  local local_sum remote_sum
  local_sum="$(printf '%s' "$ENV_CONTENT" | grep -v '^#' | shasum -a 256 | awk '{print $1}')"
  if ! have_host; then dry "no JETSON_HOST: would write $(grep -c -v '^#' <<<"$ENV_CONTENT") keys (offline plan)"; return 0; fi
  # compare without the header line (it carries a timestamp) so re-pushing is a no-op
  remote_sum="$(rssh 'f=/etc/rdf/robot-client.env; [ -r "$f" ] && grep -v "^#" "$f" | sha256sum | cut -d" " -f1 || echo none' 2>/dev/null || echo none)"
  if [ "$remote_sum" = "$local_sum" ]; then info "unchanged on the Jetson"; return 0; fi
  if [ "$DRY_RUN" = 1 ]; then dry "would update /etc/rdf/robot-client.env (remote differs: ${remote_sum:0:8} vs ${local_sum:0:8})"; return 0; fi
  printf '%s' "$ENV_CONTENT" | rssh 'set -e
    d=/etc/rdf; f=$d/robot-client.env
    [ -d "$d" ] && [ -w "$d" ] || { echo "/etc/rdf missing or not writable: run make jetson-bootstrap first" >&2; exit 3; }
    t=$(mktemp "$d/.robot-client.env.XXXXXX"); cat >"$t"; chmod 600 "$t"; mv -f "$t" "$f"'
  info "updated (0600, owner $REMOTE_USER). Restart to apply: make jetson-deploy (or sudo systemctl restart robot-client)"
}

# ---------- check / status / logs ----------

cmd_check() {
  step "connectivity check on the Jetson"
  if [ "$DRY_RUN" = 1 ]; then
    dry "would run: robot-client check ${EXTRA[*]:-}  (SpacetimeDB ping/SQL/WS, policy latency via bench_client.py, media healthz + signed upload, orchestrator)"
    return 0
  fi
  local rc=0
  rssh "PATH=\$HOME/.local/bin:\$PATH robot-client check $(rsh_quote ${EXTRA[@]+"${EXTRA[@]}"})" || rc=$?
  # bring the Jetson latency results home (TDM-37 asked for them)
  local rsh; rsh="ssh $(rsh_quote "${SSH_OPTS[@]}" -o BatchMode=yes)"
  rsync -az -e "$rsh" --include 'jetson-*.json' --exclude '*' \
    "$TARGET:$REMOTE_APP/spikes/tdm-37-latency/results/" "$REPO/spikes/tdm-37-latency/results/" 2>/dev/null \
    && info "latency results (if any) copied to spikes/tdm-37-latency/results/jetson-*.json" || true
  return $rc
}

cmd_status() {
  need_host jetson-status; remote_paths
  rssh "bash -s -- $(rsh_quote "$REMOTE_APP")" <<'EOS'
app="$1"
echo "== deployed bundle"; cat "$app/DEPLOYED_REV" 2>/dev/null || echo "(none: run make jetson-deploy)"
echo; echo "== mode"; cat /etc/rdf/robot-client.mode 2>/dev/null || echo "(no mode file)"
echo; echo "== systemd"
if [ -d /run/systemd/system ]; then
  for u in robot-client rdf-can; do
    printf '%-14s enabled=%-9s active=%s\n' "$u" "$(systemctl is-enabled $u 2>/dev/null || true)" "$(systemctl is-active $u 2>/dev/null || true)"
  done
  systemctl status robot-client --no-pager -n 15 2>/dev/null | sed 's/^/  /' || true
else echo "systemd not running (container?)"; fi
echo; echo "== devices"; ls -l /dev/rdf-* 2>/dev/null || echo "no /dev/rdf-* symlinks (udev rules TO VERIFY)"
ls /dev/v4l/by-id/ 2>/dev/null | sed 's/^/  /' || true
echo; echo "== CAN"; ip -details -brief link show type can 2>/dev/null || echo "no CAN interfaces"
echo; echo "== host"; uptime; df -h "$app" 2>/dev/null | tail -n1
command -v tegrastats >/dev/null && timeout 2 tegrastats --interval 1000 2>/dev/null | head -n1 || true
EOS
}

cmd_logs() {
  need_host jetson-logs
  local n="${LINES:-200}" follow=-f
  [ "${FOLLOW:-1}" = 0 ] && follow=--no-pager
  rssh_tty "journalctl -u robot-client -u rdf-can -n $(printf %q "$n") $follow -o short-iso"
}

cmd_mint_identity() {
  local args=(--env-file "$ENV_FILE") a
  for a in ${EXTRA[@]+"${EXTRA[@]}"}; do [ "$a" = --yes ] && [ "$DRY_RUN" = 0 ] && args+=(--yes); done
  (cd "$REPO/services/robot-client" && uv run --quiet python "$HERE/mint_robot_identity.py" "${args[@]}")
}

# ---------- commands ----------

cmd_bootstrap() {
  if ! have_host; then
    [ "$DRY_RUN" = 1 ] || need_host jetson-bootstrap
    step "offline plan (no JETSON_HOST): nothing is contacted or written"
    build_bundle
    for f in bootstrap.sh deploy.sh robot-client can-up.sh robot-client.service.in rdf-can.service 99-rdf-robot.rules; do
      [ -f "$HERE/remote/$f" ] || die "missing scripts/jetson/remote/$f"
    done
    dry "1. preflight: ssh, L4T/JetPack (/etc/nv_tegra_release), Ubuntu, python3, sudo"
    dry "2. rsync $BUNDLE_FILES files -> <host>:~/$JETSON_DIR/app (delete stale files, keep .venv)"
    dry "3. sudo bootstrap.sh: apt (v4l-utils ffmpeg gstreamer can-utils ...), groups video/dialout/plugdev,"
    dry "   CAN modules, uv $(uv --version 2>/dev/null | awk '{print $2}') + Python 3.11, /etc/rdf, udev rules, systemd units, journald, /usr/local/bin/robot-client"
    cmd_env
    dry "5. deploy.sh: uv sync robot-client (+rdf-common), I2RT SDK, import checks, service mode, restart"
    dry "6. robot-client check: SpacetimeDB, policy latency, media upload, orchestrator"
    return 0
  fi
  cmd_preflight
  cmd_sync
  cmd_remote_bootstrap
  cmd_env
  cmd_remote_deploy
  cmd_check || warn "connectivity check reported failures (see above); the install itself is done"
  step "done. Next: make jetson-status / make jetson-logs; checklist: services/robot-client/docs/jetson-bootstrap-checklist.md"
}

cmd_deploy() {
  need_host jetson-deploy
  DEPLOY_ARGS=(${EXTRA[@]+"${EXTRA[@]}"})   # e.g. --restart, --no-i2rt, --i2rt-ref <sha>
  cmd_preflight
  cmd_sync
  cmd_remote_deploy
}

main() {
  [ $# -ge 1 ] || usage 1
  CMD="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run|-n) DRY_RUN=1 ;;
      -h|--help) usage 0 ;;
      --) shift; EXTRA+=("$@"); break ;;
      *) EXTRA+=("$1") ;;
    esac
    shift
  done
  [ "${DRY_RUN_ENV:-${RDF_DRY_RUN:-0}}" = 1 ] && DRY_RUN=1
  load_config
  case "$CMD" in
    bootstrap) cmd_bootstrap ;;
    plan) DRY_RUN=1; cmd_bootstrap ;;
    deploy) cmd_deploy ;;
    env) if have_host; then remote_paths; fi; cmd_env ;;
    check) need_host jetson-check; remote_paths; cmd_check ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    ssh) need_host jetson-ssh; ssh "${SSH_OPTS[@]}" -t "$TARGET" ;;
    mint-identity) cmd_mint_identity ;;
    -h|--help|help) usage 0 ;;
    *) die "unknown command '$CMD' (bootstrap|deploy|env|check|status|logs|ssh|plan|mint-identity)" ;;
  esac
}

main "$@"
