#!/usr/bin/env bash
# TDM-11 local test: episode, review and payment reducers + approve_spend.
# Runs against a throwaway local server, never maincloud.
#   SETUP=1 ./local_test.sh   copy the module (plus seed.ts and the TDM-10/11
#                             re-exports) to $WORK, start a server on :3011, publish
#   ./local_test.sh           run the checks (expects a fresh publish)
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
WORK=${WORK:-${TMPDIR:-/tmp}/tdm11}
PORT=${PORT:-3011}
SRV=${SRV:-http://127.0.0.1:$PORT}
DB=${DB:-tdm11test}

if [ "${SETUP:-0}" = 1 ]; then
  rm -rf "$WORK/module" && mkdir -p "$WORK/module" "$WORK/data"
  rsync -a --exclude node_modules --exclude dist "$ROOT/spacetime/spacetimedb/" "$WORK/module/"
  ln -s "$ROOT/spacetime/spacetimedb/node_modules" "$WORK/module/node_modules"
  cp "$HERE/seed.ts" "$WORK/module/src/reducers/tdm11_test_seed.ts"
  idx="$WORK/module/src/index.ts"
  for f in marketplace episodes_payments tdm11_test_seed; do
    grep -q "reducers/$f'" "$idx" || echo "export * from './reducers/$f';" >> "$idx"
  done
  if ! curl -s "$SRV/v1/ping" >/dev/null; then
    nohup spacetime start --listen-addr "127.0.0.1:$PORT" --data-dir "$WORK/data" >"$WORK/server.log" 2>&1 &
    sleep 4
  fi
  spacetime publish --no-config -s "$SRV" -p "$WORK/module" "$DB" --delete-data=always --yes || exit 1
fi

pass=0; fail=0
call() { spacetime call -s "$SRV" "$DB" "$@" 2>&1; }
ok()   { local d=$1; shift; if out=$(call "$@") && ! grep -qi error <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
bad()  { local d=$1 want=$2; shift 2; out=$(call "$@"); if grep -q "$want" <<<"$out"; then echo "PASS  $d -> $(grep -o "$want.*" <<<"$out" | head -1 | cut -c1-120)"; pass=$((pass+1)); else echo "FAIL  $d (wanted '$want') :: $out"; fail=$((fail+1)); fi; }
sql()  { spacetime sql -s "$SRV" "$DB" "$1" 2>&1; }
want() { local d=$1 q=$2 pat=$3; out=$(sql "$q"); if grep -Eq "$pat" <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
NONE='{"none":[]}'
NOW=$(( $(date +%s) * 1000000 ))
TS() { echo "{\"some\":{\"__timestamp_micros_since_unix_epoch__\":$1}}"; }
# create_episode ticket task contributor kind uri ...
EP() { call create_episode 1 "$1" 0 "\"$2\"" "\"s3://test/tdm11/$3\"" "\"s3://test/tdm11/$3.parquet\"" '"{\"wrist\":\"s3://x.mp4\"}"' 300 10000 "${4:-$NONE}" "${5:-$NONE}" '{"some":true}' "$NONE"; }
epok() { local d=$1; shift; out=$(EP "$@"); if ! grep -qi error <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }

ok   "seed contributor 1"                    tdm_11_test_seed_contributor '"test.tdm11.alice"'
ok   "register robot"                        register_robot '"test.tdm11.yam"' '"i2rt_yam"' '"v0"' '"{}"'
ok   "create ticket, budget 1500"            create_ticket 1 '"test.tdm11 cups in bin"' 0.8 1500 '"{}"'
ok   "task 1: clean_teleop \$10"             create_task 1 '"clean_teleop"' 1000 '"{}"'
ok   "claim task 1"                          claim_task 1 1
epok "episode 1 from task 1"                 1 clean_teleop ep1
ok   "complete task 1 (releases the arm)"  complete_task 1 '"{}"'
want "episode 1 pending, contributor from task" "SELECT review_status, contributor_id FROM episode WHERE id = 1" '"pending" +\| +1'
ok   "accept episode 1"                      submit_review 1 true '"clean grasp"' '"{\"jerk\":0.2}"'
want "payment pending 1000 for episode 1"    "SELECT status, amount_cents, contributor_id FROM payment WHERE episode_id = 1" '"pending" +\| +1000 +\| +1'
want "dataset.accepted_count = 1"            "SELECT accepted_count, total_count FROM dataset WHERE ticket_id = 1" '^ +1 +\| +1 *$'
want "ticket.spent_cents = 1000"             "SELECT spent_cents FROM ticket WHERE id = 1" '^ +1000 *$'
bad  "second review refused (final)"         "reviews are final" submit_review 1 false '"changed mind"' '"{}"'
out=$(EP 1 clean_teleop ep1b); if grep -q "one paid episode per task" <<<"$out"; then echo "PASS  second episode on task 1 refused"; pass=$((pass+1)); else echo "FAIL  second episode on task 1 :: $out"; fail=$((fail+1)); fi

ok   "task 2"                                create_task 1 '"recovery_takeover"' 1000 '"{}"'
ok   "claim task 2"                          claim_task 2 1
epok "episode 2 (takeover ts)"               2 recovery_takeover ep2 "$(TS $NOW)" "$(TS $((NOW + 4000000)))"
ok   "complete task 2 (releases the arm)"  complete_task 2 '"{}"'
bad  "reject without a reason refused"       "needs a reason" submit_review 2 false '"  "' '"{}"'
ok   "reject episode 2 with reason"          submit_review 2 false '"jerky takeover, cup dropped"' '"{\"max_jerk\":9.1}"'
want "no payment for rejected episode 2"     "SELECT COUNT(*) AS n FROM payment WHERE episode_id = 2" '^ +0 *$'
want "rejection reason visible"              "SELECT accepted, reason FROM review WHERE episode_id = 2" 'false +\| +"jerky takeover, cup dropped"'
want "episode 2 rejected"                    "SELECT review_status FROM episode WHERE id = 2" '"rejected"'
bad  "reserve_payment on rejected refused"   "no accepted review" reserve_payment 2

ok   "task 3"                                create_task 1 '"clean_teleop"' 1000 '"{}"'
ok   "claim task 3"                          claim_task 3 1
epok "episode 3"                             3 clean_teleop ep3
ok   "complete task 3 (releases the arm)"  complete_task 3 '"{}"'
ok   "accept episode 3 (over budget)"        submit_review 3 true '"ok"' '"{}"'
want "payment for ep3 blocked_budget"        "SELECT status FROM payment WHERE episode_id = 3" '"blocked_budget"'
want "spent unchanged at 1000"               "SELECT spent_cents FROM ticket WHERE id = 1" '^ +1000 *$'
ok   "raise budget to 5000"                  update_run_settings 1 '"{\"budget_cents\":5000}"'
ok   "reserve_payment retries ep3"           reserve_payment 3
want "ep3 now pending, spent 2000"           "SELECT status FROM payment WHERE episode_id = 3" '"pending"'
want "spent 2000"                            "SELECT spent_cents FROM ticket WHERE id = 1" '^ +2000 *$'
ok   "reserve_payment again is a no-op"      reserve_payment 3
want "still one payment for ep3"             "SELECT COUNT(*) AS n FROM payment WHERE episode_id = 3" '^ +1 *$'

ok   "mark payment 1 paid"                   mark_payment_paid 1 '"stripe_test"' '"tr_test_1"'
ok   "replay with same ref is a no-op"       mark_payment_paid 1 '"stripe_test"' '"tr_test_1"'
bad  "different ref refused"                 "already paid" mark_payment_paid 1 '"stripe_test"' '"tr_other"'
want "payment 1 paid with provider_ref"      "SELECT status, provider_ref FROM payment WHERE id = 1" '"paid" +\| +\(some = "tr_test_1"\)'
want "contributor total_paid 1000"           "SELECT total_paid_cents FROM contributor WHERE id = 1" '^ +1000 *$'
ok   "mark payment 2 failed"                 mark_payment_failed 2 '"test: transfer failed"'
want "failure releases spend: 1000"          "SELECT spent_cents FROM ticket WHERE id = 1" '^ +1000 *$'
bad  "paid on failed refused"                "only pending" mark_payment_paid 2 '"stripe_test"' '"tr_x"'
ok   "re-reserve failed ep3"                 reserve_payment 3
want "spent back to 2000"                    "SELECT spent_cents FROM ticket WHERE id = 1" '^ +2000 *$'

ok   "data plan: 2 recovery takeovers"       record_data_plan 1 '"recovery_takeover"' 2 '"test.tdm11 reasoning"' '"{}"'
ok   "task 4 under plan 1 (\$5)"             create_task 1 '"recovery_takeover"' 0 '"{\"data_plan_id\":1}"'
ok   "claim task 4"                          claim_task 4 1
epok "episode 4"                             4 recovery_takeover ep4
ok   "complete task 4 (releases the arm)"  complete_task 4 '"{}"'
ok   "accept episode 4 (plan not approved)"  submit_review 4 true '"good recovery"' '"{}"'
want "ep4 blocked: no approved spend"        "SELECT status, error FROM payment WHERE episode_id = 4" '"blocked_budget" +\| +\(some = "approved spend'
bad  "approve_spend over budget refused"     "> budget_cents" approve_spend 1 1 6000 '"human:test"'
bad  "approve_spend needs approved_by"       "approved_by is required" approve_spend 1 1 0 '""'
ok   "approve_spend (est cost)"              approve_spend 1 1 0 '"human:test"'
want "plan approved 1000"                    "SELECT status, approved_cents FROM data_plan WHERE id = 1" '"approved" +\| +1000'
want "ticket.approved_cents 1000"            "SELECT approved_cents FROM ticket WHERE id = 1" '^ +1000 *$'
ok   "reserve ep4 after approval"            reserve_payment 4
want "ep4 pending 500, spent 2500"           "SELECT spent_cents FROM ticket WHERE id = 1" '^ +2500 *$'
bad  "approve less than reserved refused"    "cannot approve less" approve_spend 1 1 400 '"human:test"'

epok "baseline episode 5"                    0 baseline ep5
want "baseline not_reviewed"                 "SELECT review_status FROM episode WHERE id = 5" '"not_reviewed"'
bad  "reviewing a baseline refused"          "only pending" submit_review 5 true '"x"' '"{}"'
out=$(EP 0 purchased ep6); if grep -q "kind must be one of" <<<"$out"; then echo "PASS  purchased via create_episode refused"; pass=$((pass+1)); else echo "FAIL  purchased :: $out"; fail=$((fail+1)); fi
want "dataset total 4 (baseline excluded), accepted 3" "SELECT accepted_count, total_count FROM dataset WHERE ticket_id = 1" '^ +3 +\| +4 *$'

ok   "reset task 5 (\$2)"                    create_task 1 '"reset"' 0 '"{}"'
ok   "claim task 5"                          claim_task 5 1
bad  "reset payout before completion refused" "not completed" reserve_task_payment 5
ok   "complete task 5"                       complete_task 5 '"{}"'
ok   "reserve reset payout"                  reserve_task_payment 5
ok   "again is a no-op"                      reserve_task_payment 5
want "one reset payment, 200 pending"        "SELECT status, amount_cents FROM payment WHERE id = 4" '"pending" +\| +200'
bad  "task with an episode refused"          "paid through submit_review" reserve_task_payment 1

ok   "payment 3 awaiting onboarding"         mark_payment_awaiting_onboarding 3 '"no stripe account yet"'
ok   "paid after onboarding"                 mark_payment_paid 3 '"stripe_test"' '"tr_test_3"'
ok   "reverse payment 1"                     mark_payment_reversed 1 '"test reversal"'
want "spent 2700 - 1000 = 1700"              "SELECT spent_cents FROM ticket WHERE id = 1" '^ +1700 *$'
want "contributor total 1000+500-1000 = 500" "SELECT total_paid_cents FROM contributor WHERE id = 1" '^ +500 *$'
bad  "reversed payment not re-reserved"      "was reversed" reserve_payment 1

want "event_log review.rejected row"         "SELECT payload_json FROM event_log WHERE type = 'review.rejected'" 'cup dropped'
want "event_log spend.approved by human"     "SELECT actor FROM event_log WHERE type = 'spend.approved'" '"human:test"'
anon=$(spacetime call --anonymous -s "$SRV" "$DB" submit_review 3 true '"x"' '"{}"' 2>&1)
if grep -q "not a service identity" <<<"$anon"; then echo "PASS  anonymous submit_review refused"; pass=$((pass+1)); else echo "FAIL  anonymous :: $anon"; fail=$((fail+1)); fi
anon=$(spacetime call --anonymous -s "$SRV" "$DB" mark_payment_paid 4 '"x"' '"tr_x"' 2>&1)
if grep -q "not a service identity" <<<"$anon"; then echo "PASS  anonymous mark_payment_paid refused"; pass=$((pass+1)); else echo "FAIL  anonymous :: $anon"; fail=$((fail+1)); fi
echo "---- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
