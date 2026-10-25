#!/usr/bin/env python3
"""TDM-122: policy dry run (nothing executed). Thin wrapper; the code is rdf_robot_client.tdm122_dryrun.

Laptop (from the repo root):
    uv run --project services/robot-client python scripts/real/policy_dryrun.py --source recorded:<episode dir> --backend fake
Jetson (the jetson-deploy bundle ships services/robot-client, not scripts/):
    cd ~/rdf/app/services/robot-client && uv run python -m rdf_robot_client.tdm122_dryrun --help
Runbook: docs/tdm122-real-policy-runbook.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "robot-client" / "src"))

from rdf_robot_client.tdm122_dryrun import _main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_main())
