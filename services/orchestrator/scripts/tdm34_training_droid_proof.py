"""TDM-34 proof: a REAL local Training Droid (droid-sdk, ~3 turns of Factory credits) configures,
launches (through TDM-21) and monitors a fine-tune, retries one forced OOM, and reports.

    cd services/orchestrator && uv run python scripts/tdm34_training_droid_proof.py

NO GPU SPEND: Modal is stubbed. TDM-21's real ``handlers/training.py`` launch/track handlers run on the
bus with a ``FakeModal`` backend injected into ``TrainingRunner`` (the stub path): ``convert`` returns a
fake dataset URI, the first ``finetune`` raises a forced RESOURCE_EXHAUSTED (OOM) and the second
returns a ``fake://`` checkpoint with a synthetic loss curve. Every fake value is tagged fake.
Throwaway local SpacetimeDB on 127.0.0.1:$TDM34_PORT (3134); never maincloud. Workdirs and state
live under $TMPDIR/tdm-34-droid.

  turn 1 (start): inspect -> droid writes configs/train_<t>_<i>.yaml -> queue -> parks on the run
  TDM-21: queued -> running (convert) -> running (finetune) -> failed (forced OOM)
  turn 2 (TDM-27 bridge wake): status -> retry plan -> retry config -> queue -> parks again
  TDM-21: queued -> running -> done (fake checkpoint + loss curve)
  turn 3 (bridge wake): status (loss analysis) -> training note -> report -> DONE

TDM34_DRY=1 swaps the Factory droid for a scripted stand-in that runs the same allow-listed tool
commands (0 credits): use it to check the plumbing before spending credits.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT = int(os.environ.get("TDM34_PORT", "3134"))
ROOT = Path(tempfile.gettempdir()) / "tdm-34-droid"
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)


async def wait(pred, timeout: float, what: str, every: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


class ScriptedDroid:
    """TDM34_DRY=1 stand-in for the droid-sdk backend: runs the tool the way the brief tells the droid to."""

    mode = "local"

    def __init__(self):
        self.n = 0

    def _tool(self, spec, *args):
        import shlex
        import subprocess

        from rdf_factory.backends import _command_allowed
        from rdf_factory.droids import training as droid

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
        self.n += 1
        ctxp = text.split("--ctx ")[1].split()[0] if "--ctx " in text else self.ctx
        self.ctx = ctxp
        tdir = Path(ctxp).parent
        emit("tool_call", f"Execute training_tool (turn {self.n})", tool="Execute", tool_use_id=f"t{self.n}", input={})
        if self.n == 1:
            ins = self._tool(spec, "inspect", "--ctx", ctxp)
            hp = ins["recommendation"]["hyperparams"]
            p = Path(ins["config_path"])
            p.write_text(yaml.safe_dump({"ticket_id": ins["ticket"]["id"], "iteration": ins["ticket"]["iteration"],
                                         "episode_ids": ins["checks"]["episode_ids"], "hyperparams": hp,
                                         "rationale": ins["recommendation"]["rationale"]}))
            qd = self._tool(spec, "queue", "--ctx", ctxp, "--config", str(p))
            self.run = qd["training_run_id"]
            reply = f"queued run {self.run}\n{qd['waiting_line']}"
        elif self.n == 2:
            stt = self._tool(spec, "status", "--ctx", ctxp, "--run", str(self.run))
            p = tdir / "configs" / "retry1.yaml"
            p.write_text(yaml.safe_dump({"episode_ids": None, "hyperparams": stt["retry_plan"]["hyperparams"],
                                         "rationale": stt["retry_plan"]["rationale"], "retry_of": self.run}))
            qd = self._tool(spec, "queue", "--ctx", ctxp, "--config", str(p))
            self.run = qd["training_run_id"]
            reply = f"retry queued {self.run}\n{qd['waiting_line']}"
        else:
            stt = self._tool(spec, "status", "--ctx", ctxp, "--run", str(self.run))
            note = tdir / "training_note_dry.md"
            note.write_text(f"[DRY] verdict {stt['analysis']['verdict']}: {stt['analysis']['notes']}")
            self._tool(spec, "report", "--ctx", ctxp, "--run", str(self.run), "--note", str(note))
            reply = "reported\nDONE"
        emit("assistant_text", reply, chars=len(reply))
        return RawTurn(fsid, "success", True, reply, credits=0.0, seconds=0.1)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


class FakeModal:
    """ModalBackend for TDM-21's TrainingRunner. No Modal calls, no GPU. Finetune #1 OOMs on purpose."""

    def __init__(self, job_s: float = 2.0):
        self.job_s = job_s
        self.calls: dict[str, dict] = {}
        self.spawns: list[dict] = []
        self.finetunes = 0
        self.lock = threading.Lock()

    def spawn(self, function: str, **kw) -> str:
        with self.lock:
            cid = f"fake-{function}-{uuid.uuid4().hex[:8]}"
            n = None
            if function == "finetune":
                self.finetunes += 1
                n = self.finetunes
            self.calls[cid] = {"fn": function, "kw": kw, "t": time.monotonic(), "n": n}
            self.spawns.append({"call_id": cid, "function": function, **{k: v for k, v in kw.items() if k != "episode_manifest"},
                                **({"episodes": len(kw["episode_manifest"]["episodes"])} if "episode_manifest" in kw else {})})
            return cid

    def poll(self, call_id: str):
        from rdf_orchestrator.modal_client import PollResult

        c = self.calls[call_id]
        if time.monotonic() - c["t"] < self.job_s:
            return PollResult("pending")
        if c["fn"] == "convert":
            return PollResult("done", value=f"modal-vol://rdf-datasets/lerobot/{c['kw']['episode_manifest']['repo_id']}-FAKE")
        hp = c["kw"]["hyperparams"]
        if c["n"] == 1:
            return PollResult("failed", error=(
                "XlaRuntimeError: RESOURCE_EXHAUSTED: Out of memory while trying to allocate 18.4GiB "
                f"(batch_size={hp.get('batch_size')}, H100 80GB) [FAKE: forced OOM for the TDM-34 proof]"))
        steps = int(hp["steps"])
        curve = [[s, round(0.06 + 0.42 * math.exp(-3.5 * s / steps) + 0.004 * math.sin(s / 37.0), 5)]
                 for s in range(steps // 40, steps + 1, steps // 40)]
        return PollResult("done", value={
            "checkpoint_uri": f"fake://tdm34/ticket-{c['kw']['ticket_id']}/run-{hp.get('training_run_id')}/{steps}",
            "metrics": {"fake": True, "stub": "tdm34 FakeModal", "steps": steps, "loss_curve": curve,
                        "first_loss": curve[0][1], "final_loss": curve[-1][1], "wall_s": 0, "gpu": None}})

    def cancel(self, call_id: str) -> None:
        pass


async def main() -> int:
    import shutil

    for d in ("sessions", "state"):  # fresh agent_session ids every run: no stale specs/configs
        shutil.rmtree(ROOT / d, ignore_errors=True)
    st = LocalStack(PORT, "tdm34droid", root=ROOT / "stack")
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
    # Point every env-driven client (and the droid's tool subprocess) at the throwaway DB before
    # rdf_factory loads .env (setdefault). The droid's tool acts as the factory service.
    os.environ.update({"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "SPACETIME_TOKEN": fac,
                       "FACTORY_MODE": "local"})
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient
    from rdf_factory import FactoryClient, FactoryConfig
    from rdf_factory.droids import training as droid

    from rdf_orchestrator import resume_bridge as rb
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.handlers import resume_bridge as rb_handlers
    from rdf_orchestrator.handlers import training as tdm21
    from rdf_orchestrator.log import configure_logging
    from rdf_orchestrator.modal_client import TrainingRunner
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    configure_logging("WARNING")
    c = SpacetimeClient(st.host, st.db, orch)
    q = st.sql

    # ---- fixtures: robot, ticket, 5 accepted 20 s demos (fake media URIs)
    c.call_reducer("register_robot", "test.tdm34-droid-robot", "yam", "fake", "{}")
    rid = int(q("SELECT * FROM robot WHERE name = 'test.tdm34-droid-robot'")[0]["id"])
    c.call_reducer("create_ticket", rid, "[FAKE] pick up the red cube and place it in the bin", 0.8, 5000, "")
    tid = int(st.newest("ticket")["id"])
    it = int(st.newest("ticket")["iteration"])
    for i in range(5):
        frames = 600 + 30 * i
        cams = json.dumps({"exo": f"fake://tdm34/ep{i}/exo.mp4", "wrist": f"fake://tdm34/ep{i}/wrist.mp4"})
        c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", f"fake://tdm34/ep{i}",
                       f"fake://tdm34/ep{i}/actions.parquet", cams, frames, int(frames / 30 * 1000), None, None, None, None)
        c.call_reducer("submit_review", int(st.newest("episode")["id"]), True, "[FAKE] ok", "{}")

    # ---- the orchestrator side: TDM-21's own handlers (FakeModal injected) + the TDM-27 bridge
    fake = FakeModal()
    tdm21.RUNNER = TrainingRunner(writer=lambda: SpacetimeClient(st.host, st.db, orch), modal=lambda: fake, poll_s=1.0)
    reg = Registry()
    for t in ("ticket", "episode"):
        reg.watch(t)
    reg.on_enter("training_run", where=lambda r: r.status == "queued", name="tdm21_training_launch")(tdm21.launch_training)
    reg.on_enter("training_run", where=lambda r: r.status == "running", name="tdm21_training_track")(tdm21.track_training)
    rb_handlers.register(reg)

    cfg = FactoryConfig.from_env("local")
    cfg.state_dir, cfg.work_root = ROOT / "state", ROOT / "sessions"
    dry = {"local": ScriptedDroid()} if os.environ.get("TDM34_DRY") == "1" else None
    starter = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, fac), backends=dry)
    bridge_client = FactoryClient(cfg, mode="local", stdb=AsyncSpacetimeClient(st.host, st.db, fac), backends=dry)
    wakes: list[tuple[int, str, float]] = []

    class Timed:
        async def resume(self, agent_session_id, text, *, reason="", claim=True, finish_on_idle=False):
            wakes.append((agent_session_id, text, time.time()))
            print(f"... bridge wakes agent_session {agent_session_id}: {text[:160]}", flush=True)
            return await bridge_client.resume(agent_session_id, text, reason=reason, claim=claim,
                                              finish_on_idle=finish_on_idle)

    bus = Bus(reg, BusClient(host=st.host, db=st.db, token=orch, max_backoff=2.0), workers=4)
    br = rb.ResumeBridge(bus, writer_factory=lambda: SpacetimeClient(st.host, st.db, orch),
                         resumer_factory=lambda: Timed(), poll_s=5, bridge_id="tdm34-bridge")
    rb._instances[id(bus)] = br
    await bus.start()
    await wait(lambda: bus.state == "live", 30, "bus live")

    steps_seen: list[str] = []
    stop_poll = threading.Event()

    def poll_steps() -> None:  # what the agents panel would show: agent_session.current_step over time
        while not stop_poll.wait(1.0):
            try:
                for r in q("SELECT * FROM agent_session"):
                    s = f"[{r['status']}] {r['current_step']}"
                    if not steps_seen or steps_seen[-1] != s:
                        steps_seen.append(s)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=poll_steps, daemon=True).start()

    # ---- turn 1
    t0 = time.time()
    r1 = await droid.launch_training_droid(
        starter, tid, it, host=st.host, db=st.db,
        stub_note="[PROOF] Modal is stubbed for this run (fake backend, no GPU): media URIs are fake:// on purpose.")
    sid = r1.agent_session_id
    print(f"turn 1: status={r1.status} credits={r1.credits} {r1.seconds}s", flush=True)
    check("turn 1: droid queued a run and parked (waiting_for)", r1.status == "waiting_for", json.dumps(r1.waiting_for)[:200])
    out: dict = {"ticket_id": tid, "iteration": it, "agent_session_id": sid, "factory_session_id": r1.factory_session_id}
    if r1.status != "waiting_for":
        out["turn1_text"] = r1.text[-1500:]
        print(json.dumps(out, indent=2, default=str))
        return 1

    # ---- let TDM-21 + the bridge drive the rest
    def final():
        rows = q(f"SELECT * FROM agent_session WHERE id = {sid}")
        return rows and rows[0]["status"] in ("done", "failed") and rows[0]
    row = await wait(final, 1200, "the droid session to finish", every=1.0)
    await asyncio.sleep(2.0)
    stop_poll.set()

    runs = sorted(q(f"SELECT * FROM training_run WHERE ticket_id = {tid}"), key=lambda r: int(r["id"]))
    evs = st.events()
    by = lambda t: [json.loads(e["payload_json"]) | {"_actor": e["actor"]} for e in evs if e["type"] == t]  # noqa: E731
    cfg_ev, rep_ev = by("training.config"), by("training.report")
    status_ev = by("training.status")
    droid_ev = [e for e in evs if e["type"].startswith("droid.")]
    turns = [json.loads(e["payload_json"]) for e in evs if e["type"] == "droid.turn_completed"]
    credits = sum(float(t["data"].get("credits") or 0) for t in turns)
    ft = [s for s in fake.spawns if s["function"] == "finetune"]

    check("session finished DONE", row["status"] == "done", row["current_step"][:200])
    check("exactly 2 training runs: the first failed (forced OOM), the retry is done",
          len(runs) == 2 and runs[0]["status"] == "failed" and runs[1]["status"] == "done",
          str([(r["id"], r["status"]) for r in runs]))
    if len(runs) == 2:
        hp1, hp2 = json.loads(runs[0]["hyperparams_json"]), json.loads(runs[1]["hyperparams_json"])
        check("handoff: the droid's config is training_run.hyperparams_json and reached finetune() via TDM-21",
              len(ft) == 2 and ft[0]["hyperparams"]["steps"] == hp1["steps"] and ft[0]["hyperparams"]["droid_session_id"] == sid,
              f"run1 {({k: hp1.get(k) for k in ('lr', 'steps', 'batch_size', 'lora_rank')})}")
        check("retry adjusted hyperparams for the OOM (smaller batch) and names retry_of",
              int(hp2["batch_size"]) < int(hp1["batch_size"]) and hp2.get("retry_of") == runs[0]["id"],
              f"run2 {({k: hp2.get(k) for k in ('lr', 'steps', 'batch_size', 'lora_rank', 'retry_of')})}")
        out["runs"] = [{"id": r["id"], "status": r["status"], "error": (r.get("error") or "")[:160],
                        "checkpoint_uri": r.get("checkpoint_uri"), "hyperparams": json.loads(r["hyperparams_json"])} for r in runs]
    check("TDM-21 drove both runs queued -> running -> failed/done (training.status events)",
          sum(1 for e in status_ev if e.get("to") == "running") >= 2 and any(e.get("to") == "done" for e in status_ev),
          f"{len(status_ev)} training.status events")
    check("droid logged its config + rationale as actor droid:training (twice: run + retry)",
          len(cfg_ev) == 2 and all(e["_actor"] == "droid:training" and e.get("rationale") for e in cfg_ev))
    check("droid logged a training.report with the loss analysis + note",
          len(rep_ev) == 1 and rep_ev[0]["_actor"] == "droid:training" and rep_ev[0].get("analysis"),
          (rep_ev[0].get("analysis") or {}).get("verdict", "") if rep_ev else "")
    resumed = by("agent.resumed")
    check("parked twice and the TDM-27 bridge resumed it twice (one claim each)", len(wakes) == 2 and len(resumed) == 2,
          f"wakes={len(wakes)} agent.resumed={len(resumed)}")
    check("3 turns, all on the same Factory session (protocol A)",
          len(turns) == 3 and len({t["factory_session_id"] for t in turns}) == 1, f"turns={len(turns)}")
    tdir = droid.ticket_dir(cfg.work_root, tid, it)
    files = sorted(str(p.relative_to(tdir)) for p in tdir.rglob("*") if p.is_file())
    check("config + retry config + note written in the ticket dir", any("retry" in f for f in files)
          and any(f.startswith("training_note") for f in files), str(files))

    out.update({
        "turns": [{"turn": t["turn"], "status": t["data"]["status"], "seconds": t["data"]["seconds"],
                   "credits": t["data"]["credits"]} for t in turns],
        "credits_total": credits, "wall_s": round(time.time() - t0, 1), "droid_events": len(droid_ev),
        "droid_event_kinds": sorted({e["type"] for e in droid_ev}),
        "current_step_changes": len(steps_seen), "current_step_sample": steps_seen[:6] + ["..."] + steps_seen[-4:],
        "wakes": [w[1][:200] for w in wakes], "fake_modal_spawns": fake.spawns,
        "config_rationale": cfg_ev[0].get("rationale") if cfg_ev else None,
        "retry_rationale": cfg_ev[1].get("rationale") if len(cfg_ev) > 1 else None,
        "report_analysis": rep_ev[0].get("analysis") if rep_ev else None,
        "training_note": rep_ev[0].get("note") if rep_ev else None,
        "ticket_dir_files": files,
    })
    await bus.stop()
    br.stop()
    await starter.aclose()
    await bridge_client.aclose()
    out["checks"] = {"passed": sum(ok for _, ok, _ in checks), "failed": [n for n, ok, _ in checks if not ok]}
    (ROOT / "proof.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    return 0 if not out["checks"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
