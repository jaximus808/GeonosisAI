"""Teleop performance mode: headless GPU Chromium FPS / CPU sample of the 3D views (no MuJoCo, no robot).

  nice -n 15 uv run --no-project --python 3.11 --with playwright==1.58.0 --with psutil==7.1.0 \
      python -I spikes/teleop-perf/perf_sample.py http://127.0.0.1:3893 spikes/teleop-perf/tmp/<label>.json /dev/simcams /practice ...

For each path (desktop 1440x900 @2x, like a retina Mac laptop), after a warm-up, it samples for
SAMPLE_S seconds:
  fps            the page's requestAnimationFrame rate (what the user sees; capped by the display loop)
  cpu_*          CPU seconds per wall second (1.0 = one core) of the Chromium GPU process, the renderer, and all
                 Chromium processes, from psutil (the GPU process's CPU is where WebGL work is submitted)
  main_thread    CDP Performance.getMetrics TaskDuration per second (the renderer's main-thread busy time)
  canvases       live WebGL canvases and their backing-store megapixels (pixels shaded per frame, per view)
  mpx_per_s      backing megapixels x each canvas's own frame rate: the fill work the GPU does per second

A path may carry `#drive` to hold the A key (move left) then D during the sample, like a driver would, and
`#sharp` to start in Quality: Sharp (default: Smooth, the remembered choice in localStorage).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import psutil
from playwright.sync_api import sync_playwright

GPU_ARGS = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"]
WARM_S = 5
SAMPLE_S = 10

# Count frames per canvas: wrap each WebGL context's drawing via a per-canvas rAF probe would need hooks in
# three.js; instead count frames where the canvas was actually drawn by patching WebGL clear/draw calls.
INIT = r"""
(() => {
  const frames = new WeakMap();
  window.__perfCanvases = new Set();
  const mark = (gl) => {
    const c = gl.canvas;
    if (!c) return;
    window.__perfCanvases.add(c);
    const f = frames.get(c) || { n: 0, last: -1 };
    if (f.last !== window.__perfFrame) { f.n++; f.last = window.__perfFrame; }
    frames.set(c, f);
  };
  for (const P of [WebGLRenderingContext.prototype, WebGL2RenderingContext.prototype]) {
    for (const k of ['drawElements', 'drawArrays', 'drawElementsInstanced', 'drawArraysInstanced', 'drawRangeElements']) {
      const o = P[k];
      if (!o) continue;
      P[k] = function (...a) { mark(this); return o.apply(this, a); };
    }
  }
  window.__perfFrame = 0;
  window.__perfRaf = 0;
  const tick = () => { window.__perfFrame++; window.__perfRaf++; requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  window.__perfSnap = () => {
    const out = [];
    for (const c of window.__perfCanvases) {
      if (!c.isConnected) continue;
      const f = frames.get(c) || { n: 0 };
      out.push({ w: c.width, h: c.height, frames: f.n });
    }
    return { raf: window.__perfRaf, canvases: out };
  };
})();
"""


def chromium_procs() -> list[psutil.Process]:
    me = psutil.Process()
    out = []
    for p in me.children(recursive=True):
        try:
            name = p.name().lower()
            if "chrom" in name or "headless" in name:
                out.append(p)
        except psutil.Error:
            pass
    return out


def cpu_by_type(procs: list[psutil.Process]) -> dict[str, float]:
    tot = {"gpu": 0.0, "renderer": 0.0, "all": 0.0}
    for p in procs:
        try:
            t = p.cpu_times()
            s = t.user + t.system
            cmd = " ".join(p.cmdline())
        except psutil.Error:
            continue
        tot["all"] += s
        if "--type=gpu-process" in cmd:
            tot["gpu"] += s
        elif "--type=renderer" in cmd:
            tot["renderer"] += s
    return tot


def sample(page, cdp, drive: bool) -> dict:
    page.wait_for_function("() => document.querySelectorAll('canvas').length >= 1", timeout=120000)
    page.wait_for_timeout(WARM_S * 1000)
    if drive:
        page.mouse.click(4, 450)
        page.keyboard.down("KeyA")
    procs = chromium_procs()
    c0 = cpu_by_type(procs)
    m0 = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    s0 = page.evaluate("() => window.__perfSnap()")
    t0 = time.monotonic()
    page.wait_for_timeout(SAMPLE_S * 500)
    if drive:
        page.keyboard.up("KeyA")
        page.keyboard.down("KeyD")
    page.wait_for_timeout(SAMPLE_S * 500)
    dt = time.monotonic() - t0
    s1 = page.evaluate("() => window.__perfSnap()")
    m1 = {m["name"]: m["value"] for m in cdp.send("Performance.getMetrics")["metrics"]}
    c1 = cpu_by_type(procs)
    if drive:
        page.keyboard.up("KeyD")
    canv = []
    for i, c in enumerate(s1["canvases"]):
        f0 = s0["canvases"][i]["frames"] if i < len(s0["canvases"]) else 0
        fps = (c["frames"] - f0) / dt
        canv.append({"px": f"{c['w']}x{c['h']}", "mpx": round(c["w"] * c["h"] / 1e6, 3), "fps": round(fps, 1)})
    return {
        "fps": round((s1["raf"] - s0["raf"]) / dt, 1),
        "cpu_gpu_proc": round((c1["gpu"] - c0["gpu"]) / dt, 3),
        "cpu_renderer": round((c1["renderer"] - c0["renderer"]) / dt, 3),
        "cpu_all_chromium": round((c1["all"] - c0["all"]) / dt, 3),
        "main_thread": round((m1["TaskDuration"] - m0["TaskDuration"]) / dt, 3),
        "canvases": canv,
        "live_canvases": sum(1 for c in canv if c["fps"] > 1),
        "mpx_per_s": round(sum(c["mpx"] * c["fps"] for c in canv), 1),
    }


def main() -> None:
    base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
    paths = sys.argv[3:]
    out.parent.mkdir(parents=True, exist_ok=True)
    rep: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(args=GPU_ARGS)
        for path in paths:
            tags = set(path.split("#")[1:])
            drive = "drive" in tags
            url = path.split("#")[0]
            ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
            ctx.add_init_script(INIT)
            ctx.add_init_script(
                "try { localStorage.setItem('rdf.tdm59.intro-seen.v1', JSON.stringify(['clean_teleop','recovery_takeover','practice'])) } catch (e) {}")
            q = "sharp" if "sharp" in tags else "smooth"
            ctx.add_init_script(f"try {{ localStorage.setItem('rdf.teleop.view-quality.v1', '{q}') }} catch (e) {{}}")
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            cdp.send("Performance.enable")
            page.goto(f"{base}{url}")
            r = sample(page, cdp, drive)
            rep.setdefault(path, []).append(r)
            shot = out.with_name(out.stem + "_" + "_".join(sorted(tags)) + "_" + url.strip("/").replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "-") + ".png")
            page.screenshot(path=str(shot))
            r["screenshot"] = str(shot)
            print(path, json.dumps({k: v for k, v in r.items() if k != "screenshot"}), flush=True)
            ctx.close()
        # repeat a path on the command line to sample it several times (alternate A B A B: the laptop is shared)
        rep["median"] = {k: {m: sorted(x[m] for x in v)[len(v) // 2] for m in ("fps", "cpu_gpu_proc", "cpu_renderer", "cpu_all_chromium", "main_thread", "live_canvases", "mpx_per_s")}
                         for k, v in rep.items() if isinstance(v, list)}
        print(json.dumps(rep["median"], indent=1), flush=True)
        rep["gpu"] = "chromium headless " + b.version + " " + " ".join(GPU_ARGS)
        b.close()
    out.write_text(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
