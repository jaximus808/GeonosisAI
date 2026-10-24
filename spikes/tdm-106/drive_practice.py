"""TDM-106: drive /practice like a first-time user who only follows the on-screen hints.

It reads the coach line (data-testid="tdm59-coach") and the legend keycaps the coach highlights
(data-hint="true" on data-testid="tdm106-key-*"), presses exactly those keys with real keyboard
events, and clicks only buttons the page shows ("Skip to the task", "Resume"). Screenshots of each
phase go to the output dir; the time from the first key press to "Done" is printed.

  uv run --no-project --python 3.11 --with playwright==1.58.0 \
      python -I spikes/tdm-106/drive_practice.py http://127.0.0.1:3783 spikes/tdm-106/tmp
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]


def coach(page):
    return page.evaluate(
        """() => { const c = document.querySelector('[data-testid="tdm59-coach"]'); if (!c) return null;
        const hints = [...document.querySelectorAll('[data-testid^="tdm106-key-"][data-hint="true"]')].map(e => e.dataset.testid.slice(11));
        return { kind: c.dataset.kind, step: c.dataset.step, text: c.innerText.replace(/\\s+/g, ' ').slice(0, 160), hints } }"""
    )


def main(base: str, out: str) -> None:
    outd = Path(out)
    outd.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=GPU_ARGS)
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{base}/practice", wait_until="networkidle", timeout=180_000)
        page.wait_for_selector('[data-testid="tdm106-legend"]', timeout=60_000)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(outd / "01-start.png"))
        t0 = None
        shots = set()
        last = None
        deadline = time.time() + 240
        while time.time() < deadline:
            c = coach(page)
            if c is None:
                break
            if (c["kind"], c["step"]) != last:
                print(f"[{0 if t0 is None else time.time() - t0:6.1f}s] {c['kind']}/{c['step']}: {c['text']}  keys={c['hints']}", flush=True)
                last = (c["kind"], c["step"])
            if c["kind"] == "task" and c["step"] == "done":
                break
            skip = page.query_selector('[data-testid="tdm106-skip-to-task"]')
            if skip:
                page.screenshot(path=str(outd / "02-basics-done.png"))
                skip.click()
                page.wait_for_timeout(200)
                continue
            resume = page.query_selector('[data-testid="tdm59-practice-resume"]')
            if resume:
                resume.click()
                page.wait_for_timeout(300)
                continue
            keys = c["hints"]
            if t0 is None and keys:
                t0 = time.time()
            if c["kind"] == "task" and c["step"] not in shots:
                shots.add(c["step"])
                page.screenshot(path=str(outd / f"03-task-{c['step']}.png"))
            if keys == ["KeyG"] or (c["step"] in ("close", "release") and keys and keys[-1] == "KeyG" and len(keys) == 1):
                page.keyboard.press("g")
                page.wait_for_timeout(1300)
                continue
            if "Escape" in keys:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
                continue
            move = [k for k in keys if k != "KeyG"]
            if c["kind"] == "lesson":
                move = move[:1]  # a lesson lists alternatives (W, A, S or D): a person picks one
            if not move:
                page.wait_for_timeout(200)
                continue
            for k in move:
                page.keyboard.down(k)
            page.wait_for_timeout(600)
            for k in move:
                page.keyboard.up(k)
            page.wait_for_timeout(60)
        took = None if t0 is None else time.time() - t0
        page.wait_for_timeout(800)
        page.screenshot(path=str(outd / "04-done.png"))
        timer = page.evaluate("() => document.querySelector('[data-testid=\"tdm106-task-time\"]')?.innerText")
        print(f"finished: {last}, wall {took and round(took, 1)} s, page says: {timer}")
        b.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3783", sys.argv[2] if len(sys.argv) > 2 else "spikes/tdm-106/tmp")
