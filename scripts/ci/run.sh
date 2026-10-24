#!/usr/bin/env bash
# TDM-6: the CI steps, one definition for GitHub Actions (.github/workflows/ci.yml) and `make ci`.
#
#   scripts/ci/run.sh all             every unit step below, keeps going, prints a summary (= make ci)
#   scripts/ci/run.sh py <service>    uv sync + pytest in services/<service>
#   scripts/ci/run.sh py-all          the same for every service in PY_SERVICES
#   scripts/ci/run.sh ts              pnpm workspace: typecheck + tests (apps/*, packages/*)
#   scripts/ci/run.sh stdb            SpacetimeDB module: tsc --noEmit
#   scripts/ci/run.sh lint            ruff, minimal ruleset (syntax errors, undefined names)
#   scripts/ci/run.sh evlog           scripts/verify_event_log.py static
#   scripts/ci/run.sh integration     OPTIONAL: tests that need the `spacetime` CLI (TDM-28 harness)
#
# Unit steps never need a real secret or the network:
#   - the env comes from scripts/ci/fake.env (RDF_ENV_FILE points there, every key named in .env.example /
#     .env is blanked first so the real .env is never read; only key NAMES are read, never values);
#   - RDF_LIVE_* and other opt-in live flags are unset (Claude / Stripe / Modal / maincloud tests skip);
#   - the `spacetime` CLI is hidden from PATH, so tests that boot a local SpacetimeDB skip exactly as they
#     do on a GitHub runner (they run in the `integration` step instead).
# Installs: CI always installs (pnpm install --frozen-lockfile, npm ci). Locally an existing node_modules is
# reused so a shared checkout is not reinstalled under other workers; CI_INSTALL=1 forces the CI behaviour.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY_SERVICES=(${PY_SERVICES:-common orchestrator factory training robot-client payments})
RUFF_VERSION="${RUFF_VERSION:-0.8.4}"
RUFF_RULES="E9,F63,F7,F82"   # syntax errors, bad comparisons, misplaced break/return, undefined names
TMP_CI="${TMPDIR:-/tmp}/tdm-6-ci"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

want_install() { [[ "${CI:-}" == "true" || "${CI_INSTALL:-}" == "1" || ! -d "$1" ]]; }

fake_env() {
  local k
  # Blank every known key (names only) so neither pydantic-settings nor rdf_common's load_dotenv
  # (os.environ.setdefault from the repo .env) can pick up a real value.
  for f in .env.example .env; do
    [[ -f "$f" ]] || continue
    while IFS= read -r k; do export "$k="; done < <(sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' "$f")
  done
  for k in $(env | sed -n 's/^\(RDF_LIVE_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$k"; done
  unset TDM20_STDB_HOST TDM20_ALLOW_MAINCLOUD
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/scripts/ci/fake.env"
  set +a
  export RDF_ENV_FILE="$ROOT/scripts/ci/fake.env"
  export PYTHONDONTWRITEBYTECODE=1
}

hide_spacetime() {
  local st d shim
  st="$(command -v spacetime 2>/dev/null)" || return 0
  d="$(dirname "$st")"
  shim="$TMP_CI/bin-$(printf '%s' "$d" | cksum | cut -d' ' -f1)"
  rm -rf "$shim" && mkdir -p "$shim"
  for f in "$d"/*; do [[ "$(basename "$f")" == spacetime ]] || ln -s "$f" "$shim/"; done
  PATH="$(printf '%s' "$PATH" | tr ':' '\n' | sed "s|^$d\$|$shim|" | paste -sd: -)"
  export PATH
  hash -r
}

step_py() {
  local svc="$1" rc
  say "python: services/$svc (uv sync + pytest)"
  ( cd "services/$svc" && uv sync --quiet ) || return 1
  # --with pytest: robot-client has no dev group; elsewhere it is already installed and this is a no-op.
  ( cd "services/$svc" && uv run --quiet --with 'pytest>=8' pytest -q -p no:cacheprovider -rs )
  rc=$?
  if [[ $rc -eq 5 ]]; then echo "(no tests collected in services/$svc)"; return 0; fi
  return $rc
}

step_ts() {
  say "ts: pnpm workspace typecheck + tests"
  if want_install node_modules; then pnpm install --frozen-lockfile || return 1; fi
  local rc=0
  pnpm -r --if-present run typecheck || rc=1
  pnpm -r --if-present run test || rc=1
  return $rc
}

step_stdb() {
  say "spacetime module: tsc --noEmit"
  cd spacetime/spacetimedb || return 1
  if want_install node_modules; then npm ci --no-audit --no-fund || return 1; fi
  npx --no-install tsc --noEmit
}

step_lint() {
  say "lint: ruff $RUFF_VERSION --select $RUFF_RULES"
  uvx --quiet "ruff@$RUFF_VERSION" check --no-cache --select "$RUFF_RULES" --target-version py311 \
    --output-format concise services scripts tests spacetime/tests
}

step_evlog() {
  say "event log: scripts/verify_event_log.py static"
  python3 scripts/verify_event_log.py static
}

step_integration() {
  say "integration (optional): local SpacetimeDB via the spacetime CLI"
  command -v spacetime >/dev/null || { echo "spacetime CLI not installed"; return 1; }
  if want_install spacetime/spacetimedb/node_modules; then (cd spacetime/spacetimedb && npm ci --no-audit --no-fund) || return 1; fi
  local rc=0
  ( cd services/orchestrator && uv sync --quiet && TDM28_PORT="${TDM28_PORT:-3028}" RDF_LOOP_DRIVER=scripted \
      uv run --quiet pytest ../../tests/integration -q -p no:cacheprovider -rs ) || rc=1
  ( cd services/robot-client && uv sync --quiet && uv run --quiet --with 'pytest>=8' \
      pytest -q -p no:cacheprovider -rs tests/test_tdm48_recorder.py tests/test_tdm49_loop.py ) || rc=1
  return $rc
}

run_all() {
  local names=() codes=() s rc failed=0
  for s in "${PY_SERVICES[@]}"; do (step_py "$s"); rc=$?; names+=("py $s"); codes+=($rc); done
  for s in ts stdb lint evlog; do ("step_$s"); rc=$?; names+=("$s"); codes+=($rc); done
  say "summary"
  for i in "${!names[@]}"; do
    if [[ ${codes[$i]} -eq 0 ]]; then printf '  PASS  %s\n' "${names[$i]}"; else printf '  FAIL  %s (exit %s)\n' "${names[$i]}" "${codes[$i]}"; failed=1; fi
  done
  return $failed
}

cmd="${1:-all}"; shift || true
mkdir -p "$TMP_CI"
fake_env
case "$cmd" in
  integration) step_integration ;;
  *)
    hide_spacetime
    case "$cmd" in
      all) run_all ;;
      py) [[ $# -ge 1 ]] || { echo "usage: $0 py <service>"; exit 2; }; step_py "$1" ;;
      py-all) rc=0; for s in "${PY_SERVICES[@]}"; do step_py "$s" || rc=1; done; exit $rc ;;
      ts|stdb|lint|evlog) "step_$cmd" ;;
      *) echo "unknown step: $cmd"; exit 2 ;;
    esac ;;
esac
