"""TDM-68 ``GET /tickets/{ticket_id}/decision``: the evidence trail behind the data-type decision.

Returns ``decision.assemble``'s chain (source eval -> failed trials -> clusters with example episodes
-> diagnosis reasoning -> data_plan + verbatim planner reasoning + evidence -> approvals -> tasks ->
episodes bought -> training runs -> after evals), plus per-link checks and a ``missing`` list.
Read-only: it never calls a reducer. Mounted from ``app.py``; the dashboard's ``DecisionCard``
(``apps/web/src/components/ticket/DecisionCard.tsx``) and the MCP connector read it.
"""
from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException

from . import decision

decision_router = APIRouter(tags=["tickets"])


class Tdm68Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...


_tdm68_client: Any = None


async def get_decision_stdb() -> Tdm68Stdb:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm68_client
    if _tdm68_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm68_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm68_client


@decision_router.get("/tickets/{ticket_id}/decision")
async def get_decision(ticket_id: int, stdb: Tdm68Stdb = Depends(get_decision_stdb)) -> dict:
    """The recovery-vs-clean decision for one ticket, every step linked to real rows by id."""
    try:
        tables = await decision.load_tables(stdb.sql, ticket_id)
    except Exception as e:  # noqa: BLE001 - surface the outage, never a partial chain
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}: {e}") from None
    try:
        return decision.assemble(tables, ticket_id)
    except LookupError:
        raise HTTPException(404, f"No ticket {ticket_id}.") from None
