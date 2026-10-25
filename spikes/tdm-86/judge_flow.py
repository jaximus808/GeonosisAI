"""TDM-86: the judge's-phone paid task, end to end, on a 390x844 touch phone (headless Chromium).

QR link -> join -> practice (touch only, following the on-screen coach) -> claim -> drive the 3D sim
arm with the thumb zones -> finish -> reviewer verdict + payout -> earnings. One screenshot per step,
a timestamp per phase. Drives ONLY what a person sees: the coach line's step and highlighted keys
(data-step / data-keys on the coach) are turned into thumb gestures on the Move / Up-down zones,
taps on Grip, STOP, Resume, and the buttons on screen.

  uv run --no-project --python 3.11 --with playwright==1.58.0 python -I spikes/tdm-86/judge_flow.py \
      --base http://localhost:3486 --task 3 [--invite '/j/3/qr?k=...'] [--out spikes/tdm-86/tmp/after] [--name Judge]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
      "Version/18.5 Mobile/15E148 Safari/604.1")
R = 46  # px of thumb travel for full speed (Tdm86ZoneStick)


class Phone:
    def __init__(self, page, cdp, out: Path):
        self.page, self.cdp, self.out = page, cdp, out
        self.t0 = time.time()
        self.marks: list[tuple[str, float]] = []
        self.n = 0

    def log(self, msg: str) -> None:
        print(f"[{time.time() - self.t0:6.1f}s] {msg}", flush=True)

    def mark(self, name: str) -> None:
        self.marks.append((name, time.time() - self.t0))
        self.log(f"== {name}")

    def shot(self, name: str) -> None:
        self.n += 1
        p = self.out / f"{self.n:02d}-{name}.png"
        self.page.screenshot(path=str(p))
        self.log(f"shot {p.name}")

    def _touch(self, kind: str, x: float, y: float) -> None:
        pts = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1, "radiusX": 8, "radiusY": 8, "force": 1}]
        self.cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})

    def drag(self, sel: str, dx: float, dy: float, hold_ms: int) -> None:
        b = self.page.locator(sel).first.bounding_box(timeout=20000)
        x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        self._touch("touchStart", x, y)
        for i in range(1, 6):
            self._touch("touchMove", x + dx * i / 5, y + dy * i / 5)
            self.page.wait_for_timeout(16)
        end = time.time() + hold_ms / 1000
        k = 0
        while time.time() < end:
            k += 1
            self._touch("touchMove", x + dx, y + dy + (0.5 if k % 2 else -0.5))
            self.page.wait_for_timeout(40)
        self._touch("touchEnd", x + dx, y + dy)

    def tap(self, sel: str, timeout: int = 20000) -> None:
        self.page.locator(sel).first.tap(timeout=timeout)

    def visible(self, sel: str) -> bool:
        loc = self.page.locator(sel)
        return loc.count() > 0 and loc.first.is_visible()

    def coach(self, testid: str) -> dict | None:
        return self.page.evaluate(
            """(id) => { const c = document.querySelector(`[data-testid="${id}"]`); if (!c) return null;
            return { kind: c.dataset.kind || '', step: c.dataset.step || '', keys: (c.dataset.keys || '').split(' ').filter(Boolean),
                     text: c.innerText.replace(/\\s+/g, ' ').slice(0, 200) } }""",
            testid,
        )

    def thumbs(self, prefix: str, keys: list[str], text: str, lesson: bool) -> None:
        """Turn the coach's keys into one thumb gesture."""
        move = {"KeyW": (0, -1), "KeyS": (0, 1), "KeyA": (-1, 0), "KeyD": (1, 0)}
        m = re.search(r"(\d+(?:\.\d+)?) cm", text)
        cm = float(m.group(1)) if m else 6.0
        frac = 1.0 if cm > 5 else 0.7 if cm > 2 else 0.45
        ms = min(1400, int(90 * cm)) if cm > 5 else 300  # a thumb holds longer for a longer move
        mk = [k for k in keys if k in move]
        if lesson:
            mk = mk[:1]
        if "KeyQ" in keys or "KeyE" in keys:
            if lesson:
                self.drag(f'[data-testid="{prefix}-stick-updown"]', 0, R, 700)  # down: away from the workspace top
                return
            if "KeyQ" in keys:  # lift first, then the sideways move on the next poll
                self.drag(f'[data-testid="{prefix}-stick-updown"]', 0, -R * frac, ms)
                return
            if not mk:
                self.drag(f'[data-testid="{prefix}-stick-updown"]', 0, R * frac, ms)
                return
        if mk:
            dx = sum(move[k][0] for k in mk) * R * frac
            dy = sum(move[k][1] for k in mk) * R * frac
            self.drag(f'[data-testid="{prefix}-stick-move"]', dx, dy, 700 if lesson else ms)
            return
        if "KeyG" in keys:
            self.tap(f'[data-testid="{prefix}-grip-toggle"]')
            self.page.wait_for_timeout(1300)
            return
        self.page.wait_for_timeout(250)


def practice(ph: Phone, deadline_s: float = 240) -> float:
    t = time.time()
    shots: set[str] = set()
    last = None
    while time.time() - t < deadline_s:
        if ph.visible('[data-testid="tdm59-continue"]'):
            ph.shot("practice-ready")
            break
        c = ph.coach("tdm59-coach")
        if not c:
            ph.page.wait_for_timeout(300)
            continue
        if (c["kind"], c["step"]) != last:
            ph.log(f"practice {c['kind']}/{c['step']}: {c['text'][:110]} keys={c['keys']}")
            last = (c["kind"], c["step"])
            tag = f"{c['kind']}-{c['step']}"
            if tag in ("lesson-move", "task-over_obj", "task-lift", "task-release") and tag not in shots:
                shots.add(tag)
                ph.shot(f"practice-{tag}")
        if ph.visible('[data-testid="tdm59-practice-resume"]'):
            ph.tap('[data-testid="tdm59-practice-resume"]')
            ph.page.wait_for_timeout(300)
            continue
        if ph.visible('[data-testid="tdm86-net-retest"]'):
            if ph.page.locator('[data-testid="tdm86-net-retest"]').first.is_enabled():
                ph.log("practice: connection check failed, tapping Test again")
                ph.tap('[data-testid="tdm86-net-retest"]')
            ph.page.wait_for_timeout(1500)
            continue
        if ph.visible('[data-testid="tdm106-skip-to-task"]'):
            ph.tap('[data-testid="tdm106-skip-to-task"]')
            ph.page.wait_for_timeout(250)
            continue
        if c["kind"] == "lesson" and c["step"] == "stop":
            ph.tap('[data-testid="tdm59-practice-stop"]')
            ph.page.wait_for_timeout(500)
            continue
        if c["kind"] == "lesson" and c["step"] == "grip":
            ph.tap('[data-testid="tdm59-grip-toggle"]')
            ph.page.wait_for_timeout(1200)
            continue
        if c["kind"] == "task" and c["step"] == "done":
            ph.page.wait_for_timeout(400)
            continue
        ph.thumbs("tdm59", c["keys"], c["text"], c["kind"] == "lesson")
        ph.page.wait_for_timeout(120)
    return time.time() - t


def teleop(ph: Phone, deadline_s: float = 300) -> float:
    t = time.time()
    shots: set[str] = set()
    last = None
    while time.time() - t < deadline_s:
        if ph.visible('[data-testid="tdm58-payout"]'):
            break
        c = ph.coach("tdm56-coach") or {"step": "", "keys": [], "text": "", "kind": ""}
        if c["step"] != last:
            ph.log(f"teleop {c['step']}: {c['text'][:110]} keys={c['keys']}")
            last = c["step"]
            if c["step"] in ("start", "over_obj", "lower", "lift", "carry", "release", "done", "submitted") and c["step"] not in shots:
                shots.add(c["step"])
                ph.shot(f"teleop-{c['step']}")
        if ph.visible('[data-testid="tdm58-confirm-yes"]'):
            ph.tap('[data-testid="tdm58-confirm-yes"]')
            ph.page.wait_for_timeout(800)
            continue
        if c["step"] == "start" and ph.visible('[data-testid="tdm58-start"]'):
            if ph.page.locator('[data-testid="tdm58-start"]').first.is_enabled():
                ph.tap('[data-testid="tdm58-start"]')
            ph.page.wait_for_timeout(700)
            continue
        if c["step"] == "done":
            fin = ph.page.locator('[data-testid="tdm58-finish"]')
            if fin.count() and fin.first.is_enabled():
                fin.first.tap()
            ph.page.wait_for_timeout(600)
            continue
        if c["step"] in ("submitted", "lease", "policy", ""):
            ph.page.wait_for_timeout(600)
            continue
        ph.thumbs("tdm56", c["keys"], c["text"], False)
        ph.page.wait_for_timeout(150)
    return time.time() - t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:3486")
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--invite", default=None, help="the QR's path, e.g. /j/3/qr?k=...")
    ap.add_argument("--out", default=str(HERE / "tmp" / "after"))
    ap.add_argument("--name", default="Judge Ada")
    ap.add_argument("--keep-profile", action="store_true")
    ap.add_argument("--skip-to-claim", action="store_true", help="joined + practised already (implies --keep-profile)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    profile = HERE / "tmp" / "judge-profile"
    if profile.exists() and not (a.keep_profile or a.skip_to_claim):
        shutil.rmtree(profile)  # a judge's phone has never seen the site
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=True, args=GPU_ARGS,
                                                   viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                                   is_mobile=True, has_touch=True, user_agent=UA)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text[:200]) if m.type == "error" else None)
        ph = Phone(page, cdp, out)
        base = a.base.rstrip("/")
        try:
            run(ph, page, a, base, errors, out)
        finally:
            ctx.close()


def run(ph: Phone, page, a, base: str, errors: list[str], out: Path) -> None:
    if True:

        if a.skip_to_claim:  # same phone, already joined and practised: straight to the claim
            page.goto(f"{base}/marketplace?task={a.task}", wait_until="domcontentloaded")
            page.wait_for_selector(f"#task-{a.task} >> text=/Claim for/", timeout=60000)
            return drive(ph, page, a, base, errors, out)
        ph.mark("scan QR")
        page.goto(base + (a.invite or f"/marketplace?task={a.task}"), wait_until="domcontentloaded", timeout=180000)
        page.wait_for_selector(f"#task-{a.task}", timeout=120000)
        page.wait_for_timeout(800)
        ph.shot("qr-landing")

        ph.mark("join")
        ph.tap(f"#task-{a.task} >> text=Join to claim")
        page.wait_for_selector('input[placeholder="e.g. Sam"]', timeout=60000)
        page.fill('input[placeholder="e.g. Sam"]', a.name)
        ph.shot("join")
        ph.tap('button[type="submit"]')
        page.wait_for_timeout(1500)
        ph.shot("joined")
        page.goto(f"{base}/marketplace?task={a.task}", wait_until="domcontentloaded")
        page.wait_for_selector(f"#task-{a.task} >> text=Practise to unlock", timeout=60000)
        page.wait_for_timeout(600)
        ph.shot("marketplace-gate")

        ph.mark("practice")
        ph.tap(f"#task-{a.task} >> text=Practise to unlock")
        page.wait_for_selector('[data-testid="tdm59-phone"]', timeout=120000)
        page.wait_for_timeout(2500)
        ph.shot("practice-start")
        page.set_viewport_size({"width": 844, "height": 390})
        page.wait_for_timeout(1500)
        ph.shot("practice-landscape")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1200)
        took = practice(ph)
        ph.log(f"practice took {took:.1f} s")

        drive(ph, page, a, base, errors, out)


def drive(ph: Phone, page, a, base: str, errors: list[str], out: Path) -> None:
    if True:
        ph.mark("claim")
        if ph.visible('[data-testid="tdm59-continue"]'):
            ph.tap('[data-testid="tdm59-continue"]')
        page.wait_for_selector(f"#task-{a.task} >> text=/Claim for/", timeout=60000)
        page.wait_for_timeout(500)
        ph.shot("marketplace-claim")
        ph.tap(f"#task-{a.task} >> text=/Claim for/")

        ph.mark("drive")
        page.wait_for_selector('[data-testid="tdm56-phone"]', timeout=120000)
        page.wait_for_function("() => document.querySelector('[data-sim-cameras]')?.dataset.state === 'live'", timeout=120000)
        page.wait_for_timeout(1500)
        ph.shot("teleop-open")
        page.set_viewport_size({"width": 844, "height": 390})  # the judge turns the phone sideways
        page.wait_for_timeout(1800)
        ph.shot("teleop-landscape")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1500)
        took = teleop(ph)
        ph.log(f"teleop took {took:.1f} s")
        ph.mark("payout")
        page.wait_for_timeout(1500)
        ph.shot("payout")
        page.evaluate("() => document.querySelector('[data-testid=\"tdm58-recording-controls\"]')?.scrollIntoView({block: 'center'})")
        ph.shot("payout-detail")

        ph.mark("earnings")
        page.goto(f"{base}/earnings", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        ph.shot("earnings")
        ph.mark("end")
        sw = page.evaluate("() => document.documentElement.scrollWidth")
        ph.log(f"scrollWidth {sw}")
        summary = {"marks": ph.marks, "console_errors": errors[-10:]}
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
