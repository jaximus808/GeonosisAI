"""Bus handlers for the flywheel state machine (TDM-22). See ``rdf_orchestrator/flywheel.py``.

* ``ticket`` entering each stage (``on_enter``, also for existing rows at boot) -> run that stage's
  step once, then evaluate the exit condition.
* Any change to a row of the ticket (eval_run, failure_cluster, data_plan, episode, training_run,
  dataset, run_settings, the ticket itself, its robot) -> re-evaluate that ticket.
* A reconcile tick re-evaluates every active ticket (started by the first handler call).

Env:
* ``FLYWHEEL_AUTO_APPROVE_MAX_CENTS`` (default 0 = off): auto-approve a proposed data plan whose
  estimate is at most this and fits the budget, as ``auto:flywheel``. Off means a human approves
  spend on the dashboard / connector.
* ``FLYWHEEL_SIM=1`` (or ``=review,onboarding``): swap every (or the listed) stage that has no
  implementation registered for the SIMULATED step in ``flywheel_sim``. Local servers only.
* ``RDF_MODAL_STUB=1``: queued training runs are marked running with a ``stub-`` call id instead
  of launching Modal (``register_modal_stub``). Local servers only.
"""
from __future__ import annotations

import os

from ..bus import Event, Registry
from ..bus import registry as default_registry
from ..flywheel import ACTIVE, STEP_STAGES, WATCHED_TABLES, Flywheel
from ..log import get_logger

log = get_logger("flywheel")

_ROW_TABLES = ("eval_run", "failure_cluster", "data_plan", "episode", "training_run", "dataset", "run_settings")


def register_flywheel(reg: Registry, fw: Flywheel) -> Flywheel:
    for t in WATCHED_TABLES:
        reg.watch(t)

    for stage in ("created", *STEP_STAGES):
        async def entered(ev: Event, _stage=stage) -> None:
            await fw.on_enter(ev.bus, ev.row, ev.source)

        reg.on_enter("ticket", where=lambda r, s=stage: r.status == s, name=f"flywheel.enter.{stage}")(entered)

    @reg.on_update("ticket", where=lambda r: r.status in ACTIVE, name="flywheel.ticket_changed")
    async def ticket_changed(ev: Event) -> None:
        if ev.old is not None and ev.old.get("status") == ev.row.status:  # status changes go via on_enter
            await fw.evaluate(ev.bus, ev.row.id, cause="ticket")

    for table in _ROW_TABLES:
        async def row_changed(ev: Event, _t=table) -> None:
            tid = ev.row.get("ticket_id")
            if tid:
                await fw.evaluate(ev.bus, tid, cause=_t)

        reg.on_insert(table, name=f"flywheel.{table}.insert")(row_changed)
        reg.on_update(table, name=f"flywheel.{table}.update")(row_changed)

    async def robot_changed(ev: Event) -> None:
        for t in ev.bus.rows("ticket"):
            if t.get("robot_id") == ev.row.get("id") and t.get("status") in ("created", "onboarding"):
                await fw.evaluate(ev.bus, t["id"], cause="robot")

    reg.on_insert("robot", name="flywheel.robot.insert")(robot_changed)
    reg.on_update("robot", name="flywheel.robot.update")(robot_changed)
    return fw


def register_modal_stub(reg: Registry) -> None:
    """``RDF_MODAL_STUB=1`` stand-in for TDM-21's stub mode: a queued training run is marked
    ``running`` with modal_call_id ``stub-<hex>`` and nothing is launched; the TDM-28 harness's
    StubTrainer then finishes it with a fake:// checkpoint. Steps aside if another handler on
    training_run entering queued (the real Modal client) is registered. Refuses maincloud."""
    if "maincloud" in os.environ.get("SPACETIME_HOST", ""):
        raise RuntimeError("RDF_MODAL_STUB=1 against maincloud: refusing to fake training runs")
    import json
    import uuid

    @reg.on_enter("training_run", where=lambda r: r.status == "queued", name="tdm22_modal_stub")
    async def modal_stub(ev: Event) -> None:
        others = [h.name for h in reg.handlers if h.table == "training_run" and h.kind == "enter"
                  and h.name != "tdm22_modal_stub"]
        if others:
            log.info("modal_stub.defer", extra={"fields": {"handlers": others}})
            return
        cur = ev.bus.get("training_run", ev.row.id) or ev.row
        if cur.get("status") != "queued":
            return
        call_id = f"stub-{uuid.uuid4().hex[:10]}"
        log.warning("modal_stub.running", extra={"fields": {"training_run_id": cur["id"], "modal_call_id": call_id}})
        await ev.bus.call_reducer("set_training_status", [cur["id"], "running", {"some": call_id}, {"none": []},
                                                          {"some": json.dumps({"fake": True, "stub": "RDF_MODAL_STUB"})},
                                                          {"none": []}])


def _from_env(reg: Registry) -> Flywheel:
    fw = Flywheel(auto_approve_max_cents=int(os.environ.get("FLYWHEEL_AUTO_APPROVE_MAX_CENTS", "0") or 0))
    sim = os.environ.get("FLYWHEEL_SIM", "").strip()
    if sim:  # "1" = every stage with no implementation; or a comma list of stages
        from ..flywheel_sim import SimScript, install_sim

        stages = None if sim == "1" else {x.strip() for x in sim.split(",") if x.strip()}
        install_sim(fw, SimScript(reject_first_episode=False), registry=reg, stages=stages)
    if os.environ.get("RDF_MODAL_STUB") == "1":
        register_modal_stub(reg)
    if os.environ.get("RDF_TRAINING_DROID") == "1":  # TDM-34: a Training Droid configures + queues the fine-tune
        from rdf_factory.droids.training import TrainingDroidStep

        fw.register_step("train", TrainingDroidStep())
    return fw


FLYWHEEL = register_flywheel(default_registry, _from_env(default_registry))
