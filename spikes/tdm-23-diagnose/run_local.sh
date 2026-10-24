#!/usr/bin/env bash
# TDM-23 local E2E: throwaway SpacetimeDB + the real orchestrator (bus + diagnose handler) + one real
# Claude call. Seeds a synthetic 10-trial baseline from the labelled fixture, finalizes it, and checks
# that failure_cluster rows + a diagnose.clusters event appear with example ids = real failed episodes.
#
#   spikes/tdm-23-diagnose/run_local.sh        # PORT=3023 DB=tdm23test; never touches maincloud
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PORT="${PORT:-3023}"; OPORT="${OPORT:-8023}"; DB="${DB:-tdm23test}"
HOST="http://127.0.0.1:$PORT"
TMP="$HERE/tmp"; mkdir -p "$TMP"
WORK="$(mktemp -d "$TMP/module.XXXXXX")"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT

spacetime start --listen-addr "127.0.0.1:$PORT" --data-dir "$TMP/stdb-data" >"$TMP/stdb.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do curl -sf "$HOST/v1/ping" >/dev/null 2>&1 && break; sleep 0.5; done

mkdir -p "$WORK/spacetimedb"
cp -R "$REPO/spacetime/spacetimedb/src" "$REPO/spacetime/spacetimedb/package.json" \
  "$REPO/spacetime/spacetimedb/tsconfig.json" "$WORK/spacetimedb/"
ln -s "$REPO/spacetime/spacetimedb/node_modules" "$WORK/spacetimedb/node_modules"
( cd "$WORK" && spacetime publish --no-config -s "$HOST" -p ./spacetimedb "$DB" --delete-data=always --yes ) >"$TMP/publish.log" 2>&1

ID_JSON="$(curl -sf -X POST "$HOST/v1/identity")"
TOKEN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["token"])' "$ID_JSON")"
IDENT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["identity"])' "$ID_JSON")"
spacetime call --no-config -s "$HOST" "$DB" register_service_identity "{\"__identity__\":\"0x$IDENT\"}" '"orchestrator"' '"test.tdm23 orchestrator"' >/dev/null

cd "$REPO/services/orchestrator"
export SPACETIME_HOST="$HOST" SPACETIME_DB_NAME="$DB" SPACETIME_TOKEN="$TOKEN"
uv run rdf-orchestrator --port "$OPORT" >"$TMP/orchestrator.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$OPORT/healthz" | grep -q '"state": *"live"' && break; sleep 0.5; done

SEED="$(uv run python "$HERE/seed_eval.py" --host "$HOST" --db "$DB" --token "$TOKEN")"
echo "seeded: $SEED"
TID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["ticket_id"])' "$SEED")"

for _ in $(seq 1 180); do
  N="$(curl -sf -X POST "$HOST/v1/database/$DB/sql" -H "Authorization: Bearer $TOKEN" \
        --data "SELECT * FROM event_log WHERE type = 'diagnose.clusters'" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)[-1]["rows"]))')"
  [ "$N" -ge 1 ] && break; sleep 1
done
uv run python - "$HOST" "$DB" "$TOKEN" "$TID" "$SEED" <<'PY'
import json, sys
from rdf_common.spacetime_client import SpacetimeClient
host, db, token, tid, seed = sys.argv[1:6]
seed = json.loads(seed)
s = SpacetimeClient(host=host, db=db, token=token)
clusters = s.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {tid}")
ev = [r for r in s.sql("SELECT * FROM event_log") if r["type"].startswith("diagnose.")]
failed_eps = {r["episode_id"] for r in s.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {seed['eval_run_id']}") if not r["success"]} - {0}
print("failure_cluster rows:")
for c in clusters:
    print(f"  id={c['id']} label={c['label']!r} count={c['count']} examples={c['example_episode_ids']} iteration={c['iteration']}")
assert clusters, "no failure_cluster rows"
assert all(set(c["example_episode_ids"]) <= failed_eps for c in clusters), "example ids are not failed episodes"
assert sum(c["count"] for c in clusters) == 7
assert len(ev) == 1 and ev[0]["type"] == "diagnose.clusters", [e["type"] for e in ev]
p = json.loads(ev[0]["payload_json"])
assert sorted(c["failure_cluster_id"] for c in p["clusters"]) == sorted(c["id"] for c in clusters)
print("event diagnose.clusters:", json.dumps({k: p[k] for k in ("eval_run_id", "failures", "recoverable_failures", "unrecoverable_failures", "model", "media")}))
for c in p["clusters"]:
    print(f"  {c['label']!r}: recoverable={c['recoverable_mid_episode']} phase={c['phase']} episodes={c['episode_ids']}")
print("OK")
PY
grep -E '"diagnose\.(start|done|skip|frames_failed)"|handler.error' "$TMP/orchestrator.log" | cut -c1-400 || true
