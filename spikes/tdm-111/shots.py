"""TDM-111 landing-page screenshots at 390 and 1440 (one headless Chromium, no secrets).

  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-111/shots.py <base_url> [tag]

Writes spikes/tdm-111/tmp/<tag>-<path>-<width>.png and prints horizontal-overflow checks.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "tmp"
base = sys.argv[1].rstrip("/")
tag = sys.argv[2] if len(sys.argv) > 2 else "local"
pages = sys.argv[3].split(",") if len(sys.argv) > 3 else ["/", "/join", "/ops/login"]
OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    for w, h in ((390, 844), (1440, 900)):
        ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, color_scheme="dark")
        pg = ctx.new_page()
        for path in pages:
            pg.goto(base + path, wait_until="networkidle", timeout=60000)
            pg.wait_for_timeout(2500)
            over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            name = (path.strip("/").replace("/", "_") or "home")
            f = OUT / f"{tag}-{name}-{w}.png"
            pg.screenshot(path=str(f), full_page=True)
            live = pg.evaluate("(document.querySelector('[data-testid=tdm111-live]')||{}).innerText||''")
            print(f"{w} {path} -> {pg.url} overflow={over}px {f}")
            if live:
                print("   live:", " | ".join(live.split("\n"))[:300])
        ctx.close()
    b.close()
