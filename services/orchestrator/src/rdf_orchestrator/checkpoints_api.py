"""TDM-42 checkpoint registry routes (``rdf_training.registry`` over the ``rdf-checkpoints`` Volume,
joined with SpacetimeDB ``training_run`` / ``eval_run`` / ``ticket`` rows and the policy server state).

    GET /checkpoints?ticket_id=<t>              every checkpoint (all tickets if omitted) + latest/deployed/base
                                                and, for a ticket, its GPU spend by cost source
    GET /checkpoints/resolve?ticket_id=&which=  latest | deployed | base -> one record (404 if none)
    GET /checkpoints/cleanup?keep_latest=2      DRY RUN of the Volume cleanup (never deletes; apply
                                                is ``make checkpoint-cleanup APPLY=1`` from the laptop)

``ticket_id`` is the SpacetimeDB ticket id (``42``) or a Volume ticket dir key (``tdm40-smoke``).
Read only: no reducer is called. The Volume scan is cached for ``RDF_CHECKPOINTS_CACHE_S`` (30 s).
Mounted from ``app.py``.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, HTTPException

from .log import get_logger

log = get_logger("checkpoints_api")

checkpoints_router = APIRouter(tags=["checkpoints"])


class Tdm42Sql(Protocol):
    async def sql(self, query: str) -> list[dict]: ...


_tdm42_client: Any = None


async def get_checkpoints_stdb() -> Tdm42Sql:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm42_client
    if _tdm42_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm42_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm42_client


class Tdm42Source:
    """Volume + policy-state readers (blocking Modal SDK calls; run in a thread). Tests override."""

    def __init__(self, volume_factory: Callable[[], Any] | None = None,
                 policy_state: Callable[[], dict] | None = None, cache_s: float | None = None) -> None:
        from rdf_training import registry

        self._volume_factory = volume_factory or registry.ModalVolume
        self._policy_state = policy_state or registry.read_policy_state
        self.cache_s = float(os.environ.get("RDF_CHECKPOINTS_CACHE_S", 30)) if cache_s is None else cache_s
        self._volume: Any = None
        self._scan: tuple[float, list[dict]] | None = None

    def scan(self) -> list[dict]:
        from rdf_training import registry

        if self._scan and time.monotonic() - self._scan[0] < self.cache_s:
            return self._scan[1]
        if self._volume is None:
            self._volume = self._volume_factory()
        recs = registry.scan(self._volume)
        self._scan = (time.monotonic(), recs)
        return recs

    def policy_state(self) -> dict:
        return self._policy_state()


_tdm42_source: Tdm42Source | None = None


def get_checkpoints_source() -> Tdm42Source:
    global _tdm42_source
    if _tdm42_source is None:
        _tdm42_source = Tdm42Source()
    return _tdm42_source


async def _state(stdb: Tdm42Sql, src: Tdm42Source):
    from rdf_training import registry

    st = registry.State()
    try:
        st.training_runs, st.eval_runs, st.tickets = await asyncio.gather(
            stdb.sql("SELECT * FROM training_run"), stdb.sql("SELECT * FROM eval_run"), stdb.sql("SELECT * FROM ticket"))
    except Exception as e:  # noqa: BLE001 - list without the join; cleanup fails closed on ok=False
        st.ok = False
        st.errors.append(f"SpacetimeDB: {type(e).__name__}: {e}")
    try:
        st.policy_state = await asyncio.to_thread(src.policy_state)
    except Exception as e:  # noqa: BLE001
        st.ok = False
        st.errors.append(f"policy state Dict: {type(e).__name__}: {e}")
    return st


async def _records(stdb: Tdm42Sql, src: Tdm42Source):
    from rdf_training import registry

    try:
        recs = await asyncio.to_thread(src.scan)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Volume rdf-checkpoints listing failed: {type(e).__name__}: {e}") from None
    st = await _state(stdb, src)
    return registry.join(recs, st), st


def _uri(r: dict | None) -> str | None:
    return r.get("uri") if r else None


@checkpoints_router.get("/checkpoints")
async def list_checkpoints(ticket_id: str | None = None, include_base: bool = True,
                           stdb: Tdm42Sql = Depends(get_checkpoints_stdb),
                           src: Tdm42Source = Depends(get_checkpoints_source)) -> dict:
    from rdf_training import cost, registry

    recs, st = await _records(stdb, src)
    out: dict[str, Any] = {"volume": registry.VOLUME, "state_ok": st.ok, "state_errors": st.errors}
    if ticket_id is None:
        out["checkpoints"] = [r for r in recs if include_base or r.get("kind") != "base"]
        return out
    mine = registry.for_ticket(recs, ticket_id)
    if include_base:
        mine = mine + [r for r in recs if r.get("kind") == "base"]
    key = registry.ticket_key(ticket_id)
    runs = [r for r in st.training_runs if key.isdigit() and int(r.get("ticket_id") or 0) == int(key)]
    out.update({
        "ticket_id": ticket_id, "checkpoints": mine,
        "latest": _uri(registry.resolve(recs, ticket_id, "latest")),
        "deployed": _uri(registry.resolve(recs, ticket_id, "deployed")),
        "base": registry.BASE_URI,
        "gpu_spend": cost.ticket_spend(runs) if runs else None,
    })
    return out


@checkpoints_router.get("/checkpoints/resolve")
async def resolve_checkpoint(ticket_id: str, which: str = "latest",
                             stdb: Tdm42Sql = Depends(get_checkpoints_stdb),
                             src: Tdm42Source = Depends(get_checkpoints_source)) -> dict:
    from rdf_training import registry

    if which not in ("latest", "deployed", "base"):
        raise HTTPException(422, "which must be latest | deployed | base")
    recs, _ = await _records(stdb, src)
    r = registry.resolve(recs, ticket_id, which)
    if r is None:
        raise HTTPException(404, f"no {which} checkpoint for ticket {ticket_id}")
    return r


@checkpoints_router.get("/checkpoints/cleanup")
async def cleanup_plan(keep_latest: int = 2, stdb: Tdm42Sql = Depends(get_checkpoints_stdb),
                       src: Tdm42Source = Depends(get_checkpoints_source)) -> dict:
    """Dry run only: what a cleanup would keep (and why) and delete."""
    from rdf_training import registry

    recs, st = await _records(stdb, src)
    return registry.plan_cleanup(recs, st, keep_latest=keep_latest)
