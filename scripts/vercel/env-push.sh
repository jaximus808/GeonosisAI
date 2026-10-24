#!/usr/bin/env bash
# TDM-110: push the web app's env to its Vercel project WITHOUT printing any value.
#
#   scripts/vercel/env-push.sh                    # production (default)
#   scripts/vercel/env-push.sh preview            # preview deployments
#   scripts/vercel/env-push.sh production --dry-run   # list what would be set (names + source only)
#
# Run from a checkout linked to the project (`vercel link`, which writes .vercel/project.json), or set
# VERCEL_CWD=<that dir>. Values come from the repo-root .env; the plain config below has fixed values.
# Only what the web app reads is pushed: never MODAL_*, FACTORY_*, ANTHROPIC_API_KEY, HF_TOKEN, the
# SpacetimeDB OWNER token (SPACETIME_TOKEN) or STRIPE_SECRET_KEY (the web app uses none of them).
# Env split and runbook: docs/MORNING.md "Hybrid deploy".
set -euo pipefail

TARGET="${1:-production}"
DRY=0
[[ "${2:-}" == "--dry-run" || "${1:-}" == "--dry-run" ]] && DRY=1
[[ "$TARGET" == "--dry-run" ]] && TARGET=production
case "$TARGET" in production|preview) ;; *) echo "target must be production|preview" >&2; exit 2 ;; esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${RDF_ENV_FILE:-$REPO/.env}"
VERCEL="${VERCEL:-npx -y vercel@65.2.0}"
CWD="${VERCEL_CWD:-$REPO}"
WEB_URL="${RDF_WEB_PUBLIC_URL:-https://geonosis.jaxonp.com}"

# Secrets: from .env (blank/missing = skipped and reported).
SECRETS=(
  RDF_EDGE_SECRET          # shared with make demo-hybrid's edge (orchestrator discovery + auth)
  RDF_OPERATOR_KEY         # X-RDF-Operator-Key the web server adds to operator calls (same as the orchestrator's)
  RDF_OPERATOR_PASSCODE    # /ops/login
  RDF_WEB_SPACETIME_TOKEN  # `web` service identity on the web's SpacetimeDB (approve plan, direct e-stop)
  RDF_PUBLIC_ACCESS_CODE   # phones enter it once at /join; the same code make demo-public and the job drops use
)
# Plain config (not secret).
PLAIN=(
  "NEXT_PUBLIC_SPACETIME_HOST=${NEXT_PUBLIC_SPACETIME_HOST:-https://maincloud.spacetimedb.com}"
  "NEXT_PUBLIC_SPACETIME_DB_NAME=${NEXT_PUBLIC_SPACETIME_DB_NAME:-robo-factory-a706j}"
  "NEXT_PUBLIC_ORCHESTRATOR_URL=/api/orch"
  "NEXT_PUBLIC_RDF_ENV=production"
  "RDF_DEPLOY_ENV=production"
  "RDF_WEB_PUBLIC_URL=$WEB_URL"
  "NEXT_PUBLIC_WEB_PUBLIC_URL=$WEB_URL"
  "ENABLE_EXPERIMENTAL_COREPACK=1"
)

env_value() {  # one value from .env, never echoed to the terminal
  local name="$1" line
  [[ -f "$ENV_FILE" ]] || return 0
  line="$(grep -E "^(export )?${name}=" "$ENV_FILE" | tail -n 1 || true)"
  line="${line#export }"
  line="${line#"${name}="}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

push() {  # name value
  local name="$1" value="$2"
  if (( DRY )); then echo "  would set $name ($TARGET)"; return; fi
  # value on stdin only; CLI output (which can echo the name) goes to /dev/null
  if printf '%s' "$value" | $VERCEL env add "$name" "$TARGET" --force --cwd "$CWD" >/dev/null 2>&1; then
    echo "  set     $name ($TARGET)"
  else
    echo "  FAILED  $name ($TARGET): rerun with VERCEL='npx -y vercel@65.2.0' after 'vercel link' in $CWD" >&2
    return 1
  fi
}

echo "vercel env ($TARGET) for the project linked in $CWD:"
missing=()
for k in "${SECRETS[@]}"; do
  v="$(env_value "$k")"
  if [[ -z "$v" ]]; then missing+=("$k"); continue; fi
  push "$k" "$v"
done
for kv in "${PLAIN[@]}"; do
  push "${kv%%=*}" "${kv#*=}"
done
# Never set RDF_ORCHESTRATOR_URL here: unset = discovery through SpacetimeDB (event_log orchestrator.endpoint).
if ((${#missing[@]})); then
  echo "  skipped (blank in $ENV_FILE): ${missing[*]}"
fi
