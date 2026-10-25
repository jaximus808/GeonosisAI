"""TDM-114 robot arbitration: who holds a robot, who waits for it, and handing it over.

One robot serves several tickets (the sim robot serves every sim ticket). Its TDM-13 job lock
already makes them take turns; this module adds the policy on top and says it out loud.

Holder (``holder``): the open job command on the robot (run_eval / run_baseline / start_teleop /
reset_request / home / load_checkpoint), its ticket, what kind of job it is, and whether it is paused
for a person (a live TDM-69 intervention ``awaiting_claim``) or a person is driving (``taken_over``,
a claimed clean_teleop). Read straight from rows, nothing cached.

Queue policy (default):
  * A ticket's baseline / eval queues FIFO behind the job that is running. A collection rollout
    (TDM-69 ``run_baseline`` purpose=collect_recovery) never starts while another ticket on the same
    robot is waiting for an eval (``collection_blocker`` -> ``yield_to_eval``), so an eval gets the
    robot as soon as the current rollout and its reset are done.
  * A collection rollout paused waiting for a person for longer than
    ``RDF_PAUSED_TAKEOVER_TIMEOUT_S`` (default 120, 0 = never) yields the robot to a queued eval on
    its own: the trial ends as a failure (the same ``resume_policy`` {end_trial} the claim timeout
    sends), its takeover task goes back to the marketplace (or is cancelled if the interventions
    module created it), and the ticket is held: ``robot.yielded``.
  * An operator can do the same at once: ``POST /tickets/{id}/robot/claim_now`` (``robot.reassigned``,
    then ``robot.claim_now``). A running rollout that is not paused is stopped with the eval
    watchdog's ``stop`` command. A person driving (takeover claimed, clean teleop) and another
    ticket's running eval are never preempted: the answer is a 409 that says who and why.

Hold: the preempted ticket does not grab the robot back (``collection_blocker`` ->
``robot_reassigned``) until ``POST /tickets/{id}/robot/resume`` (``robot.resume_requested``) or the
ticket it yielded to leaves its eval stage (its eval finished).

Every write is an existing reducer (``issue_robot_command``, ``cancel_task``, ``append_event``)
through the interventions / eval modules' own paths. Actor ``orchestrator:arbitration``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .dispatcher import JOB_TYPES, OPEN_JOB, TERMINAL_TICKET, Mirror, Writer
from .log import get_logger

log = get_logger("arbitration")

ACTOR = "orchestrator:arbitration"
REASSIGNED = "robot.reassigned"  # operator claim_now: {robot_id, ticket_id (held), to_ticket_id, by, ...}
YIELDED = "robot.yielded"  # automatic: same payload, auto: true
RESUMED = "robot.resume_requested"  # {ticket_id, robot_id, by}
RELEASED = "robot.released"  # a cancelled / failed ticket let go of its robot
ESTOP_CLEAR = "robot.estop_clear_requested"  # sim robots only; the sim robot client unlatches and logs robot.estop_cleared
CLAIM_NOW = "robot.claim_now"  # {ticket_id, robot_id, by, outcome, preempted_ticket_id?, eval_outcome}
HOLD_TYPES = (REASSIGNED, YIELDED)
EVAL_STAGES = ("baseline", "eval")
COLLECT_PURPOSE = "collect_recovery"


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


def tref(ticket_id: int) -> str:
    return f"TKT-{int(ticket_id):04d}"


def paused_takeover_timeout_s() -> float:
    try:
        return float(os.environ.get("RDF_PAUSED_TAKEOVER_TIMEOUT_S", "") or 120.0)
    except ValueError:
        return 120.0


class ArbitrationError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


# --------------------------------------------------------------------------- reads (pure, from rows)

def job_kind(cmd: dict) -> str:
    """collect | baseline | eval | teleop | reset | home | checkpoint | other."""
    t = cmd.get("type")
    p = _json(cmd.get("payload_json"))
    if t in ("run_eval", "run_baseline"):
        if p.get("purpose") == COLLECT_PURPOSE:
            return "collect"
        return "baseline" if (p.get("kind") == "baseline" or t == "run_baseline") else "eval"
    return {"start_teleop": "teleop", "reset_request": "reset", "home": "home", "load_checkpoint": "checkpoint"}.get(t or "", "other")


def open_jobs(m: Mirror, robot_id: int) -> list[dict]:
    return sorted((c for c in m.rows("robot_command") if _i(c.get("robot_id")) == robot_id
                   and c.get("status") in OPEN_JOB and c.get("type") in JOB_TYPES), key=lambda c: _i(c["id"]))


def _live_iv_events(m: Mirror, robot_id: int) -> dict | None:
    """The newest ``intervention.started`` on this robot with no ``intervention.closed`` after it (the
    payload, plus ``claimed`` if an ``intervention.claimed`` followed and no lapse after that)."""
    started: dict[str, dict] = {}
    for e in sorted(m.rows("event_log"), key=lambda r: _i(r.get("id"))):
        t = e.get("type") or ""
        if not t.startswith("intervention.") or e.get("actor") != "orchestrator:interventions":
            continue
        p = _json(e.get("payload_json"))
        ref = p.get("ref")
        if not ref:
            continue
        if t == "intervention.started" and _i(p.get("robot_id")) == robot_id:
            started[ref] = {**p, "claimed": p.get("phase") == "taken_over"}
        elif ref in started:
            if t == "intervention.closed":
                started.pop(ref, None)
            elif t == "intervention.claimed":
                started[ref]["claimed"] = True
            elif t == "intervention.claim_lapsed":
                started[ref]["claimed"] = False
            elif t == "intervention.handback":
                started[ref]["handed_back"] = True
    if not started:
        return None
    return max(started.values(), key=lambda p: _i(p.get("started_us")))


def holder(m: Mirror, robot_id: int) -> dict | None:
    """Who holds the robot right now, or None (no open job). ``state``: running | paused_for_takeover |
    taken_over | teleop | waiting_reset. ``human``: a person is in control (never preempted)."""
    jobs = open_jobs(m, robot_id)
    if not jobs:
        return None
    cmd = jobs[0]
    kind = job_kind(cmd)
    p = _json(cmd.get("payload_json"))
    robot = m.get("robot", robot_id) or {}
    out: dict[str, Any] = {"robot_id": robot_id, "ticket_id": _i(cmd.get("ticket_id")), "command_id": _i(cmd["id"]),
                           "command_type": cmd.get("type"), "kind": kind, "state": "running", "human": False,
                           "since_us": _i(cmd.get("acked_at")) or _i(cmd.get("issued_at")),
                           "eval_run_id": _i(p.get("eval_run_id")), "task_id": 0}
    iv = _live_iv_events(m, robot_id)
    if iv is not None and _i(iv.get("command_id")) == out["command_id"]:
        out.update(task_id=_i(iv.get("task_id")), intervention_ref=iv.get("ref"),
                   paused_since_us=_i(iv.get("started_us")))
        if iv.get("handed_back"):
            out["state"] = "running"
        elif iv.get("claimed"):
            out.update(state="taken_over", human=True)
        else:
            out["state"] = "paused_for_takeover"
    elif kind == "teleop":
        out.update(state="teleop", human=True, task_id=_i(p.get("task_id")))
    elif robot.get("status") == "paused":
        out["state"] = "waiting_reset"
    return out


def stage_entered_us(m: Mirror, ticket_id: int, status: str) -> int:
    best = 0
    for e in m.rows("event_log"):
        if e.get("type") != "ticket.status_changed":
            continue
        p = _json(e.get("payload_json"))
        if _i(p.get("ticket_id")) == ticket_id and p.get("to") == status:
            best = max(best, _i(e.get("ts")) or _i(e.get("id")))
    return best


def _evaluator(m: Mirror):
    """A read-only EvalOrchestrator for target / stage_runs / human gate (no writer, no watchdog)."""
    from .evaluate import EvalOrchestrator

    def _no_writer():
        raise RuntimeError("read-only evaluator")

    return EvalOrchestrator(m, _no_writer)


def needs_eval(m: Mirror, ticket: dict, ev: Any = None) -> bool:
    """The ticket is in baseline/eval and still needs an eval started on its robot (none done, none live,
    no human gate)."""
    if ticket is None or ticket.get("status") not in EVAL_STAGES:
        return False
    ev = ev or _evaluator(m)
    tgt = ev.target(ticket)
    if tgt is None:
        return False
    runs = ev.stage_runs(_i(ticket["id"]), tgt)
    if any(r.get("status") in ("done", "queued", "running") for r in runs):
        return False
    return ev._human_gate(_i(ticket["id"]), runs) is None


def eval_queue(m: Mirror, robot_id: int, *, exclude: int = 0, ev: Any = None) -> list[dict]:
    """Tickets on this robot waiting for an eval to start, oldest stage entry first (FIFO)."""
    ev = ev or _evaluator(m)
    out = [t for t in m.rows("ticket") if _i(t.get("robot_id")) == robot_id and _i(t["id"]) != exclude
           and needs_eval(m, t, ev)]
    return sorted(out, key=lambda t: (stage_entered_us(m, _i(t["id"]), t.get("status") or ""), _i(t["id"])))


def hold_for(m: Mirror, ticket_id: int) -> dict | None:
    """The live hold on this ticket (it gave its robot away), or None. Released by a later
    ``robot.resume_requested``, or once the ticket it yielded to left its eval stage."""
    hold: dict | None = None
    hold_id = 0
    resumed_id = 0
    for e in m.rows("event_log"):
        t = e.get("type")
        if t not in HOLD_TYPES and t != RESUMED:
            continue
        p = _json(e.get("payload_json"))
        if _i(p.get("ticket_id")) != ticket_id:
            continue
        eid = _i(e.get("id"))
        if t == RESUMED:
            resumed_id = max(resumed_id, eid)
        elif eid > hold_id:
            hold, hold_id = {**p, "event_id": eid, "type": t}, eid
    if hold is None or resumed_id > hold_id:
        return None
    to = m.get("ticket", _i(hold.get("to_ticket_id")))
    if to is None or to.get("status") not in EVAL_STAGES:
        return None
    return hold


def collection_blocker(m: Mirror, ticket_id: int, robot_id: int) -> tuple[str, str] | None:
    """Why this ticket must not start a collection rollout on its robot now (interventions.ensure_collection)."""
    h = hold_for(m, ticket_id)
    if h is not None:
        return "robot_reassigned", (f"paused: robot reassigned to {tref(_i(h.get('to_ticket_id')))} "
                                    f"({'auto' if h.get('auto') else h.get('by') or 'operator'}); resume with "
                                    f"POST /tickets/{ticket_id}/robot/resume")
    q = eval_queue(m, robot_id, exclude=ticket_id)
    if q:
        return "yield_to_eval", f"{tref(_i(q[0]['id']))} is waiting for its {q[0].get('status')} eval on robot {robot_id}"
    return None


def robot_queue(m: Mirror, robot_id: int) -> dict:
    """GET /robots/{id}/queue: what the robot is doing now and what's next, in plain rows."""
    h = holder(m, robot_id)
    ev = _evaluator(m)
    q = eval_queue(m, robot_id, ev=ev)
    tickets = [t for t in m.rows("ticket") if _i(t.get("robot_id")) == robot_id and t.get("status") not in TERMINAL_TICKET]
    waiting = [{"ticket_id": _i(t["id"]), "ticket": tref(_i(t["id"])), "job": t.get("status"), "state": "queued"} for t in q]
    held = []
    for t in tickets:
        hh = hold_for(m, _i(t["id"]))
        if hh is not None:
            held.append({"ticket_id": _i(t["id"]), "ticket": tref(_i(t["id"])), "job": "collect", "state": "paused",
                         "note": f"robot reassigned to {tref(_i(hh.get('to_ticket_id')))}"})
    collecting = [{"ticket_id": _i(t["id"]), "ticket": tref(_i(t["id"])), "job": "collect", "state": "queued"}
                  for t in tickets if t.get("status") == "dispatch" and not any(x["ticket_id"] == _i(t["id"]) for x in held)
                  and (h is None or h["ticket_id"] != _i(t["id"]))]
    return {"robot_id": robot_id, "holder": h, "queue": waiting + collecting + held,
            "paused_takeover_timeout_s": paused_takeover_timeout_s()}


# --------------------------------------------------------------------------- actions

@dataclass
class Arbiter:
    m: Mirror
    writer_factory: Callable[[], Writer]
    clock: Callable[[], float] = time.time
    ivm: Any = None  # InterventionManager (default: the bus singleton)
    ev: Any = None  # EvalOrchestrator (default: the bus singleton)
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        self._writer: Writer | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._last_tick = 0.0

    @property
    def w(self) -> Writer:
        if self._writer is None:
            self._writer = self.writer_factory()
        return self._writer

    @property
    def iv(self):
        if self.ivm is None:
            from .interventions import get_manager
            self.ivm = get_manager(self.m)
        return self.ivm

    @property
    def evo(self):
        if self.ev is None:
            from .evaluate import get_evaluator
            self.ev = get_evaluator(self.m)
        return self.ev

    @property
    def timeout(self) -> float:
        return paused_takeover_timeout_s() if self.timeout_s is None else self.timeout_s

    def now_us(self) -> int:
        return int(self.clock() * 1_000_000)

    def event(self, type_: str, payload: dict) -> None:
        log.info(type_, extra={"fields": payload})
        try:
            self.w.call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str))
        except Exception as e:  # noqa: BLE001 - the log line above is the fallback record
            log.warning("arbitration.event_failed", extra={"fields": {"type": type_, "error": str(e)[:300]}})

    # ---- preempt the holder (collection rollouts only)
    def _preempt(self, h: dict, *, to_ticket_id: int, by: str, auto: bool, reason: str,
                 event_type: str | None = None) -> dict:
        rid, held_tid = h["robot_id"], h["ticket_id"]
        out: dict[str, Any] = {"robot_id": rid, "ticket_id": held_tid, "to_ticket_id": to_ticket_id, "by": by,
                               "auto": auto, "command_id": h["command_id"], "holder_state": h["state"],
                               "reason": reason}
        mgr = self.iv
        iv = mgr.live_on_robot(rid)
        if h["state"] == "paused_for_takeover" and iv is not None and iv.command_id == h["command_id"]:
            out.update(mgr.preempt(iv, reason=reason, to_ticket_id=to_ticket_id))
        else:  # running, not paused: the eval watchdog's stop path (the robot cancels the job, completes it aborted)
            try:
                self.w.call_reducer("issue_robot_command", rid, "stop",
                                    json.dumps({"reason": reason, "by": ACTOR, "to_ticket_id": to_ticket_id}), held_tid)
                out["stop_sent"] = True
            except Exception as e:  # noqa: BLE001
                out.update(stop_sent=False, stop_error=str(e)[:300])
        self.event(event_type or (YIELDED if auto else REASSIGNED), out)
        return out

    def release_ticket(self, ticket_id: int, *, why: str) -> dict | None:
        """A cancelled / failed ticket lets go of its robot: its paused or running rollout is stopped (a
        person driving is left alone), and the next queued eval is kicked. ``robot.released``."""
        with self._lock:
            ticket = self.m.get("ticket", ticket_id)
            rid = _i((ticket or {}).get("robot_id"))
            h = holder(self.m, rid) if rid else None
            if h is None or h["ticket_id"] != ticket_id or h["human"]:
                return None
            q = eval_queue(self.m, rid, exclude=ticket_id)
            to = _i(q[0]["id"]) if q else 0
            out = self._preempt(h, to_ticket_id=to, by="orchestrator", auto=True, event_type=RELEASED,
                                reason=f"{tref(ticket_id)} {why}")
            try:
                self.evo.kick_robot(rid, why=f"{tref(ticket_id)} released robot {rid}")
            except Exception as e:  # noqa: BLE001
                out["kick_error"] = str(e)[:200]
            return out

    def clear_sim_estop(self, robot_id: int, *, by: str) -> dict:
        """Sim robots only: ask the sim robot client to unlatch its soft e-stop (nobody stands at a sim
        arm). The robot client acts on ``robot.estop_clear_requested`` from an orchestrator actor and logs
        ``robot.estop_cleared``. Real robots keep the rule that a person at the arm clears it."""
        from .robot_mode import robot_mode

        robot = self.m.get("robot", robot_id)
        if robot is None:
            raise ArbitrationError(404, "robot_not_found", f"no robot {robot_id}")
        if robot_mode(robot) != "sim":
            raise ArbitrationError(409, "real_robot", "A real arm's e-stop is cleared by a person at the arm "
                                                      "(type clear in the robot client, or SIGUSR1).")
        payload = {"robot_id": robot_id, "by": by, "status_was": robot.get("status")}
        self.event(ESTOP_CLEAR, payload)
        return {"requested": True, **payload}

    def claim_now(self, ticket_id: int, *, by: str = "operator") -> dict:
        with self._lock:
            ticket = self.m.get("ticket", ticket_id)
            if ticket is None:
                raise ArbitrationError(404, "ticket_not_found", f"no ticket {ticket_id}")
            st = ticket.get("status")
            if st not in EVAL_STAGES:
                raise ArbitrationError(409, "not_eval_stage", f"{tref(ticket_id)} is in {st}; only a baseline or eval "
                                                              "can claim the robot")
            ev = self.evo
            tgt = ev.target(ticket)
            if tgt is None:
                raise ArbitrationError(409, "no_checkpoint", "no finished training run to evaluate yet")
            runs = ev.stage_runs(ticket_id, tgt)
            if any(r.get("status") == "done" for r in runs):
                raise ArbitrationError(409, "already_done", f"{tref(ticket_id)}'s {st} eval already finished")
            if any(r.get("status") in ("queued", "running") for r in runs):
                raise ArbitrationError(409, "already_running", f"{tref(ticket_id)}'s {st} eval is already running")
            rid = _i(ticket.get("robot_id"))
            h = holder(self.m, rid)
            base = {"ticket_id": ticket_id, "robot_id": rid, "by": by}
            pre: dict | None = None
            if h is not None:
                who = tref(h["ticket_id"]) if h["ticket_id"] else "another job"
                if h["ticket_id"] == ticket_id:
                    raise ArbitrationError(409, "already_holds", f"{tref(ticket_id)} already holds the robot")
                if h["human"]:
                    what = f"task #{h['task_id']}" if h.get("task_id") else "teleop"
                    raise ArbitrationError(409, "person_driving", f"A person is driving the robot for {who} {what}. "
                                                                  "Wait for the handback.", holder=h)
                if h["kind"] in ("baseline", "eval"):
                    raise ArbitrationError(409, "eval_running", f"{who}'s eval is running on the robot; this ticket "
                                                                "runs next.", holder=h)
                if h["kind"] == "collect":
                    pre = self._preempt(h, to_ticket_id=ticket_id, by=by, auto=False,
                                        reason=f"operator {by}: run {tref(ticket_id)}'s {st} eval now")
                # a reset / home / checkpoint load finishes on its own; the eval is first in line after it
            out = self.evo.ensure_eval(ticket_id, why=f"claim_now by {by}")
            res = {**base, "outcome": "preempted" if pre else ("queued" if h is not None else "started"),
                   "preempted": pre, "eval_outcome": out, "holder": h}
            self.event(CLAIM_NOW, {**base, "outcome": res["outcome"], "eval_outcome": out,
                                   "preempted_ticket_id": pre["ticket_id"] if pre else 0})
            return res

    def resume(self, ticket_id: int, *, by: str = "operator") -> dict:
        with self._lock:
            ticket = self.m.get("ticket", ticket_id)
            if ticket is None:
                raise ArbitrationError(404, "ticket_not_found", f"no ticket {ticket_id}")
            h = hold_for(self.m, ticket_id)
            if h is None:
                raise ArbitrationError(409, "not_paused", f"{tref(ticket_id)} is not paused for a robot reassignment")
            self.event(RESUMED, {"ticket_id": ticket_id, "robot_id": _i(ticket.get("robot_id")), "by": by,
                                 "hold_event_id": h["event_id"]})
            # the mirror may lag our own event by a moment; ensure_collection re-checks on its watchdog tick anyway
            try:
                out = self.iv.ensure_collection(ticket_id, why=f"resumed by {by}")
            except Exception as e:  # noqa: BLE001
                out = f"error: {str(e)[:200]}"
            return {"ticket_id": ticket_id, "outcome": "resumed", "collection": out}

    # ---- automatic yield
    def tick(self) -> list[dict]:
        """A collection rollout paused for a person longer than the timeout yields to a queued eval."""
        if self.timeout <= 0:
            return []
        done: list[dict] = []
        now = self.now_us()
        with self._lock:
            robots = {_i(c.get("robot_id")) for c in self.m.rows("robot_command") if c.get("status") in OPEN_JOB}
            for rid in sorted(robots):
                h = holder(self.m, rid)
                if h is None or h["kind"] != "collect" or h["state"] != "paused_for_takeover":
                    continue
                waited = (now - _i(h.get("paused_since_us"))) / 1e6
                if waited < self.timeout:
                    continue
                q = eval_queue(self.m, rid, exclude=h["ticket_id"])
                if not q:
                    continue
                to = _i(q[0]["id"])
                pre = self._preempt(h, to_ticket_id=to, by="auto", auto=True,
                                    reason=f"paused {waited:.0f}s for a takeover nobody claimed (limit "
                                           f"{self.timeout:.0f}s, RDF_PAUSED_TAKEOVER_TIMEOUT_S); "
                                           f"{tref(to)}'s {q[0].get('status')} eval is waiting")
                try:
                    pre["eval_outcome"] = self.evo.ensure_eval(to, why="robot.yielded")
                except Exception as e:  # noqa: BLE001
                    pre["eval_outcome"] = f"error: {str(e)[:200]}"
                done.append(pre)
        return done

    def start_watchdog(self, every_s: float = 2.0) -> None:
        if every_s <= 0 or (self._watchdog and self._watchdog.is_alive()):
            return

        def loop() -> None:
            while not self._stop.wait(every_s):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the tick must never die
                    log.exception("arbitration.watchdog_error")

        self._watchdog = threading.Thread(target=loop, name="arbitration-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- per-bus singleton

def _default_writer() -> Writer:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)


make_writer: Callable[[], Writer] = _default_writer  # swappable for tests
_instances: dict[int, Arbiter] = {}
_instances_lock = threading.Lock()


def get_arbiter(bus: Mirror) -> Arbiter:
    with _instances_lock:
        a = _instances.get(id(bus))
        if a is None or a.m is not bus:
            a = _instances[id(bus)] = Arbiter(bus, lambda: make_writer())
            try:
                every = float(os.environ.get("RDF_ARBITRATION_WATCHDOG_S", "2") or 0)
            except ValueError:
                every = 2.0
            a.start_watchdog(every)
        return a
