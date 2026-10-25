"""No-layout-shift check for the teleop and practice consoles (headless GPU Chromium, no robot, no MuJoCo).

  nice -n 15 uv run --no-project --python 3.11 --with playwright==1.58.0 \
      python -I spikes/teleop-perf/layout_check.py http://127.0.0.1:3893 spikes/teleop-perf/tmp/layout

Pages: /dev/teleop-layout (the real teleop console on the sim fixture, FAKE lease + recording you can switch) and
/practice, at desktop 1440x900 @2x and phone 390x844 @3x. For each, it records the bounding boxes of the 3D
viewer and of the control dock, then injects messages one by one (a lease error with a long reason, lost
control, someone else driving, STOP, a refused recording op with a long reason, a 500 ms delay pause, safety
clamps) and asserts the boxes did not move or resize by more than 0.5 px. Screenshots of each state are saved.
Exits 1 on any shift.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]
TOL = 0.5
INIT = "try { localStorage.setItem('rdf.tdm59.intro-seen.v1', JSON.stringify(['clean_teleop','recovery_takeover','practice'])) } catch (e) {}"


def boxes(page, sel: dict[str, str]) -> dict[str, list[float]]:
    # document coordinates (a click that scrolls the page is not a layout shift)
    out = {}
    for name, css in sel.items():
        out[name] = page.evaluate(
            """(css) => { const e = document.querySelector(css); if (!e) return null; const r = e.getBoundingClientRect();
                return [r.x + scrollX, r.y + scrollY, r.width, r.height].map((v) => Math.round(v * 100) / 100); }""", css)
    return out


def same(a, b) -> bool:
    return all(x is not None and y is not None and all(abs(p - q) <= TOL for p, q in zip(x, y)) for x, y in zip(a.values(), b.values()))


def run_case(b, base: str, out: Path, name: str, path: str, ctx_kw: dict, sel: dict[str, str], steps) -> dict:
    ctx = b.new_context(**ctx_kw)
    ctx.add_init_script(INIT)
    page = ctx.new_page()
    page.goto(f"{base}{path}")
    page.wait_for_function("() => document.querySelectorAll('canvas').length >= 1", timeout=120000)
    page.wait_for_timeout(3500)
    ref = boxes(page, sel)
    page.screenshot(path=str(out / f"{name}_0_baseline.png"))
    res = {"baseline": ref, "steps": [], "ok": True}
    for i, (label, act, visible) in enumerate(steps, 1):
        try:
            act(page)
        except Exception as e:  # noqa: BLE001
            res["steps"].append({"step": label, "error": str(e).splitlines()[0]})
            res["ok"] = False
            print(name, label, "ERROR", str(e).splitlines()[0], flush=True)
            continue
        page.wait_for_timeout(900)
        shown = page.locator(visible).count() > 0 if visible else None
        now = boxes(page, sel)
        ok = same(ref, now)
        shot = out / f"{name}_{i}_{label}.png"
        page.screenshot(path=str(shot))
        res["steps"].append({"step": label, "message_visible": shown, "boxes": now, "stable": ok, "screenshot": str(shot)})
        res["ok"] &= ok and shown is not False
    ctx.close()
    return res


def main() -> None:
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    desk = dict(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    phone = dict(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True)
    click = lambda css: (lambda p: p.locator(css).first.click())  # noqa: E731
    key = lambda k: (lambda p: (p.mouse.click(4, 4), p.keyboard.press(k)))  # noqa: E731

    def hold(k: str, ms: int):
        def f(p):
            p.mouse.click(4, 4)
            p.keyboard.down(k)
            p.wait_for_timeout(ms)
            p.keyboard.up(k)
        return f

    def push(k: str, ms: int):
        # fast speed, then keep pushing into the edge: the safety chips show while the move is clamped
        def f(p):
            p.locator("[data-testid=tdm106-speed-fast]").click()
            p.mouse.click(4, 4)
            p.keyboard.down(k)
            p.wait_for_timeout(ms)
        return f

    def lag500(p):
        p.locator("summary", has_text="Feel the lag").first.click(position={"x": 12, "y": 12})
        p.locator("[data-testid=tdm59-lag-500]").click()
        p.mouse.click(4, 4)
        p.keyboard.down("KeyW")
        p.wait_for_timeout(400)
        p.keyboard.up("KeyW")

    cases = {
        "teleop_desktop": ("/dev/teleop-layout", desk,
                           {"viewer": "[data-testid=tdm56-feed-frame]", "dock": "[data-testid=tdm56-onscreen]"},
                           [("lease_error_long", click("[data-testid=inject-lease-held_error]"), "[data-testid=tdm57-bar-line]"),
                            ("lease_lost", click("[data-testid=inject-lease-lost]"), "[data-tdm57-state=lost]"),
                            ("someone_else_driving", click("[data-testid=inject-lease-view_only]"), "[data-tdm57-state=view_only]"),
                            ("back_in_control", click("[data-testid=inject-lease-held]"), "[data-tdm57-state=held]"),
                            ("stop_pressed", key("Escape"), "[data-testid=tdm56-stop-alert]"),
                            ("recording_refused_long", click("[data-testid=tdm58-start]"), "[data-testid=tdm58-error]"),
                            ("expand_long_message", click("[data-testid=tdm57-bar-line-more], [data-testid=tdm56-status-sub-more]"), None)]),
        "practice_desktop": ("/practice", desk,
                             {"viewer": "[data-testid=tdm59-practice-views]", "dock": "[data-testid=tdm59-onscreen]"},
                             [("pushing_workspace_edge", push("KeyA", 7000), "[data-testid=tdm59-safety-chips]"),
                              ("released", lambda p: p.keyboard.up("KeyA"), None),
                              ("stop_pressed", key("Escape"), "[data-testid=tdm59-stop-alert]"),
                              ("resumed", click("[data-testid=tdm59-practice-resume]"), None),
                              ("delay_500ms_pause", lag500, None)]),
        "teleop_phone": ("/dev/teleop-layout", phone,
                         {"viewer": "[data-testid=tdm56-feed-frame]", "dock": "[data-testid=tdm56-stick-move]"},
                         [("stop_pressed", click("[data-testid=tdm56-stop]"), "[data-testid=tdm56-stop-alert]"),
                          ("recording_refused_long", click("[data-testid=tdm58-start]"), "[data-testid=tdm58-error]")]),
        "practice_phone": ("/practice", phone,
                           {"viewer": "[data-testid=tdm59-practice-views]", "dock": "[data-testid=tdm59-stick-move]"},
                           [("stop_pressed", click("[data-testid=tdm59-practice-stop]"), "[data-testid=tdm59-stop-alert]"),
                            ("resumed", click("[data-testid=tdm59-practice-resume]"), None)]),
    }
    rep: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=GPU_ARGS)
        only = set(sys.argv[3:])
        for name, (path, kw, sel, steps) in cases.items():
            if only and name not in only:
                continue
            rep[name] = run_case(b, base, out, name, path, kw, sel, steps)
            print(name, "OK" if rep[name]["ok"] else "FAIL", json.dumps([(s["step"], s.get("stable"), s.get("message_visible")) for s in rep[name]["steps"]]), flush=True)
        b.close()
    (out / "layout_check.json").write_text(json.dumps(rep, indent=2))
    sys.exit(0 if all(r["ok"] for r in rep.values()) else 1)


if __name__ == "__main__":
    main()
