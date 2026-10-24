"""TDM-34: the Training Droid's one allow-listed shell tool.

    uv run --project services/factory python -m rdf_factory.droids.training_tool <cmd> --ctx <ticket dir>/droid_context.json

    inspect                          dataset sanity checks + stats, run_settings, budget, recommended config
    queue   --config <yaml>          validate bounds/budget/retry limit -> queue_training_run (the TDM-21 handoff)
    status  --run <id>               run status; failed -> classified error + retry plan; done -> loss analysis
    report  --run <id> --note <md>   log the droid's training note as event_log training.report

Every command prints one JSON object. The droid supplies the judgement (and the rationale it writes
in the config); this tool enforces the guardrails, so a confused droid cannot spend past them:
  * it only talks to the database named in droid_context.json (written by the launcher), with the
    token from SPACETIME_TOKEN; a mismatched SPACETIME_HOST is refused (never fall through to .env);
  * hyperparams must pass ``training.validate`` (lr / steps / batch / LoRA bounds, 2 h timeout,
    remaining ticket budget) and episodes must pass ``check_dataset``;
  * at most 1 + MAX_RETRIES runs per droid session, and a retry needs a failed predecessor.
The droid's own records are logged with actor ``droid:training``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import training as T

NOTE_MAX = 6000


class ToolError(RuntimeError):
    pass


# --------------------------------------------------------------------------- context / db

class Ctx:
    def __init__(self, path: str, session: int | None = None, client: Any = None):
        p = Path(path)
        if not p.exists():
            raise ToolError(f"context file {p} not found (pass the --ctx path from your brief)")
        self.path = p
        self.tdir = p.parent
        d = json.loads(p.read_text())
        self.host, self.db = str(d["host"]).rstrip("/"), str(d["db"])
        self.ticket_id, self.iteration = int(d["ticket_id"]), int(d["iteration"])
        env_host = os.environ.get("SPACETIME_HOST", "").rstrip("/")
        env_db = os.environ.get("SPACETIME_DB_NAME", "")
        if (env_host and env_host != self.host) or (env_db and env_db != self.db):
            raise ToolError(f"SPACETIME_HOST/DB_NAME in the environment ({env_host}/{env_db}) differ from the "
                            f"context ({self.host}/{self.db}); refusing to guess which database to write")
        if client is None:
            token = os.environ.get("SPACETIME_TOKEN", "").strip()
            if not token:
                raise ToolError("SPACETIME_TOKEN is not set in the environment")
            from rdf_common.spacetime_client import SpacetimeClient

            client = SpacetimeClient(self.host, self.db, token)
        self.c = client
        self._session = session

    # reads
    def sql(self, q: str) -> list[dict]:
        return [dict(r) for r in self.c.sql(q)]

    def one(self, q: str) -> dict | None:
        rows = self.sql(q)
        return rows[0] if rows else None

    @property
    def session(self) -> int:
        if self._session:
            return int(self._session)
        rows = [r for r in self.sql(f"SELECT * FROM agent_session WHERE ticket_id = {self.ticket_id}")
                if r.get("role") == T.ROLE and r.get("status") in ("starting", "running", "resuming")]
        if not rows:
            raise ToolError("no live training_droid agent_session for this ticket; pass --session <id>")
        self._session = int(max(rows, key=lambda r: int(r["id"]))["id"])
        return self._session

    def runs(self) -> list[dict]:
        return sorted(self.sql(f"SELECT * FROM training_run WHERE ticket_id = {self.ticket_id}"), key=lambda r: int(r["id"]))

    def run(self, run_id: int) -> dict:
        r = self.one(f"SELECT * FROM training_run WHERE id = {int(run_id)}")
        if r is None or int(r["ticket_id"]) != self.ticket_id:
            raise ToolError(f"training run {run_id} is not a run of ticket {self.ticket_id}")
        return r

    def my_runs(self) -> list[dict]:
        return [r for r in self.runs() if _hp(r).get("droid_session_id") == self.session]

    def budget_left(self) -> int | None:
        t = self.one(f"SELECT * FROM ticket WHERE id = {self.ticket_id}") or {}
        budget = int(t.get("budget_cents") or 0)
        if budget <= 0:
            return None
        gpu = sum(int(r.get("gpu_cost_cents") or 0) for r in self.runs())
        return budget - int(t.get("spent_cents") or 0) - gpu

    # writes
    def event(self, type_: str, payload: dict) -> None:
        body = {"ticket_id": self.ticket_id, "iteration": self.iteration, "agent_session_id": self.session, **payload}
        self.c.call_reducer("append_event", T.ACTOR, type_, json.dumps(body, default=str)[:60_000])


def _hp(run: dict) -> dict:
    try:
        v = json.loads(run.get("hyperparams_json") or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _metrics(run: dict) -> dict:
    try:
        v = json.loads(run.get("metrics_json") or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _dataset(ctx: Ctx) -> tuple[dict, list[dict]]:
    eps = ctx.sql(f"SELECT * FROM episode WHERE ticket_id = {ctx.ticket_id}")
    return T.check_dataset(eps), eps


# --------------------------------------------------------------------------- commands

def cmd_inspect(ctx: Ctx, _a: argparse.Namespace) -> dict:
    ticket = ctx.one(f"SELECT * FROM ticket WHERE id = {ctx.ticket_id}")
    if ticket is None:
        raise ToolError(f"ticket {ctx.ticket_id} not found")
    rs = ctx.one(f"SELECT * FROM run_settings WHERE ticket_id = {ctx.ticket_id}") or {}
    try:
        run_hp = json.loads(rs.get("hyperparams_json") or "{}")
    except ValueError:
        run_hp = {}
    checks, _ = _dataset(ctx)
    left = ctx.budget_left()
    rec = T.recommend(checks["stats"], run_hp, budget_left_cents=left,
                      prompt_fallback=str(ticket.get("task_description") or ""))
    ds = ctx.one(f"SELECT * FROM dataset WHERE ticket_id = {ctx.ticket_id}") or {}
    prior = [{"id": r["id"], "iteration": r.get("iteration"), "status": r["status"], "error": r.get("error"),
              "hyperparams": _hp(r)} for r in ctx.runs()]
    return {
        "ticket": {k: ticket.get(k) for k in ("id", "task_description", "status", "iteration", "budget_cents", "spent_cents")},
        "agent_session_id": ctx.session, "run_settings_hyperparams": run_hp, "budget_left_cents": left,
        "dataset": {k: ds.get(k) for k in ("id", "accepted_count", "total_count", "lerobot_uri")} if ds else None,
        "checks": checks, "recommendation": rec, "previous_runs": prior,
        "config_path": str(ctx.tdir / "configs" / f"train_{ctx.ticket_id}_{ctx.iteration}.yaml"),
        "limits": {"lr": T.LR_RANGE, "steps": [T.MIN_STEPS, T.MAX_STEPS], "batch_size": T.BATCH_CHOICES,
                   "lora_rank": T.LORA_CHOICES, "max_retries": T.MAX_RETRIES},
    }


def _load_config(path: str, ctx: Ctx) -> dict:
    import yaml

    p = Path(path)
    if not p.is_absolute():
        p = (ctx.tdir / p) if (ctx.tdir / p).exists() else Path.cwd() / p
    if ctx.tdir.resolve() not in p.resolve().parents:
        raise ToolError(f"config {p} must live under the ticket dir {ctx.tdir}")
    cfg = yaml.safe_load(p.read_text())
    if not isinstance(cfg, dict) or not isinstance(cfg.get("hyperparams"), dict):
        raise ToolError("config must be a mapping with a `hyperparams` mapping")
    cfg["_path"] = str(p)
    return cfg


def cmd_queue(ctx: Ctx, a: argparse.Namespace) -> dict:
    cfg = _load_config(a.config, ctx)
    if int(cfg.get("ticket_id", ctx.ticket_id)) != ctx.ticket_id:
        raise ToolError(f"config is for ticket {cfg.get('ticket_id')}, this droid owns ticket {ctx.ticket_id}")
    rationale = [str(x) for x in (cfg.get("rationale") or [])]
    if not rationale:
        raise ToolError("config needs a non-empty `rationale` list: say why each value was chosen")
    checks, _ = _dataset(ctx)
    if not checks["ok"]:
        raise ToolError(f"dataset checks fail, not spending GPU: {checks['errors']}")
    ids = [int(x) for x in (cfg.get("episode_ids") or checks["episode_ids"])]
    bad = sorted(set(ids) - set(checks["episode_ids"]))
    if bad:
        raise ToolError(f"episodes {bad} are not usable accepted episodes (see inspect.checks.excluded)")
    mine = ctx.my_runs()
    live = [r for r in ctx.runs() if r["status"] in ("queued", "running")]
    if live:
        raise ToolError(f"training run {live[-1]['id']} is already {live[-1]['status']}: wait for it")
    retry_of = cfg.get("retry_of")
    if mine:
        if len(mine) > T.MAX_RETRIES:
            raise ToolError(f"retry limit reached: this droid already queued {len(mine)} runs "
                            f"({[r['id'] for r in mine]}); report instead")
        last = mine[-1]
        if last["status"] != "failed":
            raise ToolError(f"run {last['id']} is {last['status']}; only a failed run is retried")
        if retry_of is None or int(retry_of) != int(last["id"]):
            raise ToolError(f"a retry config must say `retry_of: {last['id']}`")
    elif retry_of is not None:
        raise ToolError("retry_of given but this droid has not queued a run yet")
    hp_in = cfg["hyperparams"]
    left = ctx.budget_left()
    errs = T.validate(hp_in, budget_left_cents=left)
    if errs:
        raise ToolError("config refused: " + "; ".join(errs))
    hp = {"lr": float(hp_in["lr"]), "steps": int(hp_in["steps"]), "batch_size": int(hp_in["batch_size"]),
          "lora_rank": int(hp_in["lora_rank"]), "prompt": str(hp_in.get("prompt") or "").strip(),
          "droid_session_id": ctx.session, "droid_config": Path(cfg["_path"]).name}
    if retry_of is not None:
        hp["retry_of"] = int(retry_of)
    before = {int(r["id"]) for r in ctx.runs()}
    ctx.c.call_reducer("queue_training_run", ctx.ticket_id, json.dumps(ids), json.dumps(hp))
    new = [r for r in ctx.runs() if int(r["id"]) not in before]
    if not new:
        raise ToolError("queue_training_run returned but no new training_run row is visible")
    run = new[-1]
    est = T.estimate(hp, samples_per_epoch=int(checks["stats"].get("samples_per_epoch") or 0))
    ctx.event("training.config", {"training_run_id": run["id"], "hyperparams": hp, "rationale": rationale,
                                  "estimate": est, "episode_ids": ids, "warnings": checks["warnings"],
                                  "excluded": checks["excluded"], "config_path": cfg["_path"], "retry_of": retry_of})
    return {"queued": True, "training_run_id": run["id"], "status": run["status"], "hyperparams": hp,
            "estimate": est, "budget_left_cents": left, "waiting_line": T.waiting_line(int(run["id"])),
            "next": "write resume_payload.json, print RESUME_PAYLOAD, end with waiting_line as the final line"}


def cmd_status(ctx: Ctx, a: argparse.Namespace) -> dict:
    run = ctx.run(a.run)
    hp, m = _hp(run), _metrics(run)
    out: dict[str, Any] = {"training_run_id": run["id"], "status": run["status"], "hyperparams": hp,
                           "checkpoint_uri": run.get("checkpoint_uri"), "gpu_cost_cents": run.get("gpu_cost_cents"),
                           "modal_call_id": run.get("modal_call_id"), "fake": bool(m.get("fake") or (m.get("modal") or {}).get("fake"))}
    mine = ctx.my_runs()
    out["retries_left"] = max(0, 1 + T.MAX_RETRIES - len(mine))
    if run["status"] == "failed":
        err = str(run.get("error") or m.get("error") or "")
        out["error"] = err[:1500]
        out["retry_plan"] = T.retry_plan(err, hp, budget_left_cents=ctx.budget_left())
        if out["retries_left"] <= 0:
            out["retry_plan"]["retry"] = False
            out["retry_plan"]["rationale"].append("retry limit reached for this droid session")
    elif run["status"] == "done":
        checks, _ = _dataset(ctx)
        out["metrics"] = {k: v for k, v in m.items() if k not in ("loss_curve", "modal")}
        out["loss_curve_points"] = len(m.get("loss_curve") or [])
        out["analysis"] = T.analyze_loss(m, samples_per_epoch=int(checks["stats"].get("samples_per_epoch") or 0),
                                         batch_size=int(hp.get("batch_size") or 32),
                                         episodes=len(run.get("episode_ids") or []))
        out["note_path"] = str(ctx.tdir / f"training_note_{ctx.ticket_id}_{ctx.iteration}.md")
    else:
        out["next"] = f"still {run['status']}: park again with {T.waiting_line(int(run['id']))}"
    return out


def cmd_report(ctx: Ctx, a: argparse.Namespace) -> dict:
    run = ctx.run(a.run)
    p = Path(a.note)
    if not p.is_absolute():
        p = ctx.tdir / p
    if ctx.tdir.resolve() not in p.resolve().parents:
        raise ToolError(f"note {p} must live under the ticket dir {ctx.tdir}")
    note = p.read_text()[:NOTE_MAX]
    if not note.strip():
        raise ToolError("the training note is empty")
    hp, m = _hp(run), _metrics(run)
    analysis = None
    if run["status"] == "done":
        checks, _ = _dataset(ctx)
        analysis = T.analyze_loss(m, samples_per_epoch=int(checks["stats"].get("samples_per_epoch") or 0),
                                  batch_size=int(hp.get("batch_size") or 32), episodes=len(run.get("episode_ids") or []))
    payload = {"training_run_id": run["id"], "status": run["status"], "checkpoint_uri": run.get("checkpoint_uri"),
               "gpu_cost_cents": run.get("gpu_cost_cents"), "hyperparams": hp, "analysis": analysis,
               "runs_by_this_droid": [r["id"] for r in ctx.my_runs()], "note": note, "note_path": str(p),
               "fake": bool(m.get("fake"))}
    ctx.event("training.report", payload)
    return {"reported": True, "training_run_id": run["id"], "verdict": (analysis or {}).get("verdict"),
            "next": "end your reply with DONE"}


COMMANDS = {"inspect": cmd_inspect, "queue": cmd_queue, "status": cmd_status, "report": cmd_report}


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="training_tool", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--ctx", required=True, help="droid_context.json from your brief")
    ap.add_argument("--session", type=int, default=None, help="your agent_session id (inferred if omitted)")
    ap.add_argument("--config")
    ap.add_argument("--run", type=int)
    ap.add_argument("--note")
    a = ap.parse_args(argv)
    need = {"queue": ("config",), "status": ("run",), "report": ("run", "note")}.get(a.cmd, ())
    missing = [n for n in need if getattr(a, n) in (None, "")]
    if missing:
        print(json.dumps({"ok": False, "error": f"{a.cmd} needs --{' --'.join(missing)}"}))
        return 2
    try:
        ctx = Ctx(a.ctx, a.session, client)
        out = COMMANDS[a.cmd](ctx, a)
        print(json.dumps({"ok": True, **out}, indent=1, default=str))
        return 0
    except ToolError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except Exception as e:  # noqa: BLE001 - the droid reads this; keep it one JSON line
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:1500]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
