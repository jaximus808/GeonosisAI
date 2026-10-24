"""In-memory fake SpacetimeDB for the TDM-22 flywheel tests: the tables plus Python copies of the
reducers the flywheel and its simulated steps call, with the same validation that matters for the
state machine (TDM-9 ticket transitions + iteration cap, TDM-12 training/eval rules, TDM-11
review/approve rules). ``FakeDB`` holds the data and outlives clients, so a test can "restart the
orchestrator" by starting a new Bus on a new ``FakeClient`` over the same ``FakeDB``.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from rdf_orchestrator.stdb import Row

TRANSITIONS = {
    "created": ("onboarding", "baseline"), "onboarding": ("baseline",), "baseline": ("diagnose",),
    "diagnose": ("plan_data",), "plan_data": ("dispatch",), "dispatch": ("review",), "review": ("train", "dispatch"),
    "train": ("eval",), "eval": ("shipped", "diagnose", "plan_data", "dispatch"),
}
TERMINAL = ("shipped", "failed", "cancelled")
PRICES = {"clean_teleop": 1000, "recovery_takeover": 500}
DEFAULTS = {"eval_trials": 10, "success_threshold": 0.8, "require_ci_low": False, "target_episodes": 5,
            "max_iterations": 3, "hyperparams_json": json.dumps({"lr": 2.5e-5, "steps": 2000})}
TABLES = ("ticket", "run_settings", "robot", "dataset", "failure_cluster", "data_plan", "episode", "training_run",
          "eval_run", "eval_trial", "event_log", "review")


class ReducerError(Exception):
    pass


def _opt(v):
    if isinstance(v, dict) and "some" in v:
        return v["some"]
    if isinstance(v, dict) and "none" in v:
        return None
    return v


class FakeDB:
    def __init__(self) -> None:
        self.t: dict[str, dict[Any, Row]] = {name: {} for name in TABLES}
        self.next_id: dict[str, int] = {name: 1 for name in TABLES}
        self.clock = 1_000_000
        self.clients: list[FakeClient] = []
        self.errors: list[tuple[str, list, str]] = []
        self.calls: list[tuple[str, list]] = []
        self._pending: list = []

    # ---- helpers
    def now(self) -> int:
        self.clock += 1000
        return self.clock

    def key(self, table: str, row: dict):
        return row["ticket_id"] if table == "run_settings" else row["id"]

    def insert(self, table: str, **row) -> Row:
        if table != "run_settings":
            row["id"] = self.next_id[table]
            self.next_id[table] += 1
        r = Row(row)
        self.t[table][self.key(table, r)] = r
        self._pending.append((table, None, r))
        return r

    def update(self, table: str, row: Row, **changes) -> Row:
        new = Row({**row, **changes})
        self.t[table][self.key(table, new)] = new
        self._pending.append((table, row, new))
        return new

    def rows(self, table: str, **eq) -> list[Row]:
        return [r for r in self.t[table].values() if all(r.get(k) == v for k, v in eq.items())]

    def log(self, actor: str, type_: str, payload: dict) -> None:
        self.insert("event_log", ts=self.clock, actor=actor, type=type_, payload_json=json.dumps(payload))

    def events(self, type_: str | None = None) -> list[dict]:
        out = [r for r in sorted(self.t["event_log"].values(), key=lambda r: r["id"]) if type_ in (None, r["type"])]
        return [{**r, "payload": json.loads(r["payload_json"] or "{}")} for r in out]

    def ticket(self, tid: int) -> Row:
        t = self.t["ticket"].get(tid)
        if t is None:
            raise ReducerError(f"unknown ticket {tid}")
        return t

    # ---- the call path (atomic: on error nothing is applied)
    def call(self, reducer: str, args: list) -> None:
        self.calls.append((reducer, args))
        snapshot = {k: dict(v) for k, v in self.t.items()}, dict(self.next_id), self.clock
        self._pending: list = []
        try:
            getattr(self, f"r_{reducer}")(*[_opt(a) for a in args])
        except ReducerError as e:
            self.t, self.next_id, self.clock = snapshot
            self.errors.append((reducer, args, str(e)))
            return
        changes, self._pending = self._pending, []
        meta = {"reducer": reducer, "timestamp": self.clock}
        for c in list(self.clients):
            c.deliver(changes, meta)

    # ---- reducers
    def r_append_event(self, actor, type_, payload_json):
        self.insert("event_log", ts=self.clock, actor=actor, type=type_, payload_json=payload_json)

    def r_register_robot(self, name, embodiment, adapter_version, camera_config_json):
        ex = self.rows("robot", name=name)
        if ex:
            self.update("robot", ex[0], embodiment=embodiment, adapter_version=adapter_version,
                        camera_config_json=camera_config_json)
        else:
            self.insert("robot", name=name, embodiment=embodiment, adapter_version=adapter_version,
                        camera_config_json=camera_config_json, status="offline")
        self.log("orchestrator", "robot.registered", {"name": name})

    def r_create_ticket(self, robot_id, task_description, target_success, budget_cents, overrides_json):
        now = self.now()
        t = self.insert("ticket", robot_id=robot_id, task_description=task_description, target_success=target_success,
                        budget_cents=budget_cents, approved_cents=0, spent_cents=0, status="created", dataset_id=0,
                        iteration=0, deployed_checkpoint_uri=None, created_by="test", created_at=now, updated_at=now)
        ds = self.insert("dataset", ticket_id=t["id"], accepted_count=0, total_count=0, lerobot_uri=None, updated_at=now)
        self.update("ticket", t, dataset_id=ds["id"])
        s = {**DEFAULTS, "success_threshold": target_success, **json.loads(overrides_json or "{}")}
        self.insert("run_settings", ticket_id=t["id"], budget_cents=budget_cents, updated_at=now, **s)
        self.log("orchestrator", "ticket.created", {"ticket_id": t["id"]})

    def r_set_ticket_status(self, ticket_id, status, note):
        t = self.ticket(ticket_id)
        frm = t["status"]
        ok = status in TRANSITIONS.get(frm, ()) or (frm not in TERMINAL and status in ("failed", "cancelled"))
        if not ok:
            raise ReducerError(f"illegal ticket transition {frm} -> {status}")
        it = t["iteration"]
        if frm == "eval" and status in ("diagnose", "plan_data", "dispatch"):
            mx = self.t["run_settings"][ticket_id]["max_iterations"]
            if it + 1 >= mx:
                raise ReducerError(f"ticket {ticket_id} reached max_iterations {mx}")
            it += 1
        self.update("ticket", t, status=status, iteration=it, updated_at=self.now())
        self.log("orchestrator", "ticket.status_changed",
                 {"ticket_id": ticket_id, "from": frm, "to": status, "note": note, "iteration": it})

    def r_queue_training_run(self, ticket_id, episode_ids_json, hyperparams_json):
        t = self.ticket(ticket_id)
        ids = json.loads(episode_ids_json)
        if not ids:
            raise ReducerError("episode_ids_json must list at least one episode")
        for i in ids:
            ep = self.t["episode"].get(i)
            if ep is None or ep["ticket_id"] != ticket_id or ep["review_status"] != "accepted":
                raise ReducerError(f"episode {i} not accepted")
        if any(r["status"] in ("queued", "running") for r in self.rows("training_run", ticket_id=ticket_id)):
            raise ReducerError("ticket already has an active training run")
        self.insert("training_run", ticket_id=ticket_id, iteration=t["iteration"], episode_ids=ids, status="queued",
                    modal_call_id=None, base_checkpoint_uri="gs://openpi-assets/checkpoints/pi05_base/params",
                    checkpoint_uri=None, hyperparams_json=hyperparams_json or "{}", metrics_json="{}",
                    gpu_cost_cents=0, error=None, created_at=self.now(), started_at=None, finished_at=None)
        self.log("orchestrator", "training.queued", {"ticket_id": ticket_id, "episode_ids": ids})

    def r_set_training_status(self, run_id, status, modal_call_id, checkpoint_uri, metrics_json, gpu_cost_cents):
        r = self.t["training_run"].get(run_id)
        nxt = {"queued": ("queued", "running", "failed", "cancelled"), "running": ("running", "done", "failed", "cancelled")}
        if r is None or status not in nxt.get(r["status"], ()):
            raise ReducerError(f"training run {run_id}: {r and r['status']} -> {status} not allowed")
        ckpt = checkpoint_uri or r["checkpoint_uri"]
        if status == "done" and not ckpt:
            raise ReducerError("done requires checkpoint_uri")
        self.update("training_run", r, status=status, modal_call_id=modal_call_id or r["modal_call_id"],
                    checkpoint_uri=ckpt, metrics_json=metrics_json or r["metrics_json"],
                    gpu_cost_cents=r["gpu_cost_cents"] if gpu_cost_cents is None else gpu_cost_cents)
        self.log("orchestrator", "training.status", {"ticket_id": r["ticket_id"], "training_run_id": run_id, "to": status})

    def r_start_eval_run(self, ticket_id, checkpoint_uri, trials):
        t = self.ticket(ticket_id)
        n = trials or self.t["run_settings"][ticket_id]["eval_trials"]
        src = [r for r in self.rows("training_run", ticket_id=ticket_id) if r["status"] == "done" and r["checkpoint_uri"] == checkpoint_uri]
        self.insert("eval_run", ticket_id=ticket_id, robot_id=t["robot_id"], training_run_id=src[0]["id"] if src else 0,
                    kind="candidate" if src else "baseline", checkpoint_uri=checkpoint_uri, status="running", trials=n,
                    completed=0, successes=0, ci_low=0.0, ci_high=0.0, created_at=self.now(), finished_at=None)
        self.log("orchestrator", "eval.started", {"ticket_id": ticket_id})

    def r_record_eval_trial(self, eval_run_id, index, success, duration_ms, episode_id, failure_note):
        run = self.t["eval_run"].get(eval_run_id)
        if run is None or run["status"] != "running" or index >= run["trials"]:
            raise ReducerError("bad trial")
        ex = self.rows("eval_trial", eval_run_id=eval_run_id)
        if any(x["trial_index"] == index for x in ex):
            raise ReducerError(f"eval run {eval_run_id} already has trial {index}")
        self.insert("eval_trial", eval_run_id=eval_run_id, trial_index=index, success=success, duration_ms=duration_ms,
                    episode_id=episode_id, failure_note=failure_note or None, created_at=self.now())
        self.update("eval_run", run, completed=len(ex) + 1, successes=sum(x["success"] for x in ex) + bool(success))

    def r_finalize_eval_run(self, eval_run_id, successes, trials, ci_low, ci_high, summary):
        run = self.t["eval_run"].get(eval_run_id)
        if run is None or run["status"] != "running":
            raise ReducerError("not running")
        rows = self.rows("eval_trial", eval_run_id=eval_run_id)
        if len(rows) < run["trials"] or successes != sum(r["success"] for r in rows) or trials != run["trials"]:
            raise ReducerError("numbers don't match the trial rows")
        self.update("eval_run", run, status="done", ci_low=ci_low, ci_high=ci_high, finished_at=self.now())
        self.log("orchestrator", "eval.finalized", {"ticket_id": run["ticket_id"], "eval_run_id": eval_run_id,
                                                     "successes": successes, "trials": trials, "summary": summary})

    def r_record_failure_cluster(self, ticket_id, label, count, example_episode_ids_json):
        t = self.ticket(ticket_id)
        if count == 0:
            raise ReducerError("count must be > 0")
        self.insert("failure_cluster", ticket_id=ticket_id, iteration=t["iteration"], label=label, description="",
                    count=count, example_episode_ids=json.loads(example_episode_ids_json or "[]"), created_at=self.now())

    def r_record_data_plan(self, ticket_id, data_type, amount, reasoning, evidence_json):
        t = self.ticket(ticket_id)
        ev = json.loads(evidence_json or "{}")
        unit = ev.get("unit_price_cents", PRICES[data_type])
        self.insert("data_plan", ticket_id=ticket_id, iteration=t["iteration"], data_type=data_type, amount=amount,
                    unit_price_cents=unit, est_cost_cents=unit * amount, reasoning=reasoning,
                    failure_cluster_ids=ev.get("failure_cluster_ids", []), status="proposed", approved_cents=0,
                    approved_by=None, approved_at=None, created_at=self.now())
        self.log("orchestrator", "planner.data_plan", {"ticket_id": ticket_id, "reasoning": reasoning})

    def r_approve_spend(self, ticket_id, data_plan_id, approved_cents, approved_by):
        t = self.ticket(ticket_id)
        p = self.t["data_plan"].get(data_plan_id)
        if p is None or p["ticket_id"] != ticket_id or p["status"] in ("rejected", "done"):
            raise ReducerError("bad plan")
        amount = approved_cents or p["est_cost_cents"]
        other = sum(x["approved_cents"] for x in self.rows("data_plan", ticket_id=ticket_id)
                    if x["id"] != data_plan_id and x["status"] != "rejected")
        if other + amount > t["budget_cents"]:
            raise ReducerError("over budget")
        self.update("data_plan", p, status="approved" if p["status"] == "proposed" else p["status"],
                    approved_cents=amount, approved_by=approved_by, approved_at=self.now())
        self.update("ticket", t, approved_cents=other + amount)
        self.log(approved_by, "spend.approved", {"ticket_id": ticket_id, "data_plan_id": data_plan_id})

    def r_create_episode(self, ticket_id, task_id, contributor_id, kind, storage_uri, actions_uri, cameras_json,
                         frame_count, duration_ms, takeover_ts, handback_ts, success, checkpoint_uri):
        t = self.ticket(ticket_id)
        if t["status"] in TERMINAL:
            raise ReducerError("ticket closed")
        in_ds = kind in ("clean_teleop", "recovery_takeover")
        self.insert("episode", ticket_id=ticket_id, robot_id=t["robot_id"], task_id=task_id, contributor_id=contributor_id,
                    dataset_offer_id=0, kind=kind, review_status="pending" if in_ds else "not_reviewed",
                    checkpoint_uri=checkpoint_uri, storage_uri=storage_uri, cameras_json=cameras_json,
                    actions_uri=actions_uri, frame_count=frame_count, duration_ms=duration_ms, takeover_ts=takeover_ts,
                    handback_ts=handback_ts, success=success, created_at=self.now())
        if in_ds:
            ds = self.rows("dataset", ticket_id=ticket_id)[0]
            self.update("dataset", ds, total_count=ds["total_count"] + 1)

    def r_submit_review(self, episode_id, accepted, reason, metrics_json):
        ep = self.t["episode"].get(episode_id)
        if ep is None or ep["review_status"] != "pending":
            raise ReducerError("only pending episodes are reviewed")
        if not accepted and not reason.strip():
            raise ReducerError("a rejected review needs a reason")
        self.insert("review", episode_id=episode_id, ticket_id=ep["ticket_id"], accepted=accepted, reason=reason,
                    metrics_json=metrics_json, reviewer="test", created_at=self.now())
        self.update("episode", ep, review_status="accepted" if accepted else "rejected")
        if accepted:
            ds = self.rows("dataset", ticket_id=ep["ticket_id"])[0]
            self.update("dataset", ds, accepted_count=ds["accepted_count"] + 1)


class FakeClient:
    """The bus's view of FakeDB (implements ``bus.BusClient``)."""

    def __init__(self, db: FakeDB) -> None:
        self.db, self.state, self.identity, self.queries, self.cb = db, "stopped", "0xfake", [], {}
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, q): self.queries = q
    def on_snapshot(self, cb): self.cb["snapshot"] = cb
    def on_insert(self, cb): self.cb["insert"] = cb
    def on_update(self, cb): self.cb["update"] = cb
    def on_delete(self, cb): self.cb["delete"] = cb
    def on_state(self, cb): self.cb["state"] = cb
    def key(self, table, row): return self.db.key(table, row)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self.db.clients.append(self)
        self.cb["state"]("live", None)
        self.cb["snapshot"]({t: [Row(r) for r in v.values()] for t, v in self.db.t.items()})
        await self._stop.wait()

    def stop(self):
        if self in self.db.clients:
            self.db.clients.remove(self)
        self._stop.set()

    async def call_reducer(self, reducer, args):
        self.db.call(reducer, list(args))
        return len(self.db.calls)

    def deliver(self, changes, meta):  # like a TransactionUpdate: arrives on the loop, after the call
        def go():
            for table, old, new in changes:
                if old is None:
                    self.cb["insert"](table, Row(new), meta)
                else:
                    self.cb["update"](table, Row(old), Row(new), meta)
        if self._loop is not None:
            self._loop.call_soon(go)
