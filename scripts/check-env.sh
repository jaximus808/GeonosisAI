#!/usr/bin/env bash
# Report which keys in .env.example are set in .env or the exported environment.
# Never prints values. Services fail fast on their own required keys at startup
# (services/common/src/rdf_common/settings.py, packages/env/index.ts).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo ".env not found (cp .env.example .env)"; exit 1; }

missing=0
while IFS= read -r key; do
  val=$(grep -E "^${key}=" .env | tail -n1 | cut -d= -f2- || true)
  if [ -n "$val" ]; then
    printf "  set      %s\n" "$key"
  elif [ -n "${!key:-}" ]; then
    printf "  set      %s (exported env)\n" "$key"
  else
    printf "  MISSING  %s\n" "$key"
    missing=$((missing + 1))
  fi
done < <(grep -E '^[A-Z0-9_]+=' .env.example | cut -d= -f1)

echo "$missing key(s) blank"
