"""Real-robot reset asks: the operator finishes a reset task from the cockpit, or by texting DONE.

A sim robot resets its own scene (``evaluate.solo_reset``). A real arm waits in ``waiting_reset`` until its
open reset task is ``completed``; the dispatcher then pays it and issues ``resume_policy`` (TDM-26). This
module is the one place that turns "the operator says the scene is reset" into reducer calls:

  open task, operator contributor set   claim_task(task, cid) + complete_task   (paid like any reset)
  open task, no contributor (cid 0)     cancel_task + issue_robot_command(resume_policy)   (nobody paid)
  claimed task                          complete_task (the orchestrator is a market admin)
  completed / cancelled                 nothing: already done

Entry points:
  POST /resets/{task_id}/done   (reset_api; X-RDF-Operator-Key, the cockpit's "Done, scene reset" button)
  a DONE text reply             (text_offers.on_inbound, to a reset ask the number was sent)

Env: ``RDF_RESET_TEXT_CONTRIBUTOR`` (0 = off): the operator's contributor id. Reset tasks on a REAL robot are
texted to that contributor's opted-in number ("Reset the scene for TKT-0042 after trial 2/3 ..."), and the
cockpit button claims + completes resets as them. The number is read from the private text store, never
from env or code.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

ACTOR = "orchestrator:reset_done"
EV_DONE = "eval.reset_done"
_TRIAL_REF = re.compile(r"\[ref eval_trial:(\d+)\]")


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def reset_contributor() -> int:
    """RDF_RESET_TEXT_CONTRIBUTOR (environment or the repo .env); 0 = not set."""
    v = os.environ.get("RDF_RESET_TEXT_CONTRIBUTOR", "").strip()
    if not v:
        try:
            from rdf_common.settings import RdfSettings

            class _S(RdfSettings):
                rdf_reset_text_contributor: int = 0

            return max(0, int(_S().rdf_reset_text_contributor))
        except Exception:  # noqa: BLE001
            return 0
    try:
        return max(0, int(v))
    except ValueError:
        return 0


def tkt(ticket_id: int) -> str:
    return f"TKT-{int(ticket_id):04d}"


def trial_of(task: dict, rows: Callable[[str], list]) -> tuple[int, int] | None:
    """(trials done, trials planned) for an eval-trial reset (ref ``eval_trial:<trial id>``), else None."""
    m = _TRIAL_REF.search(task.get("instructions") or "")
    if not m:
        return None
    trial = next((t for t in rows("eval_trial") if _i(t.get("id")) == int(m.group(1))), None)
    if trial is None:
        return None
    run = next((r for r in rows("eval_run") if _i(r.get("id")) == _i(trial.get("eval_run_id"))), None)
    total = _i((run or {}).get("trials"))
    done = _i(trial.get("trial_index")) + 1
    return (done, total) if total else (done, 0)


def trial_words(tr: tuple[int, int] | None) -> str:
    """"after trial 2/3" ("after trial 2" without a planned count, "" when unknown)."""
    if not tr:
        return ""
    done, total = tr
    return f"after trial {done}/{total}" if total else f"after trial {done}"


def plan(task: dict, contributor_id: int, *, via: str) -> list[tuple]:
    """The reducer calls that finish this reset task (see the module docstring). [] = already done.
    Raises ValueError for a task that is not a reset."""
    if task.get("type") != "reset":
        raise ValueError(f"task {task.get('id')} is a {task.get('type')} task, not a reset")
    tid, status = _i(task.get("id")), task.get("status")
    result = json.dumps({"by": ACTOR, "via": via, "operator_reset": True}, separators=(",", ":"))
    if status == "claimed":
        return [("complete_task", tid, result)]
    if status != "open":
        return []
    if contributor_id > 0:
        return [("claim_task", tid, int(contributor_id)), ("complete_task", tid, result)]
    return fallback(task, via=via)


def fallback(task: dict, *, via: str) -> list[tuple]:
    """No contributor (or the claim was refused): withdraw the task and release the robot ourselves."""
    tid = _i(task.get("id"))
    ref = _TRIAL_REF.search(task.get("instructions") or "")
    return [("cancel_task", tid, f"operator reset ({via}): scene reset by the operator"),
            ("issue_robot_command", _i(task.get("robot_id")), "resume_policy",
             json.dumps({"reason": "operator_reset", "task_id": tid,
                         "ref": f"eval_trial:{ref.group(1)}" if ref else None}), _i(task.get("ticket_id")))]


def run_sync(call: Callable[..., Any], task: dict, contributor_id: int, *, via: str) -> dict:
    """Execute ``plan`` with a sync reducer caller (the bus writer). A refused claim falls back."""
    steps = plan(task, contributor_id, via=via)
    if not steps:
        return {"ok": True, "already": task.get("status"), "task_id": _i(task.get("id"))}
    mode = "completed_as_contributor" if steps[0][0] == "claim_task" else (
        "completed" if steps[0][0] == "complete_task" else "cancelled_and_resumed")
    try:
        call(*steps[0])
    except Exception:  # noqa: BLE001 - claim refused (contributor busy, raced): cancel + resume instead
        if steps[0][0] != "claim_task":
            raise
        steps, mode = fallback(task, via=via), "cancelled_and_resumed"
        call(*steps[0])
    for s in steps[1:]:
        call(*s)
    return {"ok": True, "mode": mode, "task_id": _i(task.get("id"))}


async def run_async(call: Callable[..., Any], task: dict, contributor_id: int, *, via: str) -> dict:
    """Same as ``run_sync`` with an async reducer caller (AsyncSpacetimeClient.call_reducer)."""
    steps = plan(task, contributor_id, via=via)
    if not steps:
        return {"ok": True, "already": task.get("status"), "task_id": _i(task.get("id"))}
    mode = "completed_as_contributor" if steps[0][0] == "claim_task" else (
        "completed" if steps[0][0] == "complete_task" else "cancelled_and_resumed")
    try:
        await call(*steps[0])
    except Exception:  # noqa: BLE001
        if steps[0][0] != "claim_task":
            raise
        steps, mode = fallback(task, via=via), "cancelled_and_resumed"
        await call(*steps[0])
    for s in steps[1:]:
        await call(*s)
    return {"ok": True, "mode": mode, "task_id": _i(task.get("id"))}


__all__ = ["EV_DONE", "fallback", "plan", "reset_contributor", "run_async", "run_sync", "tkt", "trial_of",
           "trial_words"]
