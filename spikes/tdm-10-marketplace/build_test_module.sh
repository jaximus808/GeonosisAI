#!/usr/bin/env bash
# Assemble an isolated test module from the committed module (HEAD) plus the
# CURRENT TDM-10 reducer file. Only the lease TTLs are shortened (60 s -> 3 s,
# 10 min -> 5 s) so expiry can be tested quickly. Usage: build_test_module.sh <outdir>
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/../.." && pwd)"; out="$1"
rm -rf "$out"; mkdir -p "$out/src/reducers"
# TDM-15: the committed module grew (index.ts re-exports every reducer file and
# they share event_log.ts), so take all of HEAD's src, not just the core files.
git -C "$repo" archive HEAD spacetime/spacetimedb/src | tar -x --strip-components=2 -C "$out"
grep -q "reducers/marketplace'" "$out/src/index.ts" || echo "export * from './reducers/marketplace';" >> "$out/src/index.ts"
echo "export * from './tdm10_test_seed';" >> "$out/src/index.ts"
sed -e 's/^const TASK_LEASE_MICROS = 10n \* 60n \* 1_000_000n;/const TASK_LEASE_MICROS = 5n * 1_000_000n;/' \
    -e 's/^const CONTROL_LEASE_MICROS = 60n \* 1_000_000n;/const CONTROL_LEASE_MICROS = 3n * 1_000_000n;/' \
    "$repo/spacetime/spacetimedb/src/reducers/marketplace.ts" > "$out/src/reducers/marketplace.ts"
grep -q 'TASK_LEASE_MICROS = 5n' "$out/src/reducers/marketplace.ts"
grep -q 'CONTROL_LEASE_MICROS = 3n' "$out/src/reducers/marketplace.ts"
cp "$here/tdm10_test_seed.ts" "$out/src/"
cp "$repo/spacetime/spacetimedb/package.json" "$repo/spacetime/spacetimedb/tsconfig.json" "$out/"
ln -s "$repo/spacetime/spacetimedb/node_modules" "$out/node_modules"
