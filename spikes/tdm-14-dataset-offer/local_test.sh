#!/usr/bin/env bash
# TDM-14 local test of the dataset-offer / QA reducers and the N - existing cap.
# Needs a throwaway local server with the module PLUS the test-only seed reducers
# (tdm_14_seed, tdm_14_test_review; see seed.ts next to this script) published as $DB.
#   spacetime start --listen-addr 127.0.0.1:3014 --data-dir "$TMPDIR/tdm14"
#   (copy the module, add seed.ts as src/reducers/tdm14_test_seed.ts + re-export) and
#   spacetime publish --no-config -s http://127.0.0.1:3014 -p <copy> tdm14test --yes
set -u
SRV=${SRV:-http://127.0.0.1:3014}
DB=${DB:-tdm14test}
pass=0; fail=0
call() { spacetime call -s "$SRV" "$DB" "$@" 2>&1; }
ok()   { local d=$1; shift; if out=$(call "$@") && ! grep -qi error <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
bad()  { local d=$1 want=$2; shift 2; out=$(call "$@"); if grep -q "$want" <<<"$out"; then echo "PASS  $d -> $(grep -o "$want.*" <<<"$out" | head -1 | cut -c1-140)"; pass=$((pass+1)); else echo "FAIL  $d (wanted '$want') :: $out"; fail=$((fail+1)); fi; }
sql()  { spacetime sql -s "$SRV" "$DB" "$1" 2>&1; }
want() { local d=$1 q=$2 pat=$3; out=$(sql "$q"); if grep -Eq "$pat" <<<"$out"; then echo "PASS  $d"; pass=$((pass+1)); else echo "FAIL  $d :: $out"; fail=$((fail+1)); fi; }
NONE='{"none":[]}'
EP() { echo "\"s3://test/purchased/$1\"" '"s3://test/actions.parquet"' '"{}"' 300 10000 "$NONE"; }

# target_episodes = 5, two clean_teleop episodes already accepted. Seller = contributor 1, person = 2.
ok   "seed ticket (target 5, 2 accepted)" tdm_14_seed 5 2
bad  "offer to a non-seller is refused"    "not dataset_seller" create_dataset_offer 1 2 500 "$NONE"
ok   "create offer"                          create_dataset_offer 1 1 500 '{"some":"hf://seller/ds"}'
want "needed_count = 5 - 2 = 3"            "SELECT needed_count, status FROM dataset_offer WHERE id = 1" "^ +3 +\| +\"proposed\""
bad  "2nd offer refused: 2 existing + 3 reserved = 5" "no episodes needed" create_dataset_offer 1 1 500 "$NONE"
bad  "ingest before accept is refused"     "ingest needs" ingest_purchased_episode 1 $(EP a)
ok   "seller offer accepted"                 accept_dataset_offer 1
bad  "accept twice is refused"              "not proposed" accept_dataset_offer 1
ok   "ingest 1/3"                            ingest_purchased_episode 1 $(EP 1)
ok   "ingest 2/3"                            ingest_purchased_episode 1 $(EP 2)
ok   "ingest 3/3"                            ingest_purchased_episode 1 $(EP 3)
bad  "4th ingest is rejected"               "is full: 3/3" ingest_purchased_episode 1 $(EP 4)
want "offer ingested_count 3, status qa"    "SELECT ingested_count, status FROM dataset_offer WHERE id = 1" "^ +3 +\| +\"qa\""
want "dataset.total_count 2 -> 5"           "SELECT total_count FROM dataset WHERE ticket_id = 1" "^ +5 *$"
# episodes 3,4,5 are the purchased ones
ok   "qa ep3 jerk pass"                      record_qa_result 3 '"jerk"' true '"{\"max_jerk\":0.4}"'
ok   "qa ep3 frame_drops pass"               record_qa_result 3 '"frame_drops"' true '"{\"drops\":0}"'
ok   "qa ep4 jerk pass"                      record_qa_result 4 '"jerk"' true '"{}"'
ok   "qa ep5 jerk FAIL"                      record_qa_result 5 '"jerk"' false '"{\"max_jerk\":9.1}"'
bad  "qa with bad JSON refused"            "valid JSON" record_qa_result 5 '"jerk"' false '"not json"'
want "qa rows queryable per episode"        "SELECT check_name, passed FROM qa_result WHERE episode_id = 3" "frame_drops"
bad  "finalize refused while ep3/ep4 lack a review" "without a QA verdict" finalize_offer 1
ok   "ep5 failed QA so its slot frees: replacement ingest" ingest_purchased_episode 1 $(EP 5b)
bad  "but still capped at 3 live"           "is full: 3/3" ingest_purchased_episode 1 $(EP 6)
ok   "qa ep6 pass"                           record_qa_result 6 '"jerk"' true '"{}"'
ok   "review ep3 accept"                     tdm_14_test_review 3 true
ok   "review ep4 accept"                     tdm_14_test_review 4 true
ok   "review ep6 reject (LLM reviewer)"      tdm_14_test_review 6 false
ok   "finalize"                              finalize_offer 1
want "accepted_count 2, status done"        "SELECT accepted_count, ingested_count, status FROM dataset_offer WHERE id = 1" "^ +2 +\| +4 +\| +\"done\""
want "settled event logs payout 2 x 500"    "SELECT payload_json FROM event_log WHERE type = 'dataset_offer.settled'" 'payout_cents\\?":\\?"1000'
bad  "finalize twice refused"               "cannot finalize" finalize_offer 1
ok   "new offer after settle"                create_dataset_offer 1 1 500 "$NONE"
want "needed = 5 - (2 base + 2 bought) = 1" "SELECT needed_count FROM dataset_offer WHERE id = 2" "^ +1 *$"
ok   "cancel it"                             cancel_dataset_offer 2 '"test.cancel"'
want "event_log rows written"               "SELECT type FROM event_log WHERE type = 'dataset_offer.episode_ingested'" "episode_ingested"
# Ticket 2: target 3, 0 accepted. Offer takes 3, then 2 contributor uploads land: only 1 purchase fits.
ok   "seed ticket 2 (target 3, 0 accepted)" tdm_14_seed 3 0
ok   "offer 3 on ticket 2"                   create_dataset_offer 2 1 500 "$NONE"
want "needed_count 3"                       "SELECT needed_count FROM dataset_offer WHERE id = 3" "^ +3 *$"
ok   "accept offer 3"                        accept_dataset_offer 3
ok   "contributor upload 1 lands"            tdm_14_test_upload 2
ok   "contributor upload 2 lands"            tdm_14_test_upload 2
ok   "1 purchase still fits (2 + 1 = 3)"     ingest_purchased_episode 3 $(EP t2a)
bad  "next purchase refused: ticket at target" "already has its 3 target episodes" ingest_purchased_episode 3 $(EP t2b)
anon=$(spacetime call --anonymous -s "$SRV" "$DB" create_dataset_offer 1 1 500 "$NONE" 2>&1)
if grep -q "not a service identity" <<<"$anon"; then echo "PASS  anonymous caller refused"; pass=$((pass+1)); else echo "FAIL  anonymous :: $anon"; fail=$((fail+1)); fi
echo "---- $pass passed, $fail failed"
[ "$fail" -eq 0 ]
