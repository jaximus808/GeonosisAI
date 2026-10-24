#!/bin/bash
# TDM-112: walk one ticket through baseline -> diagnose -> plan -> approve on a LOCAL make demo and
# screenshot the floor at each stage. Usage: spikes/tdm-112/stages.sh <web> <orch> <stdb_http> <db>
set -u
WEB=$1; ORCH=$2; STDB=$3; DB=$4
cd "$(dirname "$0")/../.."
SHOT="uv run --no-project --python 3.11 --with playwright==1.58.0 python -u -I spikes/tdm-112/shots.py"
status() { curl -s -X POST "$STDB/v1/database/$DB/sql" -d "SELECT status FROM ticket WHERE id = $1" | python3 -c 'import json,sys; r=json.load(sys.stdin)[0]["rows"]; print(r[0][0] if r else "")'; }
tid=$(curl -s -X POST "$ORCH/tickets" -H 'content-type: application/json' -d '{"prompt":"Pick up the cube and place it in the empty plate, 80% reliable, $40 budget","parsed":{"task_description":"Pick up the cube and place it in the empty plate.","target_success":0.8,"budget_usd":40,"policy_prompt":"put the cube on the plate","eval_trials":10},"settings_overrides":{"eval_trials":10,"max_iterations":3}}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["ticket_id"])')
echo "ticket $tid"
sleep 12
$SHOT "$WEB" s1-baseline "/tickets/$tid,/factory,/live" --widths 1440 --wait 4000 --station 0 2>&1 | grep -E "overflow|floor:|drawer|fps|PAGEERROR: [^H]" | cut -c1-600
$SHOT "$WEB" s1-baseline "/tickets/$tid" --widths 390 --wait 4000 2>&1 | grep -E "overflow|floor:" | cut -c1-300
until [ "$(status $tid)" != "baseline" ]; do sleep 2; done; echo "now $(status $tid)"
$SHOT "$WEB" s2-diagnose "/tickets/$tid,/factory" --widths 1440 --wait 3000 2>&1 | grep -E "overflow|floor:" | cut -c1-600
until [ "$(status $tid)" = "plan_data" ]; do sleep 2; done
until curl -s -X POST "$STDB/v1/database/$DB/sql" -d "SELECT status FROM data_plan WHERE ticket_id = $tid" | grep -q proposed; do sleep 2; done; echo "plan proposed"
$SHOT "$WEB" s3-plan "/tickets/$tid,/live" --widths 1440 --wait 5000 --station 2 2>&1 | grep -E "overflow|floor:|drawer" | cut -c1-600
$SHOT "$WEB" s4-approve "/tickets/$tid" --widths 1440 --wait 5000 --login --approve 2>&1 | grep -E "overflow|floor:|Approve|clicked" | cut -c1-600
sleep 20; echo "now $(status $tid)"
$SHOT "$WEB" s5-collect "/tickets/$tid,/factory,/live" --widths 1440 --wait 5000 --station 3 2>&1 | grep -E "overflow|floor:|drawer" | cut -c1-600
$SHOT "$WEB" s5-collect "/tickets/$tid,/factory" --widths 390 --wait 5000 2>&1 | grep -E "overflow|floor:" | cut -c1-300
echo done
