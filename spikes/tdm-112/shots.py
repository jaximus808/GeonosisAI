"""TDM-112 factory-floor screenshots (one headless Chromium; never prints secrets).

  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-112/shots.py <base_url> <tag> <path,...> \
      [--widths 1440,390] [--station N] [--login] [--approve] [--scroll-floor] [--fps]

--login     operator login at /ops/login with RDF_OPERATOR_PASSCODE read from the repo .env (not printed)
--approve   click "Approve plan" on the ticket page (needs --login)
--station N click station N (0-7) and capture the drawer too
--fps       measure requestAnimationFrame rate for 3 s on each page
Writes spikes/tdm-112/tmp/<tag>-<path>-<width>.png and prints overflow checks plus the floor's text.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "tmp"
REPO = HERE.parents[1]


def passcode() -> str:
    for line in (REPO / ".env").read_text().splitlines():
        if line.startswith("RDF_OPERATOR_PASSCODE="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("RDF_OPERATOR_PASSCODE missing from .env")


ap = argparse.ArgumentParser()
ap.add_argument("base")
ap.add_argument("tag")
ap.add_argument("paths")
ap.add_argument("--widths", default="1440,390")
ap.add_argument("--station", type=int, default=None)
ap.add_argument("--login", action="store_true")
ap.add_argument("--approve", action="store_true")
ap.add_argument("--fps", action="store_true")
ap.add_argument("--wait", type=int, default=5000)
a = ap.parse_args()
OUT.mkdir(parents=True, exist_ok=True)
base = a.base.rstrip("/")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"])
    for w in [int(x) for x in a.widths.split(",")]:
        h = 1080 if w >= 1600 else 900 if w >= 1000 else 844
        ctx = b.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, color_scheme="dark")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: print("   PAGEERROR:", str(e)[:300]))
        pg.on("console", lambda m: m.type == "error" and print("   console.error:", m.text[:300]))
        if a.login:
            pg.goto(base + "/ops/login", wait_until="load", timeout=60000)
            pg.fill("input[type=password], input[name=passcode]", passcode())
            pg.keyboard.press("Enter")
            pg.wait_for_timeout(2500)
        for path in a.paths.split(","):
            pg.goto(base + path, wait_until="domcontentloaded", timeout=90000)
            pg.wait_for_timeout(a.wait)
            if a.approve:
                btn = pg.get_by_role("button", name="Approve plan")
                if btn.count():
                    btn.first.click()
                    print("clicked Approve plan")
                    pg.wait_for_timeout(4000)
                else:
                    print("no Approve plan button")
            over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            name = path.strip("/").replace("/", "_").replace("?", "_").replace("=", "") or "home"
            f = OUT / f"{a.tag}-{name}-{w}.png"
            pg.screenshot(path=str(f), full_page=False, timeout=90000)
            floor = pg.evaluate("(document.querySelector('[data-testid=factory-floor]')||{}).innerText||''")
            print(f"{w} {path} overflow={over}px {f}")
            if floor:
                print("   floor:", " | ".join(x for x in floor.split("\n") if x.strip())[:900])
            if a.fps:
                fps = pg.evaluate("""() => new Promise(r => { let n = 0; const t0 = performance.now();
                    const f = () => { n++; if (performance.now() - t0 < 3000) requestAnimationFrame(f); else r(n / ((performance.now() - t0) / 1000)); };
                    requestAnimationFrame(f); })""")
                print(f"   fps={fps:.1f}")
            if a.station is not None:
                cards = pg.locator("[data-testid=factory-floor] .tdm112-card")
                if cards.count() > a.station:
                    cards.nth(a.station).click()
                    pg.wait_for_timeout(2500)
                    f2 = OUT / f"{a.tag}-{name}-{w}-drawer{a.station}.png"
                    pg.screenshot(path=str(f2), full_page=False, timeout=90000)
                    print(f"   drawer -> {f2}")
                    pg.keyboard.press("Escape")
        ctx.close()
    b.close()
