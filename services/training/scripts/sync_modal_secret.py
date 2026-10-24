"""Create/refresh the Modal Secret `rdf-training` from the repo-root .env (TDM-38).

Only the keys below are copied, only if set, and values are never printed: the script
writes them to a 0600 temp JSON file, runs `modal secret create --from-json --force`,
and deletes the file. Output lists key NAMES only.

    uv run python scripts/sync_modal_secret.py          # from services/training
    make modal-secrets                                  # from the repo root
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

SECRET_NAME = "rdf-training"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
KEYS = [
    "HF_TOKEN",
    "WANDB_API_KEY",
    "ORCHESTRATOR_URL",
    "ORCHESTRATOR_TOKEN",
    "STORAGE_ENDPOINT",
    "STORAGE_BUCKET",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "RDF_MEDIA_BACKEND",
    "RDF_MEDIA_STORE_URL",
    "POLICY_SERVER_TOKEN",  # TDM-41 policy server bearer auth
]


def read_dotenv(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def main() -> int:
    env = read_dotenv(REPO_ROOT / ".env")
    values = {k: (os.environ.get(k) or env.get(k, "")) for k in KEYS}
    values = {k: v for k, v in values.items() if v}
    if not values:
        print("no keys set; refusing to create an empty secret", file=sys.stderr)
        return 1
    fd, tmp = tempfile.mkstemp(prefix="tdm-38-secret-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(values, f)
        r = subprocess.run(
            ["modal", "secret", "create", SECRET_NAME, "--from-json", tmp, "--force"],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        # Modal's error text never echoes values, but keep only the last line to be safe.
        print(f"modal secret create failed: {(r.stderr or r.stdout).strip().splitlines()[-1:]}", file=sys.stderr)
        return r.returncode
    print(f"secret {SECRET_NAME}: set {sorted(values)}; skipped (blank) {sorted(set(KEYS) - set(values))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
