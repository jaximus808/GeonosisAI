"""Robot state machine (TDM-49).

Fine-grained states (what the robot is doing) and the coarse ``robot.status`` it reports:

    state                 robot.status   meaning
    idle                  online         ready for a job (busy while a stateless job such as
                                         load_checkpoint runs)
    homing                busy           moving to the home pose
    rollout               busy           a policy is driving the arm (run_baseline / run_eval)
    teleop                busy           a human is driving the arm (start_teleop, or a takeover
                                         inside a paused rollout)
    paused_for_takeover   paused         rollout paused in a failure state, waiting for a takeover
    waiting_reset         paused         arm held, waiting for a human to reset the scene
    locked                estop          soft e-stop latched; only a human at the arm clears it

``robot.status`` stays on the TDM-8 enum (offline/online/busy/paused/estop) because the
orchestrator keys off it: the TDM-26 dispatcher requests a reset when a robot enters ``paused``
and the TDM-13 module refuses commands while it is ``estop``. The fine state is published as
``robot.state_changed`` events in event_log (actor ``robot-client:<name>``) and in every
command's result_json.

Transitions (anything -> locked is always allowed; locked -> idle only via ``unlock``):

    idle                -> homing | rollout | teleop | waiting_reset
    homing              -> idle
    rollout             -> idle | paused_for_takeover | waiting_reset
    paused_for_takeover -> rollout | teleop | waiting_reset | idle
    teleop              -> idle | rollout | paused_for_takeover
    waiting_reset       -> idle | rollout
"""
from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass


class RobotState(str, enum.Enum):
    IDLE = "idle"
    HOMING = "homing"
    ROLLOUT = "rollout"
    TELEOP = "teleop"
    PAUSED_FOR_TAKEOVER = "paused_for_takeover"
    WAITING_RESET = "waiting_reset"
    LOCKED = "locked"

    def __str__(self) -> str:  # f-strings / json give "idle", not "RobotState.IDLE"
        return self.value


S = RobotState

TRANSITIONS: dict[RobotState, frozenset[RobotState]] = {
    S.IDLE: frozenset({S.HOMING, S.ROLLOUT, S.TELEOP, S.WAITING_RESET}),
    S.HOMING: frozenset({S.IDLE}),
    S.ROLLOUT: frozenset({S.IDLE, S.PAUSED_FOR_TAKEOVER, S.WAITING_RESET}),
    S.PAUSED_FOR_TAKEOVER: frozenset({S.ROLLOUT, S.TELEOP, S.WAITING_RESET, S.IDLE}),
    S.TELEOP: frozenset({S.IDLE, S.ROLLOUT, S.PAUSED_FOR_TAKEOVER}),
    S.WAITING_RESET: frozenset({S.IDLE, S.ROLLOUT}),
    S.LOCKED: frozenset(),  # unlock() only
}

DB_STATUS: dict[RobotState, str] = {
    S.IDLE: "online",
    S.HOMING: "busy",
    S.ROLLOUT: "busy",
    S.TELEOP: "busy",
    S.PAUSED_FOR_TAKEOVER: "paused",
    S.WAITING_RESET: "paused",
    S.LOCKED: "estop",
}

# States in which the arm may be moved by a job's control loop.
MOTION_STATES = frozenset({S.HOMING, S.ROLLOUT, S.TELEOP})


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class Transition:
    src: RobotState
    dst: RobotState
    reason: str
    command_id: int | None
    at: float  # time.time()


Listener = Callable[[Transition], None]


class StateMachine:
    """Thread-safe current state + listeners. Listeners run on the caller's thread, after the lock."""

    def __init__(self, initial: RobotState = S.IDLE) -> None:
        self._state = initial
        self._cond = threading.Condition(threading.RLock())
        self._listeners: list[Listener] = []
        self.job_active = False  # set by the command loop while any job command runs
        self.lock_reason: str | None = None
        self.history: list[Transition] = []  # bounded; for tests and /status

    @property
    def state(self) -> RobotState:
        return self._state

    @property
    def locked(self) -> bool:
        return self._state is S.LOCKED

    def db_status(self) -> str:
        if self._state is S.IDLE and self.job_active:
            return "busy"
        return DB_STATUS[self._state]

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def can(self, dst: RobotState) -> bool:
        return dst is S.LOCKED or dst is self._state or dst in TRANSITIONS[self._state]

    def transition(self, dst: RobotState, reason: str = "", *, command_id: int | None = None,
                   allowed_from: Iterable[RobotState] | None = None) -> Transition | None:
        """Move to ``dst``. Returns None if already there. Raises InvalidTransition otherwise.

        ``allowed_from`` narrows the legal sources further (e.g. resume only from paused).
        """
        with self._cond:
            src = self._state
            if dst is src:
                return None
            if allowed_from is not None and src not in set(allowed_from):
                raise InvalidTransition(f"{dst} needs the robot in {sorted(map(str, allowed_from))}, it is {src}")
            if src is S.LOCKED:
                raise InvalidTransition(f"robot is locked (soft e-stop: {self.lock_reason}); a human must clear it")
            if dst is not S.LOCKED and dst not in TRANSITIONS[src]:
                raise InvalidTransition(f"illegal transition {src} -> {dst}")
            tr = self._set(dst, reason, command_id)
        self._notify(tr)
        return tr

    def lock(self, reason: str, *, command_id: int | None = None) -> Transition | None:
        with self._cond:
            self.lock_reason = reason
            if self._state is S.LOCKED:
                return None
            tr = self._set(S.LOCKED, reason, command_id)
        self._notify(tr)
        return tr

    def unlock(self, reason: str) -> Transition | None:
        with self._cond:
            if self._state is not S.LOCKED:
                return None
            self.lock_reason = None
            tr = self._set(S.IDLE, reason, None)
        self._notify(tr)
        return tr

    def wait_for(self, pred: Callable[[RobotState], bool], timeout: float | None = None) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: pred(self._state), timeout)

    def _set(self, dst: RobotState, reason: str, command_id: int | None) -> Transition:
        tr = Transition(self._state, dst, reason, command_id, time.time())
        self._state = dst
        self.history.append(tr)
        del self.history[:-200]
        self._cond.notify_all()
        return tr

    def _notify(self, tr: Transition) -> None:
        for fn in list(self._listeners):
            try:
                fn(tr)
            except Exception:  # noqa: BLE001 - a listener must never break a transition
                pass


__all__ = ["DB_STATUS", "InvalidTransition", "MOTION_STATES", "RobotState", "StateMachine", "TRANSITIONS", "Transition"]
