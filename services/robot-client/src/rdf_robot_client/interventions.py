"""TDM-69 robot side of DAgger-style interventions: failure detection during a policy rollout and
the command contract with the orchestrator (``rdf_orchestrator.interventions``).

Who uses it: the TDM-51 trial runner (``run_eval`` / ``run_baseline``) and the TDM-28 fake robot.
Interventions are on for a rollout only when its command payload carries
``"interventions": {"enabled": true, ...}`` (the orchestrator decides: on for recovery collection,
off for plain evals so eval numbers stay clean).

Contract, per trial::

    det = FailureDetector.from_payload(payload["interventions"])
    det.start(now_s)
    each control tick:  f = det.tick(now_s, progress)   # progress: any scalar that rises as the task
                                                        # advances (e.g. -distance(gripper, goal));
                                                        # None if you have no signal
    after a grasp:      f = det.success_check(ok, note) # explicit success check said no
    if f:               append_event(f.event_type, json.dumps(f.payload(robot_id, ticket_id,
                                                          command_id, trial_index, eval_run_id)))
                        then HOLD the arm (stop stepping the policy; the recorder keeps rolling) and
                        wait up to ``verdict_wait_s`` for one of these robot_command rows whose
                        payload ``ref`` == f.ref(robot_id, command_id, trial_index):
      * ``pause_for_takeover`` {ref, task_id}: state paused_for_takeover (robot.status ``paused``).
        A person claimed/was routed the recovery_takeover task ``task_id``. When human control starts
        call ``recorder.mark_takeover()``; ticks are ``control_source = human``.
      * ``handback`` {ref?} (or the claimant's ``{t:"handback"}`` on the TDM-52 control socket: then
        append ``intervention.handed_back`` {robot_id, task_id} so the orchestrator logs it):
        control back to the policy (TDM-49 maps it to resume -> rollout);
        ``recorder.mark_handback()``; the policy finishes the trial (success check / timeout).
        The trial's recording is written as kind ``recovery_takeover`` with ``task_id`` and the
        task's ``assignee_id`` as contributor (read the public ``task`` row). Its ``success`` is the
        claimant's answer at handback ({t:"handback", success} / payload.success); no answer = unknown,
        so the reviewer rejects it and nobody is paid (docs/decisions.md).
      * ``resume_policy`` {end_trial: true, outcome: "failure", ref}: nobody took over (claim
        timeout) or the orchestrator declined: end this trial as a FAILURE now, then the usual reset
        (eval: record_eval_trial + wait_for_reset; collection: the orchestrator asks for the reset).
        Ignore an end_trial whose ``ref`` is not the current pending failure.
      * nothing within ``verdict_wait_s``: end the trial as a failure yourself.
    An eval trial that was intervened on is recorded with ``success = false`` (the policy failed)
    and a note starting ``[intervened]``; the recovery episode is separate training data.

The operator "Failing" button does not go through this module: the web page calls the
orchestrator (``POST /robots/{id}/interventions/flag``), which issues ``pause_for_takeover``
directly while the policy is still running (TDM-49 allows it from ``rollout``).

Advertise support at ``register_robot``: ``camera_config_json.capabilities`` includes
``"interventions"``. Without it the orchestrator never starts collection rollouts on the robot.
``advertise(camera_config)`` adds it (main.py does, when the TDM-51 runner handles run_baseline and
``RDF_INTERVENTIONS`` is not 0/false/off).

Where it runs: the TDM-51 trial runner (``eval_trials.EvalTrialRunner``) runs the detector only in
COLLECTION rollouts (``run_baseline`` with ``purpose = "collect_recovery"``, no eval_run). Eval and
baseline runs never run it, so their numbers stay clean; an operator ``pause_for_takeover`` during
an eval trial still records that trial as an ``[intervened]`` failure. ``TrialIntervention`` below
is the per-trial bookkeeping the runner feeds from the job's control signals and the state machine.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

FAILURE_EVENT = "intervention.failure_detected"
HANDBACK_EVENT = "intervention.handed_back"
CAPABILITY = "interventions"
COLLECT_PURPOSE = "collect_recovery"  # rdf_orchestrator.interventions.COLLECT_PURPOSE
ENV_FLAG = "RDF_INTERVENTIONS"


def robot_enabled(env: dict[str, str] | None = None) -> bool:
    """``RDF_INTERVENTIONS`` (default on): 0 / false / off / no turns the capability off."""
    v = (env if env is not None else os.environ).get(ENV_FLAG, "").strip().lower()
    return v not in ("0", "false", "off", "no")


def advertise(camera_config: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    """Add (or remove) ``"interventions"`` in ``camera_config["capabilities"]`` in place; returns it."""
    caps = [c for c in (camera_config.get("capabilities") or []) if c != CAPABILITY]
    if enabled:
        caps.append(CAPABILITY)
    if caps:
        camera_config["capabilities"] = caps
    else:
        camera_config.pop("capabilities", None)
    return camera_config


@dataclass(frozen=True)
class Failure:
    detector: str  # timeout | no_progress | success_check
    reason: str
    at_s: float  # seconds since the trial started

    event_type = FAILURE_EVENT

    @staticmethod
    def ref(robot_id: int, command_id: int, trial_index: int) -> str:
        """Same key the orchestrator uses (``iv:<robot>:<command>:<trial>``)."""
        return f"iv:{int(robot_id)}:{int(command_id)}:{int(trial_index)}"

    def payload(self, robot_id: int, ticket_id: int, command_id: int, trial_index: int,
                eval_run_id: int = 0, **extra: Any) -> dict:
        return {"robot_id": int(robot_id), "ticket_id": int(ticket_id), "command_id": int(command_id),
                "trial_index": int(trial_index), "eval_run_id": int(eval_run_id or 0), "detector": self.detector,
                "reason": self.reason, "at_s": round(self.at_s, 3),
                "ref": self.ref(robot_id, command_id, trial_index), **extra}


class FailureDetector:
    """Simple, explainable failure rules for one trial (no learned classifier):

    * ``timeout``: the trial ran ``trial_timeout_s`` (minus paused time the caller excludes).
    * ``no_progress``: ``progress`` has not improved by ``min_progress`` for ``no_progress_s``.
    * ``success_check``: the explicit success check after a grasp attempt returned False.
    It fires at most once per trial.
    """

    def __init__(self, trial_timeout_s: float = 90.0, no_progress_s: float = 20.0, min_progress: float = 0.01,
                 verdict_wait_s: float = 15.0) -> None:
        self.trial_timeout_s = float(trial_timeout_s)
        self.no_progress_s = float(no_progress_s)
        self.min_progress = float(min_progress)
        self.verdict_wait_s = float(verdict_wait_s)
        self.t0: float | None = None
        self.best: float | None = None
        self.best_at: float = 0.0
        self.fired: Failure | None = None

    @classmethod
    def from_payload(cls, cfg: dict | None) -> "FailureDetector | None":
        """None when interventions are off for this rollout."""
        if not cfg or not cfg.get("enabled"):
            return None
        return cls(trial_timeout_s=float(cfg.get("trial_timeout_s") or 90.0),
                   no_progress_s=float(cfg.get("no_progress_s") or 20.0),
                   min_progress=float(cfg.get("min_progress") or 0.01),
                   verdict_wait_s=float(cfg.get("verdict_wait_s") or 15.0))

    def start(self, now_s: float) -> None:
        self.t0, self.best, self.best_at, self.fired = now_s, None, now_s, None

    def _fire(self, detector: str, reason: str, now_s: float) -> Failure | None:
        if self.fired is not None:
            return None
        self.fired = Failure(detector, reason, now_s - (self.t0 if self.t0 is not None else now_s))
        return self.fired

    def tick(self, now_s: float, progress: float | None = None) -> Failure | None:
        if self.t0 is None:
            self.start(now_s)
        if self.fired is not None:
            return None
        if now_s - self.t0 >= self.trial_timeout_s:
            return self._fire("timeout", f"trial ran {now_s - self.t0:.1f}s (limit {self.trial_timeout_s:.0f}s)", now_s)
        if progress is not None:
            if self.best is None or progress >= self.best + self.min_progress:
                self.best, self.best_at = progress, now_s
            elif now_s - self.best_at >= self.no_progress_s:
                return self._fire("no_progress", f"no progress for {now_s - self.best_at:.1f}s "
                                                 f"(best {self.best:.3f}, limit {self.no_progress_s:.0f}s)", now_s)
        return None

    def success_check(self, ok: bool, note: str = "", now_s: float | None = None) -> Failure | None:
        if ok:
            return None
        t = now_s if now_s is not None else (self.t0 if self.t0 is not None else 0.0)
        return self._fire("success_check", note or "success check failed after the grasp attempt", t)


class TrialIntervention:
    """One collection trial's intervention state (robot side). Thread safe.

    Fed with every control command the job sees (``on_signal(type, payload, command_id)``) and with
    the robot's state (``on_state(paused)``): a ``pause_for_takeover`` only counts once the state
    machine actually entered paused_for_takeover / teleop (a refused pause changes nothing).

    Phases: ``running`` -> (``failure`` set by the detector) -> ``paused`` (pause_for_takeover for
    this rollout) -> ``handed_back`` (back in rollout without an end_trial) | ``ended`` (a
    ``resume_policy`` {end_trial, ref} for this rollout: unclaimed / declined -> the trial fails).
    """

    def __init__(self, robot_id: int, command_id: int, trial_index: int) -> None:
        self.robot_id, self.command_id, self.trial_index = int(robot_id), int(command_id), int(trial_index)
        self.prefix = f"iv:{self.robot_id}:{self.command_id}:"
        self.own_ref = Failure.ref(robot_id, command_id, trial_index)
        self.failure: Failure | None = None
        self.pause: dict[str, Any] | None = None  # payload of the pause_for_takeover that took effect
        self.paused_at_ns: int | None = None
        self.handback_at_ns: int | None = None
        self.handback_via: str = ""
        # the claimant's "Did you recover the task?" at handback ({t:"handback", success} on /control, or a
        # handback command's payload.success); None = no answer -> success unknown, the reviewer rejects
        self.handback_success: bool | None = None
        self.end_trial: dict[str, Any] | None = None
        self._pending_pause: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._changed = threading.Event()

    # ---- derived
    @property
    def ref(self) -> str:
        return str((self.pause or {}).get("ref") or self.own_ref)

    @property
    def task_id(self) -> int:
        return int((self.pause or {}).get("task_id") or 0)

    @property
    def paused(self) -> bool:
        return self.pause is not None

    @property
    def handed_back(self) -> bool:
        return self.handback_at_ns is not None and self.end_trial is None

    @property
    def intervened(self) -> bool:
        return self.paused or self.end_trial is not None

    @property
    def phase(self) -> str:
        if self.end_trial is not None:
            return "ended"
        if self.handback_at_ns is not None:
            return "handed_back"
        if self.pause is not None:
            return "paused"
        return "failure" if self.failure is not None else "running"

    def mine(self, payload: dict[str, Any]) -> bool:
        ref = str(payload.get("ref") or "")
        return ref.startswith(self.prefix)

    # ---- inputs
    def on_signal(self, type_: str, payload: dict[str, Any], command_id: int = 0) -> str | None:
        """A control command seen by the job. Returns what it meant for this trial (or None)."""
        with self._lock:
            if type_ == "pause_for_takeover" and self.pause is None and self.mine(payload):
                self._pending_pause = {**payload, "command_id": int(command_id)}
                out = "pause_pending"
            elif type_ == "resume_policy" and payload.get("end_trial") and self.end_trial is None and (
                    str(payload.get("ref") or "") in (self.ref, self.own_ref) or self.mine(payload)):
                self.end_trial = {**payload, "command_id": int(command_id)}
                out = "end_trial"
            elif type_ in ("handback", "resume_policy") and self.pause is not None and not self.handback_via \
                    and not payload.get("end_trial"):
                # a /control handback (TDM-52) flips the state first and then posts a synthetic
                # Command with id 0; a DB handback / resume_policy arrives here before the state flip
                self.handback_via = "/control" if not command_id else f"{type_} #{command_id}"
                ok = payload.get("success")
                self.handback_success = ok if isinstance(ok, bool) else None
                out = "handback_control" if not command_id else "handback_command"
            else:
                return None
        self._changed.set()
        return out

    def on_state(self, paused_now: bool, now_ns: int | None = None) -> str | None:
        """The robot's state: paused_now = paused_for_takeover or teleop. Returns a transition or None."""
        now_ns = int(now_ns or time.monotonic_ns())
        with self._lock:
            if paused_now and self.pause is None and self._pending_pause is not None:
                self.pause, self._pending_pause, self.paused_at_ns = self._pending_pause, None, now_ns
                out = "paused"
            elif not paused_now and self.pause is not None and self.handback_at_ns is None:
                self.handback_at_ns = now_ns
                out = "ended" if self.end_trial is not None else "handed_back"
            else:
                return None
        self._changed.set()
        return out

    def wait(self, timeout: float | None, check: Callable[[], None], until: Callable[[], bool]) -> bool:
        """Wait until ``until()`` (True) or ``timeout`` (False; None = forever), calling ``check``."""
        end = None if timeout is None else time.monotonic() + timeout
        while not until():
            check()
            if end is not None and time.monotonic() >= end:
                return False
            self._changed.wait(0.05)
            self._changed.clear()
        return True

    def summary(self) -> dict[str, Any]:
        return {"phase": self.phase, "ref": self.ref if self.intervened else None, "task_id": self.task_id or None,
                "detector": self.failure.detector if self.failure else ("operator" if self.paused else None),
                "reason": self.failure.reason if self.failure else str((self.pause or {}).get("reason") or "") or None,
                "end_trial": (self.end_trial or {}).get("reason"), "handback_via": self.handback_via or None,
                "handback_success": self.handback_success,
                "paused_s": round((self.handback_at_ns - self.paused_at_ns) / 1e9, 3)
                if self.paused_at_ns and self.handback_at_ns else None}


__all__ = ["CAPABILITY", "COLLECT_PURPOSE", "ENV_FLAG", "FAILURE_EVENT", "HANDBACK_EVENT", "Failure", "FailureDetector",
           "TrialIntervention", "advertise", "robot_enabled"]
