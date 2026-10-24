"""TDM-99: does the ghost (chunk_preview on /ws/state) LEAD the arm? Reads the running stack's feed.

    cd services/orchestrator && uv run python ../../spikes/tdm-99-hud/ghost_lead.py [--seconds 5]

For every state that carries chunk_preview, compare the preview's rows with the joints the arm actually
reaches later (the measured joints of later states). "Leads" means: the arm's future joints are closer
to the preview than its current joints are, and the best-matching future time is > 0.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent / "tmp"


async def read(url: str, token: str, seconds: float) -> list[dict]:
    import time

    import websockets

    out = []
    async with websockets.connect(url, max_size=1 << 20) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        json.loads(await ws.recv())
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if m.get("t") == "state":
                out.append(m)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0)
    a = ap.parse_args()
    st = json.loads((OUT / "stack.json").read_text())
    states = asyncio.run(read(st["state_url"], st["view_token"], a.seconds))
    t = np.array([s["t_ms"] for s in states], float)
    q = np.array([s["joints"] for s in states], float)
    rows = []
    for i, s in enumerate(states):
        pv = s.get("chunk_preview")
        if not pv or s["control_source"] != "policy":
            continue
        mid = np.asarray(pv[len(pv) // 2][:6])
        now_err = float(np.linalg.norm(q[i] - mid))
        fut = [(t[j] - t[i], float(np.linalg.norm(q[j] - mid))) for j in range(i, len(states)) if t[j] - t[i] <= 2000]
        if len(fut) < 10 or fut[-1][0] < 700:
            continue
        best_dt, best_err = min(fut, key=lambda x: x[1])
        rows.append((now_err, best_dt, best_err))
    r = np.array(rows) if rows else np.zeros((0, 3))
    res = {
        "states": len(states), "with_preview": sum(1 for s in states if s.get("chunk_preview")),
        "scored": len(r),
        "median_now_err_rad": round(float(np.median(r[:, 0])), 4) if len(r) else None,
        "median_best_future_err_rad": round(float(np.median(r[:, 2])), 4) if len(r) else None,
        "median_lead_ms": round(float(np.median(r[:, 1])), 1) if len(r) else None,
        "share_leading": round(float(np.mean(r[:, 1] > 0)), 3) if len(r) else None,
        "preview_points": len(next((s["chunk_preview"] for s in states if s.get("chunk_preview")), [])),
    }
    print(json.dumps(res, indent=1))
    (OUT / "ghost_lead.json").write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
