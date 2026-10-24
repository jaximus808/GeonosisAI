#!/usr/bin/env bash
# TDM-42 local test: GPU spend (training_run.gpu_cost_cents) counts in the payout budget cap.
# Throwaway local server only, never maincloud.
#   MODULE=<path to spacetime/spacetimedb> ./local_budget_test.sh   (default: this checkout's module)
# Needs spikes/tdm-11-payments/seed.ts (test-only contributor seed).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
MODULE=${MODULE:-$ROOT/spacetime/spacetimedb}
WORK=${WORK:-${TMPDIR:-/tmp}/tdm42-stdb}
PORT=${PORT:-3042}
SRV=http://127.0.0.1:$PORT
DB=tdm42test

rm -rf "$WORK/module" && mkdir -p "$WORK/module" "$WORK/data"
rsync -a --exclude node_modules --exclude dist "$MODULE/" "$WORK/module/"
ln -s "$ROOT/spacetime/spacetimedb/node_modules" "$WORK/module/node_modules"
cp "$ROOT/spikes/tdm-11-payments/seed.ts" "$WORK/module/src/reducers/tdm11_test_seed.ts"
echo "export * from './reducers/tdm11_test_seed';" >> "$WORK/module/src/index.ts"
if ! curl -s "$SRV/v1/ping" >/dev/null; then
  nohup spacetime start --listen-addr "127.0.0.1:$PORT" --data-dir "$WORK/data" >"$WORK/server.log" 2>&1 &
  echo $! > "$WORK/server.pid"
  sleep 4
fi
spacetime publish --no-config -s "$SRV" -p "$WORK/module" "$DB" --delete-data=always --yes >"$WORK/publish.log" 2>&1 || { tail -20 "$WORK/publish.log"; exit 1; }

pass=0; fail=0
call() { spacetime call -s "$SRV" "$DB" "$@" 2>&1; }
ok()   { local d=$1; shift; if out=$(call "$@") && ! grep -qi error <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
sql()  { spacetime sql -s "$SRV" "$DB" "$1" 2>&1; }
want() { local d=$1 q=$2 pat=$3; out=$(sql "$q"); if grep -Eq "$pat" <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
NONE='{"none":[]}'
EP() { call create_episode 1 "$1" 0 '"clean_teleop"' "\"s3://test/tdm42/$2\"" "\"s3://test/tdm42/$2.parquet\"" '"{\"wrist\":\"s3://x.mp4\"}"' 300 10000 "$NONE" "$NONE" '{"some":true}' "$NONE"; }
paid_episode() {  # task id, price, name
  ok "task $1: clean_teleop $2c"   create_task 1 '"clean_teleop"' "$2" '"{}"'
  ok "claim task $1"               claim_task "$1" 1
  out=$(EP "$1" "$3"); grep -qi error <<<"$out" && echo "FAIL  episode for task $1 :: $out"
  ok "complete task $1"            complete_task "$1" '"{}"'
}

ok   "seed contributor 1"            tdm_11_test_seed_contributor '"test.tdm42.alice"'
ok   "register robot"                register_robot '"test.tdm42.yam"' '"i2rt_yam"' '"v0"' '"{}"'
ok   "create ticket, budget 1500c"   create_ticket 1 '"test.tdm42 budget"' 0.8 1500 '"{}"'

paid_episode 1 500 ep1
ok   "accept episode 1 (reserves 500c)" submit_review 1 true '"ok"' '"{}"'
want "payment 1 pending 500"         "SELECT status, amount_cents FROM payment WHERE episode_id = 1" '"pending" +\| +500'

ok   "queue training run on episode 1" queue_training_run 1 '"[1]"' '"{}"'
ok   "training running"              set_training_status 1 '"running"' '{"some":"fc-test"}' "$NONE" "$NONE" "$NONE"
ok   "training done, gpu_cost 800c"  set_training_status 1 '"done"' "$NONE" '{"some":"modal-vol://rdf-checkpoints/runs/ticket-1/r/199"}' '{"some":"{\"cost\":{\"source\":\"estimate\"}}"}' '{"some":800}'
want "training_run gpu_cost_cents 800" "SELECT gpu_cost_cents FROM training_run WHERE id = 1" '^ +800 *$'

# spent 500 + GPU 800 + 500 = 1800 > 1500: blocked (without TDM-42 it was 1000 <= 1500: pending)
paid_episode 2 500 ep2
ok   "accept episode 2"              submit_review 2 true '"ok"' '"{}"'
want "payment 2 blocked_budget (GPU counted)" "SELECT status FROM payment WHERE episode_id = 2" '"blocked_budget"'
want "blocked reason names GPU"      "SELECT error FROM payment WHERE episode_id = 2" 'GPU 800'
want "spent_cents still 500 (payouts only)" "SELECT spent_cents FROM ticket WHERE id = 1" '^ +500 *$'

# 500 + 800 + 200 = 1500 <= 1500: fits exactly
paid_episode 3 200 ep3
ok   "accept episode 3"              submit_review 3 true '"ok"' '"{}"'
want "payment 3 pending 200 (fits with GPU)" "SELECT status, amount_cents FROM payment WHERE episode_id = 3" '"pending" +\| +200'
want "spent_cents 700"               "SELECT spent_cents FROM ticket WHERE id = 1" '^ +700 *$'
want "payment.reserved event logs gpu_cost_cents" "SELECT payload_json FROM event_log WHERE type = 'payment.reserved'" 'gpu_cost_cents.{0,6}800'

echo "== $pass passed, $fail failed"
[ -f "$WORK/server.pid" ] && kill "$(cat "$WORK/server.pid")" 2>/dev/null
[ "$fail" = 0 ]
