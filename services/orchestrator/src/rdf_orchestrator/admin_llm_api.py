"""Switch the LLM backend on the fly (local/dev), locked to the Claude API in production.

    GET /admin/llm   -> {backend, model, effective_model, allowed, env, locked, source, labels, updated_at, updated_by}
    PUT /admin/llm   {backend: api|droid|claude_code, model?: str|null, by?: str}
      header X-RDF-Operator-Key: <RDF_OPERATOR_KEY>
      -> 200 the GET shape plus changed {from, to, model} and audit (logged | failed)

The choice is ``rdf_common.llm.set_backend``: persisted in ``RDF_LLM_STATE_FILE`` (default
``~/.local/state/rdf/llm_backend.json``) and read on every LLM call, so it applies to the next call with no
restart; a call already in flight finishes on its old backend. Every change appends ``llm.backend_changed``
{from, to, model, from_model, by} to event_log (actor ``human:operator``).

Production lock (``RDF_DEPLOY_ENV=production``/``cloud``, ``FLY_APP_NAME``, ...; see ``rdf_common.llm``):
GET says ``locked: true`` and ``allowed: ["api"]``; PUT answers 403 "locked in production" before it even
looks at the key. This is the enforcement; the web UI hiding the control is cosmetic.

Errors: 401 missing/wrong key, 403 locked, 422 unknown backend, 503 RDF_OPERATOR_KEY not configured.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .log import get_logger
from .robot_commands_api import Tdm78Stdb, _check_key, get_operator_key, get_robot_commands_stdb

log = get_logger("admin_llm_api")

admin_llm_router = APIRouter(tags=["admin"])


class LlmBackendRequest(BaseModel):
    backend: Literal["api", "droid", "claude_code"]
    model: str | None = Field(None, max_length=128, description="Model override for that backend; null = its default")
    by: str | None = Field(None, max_length=64)


@admin_llm_router.get("/admin/llm")
def get_llm_backend() -> dict:
    from rdf_common.llm import current

    return current().describe()


@admin_llm_router.put("/admin/llm")
async def put_llm_backend(
    body: LlmBackendRequest,
    x_rdf_operator_key: str | None = Header(default=None),
    x_human_id: str | None = Header(default=None),
    stdb: Tdm78Stdb = Depends(get_robot_commands_stdb),
    operator_key: str | None = Depends(get_operator_key),
) -> dict:
    from rdf_common.llm import LLMError, LLMLocked, is_locked, set_backend

    if is_locked():
        raise HTTPException(403, "LLM backend is locked in production (api)")
    _check_key("llm_backend", x_rdf_operator_key, operator_key)
    who = (body.by or x_human_id or "operator")[:64]
    try:
        before, after = set_backend(body.backend, body.model, by=who)
    except LLMLocked as e:
        raise HTTPException(403, str(e)) from e
    except LLMError as e:
        raise HTTPException(422, str(e)) from e
    changed = {"from": before.backend, "to": after.backend, "model": after.model,
               "from_model": before.model, "by": who}
    audit = "logged"
    try:
        await stdb.call_reducer("append_event", "human:operator", "llm.backend_changed", json.dumps(changed))
    except Exception as e:  # noqa: BLE001 - the switch stands; the response says the audit row failed
        audit = "failed"
        log.warning("llm.backend_changed.audit_failed", extra={"fields": {"error": str(e)[:200]}})
    log.info("llm.backend_changed", extra={"fields": changed})
    out: dict[str, Any] = after.describe()
    out.update({"changed": changed, "audit": audit})
    return out


# --------------------------------------------------------------------------- llm.call -> event_log


class CallEventWriter:
    """An ``rdf_common.llm`` call sink: each ``llm.call`` record becomes one event_log row (actor
    ``orchestrator:llm``), written from a background thread so an LLM call never waits on SpacetimeDB.
    Off with ``RDF_LLM_CALL_EVENTS=0``."""

    def __init__(self, stdb: Any) -> None:
        import queue
        import threading

        self.stdb, self.q = stdb, queue.Queue(maxsize=1000)
        self._t = threading.Thread(target=self._run, name="llm-call-events", daemon=True)
        self._t.start()

    def __call__(self, rec: dict) -> None:
        try:
            self.q.put_nowait(rec)
        except Exception:  # noqa: BLE001 - full queue: drop (the log line still has it)
            pass

    def _run(self) -> None:
        while True:
            rec = self.q.get()
            if rec is None:
                return
            try:
                self.stdb.call_reducer("append_event", "orchestrator:llm", "llm.call", json.dumps(rec, default=str))
            except Exception as e:  # noqa: BLE001
                log.warning("llm.call_event_failed", extra={"fields": {"error": str(e)[:200]}})

    def stop(self) -> None:
        self.q.put(None)


def start_call_events() -> CallEventWriter | None:
    import os

    from rdf_common import llm

    if os.environ.get("RDF_LLM_CALL_EVENTS", "1") == "0":
        return None
    try:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import SpacetimeClient

        w = CallEventWriter(SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator")))
    except BaseException as e:  # noqa: BLE001 - optional evidence; the log line is the fallback
        log.warning("llm.call_events_off", extra={"fields": {"error": str(e)[:200]}})
        return None
    llm.add_call_sink(w)
    return w
