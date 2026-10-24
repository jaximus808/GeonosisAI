"""Run-settings API (TDM-20): read and patch a ticket's policy / research-run settings.

Routes (mounted by ``app.py``):
  GET   /tickets/{ticket_id}/settings   -> the effective ``run_settings`` row (+ ticket context)
  PATCH /tickets/{ticket_id}/settings   partial body, any of:
        eval_trials (int 1-50)            success_threshold (0-1)      require_ci_low (bool)
        budget_cents (int >= 0, >= what the ticket already approved/spent)
        target_episodes (int 1-10000)     max_iterations (int 1-20, > the ticket's current iteration)
        data_type_override (null | "clean_teleop" | "recovery_takeover"; null = the planner decides)
        hyperparams {lr (0-1], steps 1-1e6, batch_size 1-4096, lora_rank 0-1024, prompt} (merged)
      header ``X-Human-Id: <id>`` (optional, default ``anonymous``) names the person; the change
      is logged to event_log as actor ``human:<id>`` (type ``human.run_settings_changed``) next
      to the reducer's own ``ticket.run_settings_updated`` row.
      -> 200 with the settings as read back after the write (same shape as GET).

The ``update_run_settings`` reducer (spacetime/spacetimedb/src/reducers/tickets.ts) is the source
of truth. The pydantic models below mirror its rules so callers get FastAPI's field-level 422
(``{"detail": [{"loc": ["body", "eval_trials"], "msg": ..., "type": ...}]}``) instead of a reducer
string; anything the reducer still refuses comes back in the same shape with ``type:
"reducer_rejected"``. Errors: 404 unknown ticket, 409 settings frozen (shipped/failed/cancelled),
422 validation, 502 SpacetimeDB unreachable.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .log import get_logger, ticket_id_var

log = get_logger("run_settings_api")

run_settings_router = APIRouter(tags=["run-settings"])

TDM20_MAX_SAFE_INT = 2**53 - 1
TDM20_TERMINAL = ("shipped", "failed", "cancelled")
TDM20_HUMAN_ID = r"^[A-Za-z0-9._@:+-]{1,64}$"


# --------------------------------------------------------------------------- request / response models
# Fields default to None but are not Optional: an explicit JSON null is a field-level 422, an omitted
# field is simply not part of the patch (model_dump(exclude_unset=True)). data_type_override is the
# one field where null is a value ("clear the override").

class HyperparamsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lr: float = Field(default=None, gt=0, le=1, allow_inf_nan=False, description="learning rate, (0, 1]")
    steps: int = Field(default=None, ge=1, le=1_000_000)
    batch_size: int = Field(default=None, ge=1, le=4096)
    lora_rank: int = Field(default=None, ge=0, le=1024, description="0 = full fine-tune")
    prompt: str = Field(default=None, description="language instruction given to the policy")


class RunSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    eval_trials: int = Field(default=None, ge=1, le=50)
    success_threshold: float = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    require_ci_low: bool = Field(default=None, description="ship on Wilson ci_low >= threshold, not the point estimate")
    budget_cents: int = Field(default=None, ge=0, le=TDM20_MAX_SAFE_INT)
    target_episodes: int = Field(default=None, ge=1, le=10_000)
    max_iterations: int = Field(default=None, ge=1, le=20)
    data_type_override: Literal["clean_teleop", "recovery_takeover"] | None = Field(
        default=None, description="null = the planner decides"
    )
    hyperparams: HyperparamsPatch = Field(default=None, description="merged key by key into the current hyperparams")

    @model_validator(mode="after")
    def _not_empty(self) -> "RunSettingsPatch":
        if not self.model_fields_set:
            raise ValueError("patch is empty: send at least one setting")
        if "hyperparams" in self.model_fields_set and not self.hyperparams.model_fields_set:
            raise ValueError("hyperparams is empty: send at least one hyperparam or leave it out")
        return self

    def to_patch(self) -> dict[str, Any]:
        """The reducer's patch_json object (only the fields the caller sent)."""
        return self.model_dump(exclude_unset=True)


class TicketContext(BaseModel):
    status: str
    iteration: int
    budget_cents: int
    approved_cents: int
    spent_cents: int


class RunSettings(BaseModel):
    ticket_id: int
    eval_trials: int
    success_threshold: float
    require_ci_low: bool
    budget_cents: int
    target_episodes: int
    max_iterations: int
    data_type_override: Literal["clean_teleop", "recovery_takeover"] | None
    hyperparams: dict[str, Any]
    updated_at_micros: int
    ticket: TicketContext
    audit_logged: bool | None = Field(default=None, description="PATCH only: the human.run_settings_changed row was written")


# --------------------------------------------------------------------------- SpacetimeDB access

class Tdm20Stdb(Protocol):
    """The two calls this router needs (``rdf_common.spacetime_client.AsyncSpacetimeClient``)."""

    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


_tdm20_client: Any = None


async def get_run_settings_stdb() -> Tdm20Stdb:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm20_client
    if _tdm20_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm20_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm20_client


def _err(status: int, loc: list, msg: str, typ: str) -> HTTPException:
    return HTTPException(status_code=status, detail=[{"loc": loc, "msg": msg, "type": typ}])


def _stdb_down(e: Exception) -> HTTPException:
    return _err(502, [], f"SpacetimeDB request failed: {type(e).__name__}: {e}", "spacetime_unavailable")


async def _load(stdb: Tdm20Stdb, ticket_id: int) -> tuple[dict, dict]:
    try:
        tickets, settings = await asyncio.gather(
            stdb.sql(f"SELECT * FROM ticket WHERE id = {ticket_id}"),
            stdb.sql(f"SELECT * FROM run_settings WHERE ticket_id = {ticket_id}"),
        )
    except Exception as e:  # noqa: BLE001 - surface as 502, not a stack trace
        raise _stdb_down(e) from e
    if not tickets:
        raise _err(404, ["path", "ticket_id"], f"unknown ticket {ticket_id}", "not_found")
    if not settings:
        raise _err(404, ["path", "ticket_id"], f"ticket {ticket_id} has no run_settings row", "not_found")
    return tickets[0], settings[0]


def _view(ticket: dict, row: dict) -> RunSettings:
    hp = row.get("hyperparams_json") or "{}"
    return RunSettings(
        ticket_id=int(row["ticket_id"]),
        eval_trials=int(row["eval_trials"]),
        success_threshold=float(row["success_threshold"]),
        require_ci_low=bool(row["require_ci_low"]),
        budget_cents=int(row["budget_cents"]),
        target_episodes=int(row["target_episodes"]),
        max_iterations=int(row["max_iterations"]),
        data_type_override=row.get("data_type_override") or None,
        hyperparams=json.loads(hp),
        updated_at_micros=int(row["updated_at"]),
        ticket=TicketContext(
            status=ticket["status"],
            iteration=int(ticket["iteration"]),
            budget_cents=int(ticket["budget_cents"]),
            approved_cents=int(ticket["approved_cents"]),
            spent_cents=int(ticket["spent_cents"]),
        ),
    )


def _check_against_ticket(ticket_id: int, ticket: dict, patch: dict) -> None:
    """The reducer's ticket-dependent rules, mirrored for field-level errors."""
    if ticket["status"] in TDM20_TERMINAL:
        raise _err(409, ["path", "ticket_id"], f"ticket {ticket_id} is {ticket['status']}; run settings are frozen",
                   "settings_frozen")
    errors = []
    if "budget_cents" in patch:
        committed = max(int(ticket["approved_cents"]), int(ticket["spent_cents"]))
        if patch["budget_cents"] < committed:
            errors.append({"loc": ["body", "budget_cents"], "type": "below_committed",
                           "msg": f"budget_cents {patch['budget_cents']} is below what ticket {ticket_id} "
                                  f"already committed ({committed} cents approved/spent)"})
    if "max_iterations" in patch and patch["max_iterations"] <= int(ticket["iteration"]):
        errors.append({"loc": ["body", "max_iterations"], "type": "not_above_iteration",
                       "msg": f"max_iterations {patch['max_iterations']} must exceed the ticket's current "
                              f"iteration {ticket['iteration']}"})
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _reducer_error(ticket_id: int, e: Exception) -> HTTPException:
    msg = getattr(e, "message", None) or str(e)
    status = getattr(e, "status", None)
    if status is None or status >= 500 and status != 530:
        return _stdb_down(e)
    if "unknown ticket" in msg or "no run_settings" in msg:
        return _err(404, ["path", "ticket_id"], msg, "not_found")
    if "frozen" in msg:
        return _err(409, ["path", "ticket_id"], msg, "settings_frozen")
    if "unauthorized" in msg:
        return _err(502, [], f"orchestrator identity may not call update_run_settings: {msg}", "reducer_unauthorized")
    return _err(422, ["body"], msg, "reducer_rejected")


# --------------------------------------------------------------------------- routes

@run_settings_router.get("/tickets/{ticket_id}/settings", response_model=RunSettings, response_model_exclude_none=False)
async def get_run_settings(ticket_id: int, stdb: Tdm20Stdb = Depends(get_run_settings_stdb)) -> RunSettings:
    ticket, row = await _load(stdb, ticket_id)
    return _view(ticket, row)


@run_settings_router.patch("/tickets/{ticket_id}/settings", response_model=RunSettings)
async def patch_run_settings(
    ticket_id: int,
    body: RunSettingsPatch,
    x_human_id: str = Header(default="anonymous", pattern=TDM20_HUMAN_ID,
                             description="who made the change; logged as actor human:<id>"),
    stdb: Tdm20Stdb = Depends(get_run_settings_stdb),
) -> RunSettings:
    token = ticket_id_var.set(ticket_id)
    try:
        patch = body.to_patch()
        ticket, _ = await _load(stdb, ticket_id)
        _check_against_ticket(ticket_id, ticket, patch)
        try:
            await stdb.call_reducer("update_run_settings", ticket_id, json.dumps(patch))
        except Exception as e:  # noqa: BLE001 - SpacetimeError or transport error
            raise _reducer_error(ticket_id, e) from e

        actor = f"human:{x_human_id}"
        audit_logged = True
        try:
            await stdb.call_reducer("append_event", actor, "human.run_settings_changed",
                                    json.dumps({"ticket_id": ticket_id, "patch": patch, "via": "orchestrator.api"}))
        except Exception as e:  # noqa: BLE001 - the settings are saved; report the missing audit row
            audit_logged = False
            log.error("run_settings.audit_failed", extra={"fields": {"actor": actor, "error": str(e)}})

        ticket, row = await _load(stdb, ticket_id)  # read back: proves the patch is visible
        log.info("run_settings.patched", extra={"fields": {"actor": actor, "patch": patch}})
        out = _view(ticket, row)
        out.audit_logged = audit_logged
        return out
    finally:
        ticket_id_var.reset(token)
