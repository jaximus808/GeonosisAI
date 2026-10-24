"""TDM-67 eval orchestration: run an N-trial eval on the robot, reset between trials, finalize.

Bus wiring is in ``handlers/evaluate.py``. This module holds the logic. It decides from rows (the
bus mirror), never from process memory, so every entry point is idempotent and restart-safe.

The flow for a ticket entering ``baseline`` or ``eval`` (the flywheel's entry step for both stages
is ``EvalStep`` below):

  1. Pick the checkpoint. ``baseline`` uses the base pi0.5 weights; ``eval`` uses this iteration's
     latest finished training run (``TicketView.done_training_run``). Nothing to evaluate -> wait.
  2. Wait until the ticket's robot is usable: status online/busy, a heartbeat newer than
     ``EVAL_ROBOT_STALE_S``, and no open job command on it (the TDM-13 job lock).
  3. ``POST {POLICY_SERVER_URL}/policy/activate`` {checkpoint_uri, ...} so the policy server
     serves that checkpoint. No ``POLICY_SERVER_URL`` -> skipped and logged (fake robot / tests).
  4. ``start_eval_run(ticket_id, checkpoint_uri, 0)`` (0 = ``run_settings.eval_trials``; the module
     makes it ``baseline`` or ``candidate`` from the checkpoint).
  5. ``issue_robot_command(robot_id, "run_eval", {eval_run_id, checkpoint_uri, trials, timeout_s,
     kind, solo_mode}, ticket_id)``. The robot runs the trials and writes ``eval_trial`` rows with
     ``record_eval_trial``.
  6. Between trials (TDM-26 dispatcher, already on the bus): every ``eval_trial`` insert creates a
     paid ``reset`` task; when that task is ``completed`` the dispatcher pays it and issues
     ``resume_policy``, which releases the robot for the next trial. With ``EVAL_SOLO_MODE=1`` this
     module completes those reset tasks itself after ``EVAL_SOLO_RESET_DELAY_S`` (unattended
     testing; see ``solo_reset``).
  7. When every planned trial row exists: ``stats.EvalRunFinalizer`` -> ``finalize_eval_run``
     (Wilson 95% CI). The flywheel sees the eval_run go ``done`` and makes the ship / loop decision.
     The finalizer never fires with fewer rows than planned (and the reducer refuses it anyway).

Aborts (``abort_eval_run``, TDM-67 reducer): a running eval is stopped, never finalized, when
  * its run_eval command failed (``failed``: counts as a failed step, the flywheel retries it),
  * it was preempted by a soft_estop, the robot went offline / estop, its heartbeat went stale,
    the robot completed run_eval with fewer trial rows than planned, no command was issued, or
    the eval exceeded its overall timeout (``cancelled``: not the policy's fault).
The trial rows stay as evidence. A fresh eval_run is started for the re-run as soon as the robot
is usable again (robot back online, its job command closed, or the watchdog tick). After an
estop abort the re-run waits for a human: append an ``eval.rerun_requested`` event with the
ticket_id (or set ``EVAL_RERUN_AFTER_ESTOP=1``). Nobody is made to move the arm again by surprise.

If the robot only reported trials in ``result_json.outcomes`` (no rows), the orchestrator records
exactly those outcomes with ``record_eval_trial``. It never invents or edits a trial.

Env (all optional):
  POLICY_SERVER_URL              policy server base URL for /policy/activate ("" = skip)
  POLICY_SERVER_TOKEN            bearer token for /policy/activate (env, else repo .env; never logged)
  EVAL_POLICY_ACTIVATE_TIMEOUT_S 300
  EVAL_TRIAL_TIMEOUT_S           120   per-trial rollout timeout, sent to the robot as timeout_s
  EVAL_RESET_WAIT_S              900   allowance per reset when computing the eval's overall timeout
  EVAL_ROBOT_STALE_S             30    no heartbeat for this long = robot lost
  EVAL_WATCHDOG_S                5     watchdog tick (0 = off; handlers still react to row changes)
  EVAL_SOLO_MODE                 0     1 = auto-complete reset tasks (unattended testing)
  EVAL_SIM_AUTO_RESET            1     sim robots (camera_config_json.sim) reset their own scene, so their
                                       reset tasks are auto-completed like solo mode; real robots still wait
  EVAL_SOLO_RESET_DELAY_S        5
  EVAL_SOLO_CONTRIBUTOR_ID       0     >0: claim+complete resets as this contributor (paid, Stripe
                                       test mode); 0: cancel the reset task and resume the robot
  EVAL_RERUN_AFTER_ESTOP         0
  EVAL_MAX_ABORTS                3     aborted evals in a row before a re-run waits for a human
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .dispatcher import JOB_TYPES, OPEN_JOB, Mirror, Writer, task_ref
from .flywheel import PI05_BASE_CHECKPOINT, StepContext, TicketView
from . import flywheel as _flywheel
from .log import get_logger
from .policy_activate import http_activate
from .stats import EvalRunFinalizer

log = get_logger("evaluate")

ACTOR = "orchestrator:eval"
EVAL_STAGES = ("baseline", "eval")
EVAL_COMMANDS = ("run_eval", "run_baseline")
LIVE_EVAL = ("queued", "running")
USABLE_ROBOT = ("online", "busy")
NO_COMMAND_GRACE_S = 60.0  # a running eval with no run_eval command after this long is orphaned
GUARD_TTL_S = 10.0  # don't repeat the same finalize/abort write while the mirror catches up
TABLES = ("ticket", "run_settings", "robot", "training_run", "eval_run", "eval_trial", "robot_command",
          "task", "event_log")


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _json(text: Any) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class EvalConfig:
    policy_server_url: str = ""
    policy_activate_timeout_s: float = 300.0
    trial_timeout_s: float = 120.0
    reset_wait_s: float = 900.0
    robot_stale_s: float = 30.0
    watchdog_s: float = 5.0
    solo_mode: bool = False
    solo_reset_delay_s: float = 5.0
    solo_contributor_id: int = 0
    sim_auto_reset: bool = True
    rerun_after_estop: bool = False
    max_aborts: int = 3  # aborted evals in a row (per stage) before a re-run waits for a human
    retry_backoff_s: float = 60.0  # after a policy-activate / start failure

    @classmethod
    def from_env(cls) -> "EvalConfig":
        return cls(
            policy_server_url=os.environ.get("POLICY_SERVER_URL", "").strip().rstrip("/"),
            policy_activate_timeout_s=_env_float("EVAL_POLICY_ACTIVATE_TIMEOUT_S", 300.0),
            trial_timeout_s=_env_float("EVAL_TRIAL_TIMEOUT_S", 120.0),
            reset_wait_s=_env_float("EVAL_RESET_WAIT_S", 900.0),
            robot_stale_s=_env_float("EVAL_ROBOT_STALE_S", 30.0),
            watchdog_s=_env_float("EVAL_WATCHDOG_S", 5.0),
            solo_mode=os.environ.get("EVAL_SOLO_MODE", "") == "1",
            solo_reset_delay_s=_env_float("EVAL_SOLO_RESET_DELAY_S", 5.0),
            solo_contributor_id=int(_env_float("EVAL_SOLO_CONTRIBUTOR_ID", 0)),
            sim_auto_reset=os.environ.get("EVAL_SIM_AUTO_RESET", "1") != "0",
            rerun_after_estop=os.environ.get("EVAL_RERUN_AFTER_ESTOP", "") == "1",
            max_aborts=int(_env_float("EVAL_MAX_ABORTS", 3)),
        )


PolicyActivator = Callable[[str, dict, float], None]  # (url, body, timeout) -> raises on failure


_http_activate = http_activate  # bearer POLICY_SERVER_TOKEN (TDM-41 auth); shared with flywheel.ShipStep


class EvalOrchestrator:
    """One per bus. Reads the mirror, writes with a sync reducer client (raises on refusal)."""

    def __init__(self, mirror: Mirror, writer_factory: Callable[[], Writer], cfg: EvalConfig | None = None, *,
                 clock: Callable[[], float] = time.time, activate: PolicyActivator = _http_activate,
                 mirror_wait_s: float = 5.0) -> None:
        self.m = mirror
        self.cfg = cfg or EvalConfig()
        self.clock = clock
        self.activate = activate
        self.mirror_wait_s = mirror_wait_s
        self._writer_factory = writer_factory
        self._writer: Writer | None = None
        self._wlock = threading.Lock()
        self._locks: dict[int, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        self._guard: dict[tuple, float] = {}
        self._backoff: dict[int, float] = {}  # ticket_id -> monotonic time before which we don't retry
        self._last_wait: dict[int, str] = {}
        self._solo_seen: set[int] = set()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------------ plumbing
    @property
    def w(self) -> Writer:
        with self._wlock:
            if self._writer is None:
                self._writer = self._writer_factory()
            return self._writer

    def lock(self, ticket_id: int) -> threading.RLock:
        with self._locks_lock:
            return self._locks.setdefault(int(ticket_id), threading.RLock())

    def _guarded(self, key: tuple) -> bool:
        """True if ``key`` was acted on within GUARD_TTL_S (then skip); else mark it now."""
        now = time.monotonic()
        if now - self._guard.get(key, -1e9) < GUARD_TTL_S:
            return True
        self._guard[key] = now
        return False

    def event(self, type_: str, payload: dict) -> None:
        log.info(type_, extra={"fields": payload})
        try:
            self.w.call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str))
        except Exception as e:  # noqa: BLE001 - the log line above is the fallback record
            log.warning("eval.event_failed", extra={"fields": {"type": type_, "error": str(e)[:300]}})

    def _wait_for(self, pred: Callable[[], Any]) -> Any:
        deadline = time.time() + self.mirror_wait_s
        while True:
            got = pred()
            if got or time.time() >= deadline:
                return got
            time.sleep(0.05)

    def _now_us(self) -> int:
        return int(self.clock() * 1_000_000)

    # ------------------------------------------------------------------ reads
    def runs(self, ticket_id: int) -> list[dict]:
        return sorted((r for r in self.m.rows("eval_run") if _i(r.get("ticket_id")) == ticket_id), key=lambda r: _i(r["id"]))

    def trials(self, run_id: int) -> list[dict]:
        return [t for t in self.m.rows("eval_trial") if _i(t.get("eval_run_id")) == run_id]

    def command_for(self, run_id: int) -> dict | None:
        """The newest run_eval / run_baseline command whose payload names this eval run."""
        cmds = [c for c in self.m.rows("robot_command") if c.get("type") in EVAL_COMMANDS
                and _i(_json(c.get("payload_json")).get("eval_run_id")) == run_id]
        return max(cmds, key=lambda c: _i(c["id"])) if cmds else None

    def open_jobs(self, robot_id: int) -> list[dict]:
        return [c for c in self.m.rows("robot_command") if _i(c.get("robot_id")) == robot_id
                and c.get("status") in OPEN_JOB and c.get("type") in JOB_TYPES]

    def target(self, ticket: dict) -> dict | None:
        """What this ticket's current stage evaluates: {kind, checkpoint_uri, training_run_id}."""
        status = ticket.get("status")
        if status == "baseline":
            base = getattr(_flywheel, "base_checkpoint_uri", lambda: PI05_BASE_CHECKPOINT)()  # RDF_BASE_CHECKPOINT_URI
            return {"kind": "baseline", "checkpoint_uri": base, "training_run_id": 0}
        if status == "eval":
            v = TicketView.from_bus(self.m, _i(ticket["id"]))
            run = v.done_training_run if v else None
            if run and (run.get("checkpoint_uri") or "").strip():
                return {"kind": "candidate", "checkpoint_uri": run["checkpoint_uri"].strip(),
                        "training_run_id": _i(run["id"])}
        return None

    def stage_runs(self, ticket_id: int, tgt: dict) -> list[dict]:
        out = [r for r in self.runs(ticket_id) if (r.get("checkpoint_uri") or "") == tgt["checkpoint_uri"]]
        if tgt["kind"] == "candidate":
            out = [r for r in out if _i(r.get("training_run_id")) == tgt["training_run_id"]]
        else:
            out = [r for r in out if r.get("kind") == "baseline"]
        # TDM-107: only evals on a robot of the ticket's current mode count (a switched ticket re-runs its eval)
        from .robot_mode import robot_mode, same_mode_evals

        ticket = self.m.get("ticket", ticket_id)
        robots = {_i(r.get("id")): r for r in self.m.rows("robot")}
        mode = robot_mode(robots.get(_i(ticket.get("robot_id")))) if ticket is not None else None
        return same_mode_evals(out, robots, mode)

    def robot_problem(self, robot: dict | None, *, running: bool = False) -> str | None:
        """Why this robot can't run (or keep running) an eval right now, else None. ``running``: a
        live eval may sit in ``paused`` (waiting_reset between trials, or a TDM-69 takeover pause)."""
        if robot is None:
            return "robot not found"
        st = robot.get("status")
        if st not in USABLE_ROBOT and not (running and st == "paused"):
            return f"robot {robot.get('id')} is {st}"
        hb = _i(robot.get("last_heartbeat"))
        age = (self._now_us() - hb) / 1e6 if hb else None
        if age is None or age > self.cfg.robot_stale_s:
            return f"robot {robot.get('id')} has not heartbeated for {'ever' if age is None else f'{age:.0f}s'}"
        return None

    def _abort_event(self, run_id: int) -> dict | None:
        for e in self.m.rows("event_log"):
            if e.get("type") == "eval.aborted" and _i(_json(e.get("payload_json")).get("eval_run_id")) == run_id:
                return e
        return None

    def _rerun_requested_after(self, ticket_id: int, after_event_id: int) -> bool:
        return any(e.get("type") == "eval.rerun_requested" and _i(e.get("id")) > after_event_id
                   and _i(_json(e.get("payload_json")).get("ticket_id")) == ticket_id
                   for e in self.m.rows("event_log"))

    def _human_gate(self, ticket_id: int, runs: list[dict]) -> str | None:
        """Why a re-run must wait for an ``eval.rerun_requested`` event, else None."""
        aborted = [(r, self._abort_event(_i(r["id"]))) for r in runs if r.get("status") in ("failed", "cancelled")]
        aborted = [(r, e) for r, e in aborted if e is not None]
        if not aborted:
            return None
        last_run, last_ev = aborted[-1]
        if self._rerun_requested_after(ticket_id, _i(last_ev["id"])):
            return None
        reason = str(_json(last_ev.get("payload_json")).get("reason", ""))
        if reason.startswith("estop:") and not self.cfg.rerun_after_estop:
            return f"eval {last_run['id']} was stopped by an e-stop"
        since = [e for _, e in aborted if not self._rerun_requested_after(ticket_id, _i(e["id"]))]
        if len(since) >= self.cfg.max_aborts:
            return f"{len(since)} evals in a row were aborted (last: {reason[:200]})"
        return None

    def _waiting(self, ticket_id: int, outcome: str, detail: str) -> str:
        if self._last_wait.get(ticket_id) != detail:
            self._last_wait[ticket_id] = detail
            log.info("eval.wait", extra={"fields": {"ticket_id": ticket_id, "outcome": outcome, "detail": detail}})
        return outcome

    # ------------------------------------------------------------------ start
    def ensure_eval(self, ticket_id: int, why: str = "") -> str:
        """Make sure the ticket's current stage has a live or finished eval. Idempotent."""
        with self.lock(ticket_id):
            ticket = self.m.get("ticket", ticket_id)
            if ticket is None or ticket.get("status") not in EVAL_STAGES:
                return "not_eval_stage"
            tgt = self.target(ticket)
            if tgt is None:
                return self._waiting(ticket_id, "no_checkpoint", "no finished training run to evaluate")
            runs = self.stage_runs(ticket_id, tgt)
            if any(r.get("status") == "done" for r in runs):
                return "already_done"
            live = [r for r in runs if r.get("status") in LIVE_EVAL]
            if live:
                run = live[-1]
                if self.command_for(_i(run["id"])) is not None:
                    return "in_progress"
                return self._dispatch(ticket, run, tgt, why=f"{why}; command missing")

            # A new eval is needed. After an estop abort (or too many aborts in a row), wait for a human.
            gate = self._human_gate(ticket_id, runs)
            if gate:
                return self._waiting(ticket_id, "awaiting_rerun_request",
                                     f"{gate}; append an eval.rerun_requested event with ticket_id {ticket_id} to run it again")
            if time.monotonic() < self._backoff.get(ticket_id, 0):
                return "backoff"
            robot_id = _i(ticket.get("robot_id"))
            robot = self.m.get("robot", robot_id)
            problem = self.robot_problem(robot)
            if problem:
                return self._waiting(ticket_id, "waiting_for_robot", problem)
            jobs = self.open_jobs(robot_id)
            if jobs:
                return self._waiting(ticket_id, "robot_busy", f"robot {robot_id} has open job command(s) "
                                     + ", ".join(f"{c['id']}:{c.get('type')}" for c in jobs))

            base = {"ticket_id": ticket_id, "robot_id": robot_id, "kind": tgt["kind"],
                    "checkpoint_uri": tgt["checkpoint_uri"], "training_run_id": tgt["training_run_id"],
                    "iteration": _i(ticket.get("iteration")), "why": why}
            if not self._activate_policy(base):
                self._backoff[ticket_id] = time.monotonic() + self.cfg.retry_backoff_s
                return "policy_activate_failed"

            before = max((_i(r["id"]) for r in self.runs(ticket_id)), default=0)
            try:
                self.w.call_reducer("start_eval_run", ticket_id, tgt["checkpoint_uri"], 0)
            except Exception as e:  # noqa: BLE001
                self.event("eval.error", {**base, "op": "start_eval_run", "error": str(e)[:500]})
                self._backoff[ticket_id] = time.monotonic() + self.cfg.retry_backoff_s
                return "start_failed"
            run = self._wait_for(lambda: next((r for r in self.runs(ticket_id) if _i(r["id"]) > before
                                               and r.get("checkpoint_uri") == tgt["checkpoint_uri"]
                                               and r.get("status") in LIVE_EVAL), None))
            if run is None:
                log.warning("eval.start_unconfirmed", extra={"fields": base})
                return "start_unconfirmed"  # next trigger finds it running and issues the command
            return self._dispatch(ticket, run, tgt, why=why)

    def _activate_policy(self, base: dict) -> bool:
        if not self.cfg.policy_server_url:
            self.event("eval.policy_activate_skipped", {**base, "note": "POLICY_SERVER_URL not set; the robot loads "
                                                                       "checkpoint_uri from the run_eval payload"})
            return True
        url = f"{self.cfg.policy_server_url}/policy/activate"
        body = {"checkpoint_uri": base["checkpoint_uri"], "ticket_id": base["ticket_id"],
                "robot_id": base["robot_id"], "kind": base["kind"]}
        t0 = time.monotonic()
        try:
            self.activate(url, body, self.cfg.policy_activate_timeout_s)
        except Exception as e:  # noqa: BLE001
            self.event("eval.policy_activate_failed", {**base, "url": url, "error": str(e)[:500],
                                                       "retry_in_s": self.cfg.retry_backoff_s})
            return False
        self.event("eval.policy_activated", {**base, "url": url, "ms": int((time.monotonic() - t0) * 1000)})
        return True

    def eval_timeout_s(self, trials: int) -> float:
        return max(1, trials) * (self.cfg.trial_timeout_s + self.cfg.reset_wait_s)

    def _dispatch(self, ticket: dict, run: dict, tgt: dict, why: str) -> str:
        run_id, robot_id, ticket_id = _i(run["id"]), _i(run.get("robot_id")) or _i(ticket.get("robot_id")), _i(ticket["id"])
        trials = _i(run.get("trials"))
        payload = {"eval_run_id": run_id, "checkpoint_uri": run.get("checkpoint_uri"), "trials": trials,
                   "timeout_s": self.cfg.trial_timeout_s, "kind": run.get("kind"),
                   "reset_between_trials": "wait for resume_policy (TDM-26 reset task)",
                   "solo_mode": self.cfg.solo_mode}
        from .interventions import eval_payload  # TDM-69: only when run_settings explicitly enables it

        payload.update(eval_payload(self.m, ticket_id, run.get("kind") or ""))
        from .robot_mode import robot_mode  # TDM-107: the robot knows (and labels) a sim eval

        payload["robot_mode"] = robot_mode(self.m.get("robot", robot_id))
        base = {"ticket_id": ticket_id, "robot_id": robot_id, "eval_run_id": run_id, "kind": run.get("kind"),
                "checkpoint_uri": run.get("checkpoint_uri"), "trials": trials, "solo_mode": self.cfg.solo_mode,
                "why": why, "interventions": bool(payload.get("interventions"))}
        try:
            self.w.call_reducer("issue_robot_command", robot_id, "run_eval", json.dumps(payload), ticket_id)
        except Exception as e:  # noqa: BLE001 - lost the job lock (or estop): don't leave the run dangling
            self.abort(run, "cancelled", f"run_eval refused: {str(e)[:300]}", force=True)
            return "command_refused"
        self.event("eval.dispatched", {**base, "eval_timeout_s": self.eval_timeout_s(trials)})
        return "dispatched"

    # ------------------------------------------------------------------ finish / abort
    def try_finalize(self, run: dict) -> bool:
        """Finalize through stats.EvalRunFinalizer once every planned trial row exists."""
        run_id = _i(run["id"])
        if run.get("status") != "running":
            return False
        rows = self.trials(run_id)
        planned = _i(run.get("trials"))
        if planned <= 0 or len({_i(t.get("trial_index")) for t in rows}) < planned:
            return False
        if self._guarded(("finalize", run_id)):
            return False
        fired: list[dict] = []

        def finalize(**kw) -> None:
            fired.append(kw)
            self.w.call_reducer("finalize_eval_run", kw["eval_run_id"], kw["successes"], kw["trials"],
                                kw["ci_low"], kw["ci_high"], kw["summary"])

        fin = EvalRunFinalizer(finalize)  # fresh per attempt: decides from the mirror's rows only
        try:
            for t in sorted(rows, key=lambda t: _i(t.get("trial_index"))):
                fin.on_eval_trial(t, run)
        except Exception as e:  # noqa: BLE001 - next trigger retries (the guard expires)
            self.event("eval.error", {"ticket_id": _i(run.get("ticket_id")), "eval_run_id": run_id,
                                      "op": "finalize_eval_run", "error": str(e)[:500]})
            return False
        if fired:
            kw = fired[0]
            log.info("eval.finalized", extra={"fields": {"ticket_id": _i(run.get("ticket_id")), "eval_run_id": run_id,
                                                         "summary": kw["summary"]}})
        return bool(fired)

    def abort(self, run: dict, status: str, reason: str, *, force: bool = False) -> bool:
        run_id = _i(run["id"])
        if run.get("status") not in LIVE_EVAL and not force:
            return False
        if self._guarded(("abort", run_id)):
            return False
        try:
            self.w.call_reducer("abort_eval_run", run_id, status, reason[:1000])
        except Exception as e:  # noqa: BLE001
            self.event("eval.error", {"ticket_id": _i(run.get("ticket_id")), "eval_run_id": run_id,
                                      "op": "abort_eval_run", "error": str(e)[:500]})
            return False
        log.warning("eval.aborted", extra={"fields": {"ticket_id": _i(run.get("ticket_id")), "eval_run_id": run_id,
                                                      "status": status, "reason": reason}})
        return True

    def _record_reported_outcomes(self, run: dict, cmd: dict) -> int:
        """The robot completed run_eval but reported trials only in result_json.outcomes: record those
        exact outcomes (never invented, never edited). Returns how many rows were written."""
        result = _json(cmd.get("result_json"))
        outcomes = result.get("outcomes")
        if not isinstance(outcomes, list):
            return 0
        have = {_i(t.get("trial_index")) for t in self.trials(_i(run["id"]))}
        n = 0
        for o in outcomes:
            if not isinstance(o, dict) or not isinstance(o.get("success"), bool) or "index" not in o:
                continue
            idx = _i(o.get("index"))
            if idx in have or idx >= _i(run.get("trials")):
                continue
            try:
                self.w.call_reducer("record_eval_trial", _i(run["id"]), idx, o["success"], _i(o.get("duration_ms")),
                                    _i(o.get("episode_id")), str(o.get("note") or o.get("failure_note") or ""))
                n += 1
            except Exception as e:  # noqa: BLE001
                self.event("eval.error", {"eval_run_id": run["id"], "op": "record_eval_trial", "index": idx,
                                          "error": str(e)[:300]})
        if n:
            self.event("eval.trials_recorded_from_result", {"ticket_id": _i(run.get("ticket_id")), "eval_run_id": run["id"],
                                                            "command_id": cmd["id"], "recorded": n})
        return n

    def abort_reason(self, run: dict) -> tuple[str, str] | None:
        """(status, reason) if this running eval can no longer finish, else None."""
        run_id = _i(run["id"])
        cmd = self.command_for(run_id)
        done_rows = len({_i(t.get("trial_index")) for t in self.trials(run_id)})
        planned = _i(run.get("trials"))
        if cmd is not None:
            st = cmd.get("status")
            if st == "failed":
                err = _json(cmd.get("result_json")).get("error") or cmd.get("result_json") or ""
                return "failed", f"robot failed run_eval command {cmd['id']} after {done_rows}/{planned} trials: {str(err)[:300]}"
            if st == "preempted":
                by = _json(cmd.get("result_json")).get("preempted_by")
                return "cancelled", f"estop: run_eval command {cmd['id']} preempted (soft_estop {by}) after {done_rows}/{planned} trials"
            if st == "done" and done_rows < planned:
                if self._record_reported_outcomes(run, cmd):
                    return None  # rows are on their way; the next check finalizes
                res = _json(cmd.get("result_json"))
                extra = f" ({res.get('reason')})" if res.get("aborted") else ""
                return "cancelled", f"robot completed run_eval command {cmd['id']} with {done_rows}/{planned} trial rows{extra}"
            if st in OPEN_JOB:
                issued = _i(cmd.get("issued_at"))
                limit = self.eval_timeout_s(planned)
                if issued and (self._now_us() - issued) / 1e6 > limit:
                    return "cancelled", f"timeout: eval ran longer than {limit:.0f}s ({done_rows}/{planned} trials)"
        robot = self.m.get("robot", _i(run.get("robot_id")))
        if robot is not None and robot.get("status") == "estop":
            return "cancelled", f"estop: robot {robot['id']} entered estop mid-eval ({done_rows}/{planned} trials)"
        problem = self.robot_problem(robot, running=True)
        if problem:
            return "cancelled", f"robot lost mid-eval: {problem} ({done_rows}/{planned} trials)"
        if cmd is None:
            created = _i(run.get("created_at"))
            if created and (self._now_us() - created) / 1e6 > NO_COMMAND_GRACE_S:
                return "cancelled", f"no run_eval command was issued for eval {run_id} within {NO_COMMAND_GRACE_S:.0f}s"
        return None

    def check_run(self, run: dict) -> str:
        """Finalize a complete eval, or abort one that can no longer finish."""
        run = self.m.get("eval_run", _i(run["id"])) or run
        if run.get("status") != "running":
            return "not_running"
        if self.try_finalize(run):
            return "finalized"
        planned = _i(run.get("trials"))
        if len({_i(t.get("trial_index")) for t in self.trials(_i(run["id"]))}) >= planned > 0:
            return "finalizing"
        why = self.abort_reason(run)
        if why is None:
            return "running"
        status, reason = why
        cmd = self.command_for(_i(run["id"]))
        if reason.startswith("timeout:") and cmd is not None and cmd.get("status") in OPEN_JOB:
            try:  # the robot is alive but stuck: free its job lock
                self.w.call_reducer("issue_robot_command", _i(run.get("robot_id")), "stop",
                                    json.dumps({"reason": reason, "eval_run_id": run["id"]}), _i(run.get("ticket_id")))
            except Exception as e:  # noqa: BLE001
                log.warning("eval.stop_failed", extra={"fields": {"eval_run_id": run["id"], "error": str(e)[:300]}})
        return "aborted" if self.abort(run, status, reason) else "abort_pending"

    def check_all(self) -> None:
        """Watchdog tick: check every running eval, then (re)start evals tickets are waiting for."""
        for run in list(self.m.rows("eval_run")):
            if run.get("status") == "running":
                try:
                    self.check_run(run)
                except Exception:  # noqa: BLE001
                    log.exception("eval.check_error", extra={"fields": {"eval_run_id": run.get("id")}})
        for t in list(self.m.rows("ticket")):
            if t.get("status") in EVAL_STAGES:
                try:
                    self.ensure_eval(_i(t["id"]), why="watchdog")
                except Exception:  # noqa: BLE001
                    log.exception("eval.ensure_error", extra={"fields": {"ticket_id": t.get("id")}})

    def kick_robot(self, robot_id: int, why: str) -> None:
        """Robot became usable again (online / its job closed): start waiting evals on it."""
        for t in list(self.m.rows("ticket")):
            if _i(t.get("robot_id")) == robot_id and t.get("status") in EVAL_STAGES:
                self.ensure_eval(_i(t["id"]), why=why)

    # ------------------------------------------------------------------ solo mode resets
    def _robot_is_sim(self, robot_id: int) -> bool:
        from .robot_mode import robot_is_sim
        return robot_is_sim(self.m.get("robot", robot_id))

    def solo_reset(self, task: dict) -> None:
        """EVAL_SOLO_MODE: after the delay, complete an open reset task nobody claimed, so an unattended
        eval keeps going. With EVAL_SOLO_CONTRIBUTOR_ID the task is claimed + completed as that
        contributor (the dispatcher then pays it and issues resume_policy); otherwise it is cancelled
        (nobody is paid) and this module issues the resume_policy itself. A sim robot resets its own
        scene, so with EVAL_SIM_AUTO_RESET its reset tasks go the same way even outside solo mode."""
        if not (self.cfg.solo_mode or (self.cfg.sim_auto_reset and self._robot_is_sim(_i(task.get("robot_id"))))):
            return
        task_id = _i(task["id"])
        if task_id in self._solo_seen:
            return
        self._solo_seen.add(task_id)

        def go() -> None:
            if self._stop.wait(self.cfg.solo_reset_delay_s):
                return
            cur = self.m.get("task", task_id) or task
            if cur.get("type") != "reset" or cur.get("status") != "open":
                return
            rid, tid = _i(cur.get("robot_id")), _i(cur.get("ticket_id"))
            base = {"ticket_id": tid, "robot_id": rid, "task_id": task_id, "ref": task_ref(cur), "solo_mode": True,
                    "delay_s": self.cfg.solo_reset_delay_s}
            cid = self.cfg.solo_contributor_id
            try:
                if cid:
                    self.w.call_reducer("claim_task", task_id, cid)
                    self.w.call_reducer("complete_task", task_id, json.dumps({"solo_mode": True, "by": ACTOR}))
                    self.event("eval.solo_reset", {**base, "mode": "completed_as_contributor", "contributor_id": cid})
                else:
                    self.w.call_reducer("cancel_task", task_id, "solo_mode: auto-reset, no human (unattended test)")
                    self.w.call_reducer("issue_robot_command", rid, "resume_policy",
                                        json.dumps({"reason": "solo_mode_reset", "task_id": task_id,
                                                    "ref": task_ref(cur)}), tid)
                    self.event("eval.solo_reset", {**base, "mode": "cancelled_and_resumed"})
            except Exception as e:  # noqa: BLE001 - a human may have claimed it in the meantime
                self.event("eval.error", {**base, "op": "solo_reset", "error": str(e)[:300]})

        threading.Thread(target=go, name=f"eval-solo-reset-{task_id}", daemon=True).start()

    # ------------------------------------------------------------------ watchdog
    def start_watchdog(self) -> None:
        if self.cfg.watchdog_s <= 0 or (self._watchdog and self._watchdog.is_alive()):
            return

        def loop() -> None:
            while not self._stop.wait(self.cfg.watchdog_s):
                try:
                    self.check_all()
                except Exception:  # noqa: BLE001 - the tick must never die
                    log.exception("eval.watchdog_error")

        self._watchdog = threading.Thread(target=loop, name="eval-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- flywheel step

@dataclass
class EvalStep:
    """Flywheel entry step for ``baseline`` and ``eval`` (sync: runs on a worker thread)."""

    stage: str
    owner: str = "TDM-67"

    def run(self, ctx: StepContext) -> None:
        out = get_evaluator(ctx.bus).ensure_eval(ctx.ticket_id, why=f"flywheel step {self.stage}")
        ctx.log("step.eval", outcome=out)


# --------------------------------------------------------------------------- per-bus singleton

def _default_writer() -> Writer:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)


make_writer: Callable[[], Writer] = _default_writer  # swappable for tests
make_config: Callable[[], EvalConfig] = EvalConfig.from_env
_instances: dict[int, EvalOrchestrator] = {}
_instances_lock = threading.Lock()


def get_evaluator(bus: Mirror) -> EvalOrchestrator:
    with _instances_lock:
        e = _instances.get(id(bus))
        if e is None or e.m is not bus:
            e = _instances[id(bus)] = EvalOrchestrator(bus, lambda: make_writer(), make_config())
            e.start_watchdog()
        return e

