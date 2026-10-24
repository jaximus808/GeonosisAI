"""TDM-62: escalate a reset task nobody claims.

The TDM-26 dispatcher posts a $2 reset task and pages contributors once. If the task is still
``open`` (unclaimed) after ``RDF_RESET_ESCALATE_S`` (default 90 s) it is paged again and
``dispatch.reset_escalated`` {level: 1} is logged; after twice that, level 2 pages again and the
event tells the operator to reset the scene themselves (the robot holds in ``waiting_reset`` the
whole time, so an eval only loses wall-clock). The reset page shows the escalation live.

Restart-safe: the level already reached is read back from ``dispatch.reset_escalated`` rows when the
bus mirrors ``event_log``, and timers are re-armed from ``task.created_at`` on the boot snapshot.

Env: ``RDF_RESET_ESCALATE_S`` (90; 0 disables), ``RDF_RESET_ESCALATE_LEVELS`` (2).
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from .dispatcher import PRICES_CENTS, Dispatcher, Mirror
from .log import get_logger

log = get_logger("reset_watch")

EVENT = "dispatch.reset_escalated"


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class ResetWatch:
    def __init__(self, mirror: Mirror, dispatcher: Callable[[], Dispatcher], *, escalate_s: float | None = None,
                 levels: int | None = None, clock: Callable[[], float] = time.time) -> None:
        self.m = mirror
        self._d = dispatcher
        self.escalate_s = _env_float("RDF_RESET_ESCALATE_S", 90.0) if escalate_s is None else escalate_s
        self.levels = int(_env_float("RDF_RESET_ESCALATE_LEVELS", 2)) if levels is None else levels
        self.clock = clock
        self._done: dict[int, int] = {}
        self._lock = threading.Lock()
        self._timers: list[threading.Timer] = []

    @property
    def enabled(self) -> bool:
        return self.escalate_s > 0 and self.levels > 0

    def _logged_level(self, task_id: int) -> int:
        try:
            rows = self.m.rows("event_log")
        except Exception:  # noqa: BLE001 - event_log not mirrored: memory only
            return 0
        best = 0
        for e in rows:
            if e.get("type") != EVENT:
                continue
            try:
                p = json.loads(e.get("payload_json") or "{}")
            except ValueError:
                continue
            if _i(p.get("task_id")) == task_id:
                best = max(best, _i(p.get("level")))
        return best

    def age_s(self, task: dict) -> float:
        return self.clock() - _i(task.get("created_at")) / 1e6

    def schedule(self, task: dict) -> list[float]:
        """Arm one timer per escalation level still ahead of this open reset task."""
        if not self.enabled:
            return []
        task_id, age = _i(task["id"]), self.age_s(task)
        delays = []
        for level in range(1, self.levels + 1):
            delay = level * self.escalate_s - age
            if delay < -self.escalate_s:  # long past (boot snapshot of an old row): one catch-up check
                continue
            delays.append(max(0.0, delay))
        for delay in sorted(set(round(d, 3) for d in delays)) or [0.0]:
            t = threading.Timer(delay + 0.05, self.check, args=(task_id,))
            t.daemon = True
            t.start()
            self._timers.append(t)
        self._timers = [t for t in self._timers if t.is_alive()]
        return delays

    def check(self, task_id: int) -> int | None:
        """Escalate ``task_id`` to the level its unclaimed age has reached. Returns the new level."""
        task = self.m.get("task", task_id)
        if task is None or task.get("type") != "reset" or task.get("status") != "open" or not self.enabled:
            return None
        level = min(self.levels, int(self.age_s(task) // self.escalate_s))
        with self._lock:
            done = max(self._done.get(task_id, 0), self._logged_level(task_id))
            if level <= done:
                return None
            self._done[task_id] = level
        d = self._d()
        tid, rid = _i(task.get("ticket_id")), _i(task.get("robot_id"))
        waited = round(self.age_s(task))
        last = level >= self.levels
        note = (f"nobody has claimed the reset after {waited} s; operator: reset the scene yourself (claim the "
                f"task on /reset/{task_id}) or cancel the eval" if last else
                f"nobody has claimed the reset after {waited} s; paging again")
        d.event(EVENT, {"task_id": task_id, "ticket_id": tid, "robot_id": rid, "level": level, "waited_s": waited,
                        "final": last, "next_in_s": None if last else round(self.escalate_s),
                        "page": f"/reset/{task_id}", "note": note})
        try:
            d.page(tid, [task_id], "reset", _i(task.get("price_cents")) or PRICES_CENTS["reset"])
        except Exception as e:  # noqa: BLE001 - paging is best-effort
            log.warning("reset.escalate_page_failed", extra={"fields": {"task_id": task_id, "error": str(e)[:200]}})
        log.info("reset.escalated", extra={"fields": {"task_id": task_id, "level": level, "waited_s": waited}})
        return level

    def stop(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers.clear()


_instances: dict[int, ResetWatch] = {}
_instances_lock = threading.Lock()


def get_watch(bus: Any) -> ResetWatch:
    from .dispatcher import get_dispatcher

    with _instances_lock:
        w = _instances.get(id(bus))
        if w is None or w.m is not bus:
            w = _instances[id(bus)] = ResetWatch(bus, lambda: get_dispatcher(bus))
        return w


__all__ = ["EVENT", "ResetWatch", "get_watch"]
