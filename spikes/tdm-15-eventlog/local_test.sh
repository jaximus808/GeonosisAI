#!/usr/bin/env bash
# TDM-15 local test: append-only event_log, logEvent, append_event lock-down and
# scripts/verify_event_log.py. Throwaway local server only, never maincloud.
#   spikes/tdm-15-eventlog/local_test.sh        # PORT=3115 DB=tdm15test
# Publishes a copy of the CURRENT working-tree module. The CLI login is the
# publisher, so plain `spacetime call` runs as the owner.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${WORK:-${TMPDIR:-/tmp}/tdm15}
PORT=${PORT:-3115}
SRV=http://127.0.0.1:$PORT
DB=${DB:-tdm15test}

mkdir -p "$WORK/data"
rm -rf "$WORK/module" && mkdir -p "$WORK/module"
rsync -a --exclude node_modules --exclude dist "$ROOT/spacetime/spacetimedb/" "$WORK/module/"
ln -s "$ROOT/spacetime/spacetimedb/node_modules" "$WORK/module/node_modules"
if ! curl -sf "$SRV/v1/ping" >/dev/null; then
  nohup spacetime start --listen-addr "127.0.0.1:$PORT" --data-dir "$WORK/data" >"$WORK/server.log" 2>&1 &
  for _ in $(seq 20); do curl -sf "$SRV/v1/ping" >/dev/null && break; sleep 0.5; done
fi
spacetime publish --no-config -s "$SRV" -p "$WORK/module" "$DB" --delete-data=always --yes >"$WORK/publish.log" 2>&1 \
  || { cat "$WORK/publish.log"; exit 1; }

pass=0; fail=0
call()  { spacetime call --no-config -s "$SRV" "$DB" "$@" 2>&1; }
acall() { spacetime call --no-config --anonymous -s "$SRV" "$DB" "$@" 2>&1; }
# HTTP call as a minted identity: tcall <token> <reducer> <json-args>
tcall() { curl -s -o /dev/null -w '%{http_code}' -X POST "$SRV/v1/database/$DB/call/$2" \
            -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$3"; }
tbody() { curl -s -X POST "$SRV/v1/database/$DB/call/$2" -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d "$3"; }
ok()   { local d=$1; shift; if out=$(call "$@") && ! grep -qi error <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
bad()  { local d=$1 want=$2; shift 2; out=$(call "$@"); if grep -q "$want" <<<"$out"; then echo "PASS  $d -> $want"; pass=$((pass+1)); else echo "FAIL  $d (wanted '$want') :: $out"; fail=$((fail+1)); fi; }
abad() { local d=$1 want=$2; shift 2; out=$(acall "$@"); if grep -q "$want" <<<"$out"; then echo "PASS  $d -> $want"; pass=$((pass+1)); else echo "FAIL  $d (wanted '$want') :: $out"; fail=$((fail+1)); fi; }
tok()  { local d=$1 code; code=$(tcall "$2" "$3" "$4"); if [ "$code" = 200 ]; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: HTTP $code $(tbody "$2" "$3" "$4" | head -c 200)"; fail=$((fail+1)); fi; }
tbad() { local d=$1 want=$5 out; out=$(tbody "$2" "$3" "$4"); if grep -q "$want" <<<"$out"; then echo "PASS  $d -> $want"; pass=$((pass+1)); else echo "FAIL  $d (wanted '$want') :: $out"; fail=$((fail+1)); fi; }
cnt()  { spacetime sql --no-config -s "$SRV" "$DB" "SELECT COUNT(*) AS n FROM event_log" 2>/dev/null | awk 'NR==3{print $1}'; }
verify() { python3 -I "$ROOT/scripts/verify_event_log.py" --host "$SRV" --db "$DB" --anonymous "$@"; }

# --- append_event lock-down
abad "anonymous append_event refused"      "not a service identity" append_event '"orchestrator"' '"test.tdm15.anon"' '"{}"'
ok   "owner append_event"                  append_event '"planner"' '"test.tdm15.owner"' '"{\"n\":1}"'
ok   "owner append_event, empty payload"   append_event '"droid:training"' '"test.tdm15.empty"' '""'
want=$(spacetime sql --no-config -s "$SRV" "$DB" "SELECT payload_json FROM event_log WHERE type = 'test.tdm15.empty'" 2>&1)
if grep -q '"{}"' <<<"$want"; then echo "PASS  empty payload stored as {}"; pass=$((pass+1)); else echo "FAIL  empty payload :: $want"; fail=$((fail+1)); fi
bad  "bad JSON refused"                    "valid JSON"   append_event '"planner"' '"test.tdm15.badjson"' '"not json"'
bad  "actor with a space refused"          "must be <kind>" append_event '"human:jane doe"' '"test.tdm15.x"' '"{}"'
bad  "empty actor refused"                 "must be <kind>" append_event '""' '"test.tdm15.x"' '"{}"'
bad  "uppercase type refused"              "area.event"   append_event '"planner"' '"Test.X"' '"{}"'

# minted robot_client identity
ID_JSON=$(curl -sf -X POST "$SRV/v1/identity")
RTOK=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["token"])' "$ID_JSON")
RID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["identity"])' "$ID_JSON")
tbad "unregistered identity refused"       "$RTOK" append_event '["robot:1","test.tdm15.x","{}"]' "not a service identity"
ok   "register robot_client"               register_service_identity "{\"__identity__\":\"0x$RID\"}" '"robot_client"' '"test.tdm15 robot"'
tok  "robot_client logs as robot:1"        "$RTOK" append_event '["robot:1","test.tdm15.robot","{}"]'
tok  "robot_client logs as robot_client:x" "$RTOK" append_event '["robot_client:jetson","test.tdm15.robot","{}"]'
tbad "robot_client may not log as orchestrator" "$RTOK" append_event '["orchestrator","test.tdm15.x","{}"]' "may not log as"
tbad "robot_client may not log as owner"   "$RTOK" append_event '["owner","test.tdm15.x","{}"]' "may not log as"
tbad "robot_client may not log as spacetime" "$RTOK" append_event '["spacetime","test.tdm15.x","{}"]' "may not log as"

# --- verify script: checkpoint, append, prefix check, tamper detection
n0=$(cnt)
if out=$(verify --save "$WORK/cp.json") && grep -q "rows        $n0" <<<"$out"; then echo "PASS  verify prints count $n0 + chain hash"; pass=$((pass+1)); else echo "FAIL  verify :: $out"; fail=$((fail+1)); fi
ok   "append after checkpoint"             append_event '"orchestrator"' '"test.tdm15.after"' '"{}"'
if out=$(verify --check "$WORK/cp.json") && grep -q "first $n0 rows are unchanged; 1 row(s) appended" <<<"$out"; then echo "PASS  checkpoint prefix still matches"; pass=$((pass+1)); else echo "FAIL  checkpoint :: $out"; fail=$((fail+1)); fi
if out=$(verify --expect "$n0:$(printf '0%.0s' $(seq 64))"); then echo "FAIL  wrong hash accepted :: $out"; fail=$((fail+1)); else echo "PASS  wrong checkpoint hash -> exit 1"; pass=$((pass+1)); fi
if out=$(verify --expect "$((n0+50)):abc"); then echo "FAIL  missing rows accepted"; fail=$((fail+1)); else echo "PASS  checkpoint longer than log -> exit 1 (rows deleted)"; pass=$((pass+1)); fi

# --- no reducer can update/delete event_log: none exposes such a path (static audit)
python3 -I "$ROOT/scripts/verify_event_log.py" static --module "$WORK/module/src" | sed 's/^/      /'

echo "---- $pass passed, $fail failed"
[ "$fail" = 0 ]
