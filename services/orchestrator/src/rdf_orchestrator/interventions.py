"""TDM-69 DAgger-style interventions: pause a failing policy rollout, pay a person to take over,
hand back, and feed the recovery episode to review and training.

Bus wiring is in ``handlers/interventions.py``. This module holds the logic and decides from rows
(the bus mirror + its own ``intervention.*`` event trail), so every entry point is idempotent and
survives a restart (the trail is replayed on first use).

The loop, end to end::

  robot rolls out the policy (an eval trial with interventions enabled, or a collection rollout
  this module issues while a recovery_takeover plan is collecting)
    -> FAILURE SIGNAL, one of
         * robot detector: ``intervention.failure_detected`` event appended by the robot client
           {robot_id, ticket_id, command_id, trial_index, eval_run_id?, detector, reason}
           detector = timeout | no_progress | success_check (rdf_robot_client.interventions)
         * operator flag: ``POST /robots/{id}/interventions/flag`` (the robot page's "Failing"
           button) or an ``intervention.flagged`` event {robot_id, note}
         * orchestrator rollout timeout: a collection rollout acked longer than
           ``rollout_timeout_s + timeout_grace_s`` with no signal (the robot never said anything)
    -> start(): checks (enabled, the rollout command is still open, no live intervention on the
       robot, budget), then ROUTES a ``recovery_takeover`` task, in this order:
         1. a recovery_takeover task already *claimed* on this robot (a contributor waiting), or
         2. an *open* unclaimed recovery_takeover task of the ticket (the TDM-26 dispatcher's
            plan tasks), or
         3. a new one: ``create_task(ticket, recovery_takeover, $5, {instructions "[ref iv:...]",
            data_plan_id})`` (only if budget - spent - committed >= price)
       then ``pause_for_takeover`` {ref, task_id, reason} on the robot (task first, so the TDM-26
       dispatcher's "robot paused -> reset" handler sees the takeover task and stays out), pages
       contributors (dispatcher.page) and logs ``intervention.started``.
    -> CLAIM (task claimed, TDM-10 atomic claim, 60 s arm-control lease the claimant heartbeats):
       ``intervention.claimed`` {contributor_id, claim_wait_s}. The claimant drives (TDM-52/56/57
       teleop; the TDM-28 fake for now). The robot's recorder keeps rolling and stamps
       ``takeover_ts`` when human control starts (TDM-48 ``mark_takeover``).
    -> HANDBACK (a ``handback`` command reaches ``done``; or the robot appends
       ``intervention.handed_back`` after a TDM-52 control-socket ``{t:"handback"}``; or the claimant
       completes the task first and this module sends ``handback`` for them): ``intervention.handback``. The robot client maps
       handback to resume (TDM-49 ``_ctl_resume``): the policy finishes the trial, the recorder
       stamps ``handback_ts``, and the episode is written as kind ``recovery_takeover`` with the
       task's id and contributor (``control_source`` policy -> human -> policy).
    -> rollout command closes: ``intervention.closed`` outcome ``recovered``; a still-claimed task is
       completed for the claimant (lease cleared); a collection rollout gets a paid reset
       (dispatcher.request_reset) and the next rollout starts once the scene is reset.
    -> the episode lands ``pending`` -> the TDM-25 reviewer reviews it (takeover/handback timestamps,
       failure-before-takeover, smoothness, success) -> ``intervention.episode_recorded`` /
       ``intervention.reviewed``. Accepted episodes are paid by submit_review and are in the next
       ``training_run.episode_ids`` (QueueTrainingStep takes every accepted episode).

Timeouts (``tick``, the watchdog):
  * nobody claims within ``claim_timeout_s``: ``resume_policy`` {end_trial: true, outcome: failure,
    ref} (the robot ends the trial as a failure), the task is cancelled if this module created it,
    ``intervention.timeout`` {stage: claim} + ``intervention.closed`` unclaimed_timeout. The usual
    reset + next trial follow (eval: the dispatcher's per-trial reset; collection: ours).
  * a claimant drives longer than ``takeover_max_s``: ``handback`` {reason: takeover_overrun}.
  * the claimant's lease lapses (no heartbeat for 60 s): ``intervention.claim_lapsed``, re-page,
    and the claim timer starts again.
  * robot estop / offline / stale heartbeat, or the rollout ends while unclaimed: closed, a task
    this module created is cancelled.

Where interventions are on (``enabled_for``): ``run_settings.hyperparams_json.interventions_enabled``
(true/false) wins. Default: ON for collection (ticket ``dispatch`` with a recovery_takeover plan of
this iteration, on a robot that advertises ``"interventions"`` in camera_config_json.capabilities),
OFF for baseline / eval rollouts, so eval numbers stay clean. When an eval does run with
interventions explicitly on, an intervened trial is still recorded as a FAILURE of the policy.

Robot payload contract (``run_eval`` / ``run_baseline`` key ``interventions``) and the verdict
commands are documented in ``services/robot-client/src/rdf_robot_client/interventions.py``.

Env (all optional): INTERVENTION_CLAIM_TIMEOUT_S 90, INTERVENTION_TAKEOVER_MAX_S 300,
INTERVENTION_VERDICT_WAIT_S 15, INTERVENTION_NO_PROGRESS_S 20, INTERVENTION_ROLLOUT_TIMEOUT_S 120,
INTERVENTION_TIMEOUT_GRACE_S 30, INTERVENTION_WATCHDOG_S 2 (0 = off), INTERVENTION_COLLECT 1,
INTERVENTION_COLLECT_MAX_ROLLOUTS 40 (per plan).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import dispatcher as dsp
from .dispatcher import JOB_TYPES, OPEN_JOB, Mirror, Writer, task_ref
from .log import get_logger

log = get_logger("interventions")

ACTOR = "orchestrator:interventions"
ROLLOUT_COMMANDS = ("run_eval", "run_baseline")
COLLECT_PURPOSE = "collect_recovery"
TASK_TYPE = "recovery_takeover"
FAILURE_EVENT = "intervention.failure_detected"  # appended by the robot client
FLAG_EVENT = "intervention.flagged"  # appended by an operator surface (or POST .../flag)
# appended by the robot client when the claimant hands back over the TDM-52 control socket
# ({t:"handback"}) instead of a ``handback`` robot_command: {robot_id, task_id?, ref?}
HANDBACK_EVENT = "intervention.handed_back"
DETECTORS = ("timeout", "no_progress", "success_check", "operator", "orchestrator_timeout")
CAPABILITY = "interventions"
USABLE_ROBOT = ("online", "busy", "paused")
TABLES = ("ticket", "run_settings", "robot", "robot_command", "task", "episode", "review", "data_plan", "event_log",
          "eval_run", "eval_trial", "training_run", "contributor", "payment")
COLLECT_FAIL_STRIKES = 3  # this many collection rollouts failed in a row -> stop and wait for a human

LIVE = ("awaiting_claim", "taken_over", "handed_back")


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
class InterventionConfig:
    claim_timeout_s: float = 90.0
    takeover_max_s: float = 300.0
    verdict_wait_s: float = 15.0  # robot holds this long after reporting a failure, waiting for us
    no_progress_s: float = 20.0
    rollout_timeout_s: float = 120.0
    timeout_grace_s: float = 30.0
    watchdog_s: float = 2.0
    collect: bool = True
    collect_max_rollouts: int = 40
    robot_stale_s: float = 30.0

    @classmethod
    def from_env(cls) -> "InterventionConfig":
        return cls(
            claim_timeout_s=_env_float("INTERVENTION_CLAIM_TIMEOUT_S", 90.0),
            takeover_max_s=_env_float("INTERVENTION_TAKEOVER_MAX_S", 300.0),
            verdict_wait_s=_env_float("INTERVENTION_VERDICT_WAIT_S", 15.0),
            no_progress_s=_env_float("INTERVENTION_NO_PROGRESS_S", 20.0),
            rollout_timeout_s=_env_float("INTERVENTION_ROLLOUT_TIMEOUT_S", _env_float("EVAL_TRIAL_TIMEOUT_S", 120.0)),
            timeout_grace_s=_env_float("INTERVENTION_TIMEOUT_GRACE_S", 30.0),
            watchdog_s=_env_float("INTERVENTION_WATCHDOG_S", 2.0),
            collect=os.environ.get("INTERVENTION_COLLECT", "1") != "0",
            collect_max_rollouts=int(_env_float("INTERVENTION_COLLECT_MAX_ROLLOUTS", 40)),
            robot_stale_s=_env_float("EVAL_ROBOT_STALE_S", 30.0),
        )


@dataclass
class Intervention:
    ref: str
    ticket_id: int
    robot_id: int
    command_id: int
    trial_index: int
    eval_run_id: int
    purpose: str  # eval | baseline | collect
    detector: str
    reason: str
    task_id: int
    route: str  # claimed | open | created
    started_us: int
    claim_deadline_us: int
    price_cents: int = 0
    data_plan_id: int = 0
    phase: str = "awaiting_claim"  # awaiting_claim | taken_over | handed_back | closed
    contributor_id: int = 0
    claimed_us: int = 0
    handback_us: int = 0
    handback_by: str = ""
    episode_id: int = 0
    review_status: str = ""
    outcome: str = ""
    closed_us: int = 0
    timeouts: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return self.phase in LIVE

    def to_dict(self) -> dict:
        d = asdict(self)
        d["claim_wait_s"] = round((self.claimed_us - self.started_us) / 1e6, 3) if self.claimed_us else None
        d["takeover_s"] = (round((self.handback_us - self.claimed_us) / 1e6, 3)
                           if self.claimed_us and self.handback_us else None)
        return d


# --------------------------------------------------------------------------- settings

def robot_capable(robot: dict | None) -> bool:
    caps = _json((robot or {}).get("camera_config_json")).get("capabilities") or []
    return isinstance(caps, list) and CAPABILITY in caps


def explicit_setting(mirror: Mirror, ticket_id: int) -> bool | None:
    rs = mirror.get("run_settings", ticket_id) or {}
    v = _json(rs.get("hyperparams_json")).get("interventions_enabled")
    return v if isinstance(v, bool) else None


def current_recovery_plan(mirror: Mirror, ticket: dict) -> dict | None:
    it = _i(ticket.get("iteration"))
    plans = [p for p in mirror.rows("data_plan") if _i(p.get("ticket_id")) == _i(ticket["id"])
             and _i(p.get("iteration")) == it and p.get("data_type") == TASK_TYPE
             and p.get("status") in dsp.DISPATCHABLE_PLAN]
    return max(plans, key=lambda p: _i(p["id"])) if plans else None


def enabled_for(mirror: Mirror, ticket_id: int, purpose: str) -> tuple[bool, str]:
    """(on?, why). purpose: collect | eval | baseline."""
    ticket = mirror.get("ticket", ticket_id)
    if ticket is None:
        return False, "ticket not found"
    explicit = explicit_setting(mirror, ticket_id)
    if purpose == "collect":
        robot = mirror.get("robot", _i(ticket.get("robot_id")))
        if not robot_capable(robot):
            return False, (f"robot {_i(ticket.get('robot_id'))} does not advertise the '{CAPABILITY}' capability "
                           "(camera_config_json.capabilities)")
        if explicit is False:
            return False, "run_settings.hyperparams_json.interventions_enabled = false"
        if ticket.get("status") != "dispatch":
            return False, f"ticket is {ticket.get('status')}, not dispatch (collecting)"
        if current_recovery_plan(mirror, ticket) is None:
            return False, "no approved recovery_takeover plan for this iteration"
        return True, "collecting a recovery_takeover plan" + (" (explicitly enabled)" if explicit else " (default on)")
    if explicit is True:
        return True, f"{purpose} rollouts: run_settings.hyperparams_json.interventions_enabled = true"
    return False, f"{purpose} rollouts: interventions are off by default so eval numbers stay clean"


# --------------------------------------------------------------------------- manager

class InterventionManager:
    """One per bus. Reads the mirror, writes with a sync reducer client (raises on refusal)."""

    def __init__(self, mirror: Mirror, writer_factory: Callable[[], Writer], cfg: InterventionConfig | None = None,
                 *, clock: Callable[[], float] = time.time, mirror_wait_s: float = 5.0,
                 dispatcher: dsp.Dispatcher | None = None) -> None:
        self.m = mirror
        self.cfg = cfg or InterventionConfig()
        self.clock = clock
        self.mirror_wait_s = mirror_wait_s
        self._writer_factory = writer_factory
        self._writer: Writer | None = None
        self._wlock = threading.Lock()
        self._lock = threading.RLock()  # one intervention decision at a time (one arm, one operator)
        self._ivs: dict[str, Intervention] = {}
        self._declined: set[str] = set()
        self._collect_reset: set[int] = set()  # collection commands whose reset was requested
        self._loaded = False
        self._last_wait: dict[int, str] = {}
        self._dispatcher = dispatcher
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------------ plumbing
    @property
    def w(self) -> Writer:
        with self._wlock:
            if self._writer is None:
                self._writer = self._writer_factory()
            return self._writer

    @property
    def d(self) -> dsp.Dispatcher:
        if self._dispatcher is None:
            self._dispatcher = dsp.get_dispatcher(self.m)
        return self._dispatcher

    def now_us(self) -> int:
        return int(self.clock() * 1_000_000)

    def event(self, type_: str, payload: dict) -> None:
        log.info(type_, extra={"fields": payload})
        try:
            self.w.call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str))
        except Exception as e:  # noqa: BLE001 - the log line above is the fallback record
            log.warning("intervention.event_failed", extra={"fields": {"type": type_, "error": str(e)[:300]}})

    def _call(self, reducer: str, *args: Any, iv: Intervention | None = None) -> bool:
        try:
            self.w.call_reducer(reducer, *args)
            return True
        except Exception as e:  # noqa: BLE001
            self.event("intervention.error", {"ref": iv.ref if iv else None, "ticket_id": iv.ticket_id if iv else None,
                                              "op": reducer, "error": str(e)[:500]})
            return False

    def _wait_for(self, pred: Callable[[], Any]) -> Any:
        deadline = time.time() + self.mirror_wait_s
        while True:
            got = pred()
            if got or time.time() >= deadline:
                return got
            time.sleep(0.05)

    def _command(self, robot_id: int, type_: str, payload: dict, ticket_id: int, iv: Intervention | None = None) -> bool:
        return self._call("issue_robot_command", robot_id, type_, json.dumps(payload, default=str), ticket_id, iv=iv)

    # ------------------------------------------------------------------ trail replay (restart safety)
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for e in sorted(self.m.rows("event_log"), key=lambda r: _i(r["id"])):
            t = e.get("type") or ""
            if not t.startswith("intervention.") or e.get("actor") != ACTOR:
                continue
            p = _json(e.get("payload_json"))
            ref = p.get("ref")
            if not ref:
                continue
            if t == "intervention.declined":
                self._declined.add(ref)
                continue
            if t == "intervention.started":
                fields = {k: p.get(k) for k in Intervention.__dataclass_fields__ if k in p}
                try:
                    self._ivs[ref] = Intervention(**fields)
                except TypeError:
                    continue
                continue
            iv = self._ivs.get(ref)
            if iv is None:
                continue
            if t == "intervention.claimed":
                iv.phase, iv.contributor_id, iv.claimed_us = "taken_over", _i(p.get("contributor_id")), _i(p.get("at_us"))
            elif t == "intervention.claim_lapsed":
                iv.phase, iv.claim_deadline_us = "awaiting_claim", _i(p.get("claim_deadline_us"))
            elif t == "intervention.handback":
                iv.phase, iv.handback_us, iv.handback_by = "handed_back", _i(p.get("at_us")), p.get("by") or ""
            elif t == "intervention.timeout":
                iv.timeouts.append(p.get("stage") or "")
            elif t == "intervention.closed":
                iv.phase, iv.outcome, iv.closed_us = "closed", p.get("outcome") or "", _i(p.get("at_us"))
            elif t == "intervention.episode_recorded":
                iv.episode_id = _i(p.get("episode_id"))
            elif t == "intervention.reviewed":
                iv.review_status = p.get("review_status") or ""
        for e in self.m.rows("robot_command"):
            if e.get("type") == "reset_request":
                ref = _json(e.get("payload_json")).get("ref") or ""
                if ref.startswith("collect:"):
                    self._collect_reset.add(_i(ref.split(":")[1]))
        for t in self.m.rows("task"):
            ref = task_ref(t) or ""
            if t.get("type") == "reset" and ref.startswith("collect:"):
                self._collect_reset.add(_i(ref.split(":")[1]))

    def interventions(self, ticket_id: int | None = None) -> list[Intervention]:
        with self._lock:
            self._load()
            out = [iv for iv in self._ivs.values() if ticket_id is None or iv.ticket_id == ticket_id]
        return sorted(out, key=lambda iv: iv.started_us)

    def live_on_robot(self, robot_id: int) -> Intervention | None:
        with self._lock:
            self._load()
            return next((iv for iv in self._ivs.values() if iv.robot_id == robot_id and iv.live), None)

    def by_task(self, task_id: int, *, live_only: bool = False) -> Intervention | None:
        with self._lock:
            self._load()
            ivs = [iv for iv in self._ivs.values() if iv.task_id == task_id and (iv.live or not live_only)]
            return max(ivs, key=lambda iv: iv.started_us) if ivs else None

    # ------------------------------------------------------------------ reads
    def _open_rollout(self, robot_id: int, command_id: int = 0) -> dict | None:
        cmds = [c for c in self.m.rows("robot_command") if _i(c.get("robot_id")) == robot_id
                and c.get("type") in ROLLOUT_COMMANDS and c.get("status") in OPEN_JOB
                and (not command_id or _i(c["id"]) == command_id)]
        return max(cmds, key=lambda c: _i(c["id"])) if cmds else None

    @staticmethod
    def _purpose(cmd: dict) -> str:
        p = _json(cmd.get("payload_json"))
        if p.get("purpose") == COLLECT_PURPOSE:
            return "collect"
        return "baseline" if (p.get("kind") == "baseline" or cmd.get("type") == "run_baseline") else "eval"

    def _robot_problem(self, robot: dict | None) -> str | None:
        if robot is None:
            return "robot not found"
        if robot.get("status") not in USABLE_ROBOT:
            return f"robot {robot.get('id')} is {robot.get('status')}"
        hb = _i(robot.get("last_heartbeat"))
        if not hb or (self.now_us() - hb) / 1e6 > self.cfg.robot_stale_s:
            return f"robot {robot.get('id')} heartbeat is stale"
        return None

    def _task_has_episode(self, task_id: int) -> bool:
        return any(_i(e.get("task_id")) == task_id for e in self.m.rows("episode"))

    def _bound_task_ids(self) -> set[int]:
        return {iv.task_id for iv in self._ivs.values() if iv.live or iv.outcome == "recovered"}

    def _lease_live(self, t: dict) -> bool:
        exp = _i(t.get("lease_expires_at"))
        return t.get("status") == "claimed" and (not exp or exp > self.now_us())

    def _route(self, ticket: dict, robot_id: int) -> tuple[dict | None, str]:
        """An existing recovery_takeover task to route this intervention to: (task, claimed|open)."""
        bound = self._bound_task_ids()
        tid = _i(ticket["id"])
        cands = [t for t in self.m.rows("task") if t.get("type") == TASK_TYPE and _i(t["id"]) not in bound
                 and not self._task_has_episode(_i(t["id"]))]
        claimed = [t for t in cands if _i(t.get("robot_id")) == robot_id and self._lease_live(t)]
        if claimed:
            return min(claimed, key=lambda t: _i(t["id"])), "claimed"
        plan = current_recovery_plan(self.m, ticket)
        open_ = [t for t in cands if _i(t.get("ticket_id")) == tid and t.get("status") == "open"
                 and _i(t.get("robot_id")) in (0, robot_id)]
        if open_:
            # this iteration's plan tasks first, then the oldest
            return min(open_, key=lambda t: (0 if plan and _i(t.get("data_plan_id")) == _i(plan["id"]) else 1,
                                             _i(t["id"]))), "open"
        return None, ""

    # ------------------------------------------------------------------ payload for the robot
    def robot_payload(self) -> dict:
        """The ``interventions`` key of a run_eval / run_baseline payload (contract: robot-client
        ``rdf_robot_client.interventions``)."""
        return {"enabled": True, "contract": "TDM-69", "event": FAILURE_EVENT,
                "detectors": ["timeout", "no_progress", "success_check", "operator"],
                "trial_timeout_s": self.cfg.rollout_timeout_s, "no_progress_s": self.cfg.no_progress_s,
                "verdict_wait_s": self.cfg.verdict_wait_s}

    # ------------------------------------------------------------------ failure signal -> takeover
    def on_signal(self, ev_row: dict) -> str:
        """An ``intervention.failure_detected`` (robot) or ``intervention.flagged`` (operator) event row."""
        p = _json(ev_row.get("payload_json"))
        if ev_row.get("type") == HANDBACK_EVENT:
            with self._lock:
                self._load()
                iv = self.live_on_robot(_i(p.get("robot_id")))
                if iv is None or iv.phase not in ("taken_over", "awaiting_claim"):
                    return "no_live_takeover"
                self._handback(iv, by=f"robot_event:{ev_row.get('actor') or ''}")
                return "handback"
        detector = p.get("detector") or ("operator" if ev_row.get("type") == FLAG_EVENT else "")
        return self.start(_i(p.get("robot_id")), detector=detector, reason=str(p.get("reason") or p.get("note") or ""),
                          command_id=_i(p.get("command_id")), trial_index=p.get("trial_index"),
                          source_event_id=_i(ev_row.get("id")), signal_actor=ev_row.get("actor") or "")

    def start(self, robot_id: int, *, detector: str, reason: str = "", command_id: int = 0,
              trial_index: Any = None, source_event_id: int = 0, signal_actor: str = "") -> str:
        """Failure signal -> (route or create a recovery_takeover task) -> pause_for_takeover. Returns
        the outcome: started | duplicate | declined:<why>."""
        with self._lock:
            self._load()
            detector = detector if detector in DETECTORS else (detector or "unknown")
            cmd = self._open_rollout(robot_id, command_id)
            if cmd is None and command_id:
                cmd = self._open_rollout(robot_id)
                if cmd is not None and _i(cmd["id"]) != command_id:
                    cmd = None
            robot = self.m.get("robot", robot_id)
            ticket_id = _i(cmd.get("ticket_id")) if cmd else 0
            if not ticket_id and robot is not None:  # operator flag with no rollout: say so with a ticket
                tks = [t for t in self.m.rows("ticket") if _i(t.get("robot_id")) == robot_id
                       and t.get("status") not in dsp.TERMINAL_TICKET]
                ticket_id = max((_i(t["id"]) for t in tks), default=0)
            payload = _json(cmd.get("payload_json")) if cmd else {}
            run_id = _i(payload.get("eval_run_id"))
            if trial_index is None:
                trial_index = len({_i(t.get("trial_index")) for t in self.m.rows("eval_trial")
                                   if _i(t.get("eval_run_id")) == run_id}) if run_id else 0
            trial_index = _i(trial_index)
            ref = f"iv:{robot_id}:{_i(cmd['id']) if cmd else 0}:{trial_index}"
            base = {"ref": ref, "ticket_id": ticket_id, "robot_id": robot_id, "command_id": _i(cmd["id"]) if cmd else 0,
                    "trial_index": trial_index, "eval_run_id": run_id, "detector": detector, "reason": reason[:500],
                    "source_event_id": source_event_id, "signal_actor": signal_actor}
            if ref in self._ivs:
                log.info("intervention.duplicate_signal", extra={"fields": base})
                return "duplicate"
            if ref in self._declined:
                return "duplicate"

            def decline(why: str, *, verdict: bool) -> str:
                self._declined.add(ref)
                self.event("intervention.declined", {**base, "why": why, "verdict_sent": verdict})
                if verdict and cmd is not None:  # the robot holds waiting for us: tell it to end the trial
                    self._command(robot_id, "resume_policy", {"end_trial": True, "outcome": "failure", "ref": ref,
                                                              "trial_index": trial_index, "reason": f"declined: {why}"},
                                  ticket_id)
                return f"declined:{why}"

            if cmd is None:
                return decline("no open policy rollout on this robot (nothing to pause)", verdict=False)
            purpose = self._purpose(cmd)
            robot_waiting = bool((payload.get("interventions") or {}).get("enabled")) and detector != "operator"
            on, why = enabled_for(self.m, ticket_id, purpose)
            if not on:
                return decline(f"interventions disabled: {why}", verdict=robot_waiting)
            live = self.live_on_robot(robot_id)
            if live is not None:
                return decline(f"intervention {live.ref} is already live on this robot", verdict=robot_waiting)
            problem = self._robot_problem(robot)
            if problem:
                return decline(problem, verdict=False)
            ticket = self.m.get("ticket", ticket_id) or {}

            # ---- route the takeover to a claimant
            task, route = self._route(ticket, robot_id)
            price = dsp.PRICES_CENTS[TASK_TYPE]
            plan = current_recovery_plan(self.m, ticket)
            if task is None:
                price = _i((plan or {}).get("unit_price_cents")) or price
                remaining = self.d.budget_remaining(ticket_id)
                if remaining < price:
                    return decline(f"takeover costs ${price / 100:.2f} but only ${max(remaining, 0) / 100:.2f} of "
                                   "ticket budget is left", verdict=robot_waiting)
                desc = (ticket.get("task_description") or "the task").strip()
                instructions = (f"LIVE TAKEOVER: the robot is paused mid-attempt at: {desc}. It failed "
                                f"({detector}: {reason[:160] or 'no detail'}). Take over now, recover and finish the "
                                f"task, then hand control back. Pays ${price / 100:.2f} once the reviewer accepts the "
                                f"episode. [ref {ref}]")
                tpayload = {"instructions": instructions, "ticket_id": ticket_id, "robot_id": robot_id,
                            "source": "interventions", "ref": ref}
                plan_id = 0
                if plan is not None:  # count it toward the plan, only while the approval can pay for it
                    st = self.d.plan_status(_i(plan["id"]))
                    if st.state == "collecting" and st.accepted + st.in_flight < min(st.target, st.payable):
                        plan_id = _i(plan["id"])
                if plan_id:
                    tpayload["data_plan_id"] = plan_id
                if not self._call("create_task", ticket_id, TASK_TYPE, price, json.dumps(tpayload)):
                    return decline("create_task refused (see intervention.error)", verdict=robot_waiting)
                task = self._wait_for(lambda: next((t for t in self.m.rows("task") if t.get("type") == TASK_TYPE
                                                    and task_ref(t) == ref), None))
                if task is None:
                    return decline("created task did not appear in the mirror", verdict=robot_waiting)
                route = "created"
            else:
                price = _i(task.get("price_cents")) or price
            task_id = _i(task["id"])

            # ---- pause the robot (task exists first: the dispatcher's paused->reset handler skips it)
            now = self.now_us()
            iv = Intervention(ref=ref, ticket_id=ticket_id, robot_id=robot_id, command_id=_i(cmd["id"]),
                              trial_index=trial_index, eval_run_id=run_id, purpose=purpose, detector=detector,
                              reason=reason[:500], task_id=task_id, route=route, started_us=now,
                              claim_deadline_us=now + int(self.cfg.claim_timeout_s * 1e6), price_cents=price,
                              data_plan_id=_i(task.get("data_plan_id")))
            if route == "claimed":
                iv.phase, iv.contributor_id, iv.claimed_us = "taken_over", _i(task.get("assignee_id")), now
            self._ivs[ref] = iv
            paused = self._command(robot_id, "pause_for_takeover",
                                   {"ref": ref, "task_id": task_id, "trial_index": trial_index, "reason": reason[:300],
                                    "detector": detector, "eval_run_id": run_id, "source": "interventions"},
                                   ticket_id, iv=iv)
            self.event("intervention.started", {**{k: v for k, v in asdict(iv).items() if k != "timeouts"},
                                                "source_event_id": source_event_id, "signal_actor": signal_actor,
                                                "pause_issued": paused, "enabled_why": why,
                                                "claim_timeout_s": self.cfg.claim_timeout_s})
            if route == "claimed":
                self.event("intervention.claimed", {"ref": ref, "ticket_id": ticket_id, "task_id": task_id,
                                                    "contributor_id": iv.contributor_id, "at_us": now,
                                                    "claim_wait_s": 0.0, "note": "claimant was already waiting"})
            else:
                self.d.page(ticket_id, [task_id], TASK_TYPE, price)
            return "started"

    # ------------------------------------------------------------------ claim / handback / episode
    def on_task(self, task: dict, old: dict | None = None) -> str | None:
        if task.get("type") != TASK_TYPE:
            return None
        with self._lock:
            self._load()
            iv = self.by_task(_i(task["id"]), live_only=True)
            if iv is None:
                return None
            st, was = task.get("status"), (old or {}).get("status")
            now = self.now_us()
            base = {"ref": iv.ref, "ticket_id": iv.ticket_id, "robot_id": iv.robot_id, "task_id": iv.task_id}
            if st == "claimed" and iv.phase == "awaiting_claim":
                iv.phase, iv.contributor_id, iv.claimed_us = "taken_over", _i(task.get("assignee_id")), now
                self.event("intervention.claimed", {**base, "contributor_id": iv.contributor_id, "at_us": now,
                                                    "claim_wait_s": round((now - iv.started_us) / 1e6, 3)})
                return "claimed"
            if st == "open" and was == "claimed" and iv.phase == "taken_over":
                iv.phase = "awaiting_claim"
                iv.claim_deadline_us = now + int(self.cfg.claim_timeout_s * 1e6)
                self.event("intervention.claim_lapsed", {**base, "contributor_id": iv.contributor_id, "at_us": now,
                                                         "claim_deadline_us": iv.claim_deadline_us,
                                                         "note": "lease lapsed or released; re-paged"})
                self.d.page(iv.ticket_id, [iv.task_id], TASK_TYPE, iv.price_cents)
                return "claim_lapsed"
            if st == "completed" and iv.phase == "taken_over":
                # the claimant tapped done without a handback command: hand back for them
                sent = self._command(iv.robot_id, "handback", {"ref": iv.ref, "task_id": iv.task_id,
                                                              "reason": "task_completed", "by": ACTOR}, iv.ticket_id, iv=iv)
                self._handback(iv, by="orchestrator:task_completed", command_sent=sent)
                return "handback_sent"
            if st in ("cancelled", "expired") and iv.live:
                if iv.phase != "handed_back":
                    self._end_trial(iv, f"task {st}")
                self._close(iv, f"task_{st}")
                return f"task_{st}"
        return None

    def _handback(self, iv: Intervention, *, by: str, command_id: int = 0, command_sent: bool | None = None) -> None:
        now = self.now_us()
        iv.phase, iv.handback_us, iv.handback_by = "handed_back", now, by
        self.event("intervention.handback", {"ref": iv.ref, "ticket_id": iv.ticket_id, "robot_id": iv.robot_id,
                                             "task_id": iv.task_id, "contributor_id": iv.contributor_id, "at_us": now,
                                             "by": by, "command_id": command_id, "command_sent": command_sent,
                                             "takeover_s": round((now - iv.claimed_us) / 1e6, 3) if iv.claimed_us else None,
                                             "note": "control back to the policy; the trial finishes and the recorder "
                                                     "writes the recovery_takeover episode"})

    def on_command(self, cmd: dict, old: dict | None = None) -> str | None:
        st, was = cmd.get("status"), (old or {}).get("status")
        if st == was:
            return None
        rid = _i(cmd.get("robot_id"))
        out = None
        with self._lock:
            self._load()
            iv = self.live_on_robot(rid)
            if iv is not None and cmd.get("type") == "handback" and st == "done" and iv.phase in ("taken_over", "awaiting_claim"):
                self._handback(iv, by=cmd.get("issued_by") or "claimant", command_id=_i(cmd["id"]))
                out = "handback"
            if iv is not None and cmd.get("type") == "pause_for_takeover" and st == "failed" \
                    and _json(cmd.get("payload_json")).get("ref") == iv.ref:
                self.event("intervention.pause_refused", {"ref": iv.ref, "ticket_id": iv.ticket_id, "robot_id": rid,
                                                          "command_id": cmd["id"], "result": cmd.get("result_json")})
            if iv is not None and _i(cmd["id"]) == iv.command_id and st not in OPEN_JOB:
                self._rollout_ended(iv, cmd)
                out = "closed"
        if cmd.get("type") in ROLLOUT_COMMANDS and st not in OPEN_JOB and self._purpose(cmd) == "collect":
            if st == "done":
                self._reset_after_collect(cmd)
            else:  # failed / preempted: no automatic reset (a human looks at the arm), see COLLECT_FAIL_STRIKES
                self.event("intervention.collect_rollout_ended", {"ticket_id": _i(cmd.get("ticket_id")),
                                                                  "robot_id": rid, "command_id": cmd["id"],
                                                                  "status": st, "result": cmd.get("result_json")})
        if cmd.get("type") in JOB_TYPES and was in OPEN_JOB and st not in OPEN_JOB:
            tid = _i(cmd.get("ticket_id"))
            if tid:
                self.ensure_collection(tid, why=f"job command {cmd['id']} {st}")
        return out

    def _rollout_ended(self, iv: Intervention, cmd: dict) -> None:
        if iv.phase == "handed_back":
            outcome = "recovered"
        elif iv.phase == "taken_over":
            outcome = "rollout_ended_during_takeover"
        else:
            outcome = "rollout_ended_unclaimed"
        task = self.m.get("task", iv.task_id) or {}
        if iv.phase in ("handed_back", "taken_over") and self._lease_live(task):
            # the claimant handed back but has not tapped done: complete for them (clears the lease)
            self._call("complete_task", iv.task_id, json.dumps({"by": ACTOR, "ref": iv.ref, "reason": "handed_back",
                                                                "rollout_command_id": cmd["id"]}), iv=iv)
        elif iv.phase == "awaiting_claim" and iv.route == "created" and task.get("status") == "open":
            self._call("cancel_task", iv.task_id, f"intervention {iv.ref}: rollout ended before anyone claimed", iv=iv)
        self._close(iv, outcome, extra={"rollout_command_id": cmd["id"], "rollout_status": cmd.get("status")})

    def _close(self, iv: Intervention, outcome: str, extra: dict | None = None) -> None:
        if iv.phase == "closed":
            return
        now = self.now_us()
        iv.phase, iv.outcome, iv.closed_us = "closed", outcome, now
        self.event("intervention.closed", {"ref": iv.ref, "ticket_id": iv.ticket_id, "robot_id": iv.robot_id,
                                           "task_id": iv.task_id, "contributor_id": iv.contributor_id,
                                           "outcome": outcome, "at_us": now, "purpose": iv.purpose,
                                           "detector": iv.detector, "timeouts": iv.timeouts,
                                           "duration_s": round((now - iv.started_us) / 1e6, 3), **(extra or {})})

    def _end_trial(self, iv: Intervention, reason: str) -> bool:
        """Tell the paused robot to end this trial as a failure (it then resets and goes on)."""
        return self._command(iv.robot_id, "resume_policy", {"end_trial": True, "outcome": "failure", "ref": iv.ref,
                                                            "trial_index": iv.trial_index, "task_id": iv.task_id,
                                                            "reason": reason}, iv.ticket_id, iv=iv)

    def on_episode(self, ep: dict, old: dict | None = None) -> str | None:
        task_id = _i(ep.get("task_id"))
        if not task_id:
            return None
        with self._lock:
            self._load()
            iv = self.by_task(task_id)
            if iv is None:
                return None
            base = {"ref": iv.ref, "ticket_id": iv.ticket_id, "task_id": task_id, "episode_id": _i(ep["id"])}
            out = None
            if iv.episode_id != _i(ep["id"]):
                iv.episode_id = _i(ep["id"])
                self.event("intervention.episode_recorded", {
                    **base, "kind": ep.get("kind"), "contributor_id": _i(ep.get("contributor_id")),
                    "takeover_ts": ep.get("takeover_ts"), "handback_ts": ep.get("handback_ts"),
                    "duration_ms": _i(ep.get("duration_ms")), "success": ep.get("success"),
                    "review_status": ep.get("review_status"), "storage_uri": ep.get("storage_uri")})
                out = "recorded"
            rs = ep.get("review_status")
            if rs in ("accepted", "rejected") and iv.review_status != rs:
                iv.review_status = rs
                review = next((r for r in self.m.rows("review") if _i(r.get("episode_id")) == _i(ep["id"])), None) \
                    if hasattr(self.m, "rows") else None
                self.event("intervention.reviewed", {**base, "review_status": rs,
                                                     "reason": (review or {}).get("reason")})
                out = "reviewed"
            return out

    # ------------------------------------------------------------------ watchdog: timeouts
    def tick(self) -> None:
        now = self.now_us()
        with self._lock:
            self._load()
            for iv in [iv for iv in self._ivs.values() if iv.live]:
                base = {"ref": iv.ref, "ticket_id": iv.ticket_id, "robot_id": iv.robot_id, "task_id": iv.task_id}
                robot = self.m.get("robot", iv.robot_id)
                if robot is not None and robot.get("status") in ("estop", "offline"):
                    task = self.m.get("task", iv.task_id) or {}
                    if iv.route == "created" and task.get("status") == "open":
                        self._call("cancel_task", iv.task_id, f"intervention {iv.ref}: robot {robot.get('status')}", iv=iv)
                    self._close(iv, f"robot_{robot.get('status')}")
                    continue
                cmd = self.m.get("robot_command", iv.command_id)
                if cmd is not None and cmd.get("status") not in OPEN_JOB:
                    self._rollout_ended(iv, cmd)  # missed the update (restart): same as on_command
                    continue
                if iv.phase == "awaiting_claim" and now > iv.claim_deadline_us:
                    waited = round((now - iv.started_us) / 1e6, 1)
                    iv.timeouts.append("claim")
                    self.event("intervention.timeout", {**base, "stage": "claim", "waited_s": waited,
                                                        "claim_timeout_s": self.cfg.claim_timeout_s,
                                                        "fallback": "end the trial as a failure, reset, next trial"})
                    self._end_trial(iv, f"nobody claimed the takeover within {self.cfg.claim_timeout_s:.0f}s")
                    task = self.m.get("task", iv.task_id) or {}
                    if iv.route == "created" and task.get("status") == "open":
                        self._call("cancel_task", iv.task_id, f"intervention {iv.ref}: unclaimed after {waited}s", iv=iv)
                    self._close(iv, "unclaimed_timeout")
                elif iv.phase == "taken_over" and iv.claimed_us and now - iv.claimed_us > self.cfg.takeover_max_s * 1e6:
                    iv.timeouts.append("takeover")
                    self.event("intervention.timeout", {**base, "stage": "takeover",
                                                        "takeover_s": round((now - iv.claimed_us) / 1e6, 1),
                                                        "takeover_max_s": self.cfg.takeover_max_s,
                                                        "fallback": "handback to the policy"})
                    sent = self._command(iv.robot_id, "handback", {"ref": iv.ref, "task_id": iv.task_id, "by": ACTOR,
                                                                  "reason": "takeover_overrun"}, iv.ticket_id, iv=iv)
                    self._handback(iv, by="orchestrator:takeover_timeout", command_sent=sent)
            self._orchestrator_timeouts(now)
        for t in list(self.m.rows("ticket")):
            if t.get("status") == "dispatch":
                try:
                    self.ensure_collection(_i(t["id"]), why="watchdog")
                except Exception:  # noqa: BLE001
                    log.exception("intervention.collect_error", extra={"fields": {"ticket_id": t.get("id")}})

    def _orchestrator_timeouts(self, now: int) -> None:
        """A collection rollout that ran past its timeout without any failure signal: flag it ourselves."""
        for c in self.m.rows("robot_command"):
            if c.get("type") not in ROLLOUT_COMMANDS or c.get("status") != "acked" or self._purpose(c) != "collect":
                continue
            p = _json(c.get("payload_json"))
            limit = float(p.get("timeout_s") or self.cfg.rollout_timeout_s) + self.cfg.timeout_grace_s
            acked = _i(c.get("acked_at")) or _i(c.get("issued_at"))
            if acked and (now - acked) / 1e6 > limit and self.live_on_robot(_i(c.get("robot_id"))) is None:
                ref = f"iv:{_i(c.get('robot_id'))}:{_i(c['id'])}:0"
                if ref not in self._ivs and ref not in self._declined:
                    self.start(_i(c.get("robot_id")), detector="orchestrator_timeout", command_id=_i(c["id"]),
                               trial_index=0, reason=f"collection rollout ran {limit:.0f}s with no failure signal")

    # ------------------------------------------------------------------ collection rollouts
    def _plan_collected(self, plan: dict) -> int:
        """Recovery episodes of this plan accepted or waiting for review."""
        task_ids = {_i(t["id"]) for t in self.m.rows("task") if _i(t.get("data_plan_id")) == _i(plan["id"])}
        return sum(1 for e in self.m.rows("episode") if _i(e.get("task_id")) in task_ids
                   and e.get("kind") == TASK_TYPE and e.get("review_status") in ("accepted", "pending"))

    def _wait(self, ticket_id: int, outcome: str, detail: str) -> str:
        if self._last_wait.get(ticket_id) != detail:
            self._last_wait[ticket_id] = detail
            log.info("intervention.collect_wait", extra={"fields": {"ticket_id": ticket_id, "outcome": outcome,
                                                                   "detail": detail}})
        return outcome

    def collection_checkpoint(self, ticket_id: int) -> str:
        runs = [r for r in self.m.rows("training_run") if _i(r.get("ticket_id")) == ticket_id
                and r.get("status") == "done" and (r.get("checkpoint_uri") or "").strip()]
        if runs:
            return max(runs, key=lambda r: _i(r["id"]))["checkpoint_uri"].strip()
        from . import flywheel as _fw

        return getattr(_fw, "base_checkpoint_uri", lambda: _fw.PI05_BASE_CHECKPOINT)()

    def ensure_collection(self, ticket_id: int, why: str = "") -> str:
        """While a recovery_takeover plan is collecting: keep one policy rollout with interventions
        running on the ticket's robot (``run_baseline`` purpose=collect_recovery, no eval_run, no
        eval_trial rows), until the plan's target is covered. Idempotent."""
        if not self.cfg.collect:
            return "collect_off"
        ticket = self.m.get("ticket", ticket_id)
        if ticket is None or ticket.get("status") != "dispatch":
            return "not_collecting"
        on, reason = enabled_for(self.m, ticket_id, "collect")
        if not on:
            return self._wait(ticket_id, "disabled", reason)
        with self._lock:
            self._load()
            plan = current_recovery_plan(self.m, ticket)
            unit = _i(plan.get("unit_price_cents")) or dsp.PRICES_CENTS[TASK_TYPE]
            target = min(_i(plan.get("amount")), _i(plan.get("approved_cents")) // unit if unit else 0)
            have = self._plan_collected(plan)
            if target <= 0 or have >= target:
                return self._wait(ticket_id, "target_covered", f"plan {plan['id']}: {have}/{target} recovery episodes "
                                                               "accepted or in review")
            rid = _i(ticket.get("robot_id"))
            rollouts = [c for c in self.m.rows("robot_command") if c.get("type") in ROLLOUT_COMMANDS
                        and self._purpose(c) == "collect" and _i(_json(c.get("payload_json")).get("data_plan_id")) == _i(plan["id"])]
            if len(rollouts) >= self.cfg.collect_max_rollouts:
                return self._wait(ticket_id, "max_rollouts", f"plan {plan['id']}: {len(rollouts)} collection rollouts "
                                                             f"(INTERVENTION_COLLECT_MAX_ROLLOUTS)")
            robot = self.m.get("robot", rid)
            problem = self._robot_problem(robot)
            if problem or (robot or {}).get("status") == "paused":
                return self._wait(ticket_id, "waiting_for_robot", problem or f"robot {rid} is paused")
            jobs = [c for c in self.m.rows("robot_command") if _i(c.get("robot_id")) == rid
                    and c.get("status") in OPEN_JOB and c.get("type") in JOB_TYPES]
            if jobs:
                return self._wait(ticket_id, "robot_busy", ", ".join(f"{c['id']}:{c.get('type')}" for c in jobs))
            if self.live_on_robot(rid) is not None:
                return self._wait(ticket_id, "intervention_live", "an intervention is live on the robot")
            resets = [t for t in self.m.rows("task") if t.get("type") == "reset" and _i(t.get("robot_id")) == rid
                      and t.get("status") in ("open", "claimed")]
            if resets:
                return self._wait(ticket_id, "waiting_for_reset", f"reset task {resets[0]['id']} is {resets[0].get('status')}")
            ended = sorted((c for c in rollouts if c.get("status") not in OPEN_JOB), key=lambda c: _i(c["id"]))
            last = ended[-COLLECT_FAIL_STRIKES:]
            if len(last) == COLLECT_FAIL_STRIKES and all(c.get("status") != "done" for c in last):
                return self._wait(ticket_id, "rollouts_failing",
                                  f"the last {COLLECT_FAIL_STRIKES} collection rollouts ended "
                                  + ", ".join(f"{c['id']}:{c.get('status')}" for c in last) + "; check the robot")
            unreset = [c for c in ended if c.get("status") == "done" and _i(c["id"]) not in self._collect_reset]
            if unreset:  # the last rollout ended but no reset was asked for yet (budget?) -> ask now
                for c in unreset:
                    self._reset_after_collect(c)
                return self._wait(ticket_id, "waiting_for_reset", "reset after the last rollout requested")
            if self.d.budget_remaining(ticket_id) < dsp.PRICES_CENTS["reset"]:
                return self._wait(ticket_id, "budget", "not enough budget left for the reset after a rollout")
            ckpt = self.collection_checkpoint(ticket_id)
            payload = {"purpose": COLLECT_PURPOSE, "eval_run_id": 0, "data_plan_id": _i(plan["id"]),
                       "checkpoint_uri": ckpt, "trials": 1, "timeout_s": self.cfg.rollout_timeout_s,
                       "kind": COLLECT_PURPOSE, "interventions": self.robot_payload(),
                       "note": "TDM-69 collection rollout: no eval_run, no eval_trial rows; a failure pauses "
                               "for a paid takeover, the recording becomes a recovery_takeover episode"}
            if not self._call("issue_robot_command", rid, "run_baseline", json.dumps(payload), ticket_id):
                return "command_refused"
            self.event("intervention.collect_rollout", {"ticket_id": ticket_id, "robot_id": rid,
                                                        "data_plan_id": _i(plan["id"]), "checkpoint_uri": ckpt,
                                                        "rollout_number": len(rollouts) + 1, "collected": have,
                                                        "target": target, "why": why, "enabled_why": reason})
            return "issued"

    def _reset_after_collect(self, cmd: dict) -> None:
        cid = _i(cmd["id"])
        with self._lock:
            self._load()
            if cid in self._collect_reset:
                return
            self._collect_reset.add(cid)
        res = self.d.request_reset(_i(cmd.get("robot_id")), _i(cmd.get("ticket_id")), reason="collect_rollout",
                                   ref=f"collect:{cid}", context={"rollout_command_id": cid})
        if res.task_id is None:
            with self._lock:
                self._collect_reset.discard(cid)  # retry on the next tick (e.g. budget came back)

    # ------------------------------------------------------------------ report
    def summary(self, ticket_id: int) -> dict:
        ivs = self.interventions(ticket_id)
        waits = sorted(iv.to_dict()["claim_wait_s"] for iv in ivs if iv.claimed_us)
        by_outcome: dict[str, int] = {}
        for iv in ivs:
            k = iv.outcome or iv.phase
            by_outcome[k] = by_outcome.get(k, 0) + 1
        return {
            "ticket_id": ticket_id,
            "count": len(ivs),
            "by_outcome": by_outcome,
            "by_detector": {d: sum(1 for iv in ivs if iv.detector == d) for d in sorted({iv.detector for iv in ivs})},
            "claimed": sum(1 for iv in ivs if iv.claimed_us),
            "timeouts": {s: sum(iv.timeouts.count(s) for iv in ivs) for s in ("claim", "takeover")},
            "episodes": [iv.episode_id for iv in ivs if iv.episode_id],
            "accepted_episodes": [iv.episode_id for iv in ivs if iv.review_status == "accepted"],
            "median_claim_wait_s": waits[len(waits) // 2] if waits else None,
            "interventions": [iv.to_dict() for iv in ivs],
        }

    # ------------------------------------------------------------------ watchdog thread
    def start_watchdog(self) -> None:
        if self.cfg.watchdog_s <= 0 or (self._watchdog and self._watchdog.is_alive()):
            return

        def loop() -> None:
            while not self._stop.wait(self.cfg.watchdog_s):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the tick must never die
                    log.exception("intervention.watchdog_error")

        self._watchdog = threading.Thread(target=loop, name="intervention-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------- eval hook (TDM-67)

def eval_payload(mirror: Mirror, ticket_id: int, kind: str) -> dict:
    """``{"interventions": {...}}`` for a run_eval payload when run_settings explicitly enables
    interventions for evals; ``{}`` otherwise (the default: eval numbers stay clean)."""
    on, _why = enabled_for(mirror, ticket_id, "baseline" if kind == "baseline" else "eval")
    return {"interventions": get_manager(mirror).robot_payload()} if on else {}


# --------------------------------------------------------------------------- per-bus singleton

def _default_writer() -> Writer:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)


make_writer: Callable[[], Writer] = _default_writer  # swappable for tests
make_config: Callable[[], InterventionConfig] = InterventionConfig.from_env
_instances: dict[int, InterventionManager] = {}
_instances_lock = threading.Lock()


def get_manager(bus: Mirror) -> InterventionManager:
    with _instances_lock:
        mgr = _instances.get(id(bus))
        if mgr is None or mgr.m is not bus:
            mgr = _instances[id(bus)] = InterventionManager(bus, lambda: make_writer(), make_config())
            mgr.start_watchdog()
        return mgr
