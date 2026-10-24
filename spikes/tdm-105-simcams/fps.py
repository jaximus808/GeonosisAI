"""TDM-105: headless fps + screenshots of the rendered sim cameras (GPU Chromium, Metal).

  uv run --no-project --python 3.11 --with playwright==1.58.0 \
      python -I spikes/tdm-105-simcams/fps.py http://127.0.0.1:3815 spikes/tdm-105-simcams/tmp [/teleop/1]

Measures window.__rdfSimCams.fps() (the page's render loop; every pane's canvas draws on it) on
/dev/simcams (fixture playback) at desktop 1440x900@2x and phone 390x844@3x, and on an optional extra
path (e.g. the teleop page of the proof stack, with a browser token file in $TDM105_BROWSER_TOKEN_FILE).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]


def fps(page, seconds=5):
    page.wait_for_function("() => window.__rdfSimCams && document.querySelectorAll('[data-sim-camera] canvas').length >= 3",
                           timeout=90000)
    page.wait_for_timeout(3000)
    out = []
    for _ in range(seconds):
        page.wait_for_timeout(1000)
        out.append(page.evaluate("() => window.__rdfSimCams.fps()"))
    return out


def main() -> None:
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    extra = sys.argv[3] if len(sys.argv) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=GPU_ARGS)
        for name, kw in (("desktop_1440@2x", dict(viewport={"width": 1440, "height": 900}, device_scale_factor=2)),
                         ("phone_390@3x", dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True,
                                                has_touch=True))):
            ctx = b.new_context(**kw)
            page = ctx.new_page()
            page.goto(f"{base}/dev/simcams")
            rep[f"dev_simcams {name}"] = fps(page)
            page.screenshot(path=str(out / f"tdm105_simcams_{name}.png"))
            ctx.close()
        if extra:
            ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
            tok_file = os.environ.get("TDM105_BROWSER_TOKEN_FILE")
            if tok_file:
                key, tok = os.environ["TDM105_TOKEN_KEY"], Path(tok_file).read_text().strip()
                ctx.add_init_script(f"try {{ localStorage.setItem({json.dumps(key)}, {json.dumps(tok)}) }} catch (e) {{}}")
            page = ctx.new_page()
            page.goto(f"{base}{extra}")
            rep[f"{extra} desktop_1440@2x"] = fps(page)
            page.screenshot(path=str(out / "tdm105_teleop_desktop.png"), full_page=False)
            ctx.close()
        rep["gpu"] = None
        b.close()
    print(json.dumps(rep, indent=2))
    (out / "tdm105_fps.json").write_text(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
