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
        task's ``assignee_id`` as contributor (read the public ``task`` row).
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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FAILURE_EVENT = "intervention.failure_detected"
HANDBACK_EVENT = "intervention.handed_back"
CAPABILITY = "interventions"


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

    def __init__(self, trial_timeout_s: float = 120.0, no_progress_s: float = 20.0, min_progress: float = 0.01,
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
        return cls(trial_timeout_s=float(cfg.get("trial_timeout_s") or 120.0),
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


__all__ = ["CAPABILITY", "FAILURE_EVENT", "HANDBACK_EVENT", "Failure", "FailureDetector"]
