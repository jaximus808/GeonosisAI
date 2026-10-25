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

A failed attempt (refusal, API error, bad output) is retried, bounded and restart-safe
(``llm_retry.py``): one ``planner.error`` {ticket_id, iteration, attempt, kind, max_attempts,
retry_in_s} per failure, counted from event_log; the next attempt runs after a backoff (after a
refusal with ``planner.plan(reduced=True)``). After ``RDF_PLANNER_MAX_ATTEMPTS`` (default 3) one
``planner.failed`` is logged and the flywheel fails the ticket with the reason. ``planner.blocked``
(no clusters, no budget) is a decision, not a failure, and is not retried.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .. import agent_steps, llm_retry, planner
from ..bus import Event, on_enter, watch
from ..log import get_logger

log = get_logger("planner")

# Tables the planner reads from the bus mirror.
for _t in ("failure_cluster", "eval_run", "eval_trial", "run_settings", "dataset", "data_plan",
           "dataset_offer", "contributor", "event_log"):
    watch(_t)

_claims: set[tuple[int, int]] = set()  # (ticket, iteration) in flight or waiting on a scheduled retry
_claims_lock = threading.Lock()
_attempts = llm_retry.Attempts()
RETRY = llm_retry.RetryPolicy.from_env("PLANNER")
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


def _event(bus, typ: str, payload: dict) -> None:
    bus.call_reducer_threadsafe("append_event", ["planner", typ, json.dumps(payload, default=str)])


def _for(tid: int, it: int):
    return lambda p: p.get("ticket_id") == tid and p.get("iteration") == it


def failures(bus, tid: int, it: int) -> list[str]:
    logged = llm_retry.prior_failures(bus.rows("event_log"), "planner.error", _for(tid, it))
    return _attempts.kinds((tid, it), logged)


def gave_up(bus, tid: int, it: int) -> dict | None:
    return llm_retry.find_event(bus.rows("event_log"), "planner.failed", _for(tid, it))


@on_enter("ticket", where=lambda r: r.status == "plan_data", name="tdm24_planner")
def plan_data_for_ticket(ev: Event) -> None:
    _plan(ev.bus, ev.row, ev.source)


def _plan(bus, t, source: str) -> None:
    tid, it = int(t["id"]), int(t["iteration"])
    if any(int(p.get("ticket_id", 0)) == tid and int(p.get("iteration", -1)) == it for p in bus.rows("data_plan")):
        log.info("planner.skip_existing", extra={"fields": {"iteration": it, "source": source}})
        return
    if gave_up(bus, tid, it):
        log.info("planner.skip_failed", extra={"fields": {"iteration": it, "source": source}})
        return
    with _claims_lock:
        if (tid, it) in _claims:
            log.info("planner.skip_inflight", extra={"fields": {"iteration": it, "source": source}})
            return
        _claims.add((tid, it))
    kinds = failures(bus, tid, it)
    if RETRY.exhausted(len(kinds)):  # the last attempt failed but its planner.failed never landed
        _give_up(bus, tid, it, kinds, "retries exhausted before this boot")
        return
    reduced = llm_retry.REFUSAL in kinds
    try:
        with agent_steps.work(tid, "plan_data", iteration=it):
            inputs = planner.gather_inputs(bus.rows, t)
            fails = sum(c.count for c in inputs.clusters)
            agent_steps.step(f"Reading {agent_steps.plural(len(inputs.clusters), 'failure cluster')} "
                             f"({agent_steps.plural(fails, 'failed trial')}), budget "
                             f"${max(0, inputs.budget_cents - inputs.spent_cents) / 100:.2f} left")
            if inputs.dataset_offers:
                agent_steps.step(f"Comparing against {agent_steps.plural(len(inputs.dataset_offers), 'dataset offer')}")
            outcome = planner.plan(inputs, _anthropic(), reduced=reduced,
                                   log=lambda msg, f: log.warning(msg, extra={"fields": f}))
            if outcome.blocked:
                agent_steps.step(f"Blocked: {outcome.blocked}", decision=True, error=True)
            else:
                unit = (outcome.data_type or "").replace("_", " ")
                cost = outcome.amount * outcome.unit_price_cents
                agent_steps.step(f"Proposing {outcome.amount} {unit}{'' if outcome.amount == 1 else 's'}"
                                 + (f" at ${cost / 100:.2f}" if cost else "") + "; waiting for approval",
                                 decision=True)
    except Exception as e:  # noqa: BLE001 - logged to event_log; retried after a backoff, bounded
        kinds = kinds + [llm_retry.classify(e)]
        _attempts.record((tid, it), kinds)
        fields = llm_retry.error_fields(e, kinds, RETRY)
        _event(bus, "planner.error", {"ticket_id": tid, "iteration": it, **fields})
        if RETRY.exhausted(len(kinds)):
            _give_up(bus, tid, it, kinds, fields["error"])  # (tid, it) stays claimed: no more tries
        else:
            log.warning("planner.retry_scheduled", extra={"fields": {
                "iteration": it, "attempt": len(kinds), "kind": kinds[-1], "retry_in_s": fields["retry_in_s"]}})
            llm_retry.schedule(fields["retry_in_s"], lambda: _retry(bus, tid, it), name=f"planner-{tid}-{it}")
        raise
    _attempts.clear((tid, it))
    if outcome.blocked:
        _event(bus, "planner.blocked", {"ticket_id": tid, "iteration": it, "reason": outcome.blocked,
                                        "evidence": outcome.evidence})
        log.warning("planner.blocked", extra={"fields": {"reason": outcome.blocked}})
        return
    bus.call_reducer_threadsafe("record_data_plan", outcome.reducer_args())
    log.info("planner.data_plan", extra={"fields": {
        "iteration": it, "data_type": outcome.data_type, "amount": outcome.amount,
        "clusters": outcome.failure_cluster_ids, "forced": outcome.evidence.get("guardrail_forced"),
        "prompt": outcome.evidence.get("prompt")}})


def _give_up(bus, tid: int, it: int, kinds: list[str], last_error: str) -> None:
    fields = llm_retry.failed_fields(kinds, last_error, RETRY, "no data plan for this iteration; the flywheel "
                                                               "fails the ticket")
    log.error("planner.failed", extra={"fields": {"iteration": it, **fields}})
    try:
        _event(bus, "planner.failed", {"ticket_id": tid, "iteration": it, **fields})
    except Exception:  # noqa: BLE001 - the next boot sees the exhausted count and logs it again
        with _claims_lock:
            _claims.discard((tid, it))
        raise


def _retry(bus, tid: int, it: int) -> None:
    """Timer callback: release the claim and try again if the ticket is still planning this iteration."""
    with _claims_lock:
        _claims.discard((tid, it))
    t = bus.get("ticket", tid)
    if t is None or t.get("status") != "plan_data" or int(t.get("iteration") or 0) != it:
        return
    _plan(bus, t, "retry")
