"""TDM-114: a SIM robot clears its soft e-stop when an operator asks from the web.

Nobody stands at a simulated arm, so "a person at the arm types clear" is a dead end for the sim robot.
The orchestrator's ``POST /robots/{id}/estop/clear`` (operator key, sim robots only) appends
``robot.estop_clear_requested`` {robot_id, by}. This watcher subscribes to those rows and, for a new one
naming this robot written by an orchestrator actor, calls ``client.clear_estop(by=...)``, which logs
``robot.estop_cleared``. No new robot_command type (the reducer refuses commands while in estop), so no
module publish.

Only the sim client starts it (``main.py``: ``--sim`` / ``--adapter mujoco``); the real arm never does.
Rows already in the table when it subscribes are ignored (no replay of an old request after a restart).
"""
from __future__ import annotations

import json
import threading
from typing import Any

from rdf_common.spacetime_client import Subscription

from .loop import _log

EV_CLEAR_REQUESTED = "robot.estop_clear_requested"


class Tdm114EstopWatch:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.sub: Subscription | None = None
        self.cleared: list[int] = []

    def start(self) -> "Tdm114EstopWatch":
        self.sub = Subscription([f"SELECT * FROM event_log WHERE type = '{EV_CLEAR_REQUESTED}'"],
                                on_insert=self._on_insert,
                                on_error=lambda e: _log("estop-clear watch subscription error", error=str(e)[:300]),
                                config=self.client.stdb, max_backoff=10.0).start_thread()
        return self

    def stop(self) -> None:
        if self.sub is not None:
            self.sub.stop()

    def handle(self, row: dict[str, Any]) -> bool:
        if row.get("type") != EV_CLEAR_REQUESTED or not str(row.get("actor") or "").startswith("orchestrator"):
            return False
        try:
            p = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            return False
        if not isinstance(p, dict) or self.client.robot_id is None or int(p.get("robot_id") or 0) != int(self.client.robot_id):
            return False
        by = str(p.get("by") or "operator")[:60]
        ok = bool(self.client.clear_estop(by=f"web operator {by} (sim robot, event #{row.get('id')})"))
        if ok:
            self.cleared.append(int(row.get("id") or 0))
        else:
            _log("estop-clear request: robot was not locked", by=by)
        return ok

    def _on_insert(self, table: str, row: dict[str, Any], meta: dict[str, Any]) -> None:
        if table == "event_log":
            threading.Thread(target=self.handle, args=(dict(row),), daemon=True).start()


def start_tdm114_estop_watch(client: Any) -> Tdm114EstopWatch:
    w = Tdm114EstopWatch(client)
    client.services["tdm114_estop_watch"] = w
    return w.start()


__all__ = ["EV_CLEAR_REQUESTED", "Tdm114EstopWatch", "start_tdm114_estop_watch"]
