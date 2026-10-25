"""Agent workbench steps: the short "what I'm doing now" lines each station's agent writes.

The cockpit's AgentWorkbench (apps/web/src/components/cockpit/AgentWorkbench.tsx) draws a droid for the
current station and a Claude-Code-style transcript next to it. Its lines are event_log rows
``agent.step`` / ``agent.thought`` {ticket_id, station, agent, text, ...}, written through the shared LLM
client's agent sink (``rdf_common.llm.agent_event``: paced to one row per 1.5 s per agent, off with
``RDF_AGENT_THOUGHTS=0``, a no-op when no sink is registered, as in tests). LLM calls made inside
``work(...)`` add their own start / heartbeat / thought rows.

    with agent_steps.work(ticket_id, "diagnose"):
        agent_steps.step("Reading 10 baseline trials: 6 failed")
        ...                                  # Claude call: "Asking Claude", "Still thinking… 10s", thoughts
        agent_steps.step("Grouped 6 failures into 2 causes", decision=True)

Stations are the cockpit's: baseline, diagnose, plan_data, collect, review, train, eval, ship.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

from rdf_common import llm

# station -> the agent that works it (the web's AgentWorkbench and FactoryIso use the same keys)
AGENT_OF = {
    "baseline": "eval_runner",
    "diagnose": "clusterer",
    "plan_data": "planner",
    "collect": "dispatcher",
    "review": "reviewer",
    "train": "trainer",
    "eval": "eval_runner",
    "ship": "flywheel",
}


@contextlib.contextmanager
def work(ticket_id: Any, station: str, agent: str | None = None, **extra: Any):
    with llm.agent_context(ticket_id=ticket_id, station=station, agent=agent or AGENT_OF.get(station, station),
                           **extra) as ctx:
        yield ctx


def step(text: str, *, ticket_id: Any = None, station: str | None = None, agent: str | None = None,
         decision: bool = False, error: bool = False, **extra: Any) -> bool:
    """One ``agent.step`` row (``decision`` -> the transcript's arrow line, ``error`` -> a failure line).
    Never raises."""
    try:
        if station and not agent:
            agent = AGENT_OF.get(station, station)
        return llm.agent_event("step", text, ticket_id=ticket_id, station=station, agent=agent,
                               decision=decision or None, is_error=error or None, **extra)
    except Exception:  # noqa: BLE001 - narration never breaks a station
        return False


def thought(text: str, **kw: Any) -> bool:
    try:
        st = kw.get("station")
        if st and not kw.get("agent"):
            kw["agent"] = AGENT_OF.get(st, st)
        return llm.agent_event("thought", text, **kw)
    except Exception:  # noqa: BLE001
        return False


class Every:
    """At most one call per ``seconds`` per key (training progress, long waits)."""

    def __init__(self, seconds: float) -> None:
        self.seconds, self._last, self._lock = seconds, {}, threading.Lock()

    def due(self, key: Any) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(key, -1e9) < self.seconds:
                return False
            self._last[key] = now
            return True


def plural(k: int, one: str, many: str | None = None) -> str:
    return f"{k} {one if k == 1 else (many or one + 's')}"


def clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {(s // 60) % 60:02d}m"
