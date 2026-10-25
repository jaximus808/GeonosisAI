"""TDM-116 ticket cockpit screenshots and checks (one headless Chromium, GPU flags; never prints secrets).

  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-116/cockpit_shots.py <base_url> <tag> <mode> [opts]

modes
  shot   <path>                         screenshot <path> at every --sizes; report page scroll and overflow
  pins   <path>                         click each station on the line: screenshot the pinned view, then Back to live
  watch  <path> --until shipped         keep one page open; whenever the cockpit's live stage changes, screenshot it
                                        at every --sizes (proves the stage follows the ticket on its own)
  tabs   <path>                         screenshot every rail tab
  cancel <path>                         More -> Cancel ticket -> confirm (needs --login); screenshots the dialog and after
options
  --sizes 1440x900,1280x800,390x844     --login (operator login, RDF_OPERATOR_PASSCODE from the repo .env)
  --approve (watch: click the stage's Approve plan when it appears)   --timeout S
Writes spikes/tdm-116/tmp/<tag>-<name>-<w>x<h>.png.
"""
from __future__ import annotations

import argparse
import time
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
ap.add_argument("mode", choices=["shot", "pins", "watch", "tabs", "cancel"])
ap.add_argument("path")
ap.add_argument("--sizes", default="1440x900,1280x800,390x844")
ap.add_argument("--login", action="store_true")
ap.add_argument("--approve", action="store_true")
ap.add_argument("--until", default="shipped")
ap.add_argument("--timeout", type=int, default=1800)
ap.add_argument("--wait", type=int, default=4000)
ap.add_argument("--every", type=int, default=0, help="watch: also screenshot every N seconds")
a = ap.parse_args()
OUT.mkdir(parents=True, exist_ok=True)
base = a.base.rstrip("/")
sizes = [tuple(int(v) for v in s.split("x")) for s in a.sizes.split(",")]

CHECK = """() => {
  const d = document.documentElement;
  const c = document.querySelector('[data-testid=ticket-cockpit]');
  return { vScroll: d.scrollHeight - d.clientHeight, hScroll: d.scrollWidth - d.clientWidth,
           stage: c && c.getAttribute('data-stage'), live: c && c.getAttribute('data-live-stage'), tab: c && c.getAttribute('data-tab'),
           status: (document.querySelector('[data-testid=cockpit-top] .gx-tag')||{}).textContent || '' };
}"""


def shoot(pg, name: str, w: int, h: int) -> None:
    pg.set_viewport_size({"width": w, "height": h})
    pg.wait_for_timeout(1200)
    info = pg.evaluate(CHECK)
    f = OUT / f"{a.tag}-{name}-{w}x{h}.png"
    pg.screenshot(path=str(f), full_page=False, timeout=90000)
    flag = "" if w < 900 or (info["vScroll"] <= 0 and info["hScroll"] <= 0) else "  <-- PAGE SCROLLS"
    print(f"{w}x{h} {name}: stage={info['stage']} live={info['live']} tab={info['tab']} status={info['status']} "
          f"vScroll={info['vScroll']} hScroll={info['hScroll']}{flag} -> {f}", flush=True)


with sync_playwright() as p:
    b = p.chromium.launch(args=["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"])
    w0, h0 = sizes[0]
    ctx = b.new_context(viewport={"width": w0, "height": h0}, device_scale_factor=1, color_scheme="dark")
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: print("   PAGEERROR:", str(e)[:300], flush=True))
    if a.login:
        pg.goto(base + "/ops/login", wait_until="load", timeout=90000)
        pg.fill("input[type=password], input[name=passcode]", passcode())
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(2500)
    pg.goto(base + a.path, wait_until="domcontentloaded", timeout=120000)
    pg.wait_for_selector("[data-testid=ticket-cockpit][data-stage]", timeout=120000)
    pg.wait_for_timeout(a.wait)
    name = a.path.strip("/").replace("/", "_").replace("?", "_").replace("=", "").replace("&", "_") or "home"

    if a.mode == "shot":
        for w, h in sizes:
            shoot(pg, name, w, h)
    elif a.mode == "tabs":
        for tab in ["overview", "robot", "data", "money", "agents", "events", "settings", "report"]:
            pg.set_viewport_size({"width": w0, "height": h0})
            pg.locator(f".tdm116-tab[data-tab={tab}]").click()
            pg.wait_for_timeout(3000)
            for w, h in sizes:
                shoot(pg, f"{name}-tab-{tab}", w, h)
        pg.locator(".tdm116-tab[data-tab=overview]").click()
    elif a.mode == "cancel":
        pg.locator("[data-testid=tdm116-menu]").click()
        pg.wait_for_timeout(400)
        shoot(pg, f"{name}-cancel-menu", w0, h0)
        pg.locator("[data-testid=tdm116-menu-cancel]").click()
        pg.wait_for_timeout(600)
        pg.fill(".tdm116-dialog textarea", "TDM-116 test: cancel from the cockpit")
        shoot(pg, f"{name}-cancel-dialog", w0, h0)
        pg.locator("[data-testid=tdm116-cancel-confirm]").click()
        for _ in range(40):
            pg.wait_for_timeout(1000)
            if "Cancelled" in pg.evaluate(CHECK)["status"] and pg.locator("text=Robot released").count():
                break
        print("released tag:", pg.locator("text=Robot released").count(), "releasing tag:", pg.locator("text=Releasing robot").count(), flush=True)
        shoot(pg, f"{name}-cancel-after", w0, h0)
    elif a.mode == "pins":
        live = pg.evaluate(CHECK)["live"]
        keys = pg.eval_on_selector_all("[data-testid=cockpit-line] .tdm116-st", "els => els.map(e => e.dataset.station)")
        for k in keys:
            pg.set_viewport_size({"width": w0, "height": h0})
            pg.locator(f"[data-testid=cockpit-line] .tdm116-st[data-station={k}]").click()
            pg.wait_for_timeout(1800)
            pinned = pg.locator("[data-testid=cockpit-pinned]").count() > 0
            info = pg.evaluate(CHECK)
            print(f"clicked {k}: stage={info['stage']} pinned_label={pinned} (live {live})", flush=True)
            for w, h in sizes:
                shoot(pg, f"{name}-pin-{k}", w, h)
            if pinned:
                pg.set_viewport_size({"width": w0, "height": h0})
                pg.locator("[data-testid=cockpit-back-to-live]").click()
                pg.wait_for_timeout(800)
                print(f"   back to live: stage={pg.evaluate(CHECK)['stage']}", flush=True)
    else:  # watch
        seen: set[str] = set()
        last = None
        t0 = time.time()
        last_every = t0
        while time.time() - t0 < a.timeout:
            info = pg.evaluate(CHECK)
            key = f"{info['live']}|{info['status']}"
            if key != last:
                last = key
                print(f"[{int(time.time() - t0)}s] live stage -> {info['live']} ({info['status']}), stage shown {info['stage']}", flush=True)
                pg.wait_for_timeout(2500)
                tag = f"{name}-live-{info['live']}-{info['status'].strip().lower().replace(' ', '_')}"
                n = sum(1 for s in seen if s.startswith(tag))
                seen.add(f"{tag}-{n}")
                for w, h in sizes:
                    shoot(pg, f"{tag}{'-' + str(n) if n else ''}", w, h)
                pg.set_viewport_size({"width": w0, "height": h0})
            if a.every and time.time() - last_every >= a.every:
                last_every = time.time()
                shoot(pg, f"{name}-t{int(last_every - t0):04d}-{info['live']}", w0, h0)
            if a.approve:
                btn = pg.locator("[data-testid=stage-plan] [data-testid=tdm116-primary]")
                if btn.count() and btn.first.is_enabled() and "Approve" in (btn.first.text_content() or ""):
                    pg.wait_for_timeout(2000)
                    shoot(pg, f"{name}-plan-before-approve", w0, h0)
                    btn.first.click()
                    print(f"[{int(time.time() - t0)}s] clicked {btn.first.text_content()}", flush=True)
                    pg.wait_for_timeout(3000)
            if a.until in (info["status"] or "").lower():
                pg.wait_for_timeout(4000)
                for w, h in sizes:
                    shoot(pg, f"{name}-final", w, h)
                break
            pg.wait_for_timeout(1500)
    ctx.close()
    b.close()
