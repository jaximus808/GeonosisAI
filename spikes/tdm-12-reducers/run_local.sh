#!/usr/bin/env bash
# TDM-12 local test: publishes a throwaway copy of the module (plus the
# test-only seed reducer) to a local SpacetimeDB and runs walk.py against it.
#
#   spacetime start --listen-addr 127.0.0.1:3012 --data-dir "$TMPDIR/tdm12-stdb" &
#   spikes/tdm-12-reducers/run_local.sh            # PORT=3012 DB=tdm12test by default
#
# Never touches maincloud. The seed reducer only exists in the copy.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PORT="${PORT:-3012}"
DB="${DB:-tdm12test}"
HOST="http://127.0.0.1:$PORT"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tdm12-module.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/spacetimedb"
cp -R "$REPO/spacetime/spacetimedb/src" "$REPO/spacetime/spacetimedb/package.json" \
  "$REPO/spacetime/spacetimedb/tsconfig.json" "$WORK/spacetimedb/"
ln -s "$REPO/spacetime/spacetimedb/node_modules" "$WORK/spacetimedb/node_modules"
cp "$HERE/test_seed.ts" "$WORK/spacetimedb/src/reducers/tdm12_test_seed.ts"
echo "export * from './reducers/tdm12_test_seed'; // TDM-12 local test only" >> "$WORK/spacetimedb/src/index.ts"

( cd "$WORK" && spacetime publish --no-config -s "$HOST" -p ./spacetimedb "$DB" --delete-data=always --yes )

# Fresh identity for the orchestrator role, allow-listed by the publisher (owner).
ID_JSON="$(curl -sf -X POST "$HOST/v1/identity")"
TOKEN_FILE="$WORK/orchestrator.token"
python3 -c 'import json,sys; print(json.loads(sys.argv[1])["token"])' "$ID_JSON" > "$TOKEN_FILE"
IDENT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["identity"])' "$ID_JSON")"
spacetime call --no-config -s "$HOST" "$DB" register_service_identity "{\"__identity__\":\"0x$IDENT\"}" '"orchestrator"' '"test.tdm12 orchestrator"'
spacetime call --no-config -s "$HOST" "$DB" tdm_12_test_seed  # export tdm12_test_seed is snake-cased by the module

# Fresh DB: ticket 1, accepted episodes 1-3, pending episode 4.
python3 -I "$HERE/walk.py" --host "$HOST" --db "$DB" --token-file "$TOKEN_FILE" \
  --ticket 1 --episodes 1,2,3 --pending-episode 4
