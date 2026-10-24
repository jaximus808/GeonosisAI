"""TDM-58: recording controls on the teleop ``/control`` socket -> the TDM-48 recorder.

The browser's ``<RecordingControls/>`` (apps/web/src/components/teleop/RecordingControls.tsx) sends
these messages on the ``/control`` WebSocket (docs/WORKER_GUIDE.md, "Teleop contract"). The socket
server (TDM-52's teleop executor) owns auth, the clutch and motion; it forwards every message whose
``t`` is in ``RECORDING_MESSAGES`` to ``RecordingControl.handle`` and merges ``RecordingControl.state()``
into its ~10 Hz ``{t:"state", ...}`` as ``recording``::

    from rdf_robot_client.recorder import recorder_for
    from rdf_robot_client.tdm58_recording import RECORDING_MESSAGES, RecordingControl, lookup_task

    rc = RecordingControl(recorder_for(ctx), lambda: lookup_task(ctx.client.sql, claims.task_id),
                          on_takeover=pause_policy_and_give_human_control,   # executor's own transitions
                          on_handback=resume_policy)
    ...
    if msg["t"] in RECORDING_MESSAGES:
        await ws.send(json.dumps(await rc.handle_async(msg)))   # stop() blocks ~1 s: off the event loop
    state = {"t": "state", ..., "control_source": rc.control_source() or executor_source,
             "recording": rc.state()}
    ...
    rc.abandon("operator disconnected")      # socket gone mid-recording: discard, never upload half a demo

Messages (browser -> robot; ``req`` is echoed back so the page can match replies):

  {t:"record_start", req?}                 begin the episode. kind comes from the TASK (clean_teleop or
                                           recovery_takeover), never from the browser. For a recovery
                                           task the policy is driving: ``on_record_start(kind)`` lets the
                                           executor resume it if the robot sits paused_for_takeover, so
                                           the episode opens with policy frames (the reviewer needs
                                           >= 0.5 s of them before takeover_ts).
  {t:"takeover", req?}                     recovery only: ``on_takeover()`` (pause policy, human gets
                                           control) THEN ``recorder.mark_takeover()``. Refused before
                                           ``min_pre_takeover_s`` of policy frames.
  {t:"handback", req?}                     recovery only: ``recorder.mark_handback()`` THEN
                                           ``on_handback()`` (resume_policy). Refused before
                                           ``min_takeover_s`` (the reviewer's minimum human segment).
  {t:"record_stop", success:true, note?}   finish: ``recorder.stop(success, note)`` -> local files, upload
                                           queued (signed PUTs + create_episode in the background).
                                           Recovery: refused while the human still has control.
  {t:"record_discard", reason?}            ``recorder.abort()``: files deleted, nothing uploaded, the
                                           claim is untouched (the operator can record again).

Adopted episodes (TDM-69 DAgger interventions): when the robot is already recording a
recovery_takeover episode FOR THIS TASK that someone else started (the rollout runner records the
paused rollout itself), the control adopts it: takeover / handback mark that recording, ``owner`` is
``"rollout"``, and record_stop / record_discard are refused (the rollout ends the trial). When it
stops, ``last`` reports it like an own recording (upload progress included).

Reply (robot -> browser): ``{t:"record_ack", op, req, ok, error?, recording: <state()>}``.

``state()`` (also the ``recording`` field of ``{t:"state"}``)::

  {"v": 1, "active": bool, "phase": "idle"|"recording"|"policy"|"human"|"handed_back",
   "kind", "key", "owner" (operator|rollout), "ticket_id", "task_id", "started_at_us", "elapsed_ms", "frames",
   "takeover_ts", "handback_ts" (unix us, the recorder's clock = actions.parquet timestamp_us),
   "takeover_at_ms", "handback_at_ms" (offsets from the start, for display),
   "can": {"start","takeover","handback","stop","discard"},
   "last": null | {"key", "kind", "status" (recorded|failed|empty|discarded), "problems", "frame_count",
                   "duration_ms", "success", "takeover_ts", "handback_ts", "stopped_at_us",
                   "fixture" (sim arm / synthetic cameras), "upload": {"state": none|pending|retrying|done|failed, "files_done", "files_total",
                              "attempts", "error", "episode_id", "storage_uri", "next_retry_in_s"}}}

Timestamps come from the recorder on the robot (one monotonic clock), never from the browser.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

RECORDING_MESSAGES = frozenset({"record_start", "record_stop", "record_discard", "takeover", "handback"})
RECORDABLE_KINDS = ("clean_teleop", "recovery_takeover")
TAKEOVER_KIND = "recovery_takeover"


@dataclass(frozen=True)
class RecordingTarget:
    """Who/what the episode belongs to (from the task row, i.e. the token's task)."""

    ticket_id: int
    task_id: int
    contributor_id: int
    kind: str  # task.type: clean_teleop | recovery_takeover


def target_from_task_row(row: dict[str, Any]) -> RecordingTarget:
    return RecordingTarget(ticket_id=int(row["ticket_id"]), task_id=int(row["id"]),
                           contributor_id=int(row.get("assignee_id") or 0), kind=str(row["type"]))


def lookup_task(sql: Callable[[str], list[dict[str, Any]]], task_id: int) -> RecordingTarget:
    """Resolve the recording target from SpacetimeDB (``RobotClient.sql`` / ``SpacetimeClient.sql``)."""
    rows = sql(f"SELECT * FROM task WHERE id = {int(task_id)}")
    if not rows:
        raise LookupError(f"task {task_id} not found")
    return target_from_task_row(rows[0])


class RecordingError(RuntimeError):
    """A refused recording message (the reason goes back to the browser as ``error``)."""


class RecordingControl:
    """Maps the recording messages to one ``Recorder`` for one task. Thread-safe."""

    def __init__(self, recorder: Any, target: RecordingTarget | Callable[[], RecordingTarget], *,
                 on_record_start: Callable[[str], None] | None = None,
                 on_takeover: Callable[[], None] | None = None,
                 on_handback: Callable[[], None] | None = None,
                 on_record_end: Callable[[dict[str, Any]], None] | None = None,
                 min_pre_takeover_s: float = 1.0, min_takeover_s: float = 1.0) -> None:
        if recorder is None:
            raise ValueError("no recorder (the robot client runs without cameras?)")
        self.recorder = recorder
        self._target_src = target
        self._target: RecordingTarget | None = target if isinstance(target, RecordingTarget) else None
        self.on_record_start, self.on_takeover, self.on_handback = on_record_start, on_takeover, on_handback
        self.on_record_end = on_record_end
        self.min_pre_takeover_s, self.min_takeover_s = float(min_pre_takeover_s), float(min_takeover_s)
        self._lock = threading.RLock()
        self._mine: str | None = None  # key of the recording this control started
        self._kind: str | None = None
        self._start_mono = 0.0
        self._started_at_us: int | None = None
        self._tk_mono: float | None = None
        self._hb_mono: float | None = None
        self._tk_ts: int | None = None
        self._hb_ts: int | None = None
        self._owned = False  # True: this control started it; False: adopted (a paused rollout's recording)
        self._last: dict[str, Any] | None = None
        self._last_dir: str | None = None

    # ------------------------------------------------------------------ helpers
    def target(self) -> RecordingTarget:
        if self._target is None:
            self._target = self._target_src()  # type: ignore[operator]
        return self._target

    @property
    def active(self) -> bool:
        return self._mine is not None and self.recorder.recording and self.recorder.current_key == self._mine

    def _sync(self) -> None:
        """Follow the recorder: adopt a recovery_takeover recording someone else started for this task
        (TDM-69: the rollout runner records a paused rollout itself; the operator only takes over and
        hands back), and pick up the result when a recording ended outside ``record_stop``."""
        if self._mine is not None and not self.active:
            last = getattr(self.recorder, "last", None)
            if last is not None and last.meta.get("episode_key") == self._mine:
                self._set_last(last.meta, last.dir)
            self._mine = None
        if self._mine is None and self.recorder.recording:
            sess = getattr(self.recorder, "_s", None)
            tg = self._target
            if tg is None:
                try:
                    tg = self.target()
                except Exception:  # noqa: BLE001 - lookup failure: nothing to adopt yet
                    return
            info = getattr(sess, "info", None) or {}
            if sess is not None and int(info.get("task_id") or 0) == tg.task_id and info.get("kind") == TAKEOVER_KIND:
                self._mine, self._kind, self._owned = sess.key, TAKEOVER_KIND, False
                self._start_mono = sess.start_ns / 1e9  # time.monotonic() and monotonic_ns share a clock
                self._started_at_us = sess.anchor.wall_us_of(sess.start_ns)
                self._tk_mono = sess.takeover_ns / 1e9 if sess.takeover_ns else None
                self._hb_mono = sess.handback_ns / 1e9 if sess.handback_ns else None
                self._tk_ts = sess.anchor.wall_us_of(sess.takeover_ns) if sess.takeover_ns else None
                self._hb_ts = sess.anchor.wall_us_of(sess.handback_ns) if sess.handback_ns else None

    def _set_last(self, m: dict[str, Any], d: Any) -> None:
        self._last = {"key": m["episode_key"], "kind": m["kind"], "status": m["status"],
                      "problems": m.get("problems") or [], "frame_count": m.get("frame_count", 0),
                      "duration_ms": m.get("duration_ms", 0), "success": m.get("success"),
                      "takeover_ts": m.get("takeover_ts"), "handback_ts": m.get("handback_ts"),
                      "stopped_at_us": m.get("stopped_at_us"), "fixture": bool(m.get("fixture")),
                      "files_total": len(m.get("files") or [])}
        self._last_dir = str(d)

    def phase(self) -> str:
        if not self.active:
            return "idle"
        if self._kind != TAKEOVER_KIND:
            return "recording"
        if self._tk_mono is None:
            return "policy"
        return "human" if self._hb_mono is None else "handed_back"

    def control_source(self) -> str | None:
        """Who has authority according to the recording: 'human' during a clean demo or a takeover,
        'policy' before takeover / after handback, None when not recording (executor decides)."""
        p = self.phase()
        return {"recording": "human", "human": "human", "policy": "policy", "handed_back": "policy"}.get(p)

    def _frames(self) -> int:
        s = getattr(self.recorder, "_s", None)  # TDM-48 session: rows appended by its writer thread
        try:
            return len(s.rows["t_ns"]) if s is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------ messages
    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = str(msg.get("t") or "")
        reply: dict[str, Any] = {"t": "record_ack", "op": op, "req": msg.get("req"), "ok": True}
        try:
            if op not in RECORDING_MESSAGES:
                raise RecordingError(f"not a recording message: {op!r}")
            with self._lock:
                self._sync()
                getattr(self, "_op_" + op)(msg)
        except (RecordingError, LookupError) as e:
            reply.update(ok=False, error=str(e))
        except Exception as e:  # noqa: BLE001 - a recorder/hook failure must reach the operator, not kill the socket
            reply.update(ok=False, error=f"{type(e).__name__}: {e}"[:300])
        reply["recording"] = self.state()
        return reply

    async def handle_async(self, msg: dict[str, Any]) -> dict[str, Any]:
        """``handle`` on a worker thread (record_stop waits ~1 s for the encoders to flush)."""
        return await asyncio.to_thread(self.handle, msg)

    def _op_record_start(self, msg: dict[str, Any]) -> None:
        if self.active:
            raise RecordingError("already recording")
        if self.recorder.recording:
            raise RecordingError(f"the robot is busy recording another episode ({self.recorder.current_key})")
        tg = self.target()
        if tg.kind not in RECORDABLE_KINDS:
            raise RecordingError(f"task {tg.task_id} is a {tg.kind} task; only {'/'.join(RECORDABLE_KINDS)} record")
        if self.on_record_start:
            self.on_record_start(tg.kind)
        note = str(msg.get("note") or f"{tg.kind} via web recording controls (task {tg.task_id})")[:200]
        key = self.recorder.start(tg.ticket_id, tg.kind, tg.task_id, tg.contributor_id, note=note)
        self._mine, self._kind, self._owned = key, tg.kind, True
        self._start_mono = time.monotonic()
        s = getattr(self.recorder, "_s", None)
        self._started_at_us = s.anchor.wall_us_of(s.start_ns) if s is not None else int(time.time() * 1e6)
        self._tk_mono = self._hb_mono = None
        self._tk_ts = self._hb_ts = None

    def _require_takeover_kind(self) -> None:
        if not self.active:
            raise RecordingError("not recording")
        if self._kind != TAKEOVER_KIND:
            raise RecordingError("takeover/handback only apply to recovery_takeover tasks")

    def _op_takeover(self, _msg: dict[str, Any]) -> None:
        self._require_takeover_kind()
        if self._tk_mono is not None and self._hb_mono is None:
            raise RecordingError("you already have control")
        if self._hb_mono is not None:
            raise RecordingError("already handed back; finish or discard this episode")
        ran = time.monotonic() - self._start_mono
        if ran < self.min_pre_takeover_s:
            raise RecordingError(f"let the policy run {self.min_pre_takeover_s:g} s first "
                                 f"(the reviewer needs the failure before the takeover); {ran:.1f} s so far")
        if self.on_takeover:
            self.on_takeover()  # pause the policy / give the human control first, then stamp
        self._tk_ts = int(self.recorder.mark_takeover())
        self._tk_mono = time.monotonic()

    def _op_handback(self, _msg: dict[str, Any]) -> None:
        self._require_takeover_kind()
        if self._tk_mono is None:
            raise RecordingError("take over first")
        if self._hb_mono is not None:
            raise RecordingError("already handed back")
        held = time.monotonic() - self._tk_mono
        if held < self.min_takeover_s:
            raise RecordingError(f"a takeover must last at least {self.min_takeover_s:g} s ({held:.1f} s so far)")
        self._hb_ts = int(self.recorder.mark_handback())  # stamp first, then the policy resumes
        self._hb_mono = time.monotonic()
        if self.on_handback:
            self.on_handback()

    def _op_record_stop(self, msg: dict[str, Any]) -> None:
        if not self.active:
            raise RecordingError("not recording")
        if not self._owned:
            raise RecordingError("this episode belongs to the paused rollout; it ends when the rollout finishes the trial")
        success = msg.get("success")
        if not isinstance(success, bool):
            raise RecordingError("record_stop needs success: true|false (the operator confirms the outcome)")
        if self._kind == TAKEOVER_KIND and self.phase() == "human":
            raise RecordingError("hand control back to the policy before finishing")
        if self._kind == TAKEOVER_KIND and self._tk_mono is None:
            raise RecordingError("no takeover in this episode; take over (or discard)")
        note = str(msg.get("note") or "")[:500]
        ep = self.recorder.stop(success=success, note=note)
        self._set_last(ep.meta, ep.dir)
        self._mine = None
        if self.on_record_end:
            self.on_record_end(dict(self._last))

    def _op_record_discard(self, msg: dict[str, Any]) -> None:
        if not self.active:
            raise RecordingError("not recording")
        if not self._owned:
            raise RecordingError("this episode belongs to the paused rollout; hand back instead")
        key = self._mine
        kind = self._kind
        self.recorder.abort(str(msg.get("reason") or "discarded by operator")[:200])
        self._mine = None
        self._last = {"key": key, "kind": kind, "status": "discarded", "problems": [], "frame_count": 0,
                      "duration_ms": 0, "success": None, "takeover_ts": None, "handback_ts": None,
                      "stopped_at_us": int(time.time() * 1e6), "fixture": False, "files_total": 0}
        self._last_dir = None
        if self.on_record_end:
            self.on_record_end(dict(self._last))

    def abandon(self, reason: str = "operator disconnected") -> bool:
        """Discard a recording this control started (socket closed, lease lost). True if one was running."""
        with self._lock:
            if not self.active or not self._owned:
                return False
            self._op_record_discard({"reason": reason})
            return True

    # ------------------------------------------------------------------ state
    def upload_status(self) -> dict[str, Any] | None:
        if not self._last or not self._last_dir:
            return None
        up = getattr(self.recorder, "uploader", None)
        if up is None:
            return {"state": "none", "files_done": 0, "files_total": self._last.get("files_total", 0)}
        st = up.status(self._last_dir) or {}
        return {"state": st.get("state") or "pending", "files_done": len(st.get("uploaded") or {}),
                "files_total": self._last.get("files_total", 0), "attempts": int(st.get("attempts") or 0),
                "error": st.get("error"), "episode_id": st.get("episode_id"),
                "storage_uri": st.get("storage_uri"), "next_retry_in_s": st.get("next_retry_in_s")}

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._sync()
            active = self.active
            phase = self.phase()
            tg = self._target
            elapsed = int((time.monotonic() - self._start_mono) * 1000) if active else 0

            def off(m: float | None) -> int | None:
                return int((m - self._start_mono) * 1000) if (active and m is not None) else None

            last = None
            if self._last is not None:
                last = {k: v for k, v in self._last.items() if k != "files_total"}
                last["upload"] = self.upload_status() or {"state": "none", "files_done": 0, "files_total": 0}
            busy_other = (not active) and bool(self.recorder.recording)
            return {
                "v": 1, "active": active, "phase": phase,
                "kind": (self._kind if active else (tg.kind if tg else None)),
                "key": self._mine if active else None,
                "owner": ("operator" if self._owned else "rollout") if active else None,
                "ticket_id": tg.ticket_id if tg else None, "task_id": tg.task_id if tg else None,
                "started_at_us": self._started_at_us if active else None,
                "elapsed_ms": elapsed, "frames": self._frames() if active else 0,
                "takeover_ts": self._tk_ts if active else None, "handback_ts": self._hb_ts if active else None,
                "takeover_at_ms": off(self._tk_mono), "handback_at_ms": off(self._hb_mono),
                "can": {"start": not active and not busy_other,
                        "takeover": phase == "policy" and elapsed >= self.min_pre_takeover_s * 1000,
                        "handback": phase == "human" and self._tk_mono is not None
                        and time.monotonic() - self._tk_mono >= self.min_takeover_s,
                        "stop": phase in ("recording", "handed_back") and self._owned,
                        "discard": active and self._owned},
                "last": last,
            }


__all__ = ["RECORDABLE_KINDS", "RECORDING_MESSAGES", "RecordingControl", "RecordingError", "RecordingTarget",
           "lookup_task", "target_from_task_row"]
