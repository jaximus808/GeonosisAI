"""TDM-97 screenshot pair, twin side + fps. Renders /dev/twin at the poses in pair_poses.json with the
`front` preset (= the sim's exo camera "top") and the `wrist` preset (= the sim's "wrist" camera), as
640x480 frames next to mujoco_pair.py's, reads the rendered grasp_site back from the scene graph, and
measures fps on the scripted motion at a desktop and a 390 px phone viewport.

  uv run --no-project --python 3.11 --with playwright==1.58.0 --with pillow \
      python -I spikes/tdm-97/shoot_twin.py http://127.0.0.1:3797 spikes/robotview/tmp
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

POSES = json.loads((Path(__file__).parent / "pair_poses.json").read_text())
GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]


def wait_ready(page, settle_ms=1500):
    page.wait_for_function("() => window.__rdfTwin && window.__rdfTwin.ee() !== null", timeout=60000)
    page.wait_for_timeout(settle_ms)


def gpu(page):
    return page.evaluate(
        """() => { const c = document.querySelector('canvas'); const gl = c.getContext('webgl2');
        const e = gl && gl.getExtension('WEBGL_debug_renderer_info'); return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : null }"""
    )


def fps(page, seconds=4):
    samples = []
    for _ in range(seconds):
        page.wait_for_timeout(1000)
        samples.append(page.evaluate("() => window.__rdfTwin.fps()"))
    return samples


def main() -> None:
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"poses": {}, "fps": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=GPU_ARGS)
        ctx = browser.new_context(viewport={"width": 1100, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        for name, pose in POSES.items():
            q = ",".join(str(v) for v in pose["q"] + [pose.get("gripper", 1.0)])
            for preset, cam in (("front", "top"), ("wrist", "wrist")):
                page.goto(f"{base}/dev/twin?q={q}&preset={preset}&ghost=0&w=640")
                wait_ready(page)
                page.locator('[data-testid="twin-frame"]').screenshot(path=str(out / f"tdm97_{name}_{cam}_twin.png"))
            report["poses"][name] = {"ee_rendered": page.evaluate("() => window.__rdfTwin.ee()")}
        report["gpu"] = gpu(page)
        ctx.close()

        # desktop: 1440x900 @2x, frame at max-w-5xl (1024 css px wide)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(f"{base}/dev/twin")
        wait_ready(page, 2500)
        report["fps"]["desktop_1440@2x"] = fps(page)
        page.locator('[data-testid="twin-frame"]').screenshot(path=str(out / "tdm97_motion_desktop.png"))
        ctx.close()

        # phone: 390x844 @3x, touch
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(f"{base}/dev/twin")
        wait_ready(page, 2500)
        report["fps"]["phone_390@3x"] = fps(page)
        page.screenshot(path=str(out / "tdm97_motion_phone390.png"))
        for preset in ("top", "wrist"):
            page.goto(f"{base}/dev/twin?preset={preset}")
            wait_ready(page, 2500)
            page.screenshot(path=str(out / f"tdm97_motion_phone390_{preset}.png"))
        ctx.close()
        browser.close()
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
