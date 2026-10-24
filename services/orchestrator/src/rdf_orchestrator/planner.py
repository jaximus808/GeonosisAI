"""TDM-24 planner: decide what kind of data fixes the robot's failures, and how much to buy.

This is the demo's key moment, so nothing here is templated: Claude (``claude-opus-5-5``, structured
output) reads the ticket's real failure clusters, eval result, budget and run settings and writes
the decision and a paragraph of reasoning. Code sits around the model as guardrails:

* **Data type rules** (the same rules are stated to the model; code checks the answer):
    R1  eval success is 0 and clusters where the robot never approaches/reaches the object make up
        >= half of the failures  -> ``clean_teleop`` (there is no near-success state to recover from)
    R2  clusters that fail late in the trajectory (slips, drops mid-grasp, transport, place/release)
        make up >= half of the failures -> ``recovery_takeover`` (DAgger-style corrections)
    otherwise the model weighs the clusters. A user ``data_type_override`` beats both rules.
* **Amount**: 1 <= amount <= max_amount, where max_amount = what the remaining budget can pay for
  (unit price + one ~$2 reset per episode) capped at what the run settings still need
  (``target_episodes - dataset.accepted_count``; when that is already met, capped at target_episodes).
* **Reasoning** must quote at least one cluster label.
* **Existing datasets**: buying a seller's dataset (TDM-14 pays only for N - already uploaded) is a
  way to source *clean* demos, so it is only allowed with ``clean_teleop`` and below its unit price.

Inputs also include TDM-23's ``diagnose.clusters`` event (per-cluster phase, recoverability,
description), the eval's successes/trials + Wilson CI, previous plans and open dataset offers.
``data_type_override`` comes from the ``run_settings.data_type_override`` column (TDM-20, set by
PATCH /tickets/{id}/settings) when it is non-empty; otherwise from
``run_settings.hyperparams_json["data_type_override"]`` or the latest ``append_event`` of type
``planner.data_type_override`` with payload ``{"ticket_id": N, "data_type": "clean_teleop" |
"recovery_takeover" | null}`` (null clears it).

A violated rule gets one corrective retry (the model rewrites its own reasoning). If the retry still
breaks it, code forces the value and appends a clearly marked ``[guardrail]`` note to the reasoning,
so the stored text never contradicts the stored decision.

The result maps 1:1 onto TDM-12's insert-only ``record_data_plan(ticket_id, data_type, amount,
reasoning, evidence_json)``; the reducer copies reasoning + the full evidence into ``event_log``
(type ``planner.data_plan``). Nothing ever edits the plan afterwards.

Pure module: no SpacetimeDB I/O. ``handlers/planner.py`` feeds it rows from the bus mirror; tests
feed it fixtures and a fake Anthropic client.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"

DATA_TYPES = ("clean_teleop", "recovery_takeover")
# Mirrors spacetime/spacetimedb/src/enums.ts DEFAULT_PRICES_CENTS (record_data_plan's default).
PRICES_CENTS = {"reset": 200, "recovery_takeover": 500, "clean_teleop": 1000}
OVERRIDE_EVENT = "planner.data_type_override"  # append_event payload {ticket_id, data_type|null}
DIAGNOSE_EVENT = "diagnose.clusters"  # TDM-23's full diagnosis payload (see diagnose.py OUTPUT SHAPE)
RULE_SHARE = 0.5

# --------------------------------------------------------------------------- inputs


@dataclass
class Cluster:
    id: int
    label: str
    count: int
    iteration: int = 0
    description: str = ""
    example_episode_ids: list[int] = field(default_factory=list)
    example_trials: list[dict] = field(default_factory=list)  # failed eval_trial rows behind the examples
    stage: str = "unknown"  # no_approach | late | unknown (from TDM-23's phase, else label keywords)
    phase: str | None = None  # TDM-23 diagnose.clusters: start|approach|grasp|lift|transport|place|release|other
    recoverable_mid_episode: bool | None = None  # TDM-23: could a takeover finish the task in-episode
    recoverability_reason: str = ""


@dataclass
class EvalSummary:
    id: int
    kind: str
    successes: int
    trials: int
    ci_low: float
    ci_high: float
    checkpoint_uri: str = ""

    @property
    def success_rate(self) -> float:
        return self.successes / self.trials if self.trials else 0.0


@dataclass
class PlannerInputs:
    ticket_id: int
    iteration: int
    task_description: str
    target_success: float
    budget_cents: int
    approved_cents: int
    spent_cents: int
    target_episodes: int
    eval_trials: int
    success_threshold: float
    require_ci_low: bool
    max_iterations: int
    dataset_accepted: int
    dataset_total: int
    clusters: list[Cluster]
    latest_eval: EvalSummary | None = None
    baseline_eval: EvalSummary | None = None
    failed_trials: list[dict] = field(default_factory=list)
    previous_plans: list[dict] = field(default_factory=list)
    dataset_offers: list[dict] = field(default_factory=list)
    dataset_sellers: int = 0
    data_type_override: str | None = None
    diagnosis_note: str | None = None  # the clustering step's own logged reasoning, if any
    cluster_iteration: int | None = None
    prices: dict = field(default_factory=lambda: dict(PRICES_CENTS))
    fixture: str | None = None  # set by tests/proof scripts: the plan is labelled as a fixture


# --------------------------------------------------------------------------- stage heuristic

_LATE_WORDS = (
    "mid-grasp", "mid grasp", "drop", "slip", "lose grip", "loses grip", "lost grip", "loose grip",
    "release", "bin", "container", "place", "placing", "placement", "transport", "carry", "carrying",
    "lift", "knock", "tips over", "tip over", "fumble", "regrasp", "partial grasp", "grasp fail",
    "after grasp", "after pick", "while moving", "spill",
)
_NO_APPROACH_WORDS = (
    "never approach", "never reach", "never gets near", "never gets to", "never moves", "never move",
    "doesn't move", "does not move", "doesn't reach", "does not reach", "doesn't approach",
    "does not approach", "no motion", "no movement", "doesn't start", "freez", "frozen", "stuck at start",
    "stuck at home", "stays home", "wander", "aimless", "wrong direction", "wrong object", "goes the wrong",
    "never attempts", "no attempt", "idle", "random motion", "far from the",
)


_EARLY_PHASES = ("start", "approach")
_LATE_PHASES = ("grasp", "lift", "transport", "place", "release")


def classify_stage(label: str, description: str = "", phase: str | None = None) -> str:
    """Where in the trajectory a cluster fails: no_approach | late | unknown.

    TDM-23's vision-based ``phase`` wins when present (start/approach -> no_approach, grasp..release ->
    late). Otherwise keywords in the label/description; late words win (e.g. "never reaches the bin"
    is a transport failure: the object was picked)."""
    if phase in _EARLY_PHASES:
        return "no_approach"
    if phase in _LATE_PHASES:
        return "late"
    text = f"{label} {description}".lower()
    if any(w in text for w in _LATE_WORDS):
        return "late"
    if any(w in text for w in _NO_APPROACH_WORDS):
        return "no_approach"
    return "unknown"


# --------------------------------------------------------------------------- guardrails


@dataclass
class Guardrail:
    rule: str | None  # R1 | R2 | override | None
    required_type: str | None
    why: str
    failures: int
    no_approach_share: float
    late_share: float


def guardrail(inp: PlannerInputs) -> Guardrail:
    total = sum(c.count for c in inp.clusters)
    na = sum(c.count for c in inp.clusters if c.stage == "no_approach")
    late = sum(c.count for c in inp.clusters if c.stage == "late")
    na_s = na / total if total else 0.0
    late_s = late / total if total else 0.0
    ev = inp.latest_eval
    zero = ev is not None and ev.trials > 0 and ev.successes == 0
    if inp.data_type_override in DATA_TYPES:
        return Guardrail("override", inp.data_type_override,
                         f"the user fixed data_type to {inp.data_type_override}", total, na_s, late_s)
    if zero and na_s >= RULE_SHARE:
        return Guardrail("R1", "clean_teleop",
                         f"eval success is 0/{ev.trials} and {na} of {total} failures never approach the "
                         "object, so there is no near-success state to recover from", total, na_s, late_s)
    if late_s >= RULE_SHARE:
        return Guardrail("R2", "recovery_takeover",
                         f"{late} of {total} failures happen late in the trajectory, after the robot reaches "
                         "the object, so corrections from those states are the targeted fix", total, na_s, late_s)
    return Guardrail(None, None, "no rule fired; the model weighs the clusters", total, na_s, late_s)


@dataclass
class Budget:
    remaining_cents: int
    reset_cents: int
    needed: int  # target_episodes - accepted (>= 0)
    cap_by_settings: int
    per_episode_cents: dict[str, int]
    affordable: dict[str, int]
    max_amount: dict[str, int]


def budget(inp: PlannerInputs) -> Budget:
    committed = max(inp.approved_cents, inp.spent_cents)
    remaining = max(0, inp.budget_cents - committed)
    reset = int(inp.prices.get("reset", PRICES_CENTS["reset"]))
    needed = max(0, inp.target_episodes - inp.dataset_accepted)
    cap = needed if needed > 0 else max(1, inp.target_episodes)
    per = {t: int(inp.prices[t]) + reset for t in DATA_TYPES}
    afford = {t: remaining // per[t] if per[t] > 0 else 0 for t in DATA_TYPES}
    return Budget(remaining, reset, needed, cap, per, afford, {t: min(afford[t], cap) for t in DATA_TYPES})


# --------------------------------------------------------------------------- prompt

SYSTEM_PROMPT = """\
You are the data planner of the Robot Deployment Factory. A customer's robot arm runs a π0.5 policy on \
one task. The latest eval failed in the ways listed in the facts, grouped into failure clusters. You \
decide which kind of human data to buy next to fix those failures, and how many episodes, within the \
ticket's budget. Your decision and reasoning are stored unedited in the ticket's data plan and \
append-only event log, shown to the customer, and never edited afterwards, so state only what the \
facts support.

Data types (choose exactly one):
- recovery_takeover (about $5 per accepted episode): DAgger-style. The policy runs; when it reaches a \
failure state the robot pauses, a person takes over (phone clutch control or leader arm), recovers from \
that exact state, finishes the task and hands back. Training gets corrective actions from the states the \
policy actually visits. Best when the policy gets most of the way and fails at a later stage: grasp, \
lift, transport, place or release.
- clean_teleop (about $10 per accepted episode): a person teleoperates a full demonstration from start to \
finish. Best when the policy lacks the basic behavior: it never approaches the object, moves aimlessly, or \
fails before any useful interaction, so there is no near-success state to recover from.
Evidence: Northstar Robotics found 50 recovery demos beat 50 extra clean demos (78% vs 64% success) for a \
policy that already partly works. Recovery data is worth more per episode when failures happen late, and \
is wasted when the robot never gets near the object.

Decision rules (the system checks these and rejects a plan that breaks them):
1. If the eval success rate is 0 and clusters where the robot never approaches or reaches the object make \
up at least half of the failures, choose clean_teleop.
2. Otherwise, if clusters that fail late in the trajectory (after reaching the object) make up at least \
half of the failures, choose recovery_takeover.
3. Otherwise weigh the clusters yourself: their counts, where in the trajectory they fail, and the eval.
4. If data_type_override is set, use it. You still choose the amount, and you must say plainly if the \
failure data argues against the override.
Each cluster carries stage_hint (no_approach, late or unknown), computed by code from the diagnosis \
step's phase when there is one, otherwise from keywords in the label. phase and recoverable_mid_episode \
come from the diagnosis step, which looked at the failed episodes' camera frames. Use your own judgment of \
the label, description and example trial notes when the hint is unknown.
From the second iteration on, previous_plans lists this ticket's earlier plans with their effect: the \
eval that plan answered (before) and the eval of the checkpoint trained with its data (after). When the \
rules above leave the choice to you and an earlier plan of one type did not raise the success rate, say \
so, and consider switching to the other type.

Amount:
- Every episode also needs a scene reset (about $2), so the cost of one episode is its unit price plus a \
reset. The facts give max_amount for each data type: the most episodes the remaining budget can pay for, \
capped at what the run settings still need (target_episodes minus episodes already accepted). amount must \
be between 1 and max_amount for the chosen type.
- Aim the episodes at the dominant clusters. You need not spend the whole budget if fewer episodes are \
enough, but evals are only about 10 trials and every extra loop costs a fine-tune and an eval, so do not \
under-buy.

Existing datasets:
- Instead of paying people for new demonstrations, the factory can buy an existing dataset from a seller. \
The seller is paid only for the episodes still needed (target_episodes minus those already uploaded or \
accepted) and only for episodes that pass QA. Purchased datasets are full clean demonstrations of the \
task, so they can substitute only for clean_teleop, never for recovery takeovers. Set \
consider_dataset_offer to true only when data_type is clean_teleop and buying could be cheaper than \
commissioning demos; offer_max_price_cents is the most to pay per purchased episode and must be below the \
clean_teleop unit price. Otherwise set false and 0.

reasoning (the text the customer reads):
- One paragraph of plain prose, 4 to 7 sentences, no markdown, no lists.
- Quote each cluster you rely on by its exact label in double quotes with its count, for example \
"drops mid-grasp" (5 of 7 failures).
- Cite the eval result (successes out of trials, and the 95% interval) and where in the trajectory the \
failures happen.
- Say why the other data type is the worse buy here.
- Justify the amount with the budget arithmetic.
- Mention the dataset option only if it applies.
failure_cluster_ids: the ids of the clusters this plan targets."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "data_type": {"type": "string", "enum": list(DATA_TYPES)},
        "amount": {"type": "integer"},
        "reasoning": {"type": "string"},
        "failure_cluster_ids": {"type": "array", "items": {"type": "integer"}},
        "consider_dataset_offer": {"type": "boolean"},
        "offer_max_price_cents": {"type": "integer"},
    },
    "required": ["data_type", "amount", "reasoning", "failure_cluster_ids", "consider_dataset_offer",
                 "offer_max_price_cents"],
    "additionalProperties": False,
}


def _eval_dict(ev: EvalSummary | None) -> dict | None:
    if ev is None:
        return None
    return {"eval_run_id": ev.id, "kind": ev.kind, "successes": ev.successes, "trials": ev.trials,
            "success_rate": round(ev.success_rate, 3), "ci95": [round(ev.ci_low, 3), round(ev.ci_high, 3)],
            "checkpoint_uri": ev.checkpoint_uri}


def facts(inp: PlannerInputs, b: Budget) -> dict:
    """Everything the model sees (also stored in the plan's evidence)."""
    total = sum(c.count for c in inp.clusters)
    return {
        "ticket": {"id": inp.ticket_id, "task": inp.task_description, "iteration": inp.iteration,
                   "target_success": inp.target_success},
        "run_settings": {"eval_trials": inp.eval_trials, "success_threshold": inp.success_threshold,
                         "require_ci_low": inp.require_ci_low, "target_episodes": inp.target_episodes,
                         "max_iterations": inp.max_iterations},
        "latest_eval": _eval_dict(inp.latest_eval),
        "baseline_eval": _eval_dict(inp.baseline_eval),
        "failure_clusters": [
            {"id": c.id, "label": c.label, "count": c.count, "share_of_failures": round(c.count / total, 3) if total else 0,
             "description": c.description, "phase": c.phase, "recoverable_mid_episode": c.recoverable_mid_episode,
             "recoverability_reason": c.recoverability_reason, "stage_hint": c.stage,
             "example_episode_ids": c.example_episode_ids,
             "example_trials": c.example_trials}
            for c in inp.clusters
        ],
        "clusters_from_iteration": inp.cluster_iteration,
        "failed_trials": inp.failed_trials,
        "diagnosis_note": inp.diagnosis_note,
        "dataset": {"accepted_episodes": inp.dataset_accepted, "total_episodes": inp.dataset_total,
                    "still_needed_for_target": b.needed},
        "budget": {"budget_cents": inp.budget_cents, "approved_cents": inp.approved_cents,
                   "spent_cents": inp.spent_cents, "remaining_cents": b.remaining_cents},
        "prices_cents": {**{t: int(inp.prices[t]) for t in DATA_TYPES}, "reset": b.reset_cents},
        "cost_per_episode_with_reset_cents": b.per_episode_cents,
        "max_amount": b.max_amount,
        "previous_plans": inp.previous_plans,
        "dataset_sellers": {"registered_sellers": inp.dataset_sellers, "open_offers": inp.dataset_offers},
        "data_type_override": inp.data_type_override,
    }


def user_message(inp: PlannerInputs, b: Budget) -> str:
    return ("Plan the next data purchase for this ticket. Facts (JSON):\n"
            + json.dumps(facts(inp, b), indent=1, default=str))


# --------------------------------------------------------------------------- the decision


class PlannerError(RuntimeError):
    pass


@dataclass
class PlanOutcome:
    ticket_id: int
    blocked: str | None = None  # why no plan was recorded (budget exhausted, no clusters)
    data_type: str | None = None
    amount: int = 0
    reasoning: str = ""
    unit_price_cents: int = 0
    failure_cluster_ids: list[int] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def reducer_args(self) -> list:
        """Positional args for record_data_plan(ticket_id, data_type, amount, reasoning, evidence_json)."""
        assert self.blocked is None and self.data_type
        return [self.ticket_id, self.data_type, self.amount, self.reasoning, json.dumps(self.evidence, default=str)]


def _labels_cited(reasoning: str, clusters: Iterable[Cluster]) -> list[str]:
    low = reasoning.lower()
    return [c.label for c in clusters if c.label.strip() and c.label.strip().lower() in low]


def violations(d: dict, inp: PlannerInputs, g: Guardrail, b: Budget) -> list[str]:
    out = []
    t = d.get("data_type")
    if t not in DATA_TYPES:
        out.append(f"data_type must be one of {DATA_TYPES}, got {t!r}")
        return out
    if g.required_type and t != g.required_type:
        rule = "data_type_override" if g.rule == "override" else f"decision rule {g.rule[1]}"
        out.append(f"{rule} requires data_type={g.required_type} ({g.why}); you chose {t}")
    want = g.required_type or t
    mx = b.max_amount[want]
    amt = d.get("amount")
    if not isinstance(amt, int) or not 1 <= amt <= mx:
        out.append(f"amount must be an integer between 1 and max_amount[{want}]={mx}; you chose {amt!r}")
    if not _labels_cited(d.get("reasoning", ""), inp.clusters):
        out.append("reasoning must quote at least one failure cluster by its exact label: "
                   + ", ".join(f'"{c.label}"' for c in inp.clusters))
    if d.get("consider_dataset_offer") and want != "clean_teleop":
        out.append("consider_dataset_offer can only be true with clean_teleop (purchased datasets are clean demos)")
    if d.get("consider_dataset_offer") and not 0 < int(d.get("offer_max_price_cents") or 0) < int(inp.prices["clean_teleop"]):
        out.append(f"offer_max_price_cents must be > 0 and below the clean_teleop price {inp.prices['clean_teleop']}")
    return out


def _call(client: Any, messages: list[dict]) -> tuple[dict, Any]:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise PlannerError(f"Claude declined to plan (refusal, category={cat})")
    if resp.stop_reason == "max_tokens":
        raise PlannerError("Claude hit max_tokens before finishing the plan")
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        raise PlannerError(f"no text block in Claude's answer (stop_reason={resp.stop_reason})")
    try:
        d = json.loads(text)
    except ValueError as e:
        raise PlannerError(f"Claude's answer is not JSON: {e}") from e
    return d, resp


def _usage(resp: Any) -> dict:
    u = getattr(resp, "usage", None)
    return {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens")} if u else {}


def plan(inp: PlannerInputs, client: Any, *, log: Callable[[str, dict], None] | None = None) -> PlanOutcome:
    """Decide the plan for one ticket iteration. Raises PlannerError if Claude can't produce one."""
    b = budget(inp)
    g = guardrail(inp)
    base_evidence = {
        "planner": "TDM-24",
        "fixture": inp.fixture,
        "guardrail": asdict(g),
        "budget_math": asdict(b),
        "inputs": facts(inp, b),
    }
    if not inp.clusters:
        return PlanOutcome(inp.ticket_id, blocked="no failure clusters recorded for this ticket", evidence=base_evidence)
    if max(b.max_amount.values()) < 1:
        return PlanOutcome(inp.ticket_id, blocked=(
            f"remaining budget {b.remaining_cents}c cannot pay for one episode of either type "
            f"(cheapest {min(b.per_episode_cents.values())}c incl. reset)"), evidence=base_evidence)
    if g.required_type and b.max_amount[g.required_type] < 1:
        return PlanOutcome(inp.ticket_id, blocked=(
            f"{g.rule} requires {g.required_type} but the remaining budget {b.remaining_cents}c cannot pay for one "
            f"({b.per_episode_cents[g.required_type]}c incl. reset)"), evidence=base_evidence)

    messages: list[dict] = [{"role": "user", "content": user_message(inp, b)}]
    attempts: list[dict] = []
    d, resp = _call(client, messages)
    problems = violations(d, inp, g, b)
    attempts.append({"message_id": getattr(resp, "id", None), "model": getattr(resp, "model", None),
                     "usage": _usage(resp), "data_type": d.get("data_type"), "amount": d.get("amount"),
                     "violations": problems})
    if problems:
        if log:
            log("planner.retry", {"violations": problems})
        messages += [
            {"role": "assistant", "content": resp.content},
            {"role": "user", "content": "The plan breaks these checks:\n- " + "\n- ".join(problems)
             + "\nReturn a corrected plan. Rewrite the reasoning so it matches the corrected decision."},
        ]
        d, resp = _call(client, messages)
        problems = violations(d, inp, g, b)
        attempts.append({"message_id": getattr(resp, "id", None), "model": getattr(resp, "model", None),
                         "usage": _usage(resp), "data_type": d.get("data_type"), "amount": d.get("amount"),
                         "violations": problems})

    data_type = d.get("data_type") if d.get("data_type") in DATA_TYPES else None
    reasoning = str(d.get("reasoning", "")).strip()
    forced: list[str] = []
    if g.required_type and data_type != g.required_type:
        forced.append(f"data_type set to {g.required_type} ({g.rule}: {g.why})")
        data_type = g.required_type
    if data_type is None:
        raise PlannerError(f"Claude returned no valid data_type after retry: {d.get('data_type')!r}")
    mx = b.max_amount[data_type]
    amount = d.get("amount")
    if not isinstance(amount, int) or not 1 <= amount <= mx:
        clamped = min(max(int(amount) if isinstance(amount, (int, float)) else 1, 1), mx)
        forced.append(f"amount {amount!r} clamped to {clamped} (max {mx} for {data_type})")
        amount = clamped
    if not reasoning:
        raise PlannerError("Claude returned empty reasoning")
    if forced:
        reasoning += " [guardrail] " + "; ".join(forced) + "."

    offer = bool(d.get("consider_dataset_offer")) and data_type == "clean_teleop"
    price = int(d.get("offer_max_price_cents") or 0)
    if offer and not 0 < price < int(inp.prices["clean_teleop"]):
        offer = False
    known = {c.id for c in inp.clusters}
    ids = [int(i) for i in d.get("failure_cluster_ids") or [] if int(i) in known]
    ids = list(dict.fromkeys(ids)) or [c.id for c in inp.clusters]
    unit = int(inp.prices[data_type])

    evidence = {
        **base_evidence,
        "failure_cluster_ids": ids,
        "unit_price_cents": unit,
        "est_cost_incl_resets_cents": amount * b.per_episode_cents[data_type],
        "labels_cited": _labels_cited(reasoning, inp.clusters),
        "dataset_offer": {
            "consider": offer,
            "max_price_per_episode_cents": price if offer else 0,
            "max_episodes": b.needed,  # TDM-14 create_dataset_offer pays only for N - already uploaded
        },
        "model": {"requested": MODEL, "effort": EFFORT, "served": getattr(resp, "model", None),
                  "attempts": attempts},
        "guardrail_forced": forced,
        "guardrail_agreed_first_try": not attempts[0]["violations"],
    }
    return PlanOutcome(inp.ticket_id, None, data_type, amount, reasoning, unit, ids, evidence)


# --------------------------------------------------------------------------- rows -> inputs

Rows = Callable[[str], list]


def _g(r: Any, k: str, default: Any = None) -> Any:
    v = r.get(k, default) if isinstance(r, dict) else getattr(r, k, default)
    return default if v is None else v


def _payload(r: Any) -> dict:
    try:
        p = json.loads(_g(r, "payload_json", "") or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def _same(a: Any, b: Any) -> bool:
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def _tdm70_plan_effect(iteration: int, evals: list, runs: list, summ: Callable[[Any], Any]) -> dict | None:
    """TDM-70: what a plan of ``iteration`` did. ``before`` is the eval that plan answered (the
    baseline for iteration 0, else the previous iteration's candidate eval); ``after`` is the
    candidate eval of the checkpoint trained in that iteration (None while it is not finished)."""
    def cand(it: int):
        ids = {int(_g(r, "id")) for r in runs if int(_g(r, "iteration", 0)) == it and _g(r, "status") == "done"}
        return next((r for r in reversed(evals) if _g(r, "kind") != "baseline"
                     and int(_g(r, "training_run_id", 0) or 0) in ids), None)
    before = next((r for r in evals if _g(r, "kind") == "baseline"), None) if iteration == 0 else cand(iteration - 1)
    after = cand(iteration)
    if before is None and after is None:
        return None
    b, a = summ(before), summ(after)
    out = {"before": _eval_dict(b), "after": _eval_dict(a)}
    if b is not None and a is not None:
        out["success_rate_change"] = round(a.success_rate - b.success_rate, 3)
    return out


def gather_inputs(rows: Rows, ticket: Any) -> PlannerInputs:
    """Build PlannerInputs for a ticket from table rows (the bus mirror, or SQL results)."""
    tid, it = int(_g(ticket, "id")), int(_g(ticket, "iteration", 0))
    mine = lambda table: [r for r in rows(table) if _same(_g(r, "ticket_id"), tid)]  # noqa: E731

    rs = next(iter(mine("run_settings")), None)
    ds = next(iter(mine("dataset")), None)
    hp: dict = {}
    if rs is not None:
        try:
            hp = json.loads(_g(rs, "hyperparams_json", "{}") or "{}")
        except ValueError:
            hp = {}

    evals = sorted((r for r in mine("eval_run") if _g(r, "status") == "done"),
                   key=lambda r: (_g(r, "finished_at", 0) or 0, int(_g(r, "id"))))

    def summ(r):
        return None if r is None else EvalSummary(int(_g(r, "id")), _g(r, "kind", ""), int(_g(r, "successes", 0)),
                                                  int(_g(r, "trials", 0)), float(_g(r, "ci_low", 0.0)),
                                                  float(_g(r, "ci_high", 0.0)), _g(r, "checkpoint_uri", ""))
    latest = evals[-1] if evals else None
    baseline = next((r for r in evals if _g(r, "kind") == "baseline"), None)

    trials = [r for r in rows("eval_trial") if latest is not None and _same(_g(r, "eval_run_id"), _g(latest, "id"))]
    failed = sorted((r for r in trials if not _g(r, "success", False)), key=lambda r: int(_g(r, "trial_index", 0)))
    ok_durations = sorted(int(_g(r, "duration_ms", 0)) for r in trials if _g(r, "success", False))
    trial_by_ep = {int(_g(r, "episode_id")): r for r in failed if int(_g(r, "episode_id", 0) or 0)}

    def trial_view(r):
        return {"trial_index": int(_g(r, "trial_index", 0)), "episode_id": int(_g(r, "episode_id", 0) or 0),
                "duration_ms": int(_g(r, "duration_ms", 0)), "failure_note": _g(r, "failure_note", "")}

    # TDM-23 writes the full diagnosis (description, phase, recoverability) to one diagnose.clusters event.
    diags = []
    for e in rows("event_log"):
        if _g(e, "type") == DIAGNOSE_EVENT:
            p = _payload(e)
            if _same(p.get("ticket_id"), tid):
                diags.append((int(_g(e, "id")), p))
    diags.sort(key=lambda x: x[0])

    all_clusters = mine("failure_cluster")
    iters = sorted({int(_g(c, "iteration", 0)) for c in all_clusters if int(_g(c, "iteration", 0)) <= it})
    citer = iters[-1] if iters else None
    diag = next((p for _, p in reversed(diags) if _same(p.get("iteration"), citer)), None)
    by_id, by_label = {}, {}
    for dc in (diag or {}).get("clusters") or []:
        if isinstance(dc, dict):
            if dc.get("failure_cluster_id"):
                by_id[int(dc["failure_cluster_id"])] = dc
            by_label[str(dc.get("label", "")).strip().lower()] = dc
    clusters = []
    for c in sorted((c for c in all_clusters if citer is not None and int(_g(c, "iteration", 0)) == citer),
                    key=lambda c: (-int(_g(c, "count", 0)), int(_g(c, "id")))):
        ex = [int(e) for e in _g(c, "example_episode_ids", []) or []]
        label = str(_g(c, "label", ""))
        dc = by_id.get(int(_g(c, "id"))) or by_label.get(label.strip().lower()) or {}
        desc = str(_g(c, "description", "") or dc.get("description") or "")
        phase = dc.get("phase") if isinstance(dc.get("phase"), str) else None
        rec = dc.get("recoverable_mid_episode") if isinstance(dc.get("recoverable_mid_episode"), bool) else None
        clusters.append(Cluster(int(_g(c, "id")), label, int(_g(c, "count", 0)), int(_g(c, "iteration", 0)), desc, ex,
                                [trial_view(trial_by_ep[e]) for e in ex if e in trial_by_ep],
                                classify_stage(label, desc, phase), phase, rec,
                                str(dc.get("recoverability_reason") or "")))

    plans = sorted(mine("data_plan"), key=lambda r: int(_g(r, "id")))
    prev = [{"id": int(_g(p, "id")), "iteration": int(_g(p, "iteration", 0)), "data_type": _g(p, "data_type"),
             "amount": int(_g(p, "amount", 0)), "status": _g(p, "status"),
             "approved_cents": int(_g(p, "approved_cents", 0)),
             "effect": _tdm70_plan_effect(int(_g(p, "iteration", 0)), evals, mine("training_run"), summ)}
            for p in plans]
    offers = [{"id": int(_g(o, "id")), "seller_id": int(_g(o, "seller_id", 0)),
               "price_per_episode_cents": int(_g(o, "price_per_episode_cents", 0)),
               "needed_count": int(_g(o, "needed_count", 0)), "status": _g(o, "status")}
              for o in mine("dataset_offer") if _g(o, "status") not in ("rejected", "cancelled", "done")]
    sellers = sum(1 for c in rows("contributor") if _g(c, "kind") == "dataset_seller")

    # Override: the run_settings.data_type_override column (TDM-20 PATCH /settings, the dashboard's
    # "Data type" choice) when set; else hyperparams.data_type_override, or the latest
    # planner.data_type_override event.
    col = str(_g(rs, "data_type_override", "") or "").strip() if rs is not None else ""
    override = hp.get("data_type_override") if hp.get("data_type_override") in DATA_TYPES else None
    for e in sorted(rows("event_log"), key=lambda r: int(_g(r, "id"))):
        p = _payload(e) if _g(e, "type") == OVERRIDE_EVENT else {}
        if p and _same(p.get("ticket_id"), tid):
            override = p.get("data_type") if p.get("data_type") in DATA_TYPES else None
    if col in DATA_TYPES:
        override = col
    note = str(diag.get("reasoning"))[:4000] if diag and diag.get("reasoning") else None

    return PlannerInputs(
        ticket_id=tid, iteration=it, task_description=str(_g(ticket, "task_description", "")),
        target_success=float(_g(ticket, "target_success", 0.0)),
        budget_cents=int(_g(ticket, "budget_cents", 0)), approved_cents=int(_g(ticket, "approved_cents", 0)),
        spent_cents=int(_g(ticket, "spent_cents", 0)),
        target_episodes=int(_g(rs, "target_episodes", 5)) if rs is not None else 5,
        eval_trials=int(_g(rs, "eval_trials", 10)) if rs is not None else 10,
        success_threshold=float(_g(rs, "success_threshold", 0.8)) if rs is not None else 0.8,
        require_ci_low=bool(_g(rs, "require_ci_low", False)) if rs is not None else False,
        max_iterations=int(_g(rs, "max_iterations", 3)) if rs is not None else 3,
        dataset_accepted=int(_g(ds, "accepted_count", 0)) if ds is not None else 0,
        dataset_total=int(_g(ds, "total_count", 0)) if ds is not None else 0,
        clusters=clusters, latest_eval=summ(latest), baseline_eval=summ(baseline),
        failed_trials=[trial_view(r) for r in failed],
        previous_plans=prev, dataset_offers=offers, dataset_sellers=sellers,
        data_type_override=override, diagnosis_note=note, cluster_iteration=citer,
    )


def anthropic_client() -> Any:
    """The real client: API key from env / repo .env (rdf_common settings), never printed."""
    import anthropic
    from pydantic import Field, SecretStr
    from rdf_common.settings import RdfSettings, load_settings

    class _Tdm24ClaudeSettings(RdfSettings):
        anthropic_api_key: SecretStr = Field(min_length=1)

    s = load_settings(_Tdm24ClaudeSettings, "orchestrator.planner")
    return anthropic.Anthropic(api_key=s.anthropic_api_key.get_secret_value(), max_retries=3, timeout=600)
