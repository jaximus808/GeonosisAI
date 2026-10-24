"""Job handlers and the context they run with (TDM-49). This is the plug-in point for
TDM-50 (policy rollout), TDM-51 (eval trials), TDM-52 (teleop/takeover) and TDM-48 (recorder).

A job handler is ``fn(ctx: JobContext) -> dict | None``. It runs on the job thread after the
command was acked; its return value becomes ``result_json`` of ``complete_robot_command``.
Raise to fail the command (the message goes to ``fail_robot_command``). Register it with::

    from rdf_robot_client.jobs import job_handler

    @job_handler("run_eval")
    def run_eval(ctx):                      # TDM-51
        for i in range(trials):
            ctx.enter(RobotState.ROLLOUT, f"eval trial {i}")
            stats = ctx.run_control_loop(policy_step, max_seconds=timeout_s)  # pauses/e-stops handled
            ctx.call_reducer("record_eval_trial", eval_run_id, i, ok, ms, episode_id, note)
            if i < trials - 1:
                ctx.wait_for_reset()        # waiting_reset until the dispatcher's resume_policy
        return {"eval_run_id": ..., "trials": ..., "successes": ...}

TDM-67 run_eval contract: payload {eval_run_id, checkpoint_uri, trials, timeout_s, kind, solo_mode}.
Record every trial with record_eval_trial, then ``ctx.wait_for_reset()`` before the next one (the
real robot never resets the scene itself). After a soft_estop the job is cancelled and the command
stays ``preempted``; nothing restarts it automatically (not after the e-stop is cleared, not after
a client restart: leftover acked commands are failed at boot).

Rules for handlers:
  * Move the arm ONLY through ``ctx.arm`` (the safety-wrapped adapter), ideally via
    ``ctx.run_control_loop`` or ``ctx.arm.move_joints(..., should_abort=ctx.should_abort)``.
  * Call ``ctx.check()`` (or use ``ctx.sleep``) at least every 50 ms in any loop of your own:
    it raises ``Cancelled`` on soft_estop / preemption / stop / shutdown. Don't catch Cancelled
    (or re-raise it); the loop decides what to report.
  * Change state with ``ctx.enter(state)``. When the handler returns or raises, the loop holds the
    arm and returns the robot to ``idle`` (unless it is locked).
  * Control commands (pause_for_takeover, resume_policy, handback, stop_teleop) are applied by the
    loop to the state machine; ``run_control_loop`` holds the arm while the robot is not in the
    loop's active state. Every control also lands on ``ctx.signals`` for handlers that care
    (the takeover executor watches for ``handback``).
  * Optional services (cameras TDM-47, recorder TDM-48, policy client TDM-50) are in
    ``ctx.services`` under those names when main.py could build them.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .adapters.base import ArmState, ArmStopped, Target
from .commands import Cancelled, CancelToken, Command
from .state import RobotState

if TYPE_CHECKING:
    from .loop import RobotClient

JobHandler = Callable[["JobContext"], "dict[str, Any] | None"]

HANDLERS: dict[str, JobHandler] = {}


def job_handler(command_type: str) -> Callable[[JobHandler], JobHandler]:
    """Decorator: register (or replace) the default handler for a job command type."""

    def deco(fn: JobHandler) -> JobHandler:
        HANDLERS[command_type] = fn
        return fn

    return deco


@dataclass
class LoopStats:
    steps: int = 0
    seconds: float = 0.0
    paused_seconds: float = 0.0
    overruns: int = 0  # ticks whose step() took longer than the period
    ended: str = "done"  # done | max_steps | max_seconds | finish_requested


@dataclass
class ResetDone:
    how: str  # resume_policy | task_completed
    task_id: int | None
    command_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"how": self.how, "task_id": self.task_id, "resume_command_id": self.command_id}


class _ResetWaiter:
    def __init__(self, task_id: int | None, since_us: int, accept_task_completion: bool = False) -> None:
        self.task_id = task_id
        self.accept_task_completion = accept_task_completion
        self.since_us = since_us
        self.done = threading.Event()
        self.outcome: ResetDone | None = None

    def release(self, outcome: ResetDone) -> None:
        if not self.done.is_set():
            self.outcome = outcome
            self.done.set()


class JobContext:
    def __init__(self, client: "RobotClient", command: Command) -> None:
        self.client = client
        self.command = command
        self.payload: dict[str, Any] = command.payload
        self.arm = client.arm
        self.sm = client.sm
        self.cancel = CancelToken()
        self.services: dict[str, Any] = client.services
        self.signals: "queue.Queue[Command]" = queue.Queue()  # control commands seen during this job
        self.finish_requested = threading.Event()  # stop_teleop on a standalone teleop job
        self.started = threading.Event()  # set on the first ctx.enter() (controls wait for it)
        self.result: dict[str, Any] = {}  # handlers may fill this progressively

    # ---- state
    @property
    def state(self) -> RobotState:
        return self.sm.state

    def enter(self, state: RobotState, reason: str = "", **kw: Any) -> None:
        self.check()
        self.sm.transition(state, reason or self.command.type, command_id=self.command.id, **kw)
        self.started.set()

    # ---- cancellation
    def check(self) -> None:
        self.cancel.check()
        if self.sm.locked:
            raise Cancelled("estop", self.sm.lock_reason or "")

    def should_abort(self) -> bool:
        return self.cancel.cancelled or self.sm.locked

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self._nap(min(left, 0.05))

    def _nap(self, s: float) -> None:
        if self.cancel.wait(s):
            self.check()

    # ---- streaming control loop (rollout, teleop)
    def run_control_loop(
        self,
        step: Callable[[int, ArmState], "Target | None"],
        *,
        rate_hz: float | None = None,
        max_steps: int | None = None,
        max_seconds: float | None = None,
        active_state: RobotState | None = None,
        control_source: str | None = None,
    ) -> LoopStats:
        """Call ``step(i, arm_state)`` at ``rate_hz`` and stream its target to the arm.

        ``step`` returns a JointTarget/EETarget, or None to end the loop. While the robot is not in
        ``active_state`` (default: the state when the loop starts; e.g. paused_for_takeover or
        waiting_reset during a rollout) or the SpacetimeDB link is down, the arm is held and
        ``step`` is not called. Raises Cancelled on e-stop / preemption / stop.

        Every streamed target is recorded as a ``ControlTick`` (TDM-25 episode columns) with
        ``control_source`` = ``control_source`` or policy (rollout) / human (teleop); the recorder
        subscribes with ``client.add_tick_listener`` or samples ``client.control_tick()``.
        """
        active = active_state or self.sm.state
        source = control_source or ("human" if active is RobotState.TELEOP else "policy")
        period = 1.0 / (rate_hz or self.arm.control_rate_hz)
        stats = LoopStats()
        t_start = time.monotonic()
        next_t = t_start
        i = 0
        while True:
            self.check()
            if self.finish_requested.is_set():
                stats.ended = "finish_requested"
                break
            if max_steps is not None and i >= max_steps:
                stats.ended = "max_steps"
                break
            if max_seconds is not None and time.monotonic() - t_start - stats.paused_seconds >= max_seconds:
                stats.ended = "max_seconds"
                break
            if self.sm.state is not active or not self.client.link_ok():
                stats.paused_seconds += self._hold_until(lambda: self.sm.state is active and self.client.link_ok())
                next_t = time.monotonic()
                continue
            target = step(i, self.arm.read_state())
            if target is None:
                break
            try:
                self.arm.command(target)
            except ArmStopped as e:
                raise Cancelled("estop", str(e)) from e
            self.client.record_command(target, source)
            i += 1
            stats.steps = i
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                self._nap(delay)
            else:
                stats.overruns += 1
                next_t = time.monotonic()
        stats.seconds = time.monotonic() - t_start
        return stats

    def _hold_until(self, ok: Callable[[], bool]) -> float:
        t0 = time.monotonic()
        self.arm.hold()
        while not ok():
            self.check()
            self._nap(0.02)
        return time.monotonic() - t0

    # ---- resets
    def wait_for_reset(self, *, task_id: int | None = None, timeout: float | None = None,
                       reason: str = "waiting for scene reset", accept_task_completion: bool = False) -> ResetDone:
        """Hold the arm in ``waiting_reset`` (robot.status ``paused``) until the scene is reset, then go back.

        Released by a ``resume_policy`` command: the TDM-26 dispatcher creates the paid reset task
        and sends resume_policy once it is done (TDM-67 contract: between eval trials the robot
        never resets itself and never continues without that resume). With
        ``accept_task_completion`` the reset task row (type reset, this robot, ``task_id`` if given)
        entering ``completed`` also releases it; only the standalone ``reset_request`` job uses that,
        since a late resume for an earlier trial could otherwise release the next wait.
        Raises Cancelled on e-stop / stop, TimeoutError on ``timeout``.
        """
        back_to = self.sm.state
        self.arm.hold()
        self.enter(RobotState.WAITING_RESET, reason)
        waiter = self.client._add_reset_waiter(task_id, accept_task_completion)
        try:
            self.client.event("robot.waiting_reset", command_id=self.command.id, ticket_id=self.command.ticket_id,
                              task_id=task_id, reason=reason)
            end = None if timeout is None else time.monotonic() + timeout
            while not waiter.done.is_set():
                self.check()
                if end is not None and time.monotonic() > end:
                    raise TimeoutError(f"no reset within {timeout:.0f}s")
                waiter.done.wait(0.05)
        finally:
            self.client._remove_reset_waiter(waiter)
        outcome = waiter.outcome or ResetDone("unknown", task_id)
        dst = back_to if back_to in (RobotState.ROLLOUT, RobotState.IDLE) else RobotState.IDLE
        if self.sm.state is RobotState.WAITING_RESET:
            self.sm.transition(dst, f"reset done ({outcome.how})", command_id=self.command.id)
        self.client.event("robot.reset_done", command_id=self.command.id, ticket_id=self.command.ticket_id,
                          **outcome.to_dict())
        return outcome

    # ---- evidence + reducers
    def event(self, type_: str, **payload: Any) -> None:
        """Append an event_log row (async, ordered) tagged with this command."""
        self.client.event(type_, command_id=self.command.id, ticket_id=self.command.ticket_id, **payload)

    def call_reducer(self, name: str, *args: Any) -> None:
        """Synchronous reducer call as the robot_client identity (e.g. record_eval_trial)."""
        self.client.http.call_reducer(name, *args)

    def sql(self, query: str) -> list[dict[str, Any]]:
        return self.client.http.sql(query)

    def to_json(self, value: Any) -> str:
        return json.dumps(value, default=str)


__all__ = ["HANDLERS", "JobContext", "JobHandler", "LoopStats", "ResetDone", "job_handler"]
