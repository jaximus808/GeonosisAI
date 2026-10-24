"""Mirror droid sessions into SpacetimeDB (TDM-13 agent_session reducers + TDM-15 event_log).

Reducers used (positional args, see spacetime/schema.md):
  upsert_agent_session(id, role, factory_session_id, status, current_step, name, ticket_id)
  agent_wait_for(session_id, predicate_json, resume_payload_json)
  agent_resume(session_id, new_factory_session_id, reason)
  agent_step(session_id, step_text)
  append_event(actor, type, payload_json)

The caller identity needs the ``factory`` (or ``orchestrator``) service role; owner also passes.
Transport errors and 5xx are retried with backoff. A reducer that *throws* (530) is not retried:
it is reported to the caller for state transitions and only logged for steps/events, so a
SpacetimeDB hiccup never kills a running droid turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeError

log = logging.getLogger("rdf_factory.mirror")

STEP_MAX = 500  # current_step length kept on the row


class ResumeConflict(RuntimeError):
    """agent_resume refused: another bridge already claimed the wake-up, or the row is not waiting."""


class Mirror:
    def __init__(self, stdb: AsyncSpacetimeClient | None, *, retries: int = 3):
        self.stdb = stdb
        self.retries = retries
        self._lock = asyncio.Lock()  # keep reducer calls in order

    @property
    def enabled(self) -> bool:
        return self.stdb is not None

    async def _call(self, reducer: str, *args: Any, critical: bool = False) -> bool:
        if self.stdb is None:
            return False
        delay = 0.5
        async with self._lock:
            for attempt in range(self.retries + 1):
                try:
                    await self.stdb.call_reducer(reducer, *args)
                    return True
                except SpacetimeError as e:
                    retriable = e.status >= 500 and e.status != 530
                    if not retriable or attempt == self.retries:
                        if critical:
                            raise
                        log.warning("reducer %s failed: %s", reducer, e)
                        return False
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    if attempt == self.retries:
                        if critical:
                            raise
                        log.warning("reducer %s transport error: %s", reducer, e)
                        return False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)
        return False

    # ------------------------------------------------------------------ reads

    async def get(self, agent_session_id: int) -> dict | None:
        if self.stdb is None:
            return None
        rows = await self.stdb.sql(f"SELECT * FROM agent_session WHERE id = {int(agent_session_id)}")
        return rows[0] if rows else None

    # ------------------------------------------------------------------ writes

    async def create(self, role: str, name: str, ticket_id: int, step: str) -> int:
        """Create the row (status starting) and return its id (looked up by the unique name)."""
        if self.stdb is None:
            return 0
        await self._call("upsert_agent_session", 0, role, "", "starting", step[:STEP_MAX], name,
                         int(ticket_id), critical=True)
        safe = name.replace("'", "''")
        rows = await self.stdb.sql(f"SELECT * FROM agent_session WHERE name = '{safe}'")
        if not rows:
            raise RuntimeError(f"agent_session {name!r} not found after create")
        return int(max(rows, key=lambda r: int(r["id"]))["id"])

    async def upsert(self, agent_session_id: int, role: str, status: str, step: str,
                     factory_session_id: str = "", *, critical: bool = False) -> bool:
        return await self._call("upsert_agent_session", int(agent_session_id), role, factory_session_id or "",
                                status, step[:STEP_MAX], "", 0, critical=critical)

    async def step(self, agent_session_id: int, text: str) -> bool:
        return await self._call("agent_step", int(agent_session_id), text[:STEP_MAX])

    async def wait_for(self, agent_session_id: int, predicate: dict, resume_payload: Any | None) -> bool:
        payload = "" if resume_payload is None else json.dumps(resume_payload, default=str)
        return await self._call("agent_wait_for", int(agent_session_id), json.dumps(predicate), payload,
                                critical=True)

    async def resume_claim(self, agent_session_id: int, reason: str, new_factory_session_id: str = "") -> None:
        try:
            await self._call("agent_resume", int(agent_session_id), new_factory_session_id, reason[:500],
                             critical=True)
        except SpacetimeError as e:
            if e.status == 530:
                raise ResumeConflict(e.message) from e
            raise

    async def event(self, actor: str, type_: str, payload_json: str) -> bool:
        return await self._call("append_event", actor, type_, payload_json)
