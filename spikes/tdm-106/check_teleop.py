"""TDM-106: the teleop console against the TDM-56 mock robot (spikes/tdm-56/mock_control_server.py).
Checks the controls are on screen without opening anything, a held W drives (no Space), G sends a
gripper change with zero motion, Esc stops. Screenshot to the output dir.

  services/robot-client/.venv/bin/python spikes/tdm-56/mock_control_server.py --port 8166 &
  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-106/check_teleop.py http://127.0.0.1:3737 8166 spikes/tdm-106/tmp
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


def stats(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/debug/stats", timeout=5) as r:
        return json.loads(r.read())


def main(base, port, out):
    Path(out).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{base}/teleop/0?dev_url=ws://127.0.0.1:{port}/ws/cameras&token=mock", wait_until="networkidle", timeout=180_000)
        page.wait_for_selector('[data-testid="tdm106-legend"]', timeout=60_000)
        # dismiss the first-visit walkthrough if it is up
        if page.query_selector('[data-testid="tdm59-intro"]'):
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        page.wait_for_function("() => document.querySelector('[data-testid=\"tdm56-link-state\"]')?.textContent === 'Connected'", timeout=30_000)
        page.wait_for_timeout(1500)
        vis = page.evaluate("""() => ['tdm106-legend','tdm56-onscreen','tdm56-grip-toggle','tdm56-clutch-button'].map(id => {
            const e = document.querySelector(`[data-testid="${id}"]`); if (!e) return [id, 'missing'];
            const r = e.getBoundingClientRect(); return [id, r.top < innerHeight ? 'in view' : `below fold (${Math.round(r.top)})`] })""")
        print("visible:", vis)
        s0 = stats(port)
        page.keyboard.down("KeyW")
        page.wait_for_timeout(1200)
        st = page.evaluate("() => document.querySelector('[data-testid=\"tdm56-clutch-status\"]').dataset.state")
        lit = page.evaluate("() => document.querySelector('[data-testid=\"tdm106-key-KeyW\"]').dataset.held")
        page.screenshot(path=f"{out}/teleop-driving.png")
        page.keyboard.up("KeyW")
        page.wait_for_timeout(500)
        s1 = stats(port)
        print("holding W: status", st, "| W keycap lit", lit, "| cmds", s0.get("cmds"), "->", s1.get("cmds"), "| last", json.dumps(s1.get("last_cmd") or s1.get("last"))[:200])
        page.keyboard.press("g")
        page.wait_for_timeout(600)
        s2 = stats(port)
        print("after G:", json.dumps(s2.get("last_cmd") or s2.get("last"))[:200])
        page.wait_for_timeout(800)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        s3 = stats(port)
        print("after Esc: stop log", json.dumps(s3.get("stops") or s3.get("stop_log"))[:200])
        page.screenshot(path=f"{out}/teleop-stopped.png")
        b.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
