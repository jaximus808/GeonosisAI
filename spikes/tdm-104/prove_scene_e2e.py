"""TDM-104 end-to-end proof, local only (throwaway SpacetimeDB, never maincloud):

  ticket created -> the scene handler has Claude write a spec (scene.created) -> operator deploys it
  (POST /tickets/{id}/scene/deploy) -> the MuJoCo [SIM] robot client rebuilds its scene (scene.loaded)
  -> the sim's objects match the spec (geometry and fixed poses), and the twin's render form of the same
  spec (apps/web toRenderSpec) is checked against MuJoCo's own geoms. Done for two tasks with no code change.

    cd services/orchestrator
    RDF_LLM_BACKEND=claude_code uv run python ../../spikes/tdm-104/prove_scene_e2e.py

Writes tmp/<scene>_top.png (the sim's top camera after the load) and tmp/e2e_report.json.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "tests" / "integration"))
TMP = HERE / "tmp"
TMP.mkdir(exist_ok=True)
PORT, DB, ORCH_PORT, STATE_PORT = 3104, "tdm104e2e", 8104, 8105
TASKS = ["Pick up the cube and place it in the empty plate", "stack the red block on the blue block"]


def log(msg: str) -> None:
    print(f"[tdm104] {msg}", flush=True)


def main() -> int:
    from harness import LocalStack, wait_for

    os.environ["TDM28_ROOT"] = str(Path(tempfile.gettempdir()) / "tdm-104")
    st = LocalStack(PORT, DB, root=Path(os.environ["TDM28_ROOT"]))
    report: dict = {"tasks": []}
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        os.environ.update(st.env("orchestrator"))
        os.environ["RDF_OPERATOR_KEY"] = key = "tdm104-e2e-" + os.urandom(8).hex()
        os.environ.setdefault("RDF_LLM_BACKEND", "claude_code")

        robot_env = {"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "RDF_STREAM": "off",
                     "RDF_SIM_STATE_PORT": str(STATE_PORT),
                     "MUJOCO_GL": os.environ.get("MUJOCO_GL", "cgl" if sys.platform == "darwin" else "egl")}
        st._spawn("robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "--extra", "sim",
                            "rdf-robot-client", "--adapter", "mujoco", "--cameras", "off", "--name", "sim-yam-tdm104",
                            "--host", st.host, "--db", st.db, "--token-file", str(st.tokens / "robot_client.token")],
                  env=robot_env)
        orch = st.client("orchestrator")
        robot = wait_for(lambda: next((r for r in orch.sql("SELECT * FROM robot WHERE name = 'sim-yam-tdm104'")
                                       if r["status"] == "online"), None), 180, f"sim robot online ({st.logs / 'robot.log'})")
        rid = int(robot["id"])
        log(f"sim robot {rid} online")

        # the orchestrator: only the TDM-104 handler on the bus, plus the scene routes
        from fastapi.testclient import TestClient

        from rdf_orchestrator.app import create_app
        from rdf_orchestrator.bus import Registry
        from rdf_orchestrator.handlers import scene as hs
        from rdf_orchestrator.stdb import SpacetimeClient as BusStdb

        reg = Registry()
        reg.on_insert("event_log", where=lambda r: r.type == "ticket.created", name="tdm104")(hs.scene_on_ticket_created)
        app = create_app(client=BusStdb(host=st.host, db=st.db, token=st.token("orchestrator")), registry=reg)
        with TestClient(app) as c:
            wait_for(lambda: c.get("/healthz").json()["subscription"]["state"] == "live", 60, "bus live")
            H = {"x-rdf-operator-key": key}
            for task in TASKS:
                t0 = time.monotonic()
                before = {int(t["id"]) for t in orch.sql("SELECT * FROM ticket")}
                orch.call_reducer("create_ticket", rid, task, 0.8, 5000, "")
                tid = wait_for(lambda: next((int(t["id"]) for t in orch.sql("SELECT * FROM ticket")
                                             if int(t["id"]) not in before), None), 20, "ticket row")
                log(f"ticket {tid}: {task!r}")
                g = wait_for(lambda: (lambda j: j if j.get("spec") or j.get("last_error") else None)(
                    c.get(f"/tickets/{tid}/scene").json()), 300, "scene.created")
                if not g.get("spec"):
                    raise RuntimeError(f"generation failed: {g['last_error']}")
                spec = g["spec"]
                gen_s = time.monotonic() - t0
                log(f"  scene r{g['revision']} {g['success']} objects={[o['id'] for o in spec['objects']]} ({gen_s:.0f}s)")
                d = c.post(f"/tickets/{tid}/scene/deploy", json={}, headers=H)
                assert d.status_code == 200, d.text
                d = d.json()
                t1 = time.monotonic()
                rs = wait_for(lambda: (lambda j: j if j["status"] != "pending" else None)(c.get(f"/robots/{rid}/scene").json()),
                              60, "scene.loaded")
                assert rs["status"] == "loaded", rs
                log(f"  deployed to robot {d['robot_id']}: {rs['status']} in {time.monotonic() - t1:.1f}s")
                sim = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{STATE_PORT}/sim/state", timeout=5).read())
                checks = compare(spec, sim)
                log(f"  sim matches spec: {checks['ok']} ({len(checks['objects'])} objects)")
                render = twin_render(spec)
                tw = compare_twin(spec, render)
                log(f"  twin render form matches MuJoCo geoms: {tw['ok']}")
                png = TMP / f"{spec['name']}_top.png"
                render_top(spec, png)
                report["tasks"].append({"task": task, "ticket_id": tid, "revision": g["revision"], "success": g["success"],
                                        "generate_s": round(gen_s, 1), "deploy": d, "robot_scene": rs["deployed"],
                                        "sim_check": checks, "twin_check": tw, "png": str(png)})
                assert checks["ok"] and tw["ok"], (checks, tw)
        names = [t["success"] for t in report["tasks"]]
        assert len(set(names)) == len(names), names
        (TMP / "e2e_report.json").write_text(json.dumps(report, indent=1, default=str))
        log(f"OK: {names}; report {TMP / 'e2e_report.json'}")
        return 0
    finally:
        st.stop()


def compare(spec: dict, sim: dict) -> dict:
    """The sim's live objects == the spec's (ids, primitive, geometry; fixed + non-randomized poses exact)."""
    out = {"ok": True, "objects": {}}
    live = sim.get("objects") or {}
    if sim.get("scene") != spec["name"] or set(live) != {o["id"] for o in spec["objects"]}:
        out["ok"] = False
        out["why"] = f"scene {sim.get('scene')} objects {sorted(live)}"
        return out
    for o in spec["objects"]:
        lv = live[o["id"]]
        ok = lv.get("primitive") == o["primitive"]
        if not o.get("randomize"):
            d = math.dist(lv["xyz"][:2], o["pose"]["pos"][:2])
            ok = ok and d < 0.01
        else:
            reg = spec["regions"][o["randomize"]["region"]]
            ok = ok and reg["x"][0] - 1e-3 <= lv["xyz"][0] <= reg["x"][1] + 1e-3 and reg["y"][0] - 1e-3 <= lv["xyz"][1] <= reg["y"][1] + 1e-3
        out["objects"][o["id"]] = {"ok": ok, "sim_xyz": [round(v, 4) for v in lv["xyz"]], "spec_pos": o["pose"]["pos"]}
        out["ok"] = out["ok"] and ok
    return out


def twin_render(spec: dict) -> dict:
    """Run the web's toRenderSpec on the spec (node type stripping, same code the twin uses)."""
    js = ("const m = await import(process.argv[1]); let s=''; for await (const c of process.stdin) s+=c;"
          "process.stdout.write(JSON.stringify(m.toRenderSpec(JSON.parse(s))));")
    r = subprocess.run(["node", "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "--input-type=module", "-e", js,
                        (REPO / "apps/web/src/lib/robotview/scene.ts").as_uri()], input=json.dumps(spec),
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr[-1000:])
    return json.loads(r.stdout)


def _mj_geoms(spec: dict) -> dict:
    code = r"""
import json, sys, mujoco
from rdf_robot_client.adapters.mujoco_scene import build_scene_xml, load_scene
spec = json.load(sys.stdin)
m = mujoco.MjModel.from_xml_string(build_scene_xml(load_scene(spec)))
d = mujoco.MjData(m); mujoco.mj_forward(m, d)
out = {}
for o in spec["objects"]:
    g = m.geom(o["id"])
    out[o["id"]] = {"type": int(g.type[0]), "size": [float(v) for v in g.size], "pos": [float(v) for v in d.geom_xpos[g.id]]}
print(json.dumps(out))
"""
    r = subprocess.run(["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "--extra", "sim",
                        "python", "-c", code], input=json.dumps(spec), capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def compare_twin(spec: dict, render: dict) -> dict:
    """Each twin object (MuJoCo-style half sizes, centre pose) == the MuJoCo geom compiled from the same spec."""
    geoms = _mj_geoms(spec)
    types = {"box": 6, "cylinder": 5, "sphere": 2}
    out = {"ok": True, "objects": {}}
    for o in render["objects"]:
        g = geoms[o["id"]]
        n = len(o["size"])
        ok = g["type"] == types[o["type"]] and all(abs(a - b) < 1e-6 for a, b in zip(o["size"], g["size"][:n]))
        ok = ok and math.dist(o["pose"]["pos"], g["pos"]) < 1e-3  # spec pose (before physics / randomization)
        out["objects"][o["id"]] = {"ok": ok, "twin": [o["type"], o["size"], o["pose"]["pos"]], "mujoco": g}
        out["ok"] = out["ok"] and ok
    return out


def render_top(spec: dict, png: Path) -> None:
    code = r"""
import json, sys, os, mujoco
os.environ.setdefault("MUJOCO_GL", "cgl")
from rdf_robot_client.adapters.mujoco_scene import build_scene_xml, load_scene
spec = json.load(sys.stdin)
m = mujoco.MjModel.from_xml_string(build_scene_xml(load_scene(spec)))
d = mujoco.MjData(m); mujoco.mj_forward(m, d)
r = mujoco.Renderer(m, 480, 640); r.update_scene(d, camera="top")
import cv2; cv2.imwrite(sys.argv[1], r.render()[:, :, ::-1])
"""
    subprocess.run(["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "--extra", "sim",
                    "python", "-c", code, str(png)], input=json.dumps(spec), capture_output=True, text=True, timeout=120,
                   env={**os.environ, "MUJOCO_GL": "cgl" if sys.platform == "darwin" else "egl"})


if __name__ == "__main__":
    sys.exit(main())
