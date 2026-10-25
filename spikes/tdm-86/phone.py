"""TDM-86: headless phone (390x844, touch, mobile UA) for the judge's-phone flow.

  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-86/phone.py '<json actions>' [--out DIR] [--landscape]

Persistent profile under spikes/tdm-86/tmp/profile (cookies + localStorage survive between runs).
Actions (one key each): {"goto": url} {"tap": selector} {"click": selector} {"fill": [selector, value]}
{"fill_env": [selector, ENV]} {"wait": ms} {"wait_for": js} {"shot": name} {"text": n} {"js": code}
{"drag": [selector, dx, dy, hold_ms]}  a one-finger touch drag from the element's centre (CDP touch events)
{"hold": [selector, ms]}               touch and hold
{"rotate": "landscape"|"portrait"}     swap the viewport
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "tmp" / "profile"
GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.5 Mobile/15E148 Safari/604.1")
PORTRAIT = {"width": 390, "height": 844}
LANDSCAPE = {"width": 844, "height": 390}


def touch(cdp, kind: str, x: float, y: float) -> None:
    pts = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1, "radiusX": 8, "radiusY": 8, "force": 1}]
    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})


def centre(page, sel: str) -> tuple[float, float]:
    b = page.locator(sel).first.bounding_box(timeout=30000)
    if not b:
        raise RuntimeError(f"no box for {sel}")
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def drag(page, cdp, sel: str, dx: float, dy: float, hold_ms: int) -> None:
    x, y = centre(page, sel)
    touch(cdp, "touchStart", x, y)
    steps = 8
    for i in range(1, steps + 1):
        touch(cdp, "touchMove", x + dx * i / steps, y + dy * i / steps)
        page.wait_for_timeout(16)
    t_end = time.time() + hold_ms / 1000
    while time.time() < t_end:  # keep the finger alive (small jitter, like a thumb)
        touch(cdp, "touchMove", x + dx, y + dy + 0.5)
        page.wait_for_timeout(50)
    touch(cdp, "touchEnd", x + dx, y + dy)


def main() -> None:
    args = sys.argv[1:]
    acts = json.loads(args[0])
    out = Path(args[args.index("--out") + 1]) if "--out" in args else HERE / "tmp" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    vp = LANDSCAPE if "--landscape" in args else PORTRAIT
    profile = Path(args[args.index("--profile") + 1]) if "--profile" in args else PROFILE
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=True, args=GPU_ARGS, viewport=vp,
                                                   device_scale_factor=2, is_mobile=True, has_touch=True, user_agent=UA)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"[:240]) if m.type == "error" else None)
        t0 = time.time()
        if "--ws" in args:  # log control-socket traffic other than state / cmd acks
            def on_ws(ws):
                def rx(d):
                    if isinstance(d, str) and '"t":"state"' not in d.replace(" ", "") and '"seq"' not in d:
                        print(f"[ws<] {d[:300]}", flush=True)
                def tx(d):
                    if isinstance(d, str) and '"cmd"' not in d and '"ping"' not in d:
                        print(f"[ws>] {d[:300]}", flush=True)
                ws.on("framereceived", rx)
                ws.on("framesent", tx)
            page.on("websocket", on_ws)
        for a in acts:
            k, v = next(iter(a.items()))
            try:
                if k == "goto":
                    page.goto(v, wait_until="domcontentloaded", timeout=180000)
                elif k == "tap":
                    page.tap(v, timeout=a.get("timeout", 30000))
                elif k == "click":
                    page.click(v, timeout=a.get("timeout", 30000))
                elif k == "fill":
                    page.fill(v[0], v[1], timeout=30000)
                elif k == "fill_env":
                    page.fill(v[0], os.environ[v[1]], timeout=30000)
                elif k == "wait":
                    page.wait_for_timeout(v)
                elif k == "wait_for":
                    page.wait_for_function(v, timeout=a.get("timeout", 120000))
                elif k == "shot":
                    path = out / f"{v}.png"
                    page.screenshot(path=str(path), full_page=bool(a.get("full")))
                    print(f"[{time.time() - t0:6.1f}s] shot: {path}", flush=True)
                elif k == "text":
                    print(page.inner_text("body")[: int(v)])
                elif k == "js":
                    print(json.dumps(page.evaluate(v), default=str)[:4000], flush=True)
                elif k == "drag":
                    drag(page, cdp, v[0], v[1], v[2], v[3])
                elif k == "hold":
                    x, y = centre(page, v[0])
                    touch(cdp, "touchStart", x, y)
                    page.wait_for_timeout(v[1])
                    touch(cdp, "touchEnd", x, y)
                elif k == "rotate":
                    page.set_viewport_size(LANDSCAPE if v == "landscape" else PORTRAIT)
                elif k == "local":
                    page.evaluate("([k, v]) => localStorage.setItem(k, v)", v)
            except Exception as e:  # noqa: BLE001
                print(f"ERROR in {k}: {str(e)[:600]}", flush=True)
                page.screenshot(path=str(out / "error.png"))
                if not a.get("soft"):
                    break
        if logs:
            print("console errors:", json.dumps(logs[-8:]))
        print(f"total {time.time() - t0:.1f}s")
        ctx.close()


if __name__ == "__main__":
    main()
