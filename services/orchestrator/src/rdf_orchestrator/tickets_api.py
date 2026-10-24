"""Ticket entry point (TDM-19): a natural-language prompt becomes a ticket plus its run settings.

    POST /tickets        {prompt, robot_id?, settings_overrides?, budget_cents?, target_success?}
    POST /tickets/parse  same body; Claude's reading + what create would do, WITHOUT creating (TDM-74)
    GET  /tickets/{id}   ticket + run_settings + dataset + eval runs + data plans + spend + agents

Used by the website's prompt page and the Claude connector (``apps/mcp``). Mounted from ``app.py``.

POST flow:
  1. Claude (``claude-opus-5-5``, structured output) turns the prompt into
     ``{task_description, target_success, budget_usd, policy_prompt, eval_trials}``.
     Explicit ``budget_cents`` / ``target_success`` fields in the request win over the prompt.
  2. Defaults: a missing reliability target becomes 0.8 (``DEFAULT_TARGET_SUCCESS``, the module's
     default success threshold) and is listed in ``defaults_applied``. A missing budget is NOT
     defaulted: it is real spend, so the answer is a 422 ``budget_required`` asking for one.
  3. Robot: ``robot_id`` if given, else the only online robot (status online/busy/paused, names
     starting ``test.`` ignored). None or several online -> 422 listing the candidates.
  4. ``create_ticket(robot_id, task_description, target_success, budget_cents, overrides_json)``.
     ``policy_prompt`` goes into ``hyperparams.prompt`` (the language instruction the policy gets).
     Reducers return nothing, so the new id is read back from the ``ticket`` table (rows on that
     robot that were not there before the call, matched on task_description). A process-wide
     lock around read-call-read keeps two concurrent creates from picking each other's id.

Preview (TDM-74, the website's confirm step): ``POST /tickets/parse`` runs step 1-3 and returns the
result with ``missing`` (e.g. ``["budget"]``) and ``robot_error`` instead of 422s. Sending its
``parsed`` back on ``POST /tickets`` skips the second Claude call, so the ticket is exactly the
preview the person confirmed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .log import get_logger

log = get_logger("tickets_api")

MODEL = "claude-opus-5-5"
DEFAULT_TARGET_SUCCESS = 0.8  # = DEFAULT_RUN_SETTINGS.success_threshold in spacetime/.../enums.ts
ONLINE_STATUSES = ("online", "busy", "paused")

router = APIRouter(prefix="/tickets", tags=["tickets"])


# --------------------------------------------------------------------------- request / parse models

class CreateTicketRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000,
                        examples=["Put the cup in the bin, 80% reliable, $50 budget"])
    robot_id: int | None = Field(None, ge=1, description="Defaults to the only online robot.")
    settings_overrides: dict[str, Any] | None = Field(
        None, description="update_run_settings keys minus budget_cents: eval_trials, success_threshold, "
                          "require_ci_low, target_episodes, max_iterations, hyperparams{lr,steps,batch_size,lora_rank,prompt}")
    budget_cents: int | None = Field(None, ge=0, description="Overrides any budget in the prompt.")
    target_success: float | None = Field(None, gt=0, le=1, description="Overrides any target in the prompt.")
    parsed: "ParsedTicket | None" = Field(
        None, description="TDM-74: the `parsed` object from POST /tickets/parse. When given, Claude is not called "
                          "again, so the ticket matches the preview that was confirmed.")


class ParsedTicket(BaseModel):
    """Claude's structured reading of the prompt. Null means the prompt did not say."""

    task_description: str = Field(description="One clear sentence describing the job, without the reliability or budget clauses.")
    target_success: float | None = Field(description="Required success rate as a fraction in (0, 1], e.g. 80% -> 0.8. Null if not stated.")
    budget_usd: float | None = Field(description="Spending cap in US dollars, e.g. '$50' -> 50. Null if not stated.")
    policy_prompt: str = Field(description="Short lowercase imperative instruction for the robot policy, e.g. 'put the cup in the bin'.")
    eval_trials: int | None = Field(description="Number of evaluation trials if the prompt explicitly asks for one. Null otherwise.")


CreateTicketRequest.model_rebuild()


PARSER_SYSTEM = """You turn a customer's request for a robot job into a structured ticket for a robot \
deployment factory. The factory takes one robot arm and a task, buys demonstration data from people, \
fine-tunes a vision-language-action policy, and evaluates it until the robot meets a reliability target \
within a budget.

Rules:
- task_description: one sentence in plain English, capitalised, describing what the robot must do. \
Leave out the reliability target and budget.
- policy_prompt: the short instruction the policy is conditioned on, lowercase imperative, no trailing \
period (e.g. "put the cup in the bin"). Keep the objects and places the customer named.
- target_success: a fraction. "80% reliable", "8 out of 10 times" and "0.8 success" all mean 0.8. \
Null when the customer gave no target. Never invent one.
- budget_usd: the money the customer will spend, in dollars ("$50", "fifty bucks", "50 USD" -> 50). \
Null when no budget is given. Never invent one.
- eval_trials: only when the customer explicitly says how many test trials to run; otherwise null."""


class TicketPromptParser:
    """Claude call: prompt -> ParsedTicket, through ``rdf_common.llm`` (RDF_LLM_BACKEND: api | droid |
    claude_code; on the API ``beta.messages.parse`` with the refusal fallback). ``api_key`` pins an
    ``AsyncAnthropic`` client with that key for the api backend; the CLI backends ignore it."""

    def __init__(self, api_key: str | None = None, model: str = MODEL) -> None:
        self._api_key, self.model = api_key, model
        self._client = None
        self._llm = None
        self.last_meta: dict | None = None

    def _anthropic(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self._api_key) if self._api_key else anthropic.AsyncAnthropic()
        return self._client

    def _shared(self):
        if self._llm is None:
            from rdf_common.llm import LLM

            # api: the AsyncAnthropic client above (built lazily on first api call); droid / claude_code: CLIs.
            parser = self

            class _Lazy:
                @property
                def beta(self):
                    return parser._anthropic().beta

            self._llm = LLM(async_api_client=_Lazy())
        return self._llm

    async def parse(self, prompt: str) -> ParsedTicket:
        import anthropic
        from rdf_common.llm import LLMError

        try:
            r = await self._shared().acomplete_structured(
                step="ticket_parse", system=PARSER_SYSTEM, messages=[{"role": "user", "content": prompt}],
                output_model=ParsedTicket, model=self.model, effort="low",  # simple extraction
                max_tokens=4096, thinking=False)  # api: fallbacks="default" re-runs a classifier decline
        except anthropic.RateLimitError as e:
            raise TicketError(503, "parser_rate_limited", "The ticket parser is rate limited; retry shortly.") from e
        except anthropic.APIStatusError as e:
            raise TicketError(502, "parser_failed", f"Claude API error {e.status_code}.") from e
        except anthropic.APIConnectionError as e:
            raise TicketError(502, "parser_unreachable", "Could not reach the Claude API.") from e
        except LLMError as e:
            if e.reason == "refusal":
                raise TicketError(422, "prompt_refused", "The ticket parser declined this prompt.") from e
            if e.llm_kind == "transient":
                raise TicketError(503, "parser_unavailable", f"The ticket parser ({e.backend}) is busy or timed out; retry shortly.") from e
            raise TicketError(502, "parser_no_output", f"The ticket parser returned no ticket ({e.backend}: {e.reason}).") from e
        self.last_meta = r.meta
        if r.parsed is None:
            raise TicketError(502, "parser_no_output", f"The ticket parser returned no ticket (stop_reason={r.meta.get('stop_reason')}).")
        log.info("tickets.parsed", extra={"fields": {"model": r.model, "backend": r.backend,
                                                     "stop_reason": r.meta.get("stop_reason")}})
        return r.parsed


# --------------------------------------------------------------------------- service

class TicketError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def http(self) -> HTTPException:
        return HTTPException(self.status, detail={"code": self.code, "message": self.message, **self.extra})


def _public_robot(r: dict) -> dict:
    return {"id": r["id"], "name": r["name"], "status": r["status"], "embodiment": r.get("embodiment")}


def _with_hyperparams(rs: dict | None) -> dict | None:
    if rs is None:
        return None
    out = dict(rs)
    try:
        out["hyperparams"] = json.loads(rs.get("hyperparams_json") or "{}")
    except ValueError:
        out["hyperparams"] = None
    return out


class TicketsService:
    """``stdb``: an ``rdf_common.spacetime_client.AsyncSpacetimeClient`` (or a fake with
    ``sql`` / ``call_reducer``). ``parser``: anything with ``async parse(prompt) -> ParsedTicket``."""

    def __init__(self, stdb: Any, parser: Any) -> None:
        self.stdb, self.parser = stdb, parser
        self._create_lock = asyncio.Lock()

    async def _rows(self, table: str, col: str, value: int) -> list[dict]:
        return await self.stdb.sql(f"SELECT * FROM {table} WHERE {col} = {int(value)}")

    async def pick_robot(self, robot_id: int | None) -> tuple[dict, str]:
        robots = await self.stdb.sql("SELECT * FROM robot")
        if robot_id is not None:
            for r in robots:
                if r["id"] == robot_id:
                    return r, "requested"
            raise TicketError(422, "unknown_robot", f"No robot with id {robot_id}.",
                              robots=[_public_robot(r) for r in robots])
        live = [r for r in robots if r["status"] in ONLINE_STATUSES and not str(r["name"]).startswith("test.")]
        if len(live) == 1:
            return live[0], "only_online_robot"
        if not live:
            raise TicketError(422, "no_online_robot", "No robot is online. Pass robot_id to pick one.",
                              robots=[_public_robot(r) for r in robots])
        raise TicketError(422, "multiple_online_robots", "Several robots are online. Pass robot_id to pick one.",
                          robots=[_public_robot(r) for r in live])

    def _resolve(self, req: CreateTicketRequest, parsed: ParsedTicket) -> dict:
        """Steps 1-2 without any I/O: explicit fields over the prompt, defaults, overrides.
        ``budget_cents`` is None when neither the prompt nor the request gave one."""
        defaults_applied: list[str] = []

        task_description = parsed.task_description.strip()
        if not task_description:
            raise TicketError(422, "task_required", "Could not tell what the robot should do. Describe the task.",
                              parsed=parsed.model_dump())

        target = req.target_success if req.target_success is not None else parsed.target_success
        if target is None:
            target = DEFAULT_TARGET_SUCCESS
            defaults_applied.append(f"target_success={DEFAULT_TARGET_SUCCESS}")
        if not 0 < target <= 1:
            raise TicketError(422, "bad_target_success", f"target_success must be in (0, 1], got {target}.",
                              parsed=parsed.model_dump())

        budget_cents: int | None = None
        if req.budget_cents is not None:
            budget_cents = req.budget_cents
        elif parsed.budget_usd is not None:
            budget_cents = round(parsed.budget_usd * 100)
        if budget_cents is not None and budget_cents < 0:
            raise TicketError(422, "bad_budget", "budget must not be negative.", parsed=parsed.model_dump())

        overrides: dict[str, Any] = dict(req.settings_overrides or {})
        hyper = dict(overrides.get("hyperparams") or {})
        policy_prompt = parsed.policy_prompt.strip()
        if policy_prompt and "prompt" not in hyper:
            hyper["prompt"] = policy_prompt
        if hyper:
            overrides["hyperparams"] = hyper
        if parsed.eval_trials is not None and "eval_trials" not in overrides:
            overrides["eval_trials"] = parsed.eval_trials
        return {"task_description": task_description, "target_success": float(target), "budget_cents": budget_cents,
                "settings_overrides": overrides, "defaults_applied": defaults_applied}

    async def _parsed(self, req: CreateTicketRequest) -> ParsedTicket:
        return req.parsed if req.parsed is not None else await self.parser.parse(req.prompt)

    async def preview(self, req: CreateTicketRequest) -> dict:
        """TDM-74: what ``create`` would do, without creating. Missing budget and robot problems are
        reported in the body (``missing``, ``robot_error``) rather than as 422s."""
        parsed = await self._parsed(req)
        r = self._resolve(req, parsed)
        robot, selection, robot_error = None, None, None
        try:
            robot, selection = await self.pick_robot(req.robot_id)
        except TicketError as e:
            robot_error = {"code": e.code, "message": e.message, **e.extra}
        return {
            "parsed": parsed.model_dump(),
            **r,
            "missing": [] if r["budget_cents"] is not None else ["budget"],
            "robot": _public_robot(robot) if robot else None,
            "robot_selection": selection,
            "robot_error": robot_error,
        }

    async def create(self, req: CreateTicketRequest) -> dict:
        parsed = await self._parsed(req)
        r = self._resolve(req, parsed)
        task_description, target, budget_cents = r["task_description"], r["target_success"], r["budget_cents"]
        overrides, defaults_applied = r["settings_overrides"], r["defaults_applied"]
        if budget_cents is None:
            raise TicketError(422, "budget_required",
                              "How much may the factory spend on data and training? Add a budget to the prompt "
                              "(e.g. '$50 budget') or pass budget_cents.",
                              parsed=parsed.model_dump())

        robot, selection = await self.pick_robot(req.robot_id)
        rid = int(robot["id"])
        from rdf_common.spacetime_client import SpacetimeError

        async with self._create_lock:
            before = {t["id"] for t in await self._rows("ticket", "robot_id", rid)}
            try:
                await self.stdb.call_reducer("create_ticket", rid, task_description, float(target), int(budget_cents),
                                             json.dumps(overrides) if overrides else "")
            except SpacetimeError as e:
                raise TicketError(422 if e.status == 530 else 502, "create_ticket_rejected", e.message,
                                  parsed=parsed.model_dump(), settings_overrides=overrides) from e
            after = await self._rows("ticket", "robot_id", rid)
        new = sorted((t for t in after if t["id"] not in before and t["task_description"] == task_description),
                     key=lambda t: t["created_at"])
        if not new:
            raise TicketError(502, "ticket_not_found", "create_ticket succeeded but the new ticket row was not found.")
        ticket = new[0]
        settings = await self._rows("run_settings", "ticket_id", ticket["id"])
        log.info("tickets.created", extra={"fields": {"ticket_id": ticket["id"], "robot_id": rid, "selection": selection,
                                                      "budget_cents": budget_cents, "target_success": target}})
        return {
            "ticket_id": ticket["id"],
            "robot": _public_robot(robot),
            "robot_selection": selection,
            "ticket": ticket,
            "run_settings": _with_hyperparams(settings[0] if settings else None),
            "parsed": parsed.model_dump(),
            "settings_overrides": overrides,
            "defaults_applied": defaults_applied,
        }

    async def aggregate(self, ticket_id: int) -> dict:
        tickets = await self._rows("ticket", "id", ticket_id)
        if not tickets:
            raise TicketError(404, "ticket_not_found", f"No ticket {ticket_id}.")
        t = tickets[0]
        settings, dataset, evals, plans, sessions, payments, robot = await asyncio.gather(
            self._rows("run_settings", "ticket_id", ticket_id),
            self._rows("dataset", "ticket_id", ticket_id),
            self._rows("eval_run", "ticket_id", ticket_id),
            self._rows("data_plan", "ticket_id", ticket_id),
            self._rows("agent_session", "ticket_id", ticket_id),
            self._rows("payment", "ticket_id", ticket_id),
            self._rows("robot", "id", t["robot_id"]),
        )
        evals = sorted(evals, key=lambda r: r["created_at"])
        plans = sorted(plans, key=lambda r: r["created_at"])
        sessions = sorted(sessions, key=lambda r: r["created_at"])
        by_status: dict[str, int] = {}
        for p in payments:
            by_status[p["status"]] = by_status.get(p["status"], 0) + int(p["amount_cents"])
        return {
            "ticket": t,
            "robot": _public_robot(robot[0]) if robot else None,
            "run_settings": _with_hyperparams(settings[0] if settings else None),
            "dataset": dataset[0] if dataset else None,
            "latest_eval_run": evals[-1] if evals else None,
            "eval_runs": evals,
            "data_plans": plans,
            "spend": {
                "budget_cents": t["budget_cents"],
                "approved_cents": t["approved_cents"],
                "spent_cents": t["spent_cents"],
                "remaining_cents": max(0, int(t["budget_cents"]) - int(t["spent_cents"])),
                "payments_by_status_cents": by_status,
                "payment_count": len(payments),
            },
            "agent_sessions": sessions,
        }


# --------------------------------------------------------------------------- wiring

def _default_service() -> TicketsService:
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import AsyncSpacetimeClient

    s = load_settings(OrchestratorSettings, "orchestrator")
    return TicketsService(AsyncSpacetimeClient.from_settings(s),
                          TicketPromptParser(api_key=s.anthropic_api_key.get_secret_value()))


def get_tickets_service(request: Request) -> TicketsService:
    svc = getattr(request.app.state, "tickets_service", None)
    if svc is None:
        svc = request.app.state.tickets_service = _default_service()
    return svc


@router.post("", status_code=201)
async def create_ticket(body: CreateTicketRequest, svc: TicketsService = Depends(get_tickets_service)) -> dict:
    """Prompt -> ticket + run settings. 422 ``budget_required`` when no budget is given."""
    try:
        return await svc.create(body)
    except TicketError as e:
        raise e.http() from None


@router.post("/parse")
async def parse_ticket(body: CreateTicketRequest, svc: TicketsService = Depends(get_tickets_service)) -> dict:
    """TDM-74 preview: Claude's reading of the prompt and what POST /tickets would create. Creates nothing."""
    try:
        return await svc.preview(body)
    except TicketError as e:
        raise e.http() from None


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: int, svc: TicketsService = Depends(get_tickets_service)) -> dict:
    """Aggregate view of one ticket for the connector and the dashboard."""
    try:
        return await svc.aggregate(ticket_id)
    except TicketError as e:
        raise e.http() from None
