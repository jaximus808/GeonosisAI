#!/usr/bin/env python3
"""Warm or cool the deployed rdf-s05b-policy endpoint without redeploying.

  uv run --with modal python spikes/tdm-37-latency/warm.py up     # min_containers=1: one GPU kept hot (billed while up)
  uv run --with modal python spikes/tdm-37-latency/warm.py down   # min_containers=0: scale to zero after scaledown_window
  uv run --with modal python spikes/tdm-37-latency/warm.py status

Run `up` roughly 3-5 minutes before a robot session (cold start = container boot + 12 GB checkpoint
read from the Volume + JIT warmup), and `down` right after. Needs the Modal CLI login on this machine.
"""

import sys

import modal

policy = modal.Cls.from_name("rdf-s05b-policy", "Policy")()
cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
if cmd == "up":
    policy.update_autoscaler(min_containers=1)
    print("min_containers=1 (warming; poll GET /health)")
elif cmd == "down":
    policy.update_autoscaler(min_containers=0)
    print("min_containers=0 (scales to zero after the idle window)")
else:
    print("web url:", policy.web.get_web_url())
    print("containers: see `modal container list` or the Modal dashboard")
