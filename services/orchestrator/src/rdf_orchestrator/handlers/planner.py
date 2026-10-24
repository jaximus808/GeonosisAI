"""TDM-24 handler: when a ticket enters ``plan_data``, the planner decides the data type and amount.

    ticket.status -> plan_data  ==>  planner.plan(...) (Claude)  ==>  record_data_plan(...)

``record_data_plan`` (TDM-12) inserts the plan and appends the ``planner.data_plan`` event_log row
with the verbatim reasoning and the full evidence. When no plan can be made (no clusters, budget
exhausted, Claude failed) the handler appends ``planner.blocked`` / ``planner.error`` instead, so the
dashboard shows why the ticket is stuck. It does not move the ticket on: approve_spend and the
flywheel take it from here.

Idempotent across snapshots/resyncs/restarts: skipped when the bus mirror already holds a plan for
the ticket's current iteration, and an in-process claim stops a resync from starting a second Claude
call while the first is still running.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import planner
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("planner")

# Tables the planner reads from the bus mirror.
for _t in ("failure_cluster", "eval_run", "eval_trial", "run_settings", "dataset", "data_plan",
           "dataset_offer", "contributor", "event_log"):
    watch(_t)

_claims: set[tuple[int, int]] = set()
_claims_lock = threading.Lock()
_client: Any = None
_client_factory: Callable[[], Any] = planner.anthropic_client


def _anthropic() -> Any:
    global _client
    if _client is None:
        _client = _client_factory()
    return _client


def set_client_factory(factory: Callable[[], Any]) -> None:
    """Tests: swap the Anthropic client for a fake."""
    global _client, _client_factory
    _client, _client_factory = None, factory


def _event(ev: Event, typ: str, payload: dict) -> None:
    ev.bus.call_reducer_threadsafe("append_event", ["planner", typ, json.dumps(payload, default=str)])


@on_enter("ticket", where=lambda r: r.status == "plan_data", name="tdm24_planner")
def plan_data_for_ticket(ev: Event) -> None:
    t = ev.row
    tid, it = int(t.id), int(t.iteration)
    if any(int(p.get("ticket_id", 0)) == tid and int(p.get("iteration", -1)) == it for p in ev.bus.rows("data_plan")):
        log.info("planner.skip_existing", extra={"fields": {"iteration": it, "source": ev.source}})
        return
    with _claims_lock:
        if (tid, it) in _claims:
            log.info("planner.skip_inflight", extra={"fields": {"iteration": it, "source": ev.source}})
            return
        _claims.add((tid, it))
    try:
        inputs = planner.gather_inputs(ev.bus.rows, t)
        outcome = planner.plan(inputs, _anthropic(),
                               log=lambda msg, f: log.warning(msg, extra={"fields": f}))
    except Exception as e:  # noqa: BLE001 - logged to event_log, claim released so a re-entry can retry
        with _claims_lock:
            _claims.discard((tid, it))
        _event(ev, "planner.error", {"ticket_id": tid, "iteration": it, "error": f"{type(e).__name__}: {e}"})
        raise
    if outcome.blocked:
        _event(ev, "planner.blocked", {"ticket_id": tid, "iteration": it, "reason": outcome.blocked,
                                       "evidence": outcome.evidence})
        log.warning("planner.blocked", extra={"fields": {"reason": outcome.blocked}})
        return
    ev.bus.call_reducer_threadsafe("record_data_plan", outcome.reducer_args())
    log.info("planner.data_plan", extra={"fields": {
        "iteration": it, "data_type": outcome.data_type, "amount": outcome.amount,
        "clusters": outcome.failure_cluster_ids, "forced": outcome.evidence.get("guardrail_forced")}})
