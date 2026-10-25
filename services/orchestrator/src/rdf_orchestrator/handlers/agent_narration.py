"""Agent workbench: row-driven progress lines for the stations whose work is rows, not LLM calls.

  eval_trial inserted                 -> "Trial 4/10: fail · drops the cube at the bin edge"
  eval_run enters running / done      -> "Running 10 trials on sim-01" / "Finished: 3/10 passed"
  training_run status / step changes  -> "Training running" / "Step 1,200/2,000"

Live changes only (never the boot snapshot or a resync), so a restart does not replay history into the
transcript. Rows go through ``agent_steps`` (paced, off with RDF_AGENT_THOUGHTS=0). LLM stations
(diagnose, plan_data, review) narrate inline in their own handlers.
"""
from __future__ import annotations

import json
from typing import Any

from .. import agent_steps as ag
from ..bus import Event, on_enter, on_insert, on_update, watch
from ..log import get_logger

log = get_logger("agent_narration")

for _t in ("eval_run", "eval_trial", "training_run", "robot"):
    watch(_t)

_progress = ag.Every(10.0)


def _j(text: Any) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _station(run: dict) -> str:
    return "baseline" if run.get("kind") == "baseline" else "eval"


def _note(text: Any) -> str:
    t = str(text or "").strip()
    for tag in ("[SIM]", "[FAKE]", "[SEEDED]"):
        if t.upper().startswith(tag):
            t = t[len(tag):].strip()
    return t.removeprefix("Auto outcome from sim state:").strip()


@on_insert("eval_trial", name="agent_narration_trial")
def narrate_trial(ev: Event) -> None:
    if ev.source != "live":
        return
    t = ev.row
    run = ev.bus.get("eval_run", int(t.get("eval_run_id") or 0))
    if run is None:
        return
    k, n = int(t.get("trial_index") or 0) + 1, int(run.get("trials") or 0)
    ok = bool(t.get("success"))
    note = _note(t.get("failure_note")) if not ok else ""
    ag.step(f"Trial {k}/{n}: {'pass' if ok else 'fail'}" + (f" · {note}" if note else ""),
            ticket_id=run.get("ticket_id"), station=_station(run), eval_run_id=run.get("id"), trial=k, trials=n,
            success=ok, error=not ok)


@on_enter("eval_run", where=lambda r: r.status == "running", include_existing=False, name="agent_narration_run_start")
def narrate_run_start(ev: Event) -> None:
    if ev.source != "live":
        return
    run = ev.row
    robot = ev.bus.get("robot", int(run.get("robot_id") or 0)) or {}
    what = "baseline" if run.get("kind") == "baseline" else "fine-tuned policy"
    ag.step(f"Running {ag.plural(int(run.get('trials') or 0), 'trial')} of the {what}"
            + (f" on {robot.get('name')}" if robot.get("name") else ""),
            ticket_id=run.get("ticket_id"), station=_station(run), eval_run_id=run.get("id"))


@on_enter("eval_run", where=lambda r: r.status == "done", include_existing=False, name="agent_narration_run_done")
def narrate_run_done(ev: Event) -> None:
    if ev.source != "live":
        return
    run = ev.row
    s, n = int(run.get("successes") or 0), int(run.get("trials") or 0)
    ag.step(f"Finished: {s}/{n} passed ({round(100 * s / n) if n else 0}%)", ticket_id=run.get("ticket_id"),
            station=_station(run), eval_run_id=run.get("id"), decision=True)


@on_update("training_run", name="agent_narration_training")
def narrate_training(ev: Event) -> None:
    if ev.source != "live":
        return
    row, old = ev.row, ev.old or {}
    tid, rid = row.get("ticket_id"), row.get("id")
    st, was = row.get("status"), old.get("status")
    if st != was:
        words = {"running": "Training on Modal" if not str(row.get("modal_call_id") or "").startswith("stub-")
                 else "Stub trainer running (no GPU)",
                 "done": "Training done, checkpoint written", "failed": f"Training failed: {row.get('error') or ''}".rstrip(": "),
                 "cancelled": "Training cancelled"}.get(str(st), f"Training {st}")
        ag.step(words, ticket_id=tid, station="train", training_run_id=rid, decision=st == "done",
                error=st == "failed")
        return
    m, hp = _j(row.get("metrics_json")), _j(row.get("hyperparams_json"))
    step = m.get("step") or m.get("steps_done") or m.get("global_step")
    total = hp.get("num_train_steps") or hp.get("steps") or hp.get("train_steps")
    if step is not None and step != _j(old.get("metrics_json")).get("step") and _progress.due(rid):
        loss = m.get("loss")
        ag.step(f"Step {int(step):,}" + (f"/{int(total):,}" if total else "")
                + (f" · loss {float(loss):.3f}" if isinstance(loss, (int, float)) else ""),
                ticket_id=tid, station="train", training_run_id=rid, step=step, total=total)
