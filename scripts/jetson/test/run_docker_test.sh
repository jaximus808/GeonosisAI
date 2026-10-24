#!/usr/bin/env bash
# shellcheck disable=SC2086  # JETSON_SSH_OPTS is a word list on purpose
# TDM-44: run the laptop -> Jetson flow against an arm64 Ubuntu 22.04 container with systemd + sshd.
#
#   scripts/jetson/test/run_docker_test.sh            # build, dry-run, bootstrap, re-run (idempotency), status, logs
#   KEEP=1 scripts/jetson/test/run_docker_test.sh     # leave the container running afterwards
#   SERVICE_MODE=sim ... (default off)                # sim mode starts a real client: it registers a
#                                                     # test.jetson-sim-* robot on whatever SPACETIME_HOST
#                                                     # .env names, so only use it against a local DB
#                                                     # (RDF_DOTENV=<file pointing at a local SpacetimeDB>)
# What this proves: the scripts, idempotency, dry-run, env push, systemd unit loading, uv + Python 3.11
# + the robot-client venv on aarch64 Linux. What it cannot: L4T/JetPack specifics, CAN, cameras, udev.
# The container gets the robot-client keys from .env pushed into it; it is removed at the end.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
WORK="$REPO/spikes/tdm-44-jetson/tmp/docker"   # gitignored (tmp/)
NAME=tdm44-jetson-standin
PORT="${PORT:-2244}"
mkdir -p "$WORK"
[ -f "$WORK/id_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -C tdm44-test -f "$WORK/id_ed25519"
cp "$WORK/id_ed25519.pub" "$WORK/authorized_keys"
cp "$HERE/Dockerfile" "$WORK/Dockerfile"

echo "== build image"
docker build -q -t tdm44-jetson-standin "$WORK" >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
echo "== start container (systemd as PID 1, ssh on 127.0.0.1:$PORT)"
docker run -d --name "$NAME" --hostname tdm44-standin --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -p "127.0.0.1:$PORT:22" tdm44-jetson-standin >/dev/null
cleanup() { [ "${KEEP:-0}" = 1 ] && { echo "container $NAME left running (KEEP=1)"; return; }; docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

export JETSON_HOST=127.0.0.1 JETSON_PORT="$PORT" JETSON_USER=jetson JETSON_SSH_KEY="$WORK/id_ed25519"
export JETSON_SSH_OPTS="-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
export JETSON_SRC="${JETSON_SRC:-worktree}" JETSON_SERVICE_MODE="${SERVICE_MODE:-off}"
for _ in $(seq 30); do ssh -p "$PORT" -i "$JETSON_SSH_KEY" $JETSON_SSH_OPTS -o BatchMode=yes jetson@127.0.0.1 true 2>/dev/null && break; sleep 1; done

J="$REPO/scripts/jetson/jetson.sh"
redact() { sed -E 's/eyJ[A-Za-z0-9_-]{4}[A-Za-z0-9._-]*/<JWT-REDACTED>/g'; }   # belt and braces: nothing should print one

echo; echo "== 1. bootstrap --dry-run against a fresh box (must change nothing)"
"$J" bootstrap --dry-run 2>&1 | redact
ssh -p "$PORT" -i "$JETSON_SSH_KEY" $JETSON_SSH_OPTS jetson@127.0.0.1 'test ! -e /etc/rdf && test ! -e ~/rdf && echo "dry-run left no trace: OK"'

echo; echo "== 2. bootstrap (real)"
t0=$(date +%s)
"$J" bootstrap -- --no-policy 2>&1 | redact
echo "first bootstrap: $(( $(date +%s) - t0 )) s"

echo; echo "== 3. bootstrap again (idempotency: expect no changes, no restart)"
t0=$(date +%s)
"$J" bootstrap -- --no-policy 2>&1 | redact | tee "$WORK/second-run.log"
echo "second bootstrap: $(( $(date +%s) - t0 )) s"
grep -q 'nothing to change' "$WORK/second-run.log" && echo "idempotent system setup: OK" || echo "idempotent system setup: CHANGED SOMETHING (see above)"
grep -q 'unchanged on the Jetson' "$WORK/second-run.log" && echo "idempotent env push: OK"

echo; echo "== 4. status / logs / env (names only) / wrapper"
"$J" status 2>&1 | redact
FOLLOW=0 LINES=20 "$J" logs 2>&1 | redact || true
ssh -p "$PORT" -i "$JETSON_SSH_KEY" $JETSON_SSH_OPTS jetson@127.0.0.1 'robot-client env; robot-client home; echo "home exit=$?"; systemctl cat robot-client | head -n 30; systemd-analyze verify /etc/systemd/system/robot-client.service /etc/systemd/system/rdf-can.service && echo "systemd-analyze verify: OK"' 2>&1 | redact || true
echo; echo "== 5. deploy --dry-run"
"$J" deploy --dry-run 2>&1 | redact
echo "done"
