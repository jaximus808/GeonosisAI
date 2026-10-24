"""robot_command rows as Python values, plus the cancellation token jobs poll (TDM-49).

Command types and how the loop treats them (the TDM-13 module enforces the same split):
  * JOB types run one at a time on the job thread: run_baseline, run_eval, start_teleop,
    reset_request, home, load_checkpoint. The module refuses a second open job per robot.
  * CONTROL types are handled inline by the dispatcher, in seq order, while a job runs:
    stop_teleop, pause_for_takeover, resume_policy, handback, stop.
  * soft_estop is handled first, always: the arm is stopped from the subscription callback the
    moment the row arrives (before the ack round-trip), then the command is acked and completed.

Payload keys the real client reads (same contract as the TDM-28 fake, see fake.py):
  run_eval / run_baseline  {"eval_run_id": int?, "checkpoint_uri": str?, "trials": int?, "timeout_s": float?,
                            "kind": str?, "solo_mode": bool?}   (TDM-67; consumed by the TDM-51 runner)
  start_teleop             {"task_id": int?, "kind": "clean_teleop"|"recovery_takeover"?, "contributor_id": int?}
  reset_request            {"task_id": int?, "reason": str?, "ref": str?}   (TDM-26 dispatcher)
  resume_policy            {"reason": "reset_done"?, "task_id": int?}       (TDM-26 dispatcher)
  load_checkpoint / home   {"checkpoint_uri": str?} / {"speed_fraction": float?}
  trial_outcome            {"success": bool, "note": str?, "eval_run_id": int?, "index": int?}  (TDM-51 operator verdict)
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

JOB_TYPES = frozenset({"run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint"})
CONTROL_TYPES = frozenset({"stop_teleop", "pause_for_takeover", "resume_policy", "handback", "stop",
                           "trial_outcome"})  # trial_outcome: TDM-51 operator verdict
SOFT_ESTOP = "soft_estop"
OPEN_STATUSES = frozenset({"issued", "acked"})


@dataclass(frozen=True)
class Command:
    id: int
    seq: int
    robot_id: int
    ticket_id: int
    type: str
    payload: dict[str, Any]
    status: str
    issued_by: str
    issued_at_us: int | None
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Command":
        try:
            payload = json.loads(row.get("payload_json") or "{}")
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except ValueError:
            payload = {"_unparsed": row.get("payload_json")}
        return cls(
            id=int(row["id"]), seq=int(row.get("seq") or 0), robot_id=int(row.get("robot_id") or 0),
            ticket_id=int(row.get("ticket_id") or 0), type=str(row["type"]), payload=payload,
            status=str(row["status"]), issued_by=str(row.get("issued_by") or ""),
            issued_at_us=int(row["issued_at"]) if row.get("issued_at") is not None else None, raw=dict(row),
        )

    @property
    def is_estop(self) -> bool:
        return self.type == SOFT_ESTOP

    @property
    def is_job(self) -> bool:
        return self.type in JOB_TYPES

    @property
    def priority(self) -> tuple[int, int, int]:
        """Heap key: soft_estop first, then strictly by per-robot seq (id breaks ties)."""
        return (0 if self.is_estop else 1, self.seq, self.id)

    def age_s(self, now: float | None = None) -> float | None:
        if self.issued_at_us is None:
            return None
        return (now if now is not None else time.time()) - self.issued_at_us / 1e6

    def label(self) -> str:
        return f"#{self.id} seq={self.seq} {self.type}"


class Cancelled(Exception):
    """Raised inside a job when it must stop. ``reason``: preempted | estop | stop | shutdown | link."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class CancelToken:
    """Per-job cancellation. The loop cancels it; the job polls ``check()`` / ``sleep()``."""

    def __init__(self) -> None:
        self._ev = threading.Event()
        self.reason: str | None = None
        self.detail: str = ""

    def cancel(self, reason: str, detail: str = "") -> None:
        if not self._ev.is_set():
            self.reason, self.detail = reason, detail
            self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled(self.reason or "cancelled", self.detail)

    def sleep(self, seconds: float) -> None:
        """Sleep, raising Cancelled as soon as the token is cancelled."""
        if self._ev.wait(max(seconds, 0.0)):
            raise Cancelled(self.reason or "cancelled", self.detail)

    def wait(self, timeout: float | None = None) -> bool:
        return self._ev.wait(timeout)


__all__ = ["CONTROL_TYPES", "Cancelled", "CancelToken", "Command", "JOB_TYPES", "OPEN_STATUSES", "SOFT_ESTOP"]
