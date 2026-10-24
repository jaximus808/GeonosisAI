"""TDM-32 proof: the flywheel's onboarding stage starts a REAL local Onboarding Droid (droid-sdk, Factory
credits) that writes and checks the adapter config for a SIMULATED YAM, writes the ticket's success check
and reset routine, and publishes; the flywheel then moves the ticket on to baseline.

    cd services/orchestrator && uv run python scripts/tdm32_onboarding_droid_proof.py

Throwaway local SpacetimeDB on 127.0.0.1:$TDM32_PORT (3132); never maincloud. Workdirs and state under
$TMPDIR/tdm-32-droid; the droid's ticket dir and proof.json are copied to spikes/tdm-32-onboarding/proof/.

  fixtures: robot test.tdm32-yam-sim (embodiment sim_yam, adapter_version "" = not onboarded), ticket
  flywheel: created -> onboarding (robot has no adapter) -> OnboardingDroidStep starts the droid
  droid:    probe -> adapter_config.yaml -> check (contract live on the sim arm through the SafetyLayer,
            cameras + synthetic capture, tighten-only safety, FK/IK round trip) -> success_check.yaml,
            reset_routine.yaml, onboarding_report.md -> publish -> DONE
  publish:  robot.adapter_version = <yam sim adapter>+onb.t<ticket>; event onboarding.report
  flywheel: onboarding -> baseline (robot onboarded)

TDM32_DRY=1 swaps the Factory droid for a scripted stand-in that runs the same allow-listed tool
commands (0 credits) to check the plumbing first. Nothing here touches hardware.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM32_PORT", "3132"))
ROOT = Path(tempfile.gettempdir()) / "tdm-32-droid"
OUT = REPO / "spikes" / "tdm-32-onboarding" / "proof"
ROBOT = "test.tdm32-yam-sim"
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


async def wait(pred, timeout: float, what: str, every: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


class ScriptedDroid:
    """TDM32_DRY=1 stand-in for the droid-sdk backend: does what the brief tells the droid to do."""

    mode = "local"

    def _tool(self, spec, *args):
        import shlex
        import subprocess

        from rdf_factory.backends import _command_allowed
        from rdf_factory.droids import onboarding as droid

        cmd = f"{droid.TOOL_PREFIX} {' '.join(shlex.quote(a) for a in args)}"
        assert _command_allowed(cmd, spec.allow_commands), cmd
        r = subprocess.run(shlex.split(cmd), cwd=spec.cwd, capture_output=True, text=True)
        out = json.loads(r.stdout)
        assert out["ok"], out
        return out

    async def run_turn(self, spec, fsid, text, emit, on_session_id):
        import yaml

        from rdf_factory.backends import RawTurn

        if not fsid:
            fsid = "dry-1"
            await on_session_id(fsid)
        ctxp = text.split("--ctx ")[1].split()[0]
        emit("tool_call", "Execute onboarding_tool probe", tool="Execute", tool_use_id="p",
             input={"command": "rdf_robot_client.onboarding_tool probe (dry)"})
        pr = await asyncio.to_thread(self._tool, spec, "probe", "--ctx", ctxp)
        w = {k: Path(v) for k, v in pr["write_here"].items()}
        w["adapter_config"].write_text(yaml.safe_dump({**pr["draft_adapter_config"], "rationale": ["[DRY] draft kept"]}))
        ck = await asyncio.to_thread(self._tool, spec, "check", "--ctx", ctxp)
        assert ck["all_ok"], ck
        home = pr["draft_adapter_facts"]["home_joints"]
        w["success_check"].write_text(yaml.safe_dump({**pr["templates"]["success_check.yaml"], "start_state": "[DRY] cup on mark",
                                                      "success_when": ["cup in bin"], "failure_when": ["cup not in bin"],
                                                      "ambiguous": "failure", "operator_prompt": "[DRY] cup in bin?"}))
        w["reset_routine"].write_text(yaml.safe_dump({**pr["templates"]["reset_routine.yaml"], "home_joints": home,
                                                      "human_steps": ["cup back"], "operator_prompt": "[DRY] reset"}))
        w["report"].write_text("# [DRY] onboarding report\n" + json.dumps(ck["groups"], default=str)[:3000])
        pub = await asyncio.to_thread(self._tool, spec, "publish", "--ctx", ctxp)
        reply = f"[DRY] published {pub['adapter_version']}\nDONE"
        emit("assistant_text", reply, chars=len(reply))
        return RawTurn(fsid, "success", True, reply, credits=0.0, seconds=0.1)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


async def main() -> int:
    for d in ("sessions", "state"):  # fresh agent_session ids every run: no stale specs
        shutil.rmtree(ROOT / d, ignore_errors=True)
    st = LocalStack(PORT, "tdm32onb", root=ROOT / "stack")
    assert "maincloud" not in st.host
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("factory")
        return await run(st)
    finally:
        st.stop()


async def run(st: LocalStack) -> int:
    orch, fac = st.token("orchestrator"), st.token("factory")
    # Every env-driven client (and the droid's tool subprocess) points at the throwaway DB, as factory.
    os.environ.update({"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "SPACETIME_TOKEN": fac,
                       "FACTORY_MODE": "local"})
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient
    from rdf_factory import FactoryClient, FactoryConfig
    from rdf_factory.droids import onboarding as droid

    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.flywheel import Flywheel
    from rdf_orchestrator.handlers import resume_bridge as rb_handlers
    from rdf_orchestrator.handlers.flywheel import register_flywheel
    from rdf_orchestrator.log import configure_logging
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    configure_logging("WARNING")
    c = SpacetimeClient(st.host, st.db, orch)
    q = st.sql

    # ---- fixtures: a simulated YAM registered WITHOUT an adapter version (not onboarded yet) + a ticket
    arm_info = {"adapter": "yam", "embodiment": "sim_yam", "sim": True, "arm_type": "yam", "gripper_type": "linear_4310",
                "note": "registered for the TDM-32 proof; SIMULATED, not hardware"}
    cams = {"cameras": {"top": {"width": 640, "height": 480, "fps": 30}, "wrist": {"width": 640, "height": 480, "fps": 30}},
            "arm": arm_info}
    c.call_reducer("register_robot", ROBOT, "sim_yam", "", json.dumps(cams))
    rid = int(q(f"SELECT * FROM robot WHERE name = '{ROBOT}'")[0]["id"])
    c.call_reducer("create_ticket", rid, "[SIM] pick up the red cup and put it in the grey bin", 0.8, 5000, "")
    tid = int(st.newest("ticket")["id"])

    cfg = FactoryConfig.from_env("local")
    cfg.state_dir, cfg.work_root = ROOT / "state", ROOT / "sessions"
    dry = {"local": ScriptedDroid()} if os.environ.get("TDM32_DRY") == "1" else None

    def client_factory():
        return FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, fac), backends=dry)

    # ---- the orchestrator side: the real TDM-22 flywheel with the TDM-32 step on `onboarding`
    reg = Registry()
    rb_handlers.register(reg)  # mirrors agent_session (the step's idempotency check reads it)
    fw = Flywheel(steps={"onboarding": droid.OnboardingDroidStep(client_factory=client_factory)}, reconcile_s=5.0)
    register_flywheel(reg, fw)
    bus = Bus(reg, BusClient(host=st.host, db=st.db, token=orch, max_backoff=2.0), workers=4)

    steps_seen: list[str] = []
    statuses: list[str] = []
    stop_poll = threading.Event()

    def poll() -> None:  # what the agents panel shows: agent_session.current_step over time, + ticket status
        while not stop_poll.wait(1.0):
            try:
                for r in q("SELECT * FROM agent_session"):
                    s = f"[{r['status']}] {r['current_step']}"
                    if not steps_seen or steps_seen[-1] != s:
                        steps_seen.append(s)
                t = q(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"]
                if not statuses or statuses[-1] != t:
                    statuses.append(t)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=poll, daemon=True).start()
    t0 = time.time()
    await bus.start()
    await wait(lambda: bus.state == "live", 30, "bus live")

    def session_done():
        rows = [r for r in q("SELECT * FROM agent_session") if r["role"] == droid.ROLE]
        return rows and rows[0]["status"] in ("done", "failed", "paused", "waiting_for") and rows[0]

    row = await wait(session_done, 1500, "the onboarding droid's turn to end", every=1.0)
    try:
        await wait(lambda: q(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] == "baseline", 60, "ticket -> baseline")
    except TimeoutError:
        pass
    await asyncio.sleep(2.0)
    stop_poll.set()

    sid = int(row["id"])
    robot = q(f"SELECT * FROM robot WHERE id = {rid}")[0]
    ticket = q(f"SELECT * FROM ticket WHERE id = {tid}")[0]
    evs = st.events()
    by = lambda t: [json.loads(e["payload_json"]) | {"_actor": e["actor"]} for e in evs if e["type"] == t]  # noqa: E731
    reports = by("onboarding.report")
    droid_ev = [e for e in evs if e["type"].startswith("droid.")]
    turns = [json.loads(e["payload_json"]) for e in evs if e["type"] == "droid.turn_completed"]
    denied = [json.loads(e["payload_json"]) for e in evs if e["type"] == "droid.permission_denied"]
    tools = [json.loads(e["payload_json"]) for e in evs if e["type"] == "droid.tool_call"]
    credits = sum(float(t["data"].get("credits") or 0) for t in turns)
    flips = [json.loads(e["payload_json"]) for e in evs if e["type"] == "ticket.status_changed"]

    check("droid session finished DONE", row["status"] == "done", f"{row['status']}: {row['current_step'][:200]}")
    check("flywheel took the ticket created -> onboarding -> baseline",
          ticket["status"] == "baseline" and "onboarding" in statuses, f"statuses seen {statuses}")
    av = robot["adapter_version"]
    check("robot.adapter_version set by the droid's publish (sim adapter + onboarding stamp)",
          av.endswith(f"+onb.t{tid}") and "sim" in av, av)
    onb = (json.loads(robot["camera_config_json"] or "{}")).get("onboarding") or {}
    check("robot.camera_config_json.onboarding carries the spec, routine and check results",
          onb.get("status") == "passed_sim" and onb.get("success_check") and onb.get("reset_routine")
          and all(onb.get("checks_ok", {}).values()), json.dumps(onb.get("checks_ok")))
    sc = onb.get("success_check") or {}
    check("success check is operator-decided, never automatic", sc.get("decided_by") in ("operator", "human")
          and sc.get("auto_decide") is False, json.dumps(sc.get("success_when"))[:200])
    check("onboarding.report logged by droid:onboarding", len(reports) == 1 and reports[0]["_actor"] == "droid:onboarding")
    check("real-arm (HIL) checks listed as still pending", bool(onb.get("hil_pending")), f"{len(onb.get('hil_pending') or [])} items")
    check("bounds held: no permission denials, only the allow-listed tool ran",
          not denied and all(droid.TOOL_MODULE in json.dumps(t.get("data", {}).get("input", {})) for t in tools
                             if t.get("tool") == "Execute"), f"denials={len(denied)}")
    check(f"<= {droid.MAX_TURNS} turns", 0 < len(turns) <= droid.MAX_TURNS, f"turns={len(turns)}")
    check("steps visible: droid.* events in event_log + agent_session.current_step changes",
          len(droid_ev) > 5 and len(steps_seen) > 3, f"{len(droid_ev)} droid.* events, {len(steps_seen)} step changes")
    tdir = droid.ticket_dir(cfg.work_root, tid)
    files = sorted(p.name for p in tdir.iterdir() if p.is_file())
    check("ticket dir has the droid's files", {"adapter_config.yaml", "success_check.yaml", "reset_routine.yaml",
                                              "onboarding_report.md", "checks.json"} <= set(files), str(files))

    out = {
        "ticket_id": tid, "robot_id": rid, "agent_session_id": sid, "factory_session_id": row.get("factory_session_id"),
        "dry": bool(dry), "adapter_version": av, "ticket_status": ticket["status"], "statuses_seen": statuses,
        "status_changes": [{k: f.get(k) for k in ("from", "to", "note")} for f in flips],
        "turns": [{"turn": t["turn"], "status": t["data"]["status"], "seconds": t["data"]["seconds"],
                   "credits": t["data"]["credits"]} for t in turns],
        "credits_total": credits, "wall_s": round(time.time() - t0, 1), "droid_events": len(droid_ev),
        "droid_event_kinds": sorted({e["type"] for e in droid_ev}), "tool_calls": [t["text"][:200] for t in tools],
        "permission_denials": denied, "current_step_changes": len(steps_seen),
        "current_step_sample": steps_seen[:8] + ["..."] + steps_seen[-4:],
        "onboarding": {k: onb.get(k) for k in ("status", "adapter", "kwargs", "safety_profile", "safety_overrides",
                                               "checks_ok", "hil_pending")},
        "report_checks": (reports[0].get("checks") if reports else None), "ticket_dir_files": files,
    }
    await bus.stop()
    out["checks"] = {"passed": sum(ok for _, ok, _ in checks), "failed": [n for n, ok, _ in checks if not ok]}
    (ROOT / "proof.json").write_text(json.dumps(out, indent=2, default=str))
    if not dry:
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True, exist_ok=True)
        for f in files:
            if f != "droid_context.json":
                shutil.copy2(tdir / f, OUT / f)
        (OUT / "proof.json").write_text(json.dumps(out, indent=2, default=str))
        ev_path = cfg.state_dir / f"{sid}.events.jsonl"
        if ev_path.exists():
            shutil.copy2(ev_path, OUT / "droid.events.jsonl")
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["checks"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
