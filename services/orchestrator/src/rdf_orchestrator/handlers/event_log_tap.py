"""Tap on event_log: one structured log line per new event (proves the subscription runtime, and
gives the orchestrator's own log a copy of what every service did)."""
from __future__ import annotations

from ..bus import Event, on_insert
from ..log import get_logger

log = get_logger("event_log")


@on_insert("event_log", name="event_log_tap")
async def event_log_tap(ev: Event) -> None:
    r = ev.row
    sts = ev.server_ts_micros or r.get("ts")
    log.info("event_log.insert", extra={"fields": {
        "event_id": r.get("id"), "actor": r.get("actor"), "type": r.get("type"), "source": ev.source,
        "server_ts_micros": sts,
        "lag_ms": round(ev.received_at * 1000 - sts / 1000, 1) if isinstance(sts, int) else None,
    }})
