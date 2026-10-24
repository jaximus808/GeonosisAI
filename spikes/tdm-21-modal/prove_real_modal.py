"""TDM-21 proof: the real Modal client (no stub) against the deployed rdf-training app.

Boots a throwaway local SpacetimeDB (TDM-28 LocalStack, port TDM21_PORT=3121, never maincloud),
creates a [test.tdm21] ticket with two accepted episodes, then runs ONLY the TDM-21 handlers on a bus
(no diagnose/planner/payments). Two runs, each with a simulated orchestrator restart mid-flight:

  A  default: spawn convert on the run's episodes -> (finetune) -> done | failed
  B  hyperparams.dataset_uri: skip convert, spawn finetune (TDM-38 stub, ~20 s CPU) -> done

    cd services/orchestrator && uv run python ../../spikes/tdm-21-modal/prove_real_modal.py

Costs: the TDM-38 stubs request no GPU (cpu=1 finetune sleeps 20 s; cpu=8 convert).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for  # noqa: E402

from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402
from rdf_orchestrator.bus import Bus, Registry  # noqa: E402
from rdf_orchestrator.modal_client import RealModal, TrainingRunner  # noqa: E402
from rdf_orchestrator.stdb import SpacetimeClient as BusStdb  # noqa: E402

os.environ.pop("RDF_MODAL_STUB", None)
PORT = int(os.environ.get("TDM21_PORT", "3121"))


def make_bus(st: LocalStack) -> tuple[Bus, TrainingRunner]:
    writer = SpacetimeClient(st.host, st.db, st.token("orchestrator"))
    runner = TrainingRunner(writer=lambda: writer, modal=RealModal, poll_s=3)
    reg = Registry()
    reg.watch("ticket")
    reg.watch("episode")

    @reg.on_enter("training_run", where=lambda r: r.status == "queued", name="tdm21_training_launch")
    def launch(ev):
        runner.bind(ev.bus)
        runner.launch(dict(ev.row))

    @reg.on_enter("training_run", where=lambda r: r.status == "running", name="tdm21_training_track")
    def track(ev):
        runner.bind(ev.bus)
        runner.track(dict(ev.row))

    client = BusStdb(host=st.host, db=st.db, token=st.token("orchestrator"))
    return Bus(reg, client, workers=2), runner


def run_row(st, rid):
    return st.sql(f"SELECT * FROM training_run WHERE id = {rid}")[0]


def show(tag, r):
    m = r.get("metrics_json") or "{}"
    print(f"[tdm21] {tag}: run {r['id']} status={r['status']} modal_call_id={r.get('modal_call_id')} "
          f"ckpt={r.get('checkpoint_uri')} cost={r.get('gpu_cost_cents')} error={r.get('error')} metrics={m[:400]}",
          flush=True)


async def drive(st, rid, restart_when) -> dict:
    """Run the bus until restart_when(row) is true, 'crash' it, boot a fresh one, run to terminal."""
    bus, runner = make_bus(st)
    await bus.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        r = run_row(st, rid)
        if restart_when(r) or r["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.5)
    show("before restart", r)
    first_call_seen_s = round(time.monotonic() - t0, 1)
    runner.stop()
    await bus.stop()  # process memory gone: tracked set, launching set, Modal handles
    print(f"[tdm21] -- orchestrator 'restarted' after {first_call_seen_s}s --", flush=True)

    bus2, runner2 = make_bus(st)
    await bus2.start()
    t1 = time.monotonic()
    while time.monotonic() - t1 < 600:
        r = run_row(st, rid)
        if r["status"] in ("done", "failed", "cancelled"):
            break
        await asyncio.sleep(1)
    runner2.stop()
    await bus2.stop()
    show("final", r)
    return r


def main() -> None:
    st = LocalStack(port=PORT, db="tdm21proof")
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        c = st.client()
        c.call_reducer("register_robot", "test.tdm21-yam", "i2rt_yam", "test", "{}")
        robot = st.sql("SELECT * FROM robot WHERE name = 'test.tdm21-yam'")[0]
        c.call_reducer("create_ticket", int(robot["id"]), "[test.tdm21] put the cup in the bin", 0.8, 5000, "")
        tid = int(st.newest("ticket")["id"])
        for i in range(2):
            c.call_reducer("create_episode", tid, 0, 0, "clean_teleop",
                           f"modal-vol://rdf-media/tickets/{tid}/episodes/test-{i}",
                           f"modal-vol://rdf-media/tickets/{tid}/episodes/test-{i}/actions.parquet",
                           json.dumps({"exo": f"modal-vol://rdf-media/tickets/{tid}/episodes/test-{i}/exo.mp4"}),
                           300, 10000, None, None, None, None)
        eps = [int(e["id"]) for e in st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")]
        for e in eps:
            c.call_reducer("submit_review", e, True, "", json.dumps({"test": "tdm21"}))

        # A: real convert on the run's episodes; restart as soon as the call id is on the row.
        t0 = time.monotonic()
        c.call_reducer("queue_training_run", tid, json.dumps(eps), json.dumps({"steps": 10, "stub_sleep_s": 15}))
        ra = st.newest("training_run", f"ticket_id = {tid}")
        a = asyncio.run(drive(st, int(ra["id"]), lambda r: bool(r.get("modal_call_id"))))
        print(f"[tdm21] run A finished in {time.monotonic() - t0:.1f}s", flush=True)

        # B: dataset_uri -> finetune only; restart while the finetune call is running.
        c.call_reducer("queue_training_run", tid, json.dumps(eps), json.dumps(
            {"steps": 10, "stub_sleep_s": 20, "dataset_uri": "modal-vol://rdf-datasets/lerobot/rdf/test-tdm21"}))
        rb = st.newest("training_run", f"ticket_id = {tid}")
        t0 = time.monotonic()
        b = asyncio.run(drive(st, int(rb["id"]), lambda r: r["status"] == "running"))
        print(f"[tdm21] run B finished in {time.monotonic() - t0:.1f}s", flush=True)

        for e in st.events():
            if e["type"] == "training.status":
                print(f"[tdm21] event {e['id']} {e['payload_json'][:300]}")
        print(json.dumps({"A": {k: a.get(k) for k in ("status", "modal_call_id", "checkpoint_uri", "error")},
                          "B": {k: b.get(k) for k in ("status", "modal_call_id", "checkpoint_uri", "error")}}, indent=1))
    finally:
        st.stop()


if __name__ == "__main__":
    main()
