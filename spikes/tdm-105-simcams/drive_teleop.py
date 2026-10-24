"""TDM-105: drive the no-camera MuJoCo sim robot from /teleop/<task> headless and prove the rendered
sim views follow it (stack.py must be up, plus a web server on it).

  TDM105_BROWSER_TOKEN_FILE=$TMPDIR/tdm-105/run-3115/browser.token \
  uv run --no-project --python 3.11 --with playwright==1.58.0 \
      python -I spikes/tdm-105-simcams/drive_teleop.py http://127.0.0.1:3815 1 tdm105simcams spikes/tdm-105-simcams/tmp

The browser token (the test contributor that claimed the task) is injected into localStorage from the
file and never printed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]


def main() -> None:
    base, task, db, out = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3], Path(sys.argv[4])
    out.mkdir(parents=True, exist_ok=True)
    tok = Path(os.environ["TDM105_BROWSER_TOKEN_FILE"]).read_text().strip()
    rep: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=GPU_ARGS)
        ctx = b.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
        ctx.add_init_script(
            f"try {{ localStorage.setItem({json.dumps('rdf.spacetime.token.' + db)}, {json.dumps(tok)});"
            " localStorage.setItem('rdf.tdm59.intro-seen.v1', JSON.stringify(['clean_teleop', 'recovery_takeover'])) } catch (e) {}")
        page = ctx.new_page()
        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"[:300]) if m.type in ("error", "warning") else None)
        page.goto(f"{base}/teleop/{task}")
        page.wait_for_function("() => document.querySelectorAll('[data-sim-camera] canvas').length >= 3", timeout=120000)
        page.wait_for_function("() => document.querySelector('[data-sim-cameras]')?.dataset.state === 'live'", timeout=60000)
        page.wait_for_timeout(4000)
        # close the first-visit walkthrough if it is open
        for label in ("Got it", "Close", "Skip", "Start driving"):
            btn = page.get_by_role("button", name=label)
            if btn.count():
                try:
                    btn.first.click(timeout=1500)
                except Exception:  # noqa: BLE001
                    pass
        page.wait_for_timeout(1000)
        rep["fps_idle"] = [page.wait_for_timeout(1000) or page.evaluate("() => window.__rdfSimCams.fps()") for _ in range(4)]
        rep["text_no_video"] = page.get_by_text("No video").count()
        rep["q_before"] = page.evaluate("() => window.__rdfTwin.q()")
        page.screenshot(path=str(out / "tdm105_teleop_before.png"))
        # drive: hold the clutch (Space) and move +y (A), then -z (E)
        page.mouse.click(5, 300)
        page.keyboard.down("Space")
        page.keyboard.down("KeyA")
        fps_drive = []
        for _ in range(3):
            page.wait_for_timeout(1000)
            fps_drive.append(page.evaluate("() => window.__rdfSimCams.fps()"))
        page.keyboard.up("KeyA")
        page.keyboard.down("KeyE")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "tdm105_teleop_driving.png"))
        page.keyboard.up("KeyE")
        page.keyboard.up("Space")
        rep["fps_driving"] = fps_drive
        page.wait_for_timeout(800)
        rep["q_after"] = page.evaluate("() => window.__rdfTwin.q()")
        page.screenshot(path=str(out / "tdm105_teleop_after.png"))
        rep["drive_block"] = page.evaluate("() => [...document.querySelectorAll('[role=alert], [data-testid*=block]')].map(e => e.textContent.trim()).slice(0, 5)")
        rep["console"] = logs[-15:]
        b.close()
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
