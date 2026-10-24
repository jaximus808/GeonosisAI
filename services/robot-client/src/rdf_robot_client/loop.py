"""SpacetimeDB command loop (TDM-49): register, heartbeat, and run robot_command rows in seq order.

Threads:
  * stdb-subscription  (rdf_common Subscription, sync thread mode) receives robot_command and task
                       rows for this robot. Callbacks only enqueue, except the soft_estop fast path,
                       which calls ``arm.stop()`` right there (no network round-trip first).
  * rdf-dispatch       pops commands strictly by (estop first, then seq): checks the cached status
                       (anything not ``issued`` -- e.g. ``preempted`` -- is refused and never run),
                       ACKS, then hands jobs to the job thread and applies controls inline.
  * rdf-job            runs one job handler at a time (jobs.py), then completes/fails the command.
  * rdf-heartbeat      ``robot_heartbeat`` every ``heartbeat_s`` and immediately on a state change,
                       plus the ordered event outbox (``append_event``).

Protocol (TDM-13): ack -> complete | fail. Ack is refused for a preempted command, so a command
is executed only after its ack succeeded. A job cancelled by preemption/e-stop is left
``preempted`` (complete would be refused anyway); ``stop`` completes it with ``aborted: true``.

Boot safety: acked commands left over from a previous process are failed ("restarted"), and
issued commands older than ``max_command_age_s`` are failed as stale instead of moving the arm.
If the robot row says ``estop`` at boot the client starts ``locked`` (unless
``clear_estop_on_boot``).
"""
from __future__ import annotations

import heapq
import json
import queue
import sys
import threading
import time
from typing import Any

from rdf_common.spacetime_client import SpacetimeClient, SpacetimeConfig, Subscription

from . import handlers as _default_handlers  # noqa: F401 - registers the default job handlers
from .adapters.base import ArmAdapter, ArmStopped, ControlTick, EETarget, Target
from .commands import SOFT_ESTOP, Cancelled, Command
from .jobs import HANDLERS, JobContext, JobHandler, ResetDone, _ResetWaiter
from .state import InvalidTransition, RobotState, StateMachine, Transition

S = RobotState


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "robot-client", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


class RobotClient:
    def __init__(
        self,
        arm: ArmAdapter,
        *,
        name: str,
        stdb: SpacetimeConfig,
        camera_config: dict[str, Any] | None = None,
        heartbeat_s: float = 5.0,
        handlers: dict[str, JobHandler] | None = None,
        services: dict[str, Any] | None = None,
        clear_estop_on_boot: bool = False,
        max_command_age_s: float = 120.0,
        link_loss_hold_s: float = 2.0,
        estop_auto_clear_s: float = 0.0,
    ) -> None:
        self.arm = arm
        self.name = name
        self.stdb = stdb
        self.camera_config = camera_config or {}
        self.heartbeat_s = heartbeat_s
        self.handlers: dict[str, JobHandler] = {**HANDLERS, **(handlers or {})}
        self.services: dict[str, Any] = services or {}
        self.clear_estop_on_boot = clear_estop_on_boot
        self.max_command_age_s = max_command_age_s
        self.link_loss_hold_s = link_loss_hold_s
        self.estop_auto_clear_s = estop_auto_clear_s
        self.actor = f"robot-client:{name}"

        self.http = SpacetimeClient(config=stdb)
        self.sm = StateMachine()
        self.sm.add_listener(self._on_transition)
        self.robot_id: int | None = None
        self.sub: Subscription | None = None
        self.checkpoint_uri: str | None = None
        self.last_estop: dict[str, Any] | None = None

        self._heap: list[tuple[tuple[int, int, int], Command]] = []
        self._heap_cv = threading.Condition()
        self._seen: set[int] = set()
        self._boot_stale: dict[int, str] = {}  # command id -> reason to fail it at boot
        self._first_snapshot = True
        self._estopped_ids: set[int] = set()
        self._estop_lock = threading.Lock()
        self._job: JobContext | None = None
        self._job_lock = threading.Lock()
        self._jobs: "queue.Queue[JobContext]" = queue.Queue()
        self._reset_waiters: list[_ResetWaiter] = []
        self._reset_lock = threading.Lock()
        self._consumed_resets: set[int] = set()  # reset task ids that already released a waiter
        self._status_q: "queue.Queue[str]" = queue.Queue()  # db statuses in transition order
        self._outbox: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
        self._dirty = threading.Event()
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []
        self._link_down_since: float | None = None
        self._last_sent_status: str | None = None
        self._estop_timer: threading.Timer | None = None
        # last streamed command, for the recorder (TDM-48): (pose, gripper_cmd, source, monotonic t)
        self._last_cmd: tuple[Any, float, str, float] | None = None
        self._tick_listeners: list[Any] = []

    # ================================================================== lifecycle
    def start(self, ready_timeout: float = 20.0) -> "RobotClient":
        self.arm.connect()
        self._register()
        self.sub = Subscription(
            [f"SELECT * FROM robot_command WHERE robot_id = {self.robot_id}",
             f"SELECT * FROM task WHERE robot_id = {self.robot_id}"],
            on_applied=self._on_applied, on_insert=self._on_insert, on_update=self._on_update,
            on_error=lambda e: _log("subscription error", error=str(e)[:300]),
            config=self.stdb, max_backoff=5.0,
        ).start_thread()
        for target, name in ((self._dispatch_loop, "rdf-dispatch"), (self._job_loop, "rdf-job"),
                             (self._heartbeat_loop, "rdf-heartbeat")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if not self.sub.wait_ready(ready_timeout):
            raise RuntimeError(f"robot_command subscription not ready after {ready_timeout:.0f}s")
        _log("ready", robot_id=self.robot_id, name=self.name, state=str(self.sm.state), adapter=self.arm.name)
        return self

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        job = self._job
        if job is not None:
            job.cancel.cancel("shutdown", "robot client stopping")
        for t in self._threads:
            if t.name == "rdf-job":
                t.join(5.0)
        try:
            self.arm.hold()
        except Exception:  # noqa: BLE001
            pass
        self._flush_outbox()
        try:
            if self.robot_id is not None:
                status = "estop" if self.sm.locked else "offline"
                self.http.call_reducer("robot_heartbeat", self.robot_id, status)
        except Exception as e:  # noqa: BLE001
            _log("final heartbeat failed", error=str(e)[:200])
        with self._heap_cv:
            self._heap_cv.notify_all()
        if self.sub:
            self.sub.stop()
        if self._estop_timer:
            self._estop_timer.cancel()
        self.http.close()
        self.arm.disconnect()
        _log("stopped", robot_id=self.robot_id)

    def _register(self) -> None:
        cams = json.dumps({**self.camera_config, "arm": self.arm.describe()})
        self.http.call_reducer("register_robot", self.name, self.arm.embodiment, self.arm.adapter_version, cams)
        safe = self.name.replace("'", "''")
        rows = self.http.sql(f"SELECT * FROM robot WHERE name = '{safe}'")
        if not rows:
            raise RuntimeError(f"register_robot did not create robot {self.name!r}")
        row = rows[0]
        self.robot_id = int(row["id"])
        if row.get("status") == "estop" and not self.clear_estop_on_boot:
            self.arm.stop()
            self.sm.lock("robot was in estop when the client booted; a human must clear it")
        # First heartbeat is synchronous so a binding conflict fails loudly at boot.
        self.http.call_reducer("robot_heartbeat", self.robot_id, self.sm.db_status())
        self._last_sent_status = self.sm.db_status()
        self.event("robot.client_started", adapter=self.arm.name, embodiment=self.arm.embodiment,
                   adapter_version=self.arm.adapter_version, state=str(self.sm.state))
        _log("registered", robot_id=self.robot_id, name=self.name, db_status=row.get("status"))

    # ================================================================== public helpers
    def link_ok(self) -> bool:
        """False once the subscription has been down for more than ``link_loss_hold_s``."""
        if self.sub is not None and self.sub.ready:
            self._link_down_since = None
            return True
        now = time.monotonic()
        if self._link_down_since is None:
            self._link_down_since = now
        return now - self._link_down_since < self.link_loss_hold_s

    def event(self, type_: str, **payload: Any) -> None:
        """Queue an event_log row (sent in order by the heartbeat thread)."""
        self._outbox.put((type_, {"robot_id": self.robot_id, "robot": self.name, **payload}))
        self._dirty.set()

    def clear_estop(self, by: str = "local operator") -> bool:
        """Unlatch after a human checked the arm (SIGUSR1 / stdin 'clear' in main.py)."""
        if not self.sm.locked:
            return False
        self.arm.clear_stop()
        self.sm.unlock(f"soft e-stop cleared by {by}")
        self.event("robot.estop_cleared", by=by)
        _log("estop cleared", by=by)
        return True

    def snapshot(self) -> dict[str, Any]:
        job = self._job
        return {"robot_id": self.robot_id, "state": str(self.sm.state), "db_status": self.sm.db_status(),
                "job": job.command.label() if job else None, "link_ok": self.link_ok(),
                "arm": self.arm.status().__dict__, "last_estop": self.last_estop}

    @property
    def current_job(self) -> JobContext | None:
        return self._job

    # ---- recorder hooks (TDM-48), episode format = rdf_common.episode_format (TDM-25)
    def add_tick_listener(self, fn: Any) -> None:
        """``fn(tick: ControlTick)`` after every command streamed by ``run_control_loop``.

        Called on the job thread inside the control loop: keep it fast (append to a buffer)."""
        self._tick_listeners.append(fn)

    def remove_tick_listener(self, fn: Any) -> None:
        if fn in self._tick_listeners:
            self._tick_listeners.remove(fn)

    def record_command(self, target: Target, source: str) -> ControlTick:
        """Remember a streamed command (``run_control_loop`` calls this) and notify listeners."""
        if isinstance(target, EETarget):
            pose = target.pose
        else:
            pose = self.arm.fk(target.joints)
        g = target.gripper if target.gripper is not None else (self._last_cmd[1] if self._last_cmd else self.arm.read_state().gripper)
        self._last_cmd = (pose, float(g), source, time.monotonic())
        tick = self.control_tick(_just_streamed=True)
        for fn in list(self._tick_listeners):
            try:
                fn(tick)
            except Exception as e:  # noqa: BLE001 - a recorder bug must not stop the arm loop
                _log("tick listener failed", error=repr(e)[:200])
        return tick

    def control_tick(self, *, _just_streamed: bool = False) -> ControlTick:
        """The current control tick, any time (e.g. once per camera frame).

        ``control_source`` is ``idle`` (target = measured pose) unless a command was streamed within
        the last ~3 control periods and the robot is in a motion state. ``record_command``'s own tick
        (``_just_streamed``) skips the age check: a process stall between recording the command and
        reading the arm must not relabel that streamed command ``idle``.
        """
        st = self.arm.read_state()
        recent = self._last_cmd is not None and (
            _just_streamed or time.monotonic() - self._last_cmd[3] < 3.0 / self.arm.control_rate_hz)
        if recent and self.sm.state in (S.ROLLOUT, S.TELEOP, S.HOMING):
            pose, g, source, _ = self._last_cmd  # type: ignore[misc]
        else:
            pose, g, source = st.ee_pose, st.gripper, "idle"
        return ControlTick(
            timestamp_us=int(time.time() * 1e6), joint_pos=st.joints, gripper_pos=st.gripper,
            ik_target_pos=pose.xyz, ik_target_quat=pose.quat, gripper_cmd=g, control_source=source,
            joint_cmd=self.arm.last_joint_cmd, t_ns=time.monotonic_ns(),
        )

    # ================================================================== subscription callbacks
    def _row_status(self, cid: int) -> str | None:
        row = self.sub.get("robot_command", cid) if self.sub else None
        return row["status"] if row else None

    def _on_applied(self, snapshot: dict[str, list[Any]]) -> None:
        first, self._first_snapshot = self._first_snapshot, False
        cmds = sorted((Command.from_row(r) for r in snapshot.get("robot_command", [])), key=lambda c: c.priority)
        now = time.time()
        job = self._job
        for c in cmds:
            if c.status == "issued":
                if c.is_estop:
                    self._estop_now(c, source="snapshot")
                elif self.max_command_age_s and (c.age_s(now) or 0) > self.max_command_age_s:
                    # boot, or a long link outage: never move the arm on an old command
                    self._boot_stale[c.id] = f"stale: issued {c.age_s(now):.0f}s before the robot client saw it"
                self._enqueue(c)
            elif c.status == "acked" and first:
                self._boot_stale[c.id] = "robot client restarted mid-command"
                self._enqueue(c)
            elif c.status == "preempted" and job is not None and job.command.id == c.id:
                job.cancel.cancel("preempted", "preempted while the link was down")
        for t in snapshot.get("task", []):
            self._check_reset_task(t)
        _log("subscription applied", first=first, commands=len(cmds),
             open=sum(c.status in ("issued", "acked") for c in cmds))

    def _on_insert(self, table: str, row: dict[str, Any], meta: dict[str, Any]) -> None:
        if table == "robot_command":
            c = Command.from_row(row)
            if c.status == "issued" and c.is_estop:
                self._estop_now(c, source="insert")
            if c.status == "issued":
                self._enqueue(c)
        elif table == "task":
            self._check_reset_task(row)

    def _on_update(self, table: str, old: dict[str, Any], new: dict[str, Any], meta: dict[str, Any]) -> None:
        if table == "robot_command":
            if new["status"] == "preempted":
                job = self._job
                if job is not None and job.command.id == int(new["id"]):
                    _log("current job preempted", command=job.command.label(), by=new.get("result_json"))
                    job.cancel.cancel("preempted", str(new.get("result_json") or ""))
        elif table == "task":
            self._check_reset_task(new)

    # ================================================================== soft e-stop fast path
    def _estop_now(self, c: Command, *, source: str) -> None:
        with self._estop_lock:
            if c.id in self._estopped_ids:
                return
            self._estopped_ids.add(c.id)
        t_rx = time.time()
        self.arm.stop()  # FIRST: physical stop, before any bookkeeping or network call
        t_stop = time.time()
        self.sm.lock(f"soft_estop {c.label()} by {c.issued_by or '?'}", command_id=c.id)
        job = self._job
        if job is not None:
            job.cancel.cancel("estop", c.label())
        issued = c.issued_at_us / 1e6 if c.issued_at_us else None
        self.last_estop = {
            "command_id": c.id, "source": source, "preempted_job": job.command.label() if job else None,
            "issue_to_stop_ms": round((t_stop - issued) * 1000, 1) if issued else None,
            "rx_to_stop_ms": round((t_stop - t_rx) * 1000, 2),
        }
        _log("SOFT E-STOP: arm stopped", **self.last_estop)
        if self.estop_auto_clear_s > 0:
            if self._estop_timer:
                self._estop_timer.cancel()
            self._estop_timer = threading.Timer(self.estop_auto_clear_s, self.clear_estop, kwargs={"by": "sim auto-clear"})
            self._estop_timer.daemon = True
            self._estop_timer.start()

    # ================================================================== dispatcher
    def _enqueue(self, c: Command) -> None:
        with self._heap_cv:
            if c.id in self._seen:
                return
            self._seen.add(c.id)
            heapq.heappush(self._heap, (c.priority, c))
            self._heap_cv.notify()

    def _dispatch_loop(self) -> None:
        while not self._stopping.is_set():
            with self._heap_cv:
                while not self._heap and not self._stopping.is_set():
                    self._heap_cv.wait(0.5)
                if self._stopping.is_set():
                    return
                _, c = heapq.heappop(self._heap)
            try:
                self._process(c)
            except Exception as e:  # noqa: BLE001 - never kill the dispatcher
                _log("dispatch error", command=c.label(), error=repr(e)[:300])

    def _process(self, c: Command) -> None:
        if c.id in self._boot_stale:
            self._fail(c, self._boot_stale.pop(c.id))
            return
        status = self._row_status(c.id) or c.status
        if status != "issued":
            _log("refused: not issued (never run)", command=c.label(), status=status)
            return
        if c.is_estop:
            self._estop_now(c, source="dispatch")  # no-op if the fast path already ran
            if self._ack(c):
                self._complete(c, {"estop": True, "state": str(self.sm.state), **(self.last_estop or {})})
            return
        if self.sm.locked:
            self._fail(c, f"robot is locked (soft e-stop: {self.sm.lock_reason}); a human must clear it")
            return
        if c.type not in self.handlers and c.type not in _CONTROLS:
            self._fail(c, f"unknown command type {c.type!r}")
            return
        if not self._ack(c):
            return
        if c.is_job:
            with self._job_lock:
                if self._job is not None:
                    busy = self._job.command.label()
                    self._fail(c, f"another job is running ({busy})")
                    return
                ctx = JobContext(self, c)
                self._job = ctx
                self.sm.job_active = True
            self._dirty.set()
            self._jobs.put(ctx)
        else:
            self._run_control(c)

    def _ack(self, c: Command) -> bool:
        try:
            self.http.call_reducer("ack_robot_command", c.id)
        except Exception as e:  # noqa: BLE001 - preempted between insert and ack: must not run
            _log("ack refused; not running", command=c.label(), error=str(e)[:300])
            return False
        _log("acked", command=c.label(), payload=c.payload)
        return True

    def _complete(self, c: Command, result: dict[str, Any]) -> None:
        try:
            self.http.call_reducer("complete_robot_command", c.id, json.dumps(result, default=str))
            _log("done", command=c.label(), result=result)
        except Exception as e:  # noqa: BLE001
            _log("complete refused", command=c.label(), error=str(e)[:300])

    def _fail(self, c: Command, error: str) -> None:
        try:
            self.http.call_reducer("fail_robot_command", c.id, error[:2000])
            _log("failed", command=c.label(), error=error[:300])
        except Exception as e:  # noqa: BLE001
            _log("fail refused", command=c.label(), error=str(e)[:300])

    # ================================================================== controls
    def _run_control(self, c: Command) -> None:
        job = self._job
        if job is not None:
            job.started.wait(2.0)  # let a just-dispatched job enter its state first
            job.signals.put(c)
        try:
            result = _CONTROLS[c.type](self, c, job)
        except (InvalidTransition, ValueError) as e:
            self._fail(c, f"{c.type} refused in state {self.sm.state}: {e}")
            return
        self._complete(c, {"state": str(self.sm.state), **result})

    def _ctl_pause(self, c: Command, job: JobContext | None) -> dict[str, Any]:
        self.sm.transition(S.PAUSED_FOR_TAKEOVER, "pause_for_takeover", command_id=c.id, allowed_from=[S.ROLLOUT])
        self.arm.hold()
        return {"paused_job": job.command.label() if job else None}

    def _ctl_resume(self, c: Command, job: JobContext | None) -> dict[str, Any]:
        st = self.sm.state
        if st is S.WAITING_RESET:
            n = self._release_resets(ResetDone("resume_policy", c.payload.get("task_id"), c.id))
            return {"released_reset_waiters": n}
        if st in (S.PAUSED_FOR_TAKEOVER, S.TELEOP) and job is not None and job.command.type in ("run_eval", "run_baseline"):
            self.sm.transition(S.ROLLOUT, c.type, command_id=c.id)
            return {"resumed_job": job.command.label()}
        if c.type == "resume_policy" and st in (S.IDLE, S.ROLLOUT, S.HOMING):
            # e.g. the dispatcher's resume after a reset the robot already saw complete: idempotent
            return {"noop": True}
        raise InvalidTransition(f"nothing to resume (state {st}, job {job.command.label() if job else None})")

    def _ctl_stop_teleop(self, c: Command, job: JobContext | None) -> dict[str, Any]:
        if job is not None and job.command.type == "start_teleop":
            job.finish_requested.set()
            return {"ending_job": job.command.label()}
        if self.sm.state is S.TELEOP:  # takeover inside a paused rollout -> back to paused
            self.sm.transition(S.PAUSED_FOR_TAKEOVER, "stop_teleop", command_id=c.id)
            self.arm.hold()
            return {}
        return {"noop": True}

    def _ctl_trial_outcome(self, c: Command, job: JobContext | None) -> dict[str, Any]:
        """TDM-51: operator verdict for the open eval trial (dashboard). ValueError fails the command."""
        from .eval_trials import deliver_outcome

        return deliver_outcome(self, c.payload, source=f"command #{c.id} by {c.issued_by or '?'}")

    def _ctl_stop(self, c: Command, job: JobContext | None) -> dict[str, Any]:
        self.arm.hold()
        if job is not None:
            job.cancel.cancel("stop", c.label())
        return {"stopped_job": job.command.label() if job else None}

    # ================================================================== jobs
    def _job_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                ctx = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._run_job(ctx)

    def _run_job(self, ctx: JobContext) -> None:
        c = ctx.command
        handler = self.handlers[c.type]
        try:
            result = handler(ctx) or {}
            if self._row_status(c.id) == "preempted":
                _log("finished after preemption; left preempted", command=c.label())
            else:
                self._complete(c, {"state": str(self.sm.state), **ctx.result, **result})
        except (Cancelled, ArmStopped) as e:
            reason = e.reason if isinstance(e, Cancelled) else "estop"
            status = self._row_status(c.id)
            _log("job cancelled", command=c.label(), reason=reason, status=status)
            if status == "acked":
                if reason == "stop":
                    self._complete(c, {"aborted": True, "reason": "stop", **ctx.result})
                else:
                    self._fail(c, f"cancelled: {e}")
        except Exception as e:  # noqa: BLE001
            _log("job failed", command=c.label(), error=repr(e)[:500])
            if self._row_status(c.id) == "acked":
                self._fail(c, f"{type(e).__name__}: {e}")
        finally:
            ctx.started.set()
            if not self.sm.locked:
                try:
                    self.arm.hold()
                except Exception:  # noqa: BLE001
                    pass
                if self.sm.state is not S.IDLE:
                    try:
                        self.sm.transition(S.IDLE, f"{c.type} ended", command_id=c.id)
                    except InvalidTransition:
                        pass
            with self._job_lock:
                self._job = None
                self.sm.job_active = False
            self._dirty.set()

    # ================================================================== resets
    def _add_reset_waiter(self, task_id: int | None, accept_task_completion: bool = False) -> _ResetWaiter:
        w = _ResetWaiter(task_id, int(time.time() * 1e6), accept_task_completion)
        with self._reset_lock:
            self._reset_waiters.append(w)
        if self.sub is not None:  # a reset that completed just before we started waiting
            for t in self.sub.rows("task"):
                self._check_reset_task(t)
        return w

    def _remove_reset_waiter(self, w: _ResetWaiter) -> None:
        with self._reset_lock:
            if w in self._reset_waiters:
                self._reset_waiters.remove(w)

    def _release_resets(self, outcome: ResetDone) -> int:
        with self._reset_lock:
            waiters = list(self._reset_waiters)
        for w in waiters:
            w.release(outcome)
        return len(waiters)

    def _check_reset_task(self, task: dict[str, Any]) -> None:
        if task.get("type") != "reset" or task.get("status") != "completed":
            return
        done_us = int(task.get("completed_at") or 0)
        tid = int(task["id"])
        with self._reset_lock:
            waiters = list(self._reset_waiters)
        for w in waiters:
            # 2 s slack for clock skew between the server and this machine; each task releases once
            if not w.accept_task_completion:
                continue
            if (w.task_id is None or w.task_id == tid) and done_us >= w.since_us - 2_000_000:
                with self._reset_lock:
                    if tid in self._consumed_resets:
                        return
                    self._consumed_resets.add(tid)
                w.release(ResetDone("task_completed", tid))
                return

    # ================================================================== status + events
    def _on_transition(self, tr: Transition) -> None:
        # Runs on whatever thread transitioned (maybe the subscription thread): never block here.
        self._outbox.put(("robot.state_changed", {
            "robot_id": self.robot_id, "robot": self.name, "from": str(tr.src), "to": str(tr.dst),
            "reason": tr.reason, "command_id": tr.command_id, "db_status": self.sm.db_status(),
        }))
        self._status_q.put(self.sm.db_status())
        self._dirty.set()

    def _heartbeat_loop(self) -> None:
        next_beat = 0.0
        while not self._stopping.is_set():
            self._dirty.wait(max(0.0, next_beat - time.monotonic()))
            self._dirty.clear()
            if self._stopping.is_set():
                return
            # every distinct status a transition passed through (so homing->idle shows as
            # busy->online even if both happened between two wake-ups), then the current one
            pending: list[str] = []
            while True:
                try:
                    pending.append(self._status_q.get_nowait())
                except queue.Empty:
                    break
            pending.append(self.sm.db_status())
            for i, status in enumerate(pending):
                last = i == len(pending) - 1
                if status == self._last_sent_status and not (last and time.monotonic() >= next_beat):
                    continue
                try:
                    self.http.call_reducer("robot_heartbeat", self.robot_id, status)
                    self._last_sent_status = status
                except Exception as e:  # noqa: BLE001
                    _log("heartbeat failed", error=str(e)[:200])
                next_beat = time.monotonic() + self.heartbeat_s
            self._flush_outbox()

    def _flush_outbox(self) -> None:
        while True:
            try:
                type_, payload = self._outbox.get_nowait()
            except queue.Empty:
                return
            try:
                self.http.call_reducer("append_event", self.actor, type_, json.dumps(payload, default=str))
            except Exception as e:  # noqa: BLE001 - evidence is best effort, never kills the robot
                _log("append_event failed", type=type_, error=str(e)[:200])


_CONTROLS = {
    "pause_for_takeover": RobotClient._ctl_pause,
    "resume_policy": RobotClient._ctl_resume,
    "handback": RobotClient._ctl_resume,
    "stop_teleop": RobotClient._ctl_stop_teleop,
    "stop": RobotClient._ctl_stop,
    "trial_outcome": RobotClient._ctl_trial_outcome,  # TDM-51
}
assert SOFT_ESTOP not in _CONTROLS

__all__ = ["RobotClient"]
