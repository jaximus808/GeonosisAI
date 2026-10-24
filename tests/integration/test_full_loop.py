"""TDM-28: a ticket goes created -> shipped on a laptop with no arm (fake robot + stub Modal).

Driver selection (env ``RDF_LOOP_DRIVER``, or ``make test-loop DRIVER=...``):
  * ``scripted``      (default) ``scripted_flywheel.ScriptedFlywheel`` calls the same reducers the
                      orchestrator will, tagged ``[FAKE scripted driver]``. Proves the fake robot,
                      the reducer chain and the stub Modal trainer end to end.
  * ``orchestrator``  the real orchestrator process (rdf-orchestrator against the local DB) drives
                      the flywheel; the test only creates the ticket and watches. Skipped until the
                      TDM-22 flywheel is registered (``rdf_orchestrator.handlers.flywheel``).
                      The dispatcher's paid tasks are claimed and done by
                      ``tdm26_fake_contributor.FakeContributor`` ([FAKE], local only).
Both modes assert the same end state: shipped within 3 minutes with a data_plan, at least one
training_run done, at least two finalized eval_runs, every eval_trial tagged [FAKE], and an
unbroken append-only event_log.
"""
from __future__ import annotations

import os
import time

import pytest

from conftest import assert_event_log_unbroken, new_ticket
from harness import wait_for
from scripted_flywheel import ScriptedFlywheel
from stub_modal import FAKE_CKPT_PREFIX, StubTrainer
from tdm26_fake_contributor import FakeContributor

LOOP_TIMEOUT_S = 180


def _orchestrator_handlers() -> tuple[bool, set[str]]:
    """(flywheel registered?, tables the orchestrator's handlers watch)."""
    import sys

    from rdf_orchestrator import handlers  # noqa: F401 - registers handlers
    from rdf_orchestrator.bus import registry

    return "rdf_orchestrator.handlers.flywheel" in sys.modules, {h.table for h in registry.handlers}


def test_ticket_reaches_shipped(stack, robot_id, event_watch):
    st = stack
    driver = os.environ.get("RDF_LOOP_DRIVER") or "scripted"
    has_training_handler = False
    if driver == "orchestrator":  # scripted mode never imports other tickets' in-progress handlers
        has_flywheel, watched = _orchestrator_handlers()
        if not has_flywheel:
            pytest.skip("orchestrator driver: TDM-22 flywheel not registered in rdf_orchestrator.handlers yet")
        has_training_handler = "training_run" in watched
    # TODO(TDM-21): in orchestrator mode the Modal client must honour RDF_MODAL_STUB=1 (see stub_modal.py).
    trainer = StubTrainer(st.client(), mode="stub-call" if driver == "orchestrator" and has_training_handler
                          else "queued").start()
    t0 = time.monotonic()
    contributor = None
    try:
        tid = new_ticket(st, robot_id, "put the cup in the bin (full loop)",
                         '{"eval_trials": 10, "target_episodes": 3, "max_iterations": 3}')
        if driver == "orchestrator":
            contributor = FakeContributor(st.host, st.db, ticket_id=tid).start()  # does the dispatcher's tasks
            st.spawn_orchestrator(extra_env={"RDF_MODAL_STUB": "1"})
            wait_for(lambda: st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] in ("shipped", "failed"),
                     LOOP_TIMEOUT_S, f"ticket {tid} shipped (orchestrator log: {st.logs / 'orchestrator.log'})", every=1)
        else:
            ScriptedFlywheel(st, robot_id).run(tid)
    finally:
        trainer.stop()
        if contributor is not None:
            contributor.stop()
    elapsed = time.monotonic() - t0
    print(f"\n[tdm28] driver={driver} ticket {tid} finished in {elapsed:.1f}s")

    ticket = st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
    assert ticket["status"] == "shipped", ticket
    assert elapsed < LOOP_TIMEOUT_S
    assert st.sql(f"SELECT * FROM data_plan WHERE ticket_id = {tid}"), "no data_plan"
    trains = [r for r in st.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}") if r["status"] == "done"]
    assert trains and all(r["checkpoint_uri"].startswith(FAKE_CKPT_PREFIX) for r in trains)
    evals = [r for r in st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}") if r["status"] == "done"]
    assert len(evals) >= 2 and {"baseline", "candidate"} <= {r["kind"] for r in evals}
    for r in evals:
        trials = st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {r['id']}")
        assert len(trials) == r["trials"] and all(t["failure_note"].startswith("[FAKE]") for t in trials)
    assert not trainer.errors, trainer.errors

    ev = assert_event_log_unbroken(st, event_watch)
    statuses = [e for e in ev if e["type"] == "ticket.status_changed" and f'"ticket_id":{tid}' in e["payload_json"].replace(" ", "")]
    assert statuses, "no ticket.status_changed events for the ticket"
