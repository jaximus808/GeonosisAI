"""TDM-71 ticket report: what data was bought, why, what it cost, and success before and after.

The report is the customer's deliverable ("a deployed checkpoint plus a report"), so it only *joins*
rows that already exist in SpacetimeDB. It writes nothing, invents no numbers and never fills a gap
with a default: a missing link goes in ``missing`` and shows up in the report. Every derived string
(``ci_text``, ``verdict``) is computed here from row counts, never in the UI.

Inputs (all by ``ticket_id``): ticket, run_settings, robot, eval_run + eval_trial, failure_cluster,
data_plan, task, episode, review, payment, dataset_offer, training_run, agent_session, and the whole
``event_log`` (diagnose.clusters reasoning, planner.data_plan evidence, spend.approved,
ticket.status_changed, flywheel.shipped, report.saved).

Shape (``assemble``)::

    {report_version, ticket_id, generated_at,
     fake: {any, banner, components[]},          # [FAKE]-tagged harness data, said loudly
     mocked: [...],                              # e.g. Stripe TEST MODE payouts
     task, robot, outcome,
     baseline: {eval_run_id, rate, ci:[lo,hi], ci_text, trials, successes, ...},
     final:    {... same, the eval of the deployed checkpoint ...},
     iterations: [{iteration, diagnosis, data_plan, tasks, episodes_bought, episodes_accepted,
                   episodes_rejected[{episode_id, reason}], dataset_offers, cost_cents{data, gpu, total},
                   training_run, eval}],
     total_cost: {data{...}, gpu{...}, total_cents, budget_cents, ...},
     deployed_checkpoint, agents, timeline[], event_log{...seal + ticket span}, event_log_hash,
     saved_reports[], missing[]}

``render_markdown(report)`` turns it into the saved/served markdown. ``chain_summary`` is the
``rdf-evlog-chain-v1`` hash (TDM-77, ``apps/web/src/lib/eventlog/chain.ts``): the same algorithm
over every event_log row, so the report's seal can be checked against the /events page.

Pure: ``assemble(tables, ticket_id)`` takes SQL rows as dicts (snake_case, options as value/None,
timestamps as int micros). ``load_tables(sql, ticket_id)`` fetches them with any async ``sql``.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import re
import time
from typing import Any, Awaitable, Callable

from .stats import explain_threshold, format_ci, meets_threshold

REPORT_VERSION = "rdf-report-v1"
CHAIN_ALGORITHM = "rdf-evlog-chain-v1"
DATA_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
# payment.status values that are money committed (paid, or reserved and on its way).
COMMITTED = ("paid", "pending", "awaiting_onboarding")
NOT_SPENT = ("failed", "reversed", "blocked_budget")
TIMELINE_TYPES = ("ticket.created", "ticket.status_changed", "ticket.run_settings_updated", "eval.finalized",
                  "diagnose.clusters", "diagnose.no_failures", "planner.data_plan", "planner.blocked",
                  "planner.error", "spend.approved", "flywheel.shipped", "report.saved")
TIMELINE_PREFIXES = ("training.",)
TICKET_TABLES = ("run_settings", "eval_run", "failure_cluster", "data_plan", "task", "episode", "review",
                 "payment", "dataset_offer", "training_run", "agent_session")

Tables = dict[str, list[dict]]

_FAKE_JSON = re.compile(r'"fake"\s*:\s*true')


# --------------------------------------------------------------------------- helpers

def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _json(v: Any) -> Any:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def _payload(e: dict) -> dict:
    p = _json(e.get("payload_json"))
    return p if isinstance(p, dict) else {}


def _ids(v: Any) -> list[int]:
    if isinstance(v, str):
        v = _json(v) or []
    return [_i(x) for x in (v or []) if _i(x)]


def _ref(e: dict | None) -> dict | None:
    if e is None:
        return None
    return {"event_id": _i(e["id"]), "ts": _i(e["ts"]), "actor": e.get("actor"), "type": e.get("type")}


def _key(r: dict) -> tuple[int, int]:
    return (_i(r.get("created_at")), _i(r.get("id")))


def iso(micros: Any) -> str | None:
    m = _i(micros)
    if not m:
        return None
    return _dt.datetime.fromtimestamp(m / 1_000_000, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def usd(cents: Any) -> str:
    c = _i(cents)
    return f"-${abs(c) / 100:,.2f}" if c < 0 else f"${c / 100:,.2f}"


def fake_markers(row: dict) -> list[str]:
    """Why a row looks like harness data: the TDM-28 fake robot / stub Modal / scripted driver tags
    (``[FAKE]`` text, ``"fake": true`` JSON, ``fake://`` or ``FAKE-`` URIs, ``fake_*`` embodiments,
    ``stub-`` Modal call ids). Empty list = no marker found."""
    found: list[str] = []
    for k, v in row.items():
        if isinstance(v, str):
            if "[FAKE" in v:
                found.append(f"{k}: [FAKE]")
            elif _FAKE_JSON.search(v):
                found.append(f'{k}: "fake": true')
            elif "fake://" in v or "FAKE-" in v:
                found.append(f"{k}: fake uri")
            elif k in ("embodiment", "adapter_version") and v.lower().startswith("fake"):
                found.append(f"{k}: {v}")
            elif k == "name" and v.lower().startswith("fake-"):
                found.append(f"{k}: {v}")
            elif k == "modal_call_id" and v.startswith("stub-"):
                found.append(f"{k}: stub")
    return found


# --------------------------------------------------------------------------- event_log seal

def _field(s: str) -> str:
    return f"{len(s.encode('utf-8'))}:{s}"


def canonical_record(id_: int, ts_micros: int, actor: str, type_: str, payload_json: str) -> bytes:
    return f"{id_}\n{ts_micros}\n{_field(actor)}\n{_field(type_)}\n{_field(payload_json)}".encode("utf-8")


def chain_summary(rows: list[dict]) -> dict:
    """``rdf-evlog-chain-v1`` over every event_log row in id order (TDM-77's seal, byte for byte)."""
    rows = sorted(rows, key=lambda r: _i(r["id"]))
    h = bytes(32)
    for r in rows:
        rec = canonical_record(_i(r["id"]), _i(r["ts"]), str(r.get("actor") or ""), str(r.get("type") or ""),
                               str(r.get("payload_json") or ""))
        h = hashlib.sha256(h + rec).digest()
    ids = [_i(r["id"]) for r in rows]
    return {"algorithm": CHAIN_ALGORITHM, "count": len(rows), "first_id": ids[0] if ids else None,
            "last_id": ids[-1] if ids else None,
            "first_ts": _i(rows[0]["ts"]) if rows else None, "last_ts": _i(rows[-1]["ts"]) if rows else None,
            "gaps": [[a, b] for a, b in zip(ids, ids[1:]) if b - a > 1], "hash": h.hex()}


# --------------------------------------------------------------------------- views

def _eval_view(r: dict | None, trials: list[dict], finalized: dict | None) -> dict | None:
    if r is None:
        return None
    s, n = _i(r.get("successes")), _i(r.get("trials"))
    done = r.get("status") == "done"
    view = {
        "eval_run_id": _i(r["id"]), "kind": r.get("kind"), "status": r.get("status"),
        "checkpoint_uri": r.get("checkpoint_uri"), "training_run_id": _i(r.get("training_run_id")),
        "trials": n, "completed": _i(r.get("completed")), "successes": s,
        "rate": (s / n) if n and done else None,
        "ci": [r.get("ci_low"), r.get("ci_high")] if done else None,
        "ci_text": format_ci(s, n) if n and done and s <= n else None,
        "created_at": _i(r.get("created_at")), "finished_at": _i(r.get("finished_at")) or None,
        "summary": _payload(finalized).get("summary") if finalized else None,
        "finalized_event": _ref(finalized),
        "trials_recorded": len(trials),
        "trial_outcomes": [{"trial_index": _i(t.get("trial_index")), "success": bool(t.get("success")),
                            "episode_id": _i(t.get("episode_id")), "failure_note": t.get("failure_note")}
                           for t in trials],
        "fake_trials": sum(1 for t in trials if fake_markers(t)),
    }
    return view


def _tdm42_by_source(runs: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in runs:
        c = _i(r.get("gpu_cost_cents"))
        src = (_json(r.get("metrics_json")).get("cost") or {}).get("source") or ("unlabelled" if c else "none")
        out[src] = out.get(src, 0) + c
    return out


def _tdm42_cost_label(c: dict) -> str:
    """``estimate: 482.8 s x Modal rates (GPU H100 $0.53 + CPU/mem $0.20)`` or ``Modal billing ...``."""
    usd2 = lambda cents: f"${float(cents or 0) / 100:.2f}"  # noqa: E731
    if c.get("source") == "modal_billing":
        return f"Modal billing, app {(c.get('billing') or {}).get('app_id')}; GPU {usd2(c.get('gpu_cents'))}"
    rest = float(c.get("cpu_cents") or 0) + float(c.get("mem_cents") or 0)
    return (f"estimate: {c.get('seconds')} s x Modal rates as of {(c.get('rates') or {}).get('as_of', '?')}; "
            f"GPU {c.get('gpu') or 'none'} {usd2(c.get('gpu_cents'))} + reserved CPU/mem {usd2(rest)}")


def _training_view(r: dict | None) -> dict | None:
    if r is None:
        return None
    started, finished = _i(r.get("started_at")), _i(r.get("finished_at"))
    return {"training_run_id": _i(r["id"]), "iteration": _i(r.get("iteration")), "status": r.get("status"),
            "episodes": len(_ids(r.get("episode_ids"))), "episode_ids": _ids(r.get("episode_ids")),
            "base_checkpoint_uri": r.get("base_checkpoint_uri"), "checkpoint_uri": r.get("checkpoint_uri"),
            "modal_call_id": r.get("modal_call_id"), "gpu_cost_cents": _i(r.get("gpu_cost_cents")),
            "hyperparams": _json(r.get("hyperparams_json")), "metrics": _json(r.get("metrics_json")),
            "error": r.get("error"), "created_at": _i(r.get("created_at")), "started_at": started or None,
            "finished_at": finished or None,
            "duration_s": round((finished - started) / 1e6, 1) if started and finished else None,
            # TDM-42: how gpu_cost_cents was computed (source estimate | modal_billing, formula, GPU part)
            "cost": _json(r.get("metrics_json")).get("cost"),
            "fake": bool(fake_markers(r))}


# --------------------------------------------------------------------------- the assembler

def _tdm95_offer_view(o: dict, mine: Tables, events: list, settings: dict | None) -> dict:
    """TDM-95: one dataset offer for the report, listed apart from paid demos: the counts, the
    refused deliveries (``dataset_offer.delivery``), its plan decision / timeout and the seller payout."""
    from .plan_offers import EVENT_CREATED, EVENT_TIMEOUT, offer_line

    oid = _i(o["id"])
    eps = [e for e in mine["episode"] if _i(e.get("dataset_offer_id")) == oid and e.get("kind") == "purchased"]
    reviews = {_i(r.get("episode_id")): r for r in mine["review"]}
    mine_ev = [(e, p) for e, p in events if _i(p.get("offer_id")) == oid]
    refused = [x for e, p in mine_ev if e.get("type") == "dataset_offer.delivery" for x in (p.get("refused") or [])]
    created = next((p for e, p in mine_ev if e.get("type") == EVENT_CREATED), None)
    timeout = next((p for e, p in mine_ev if e.get("type") == EVENT_TIMEOUT), None)
    pays = [p for p in mine["payment"] if _i(p.get("dataset_offer_id")) == oid]
    origin = (f"planner suggestion on data_plan {created.get('data_plan_id')}, after approve_spend" if created else "")
    if timeout:
        origin += ("; " if origin else "") + f"timed out after {timeout.get('waited_s')} s: {timeout.get('action')}"
    target = _i((settings or {}).get("target_episodes")) or None
    return {"dataset_offer_id": oid, "seller_id": _i(o.get("seller_id")), "status": o.get("status"),
            "price_per_episode_cents": _i(o.get("price_per_episode_cents")), "needed_count": _i(o.get("needed_count")),
            "ingested_count": _i(o.get("ingested_count")), "accepted_count": _i(o.get("accepted_count")),
            "refused_at_ingest": refused, "line": offer_line(o, eps, reviews, refused, target),
            "origin": origin or None, "data_plan_id": (created or {}).get("data_plan_id"),
            "payout_cents": sum(_i(p.get("amount_cents")) for p in pays if p.get("status") in COMMITTED),
            "payments": [{"payment_id": _i(p["id"]), "amount_cents": _i(p.get("amount_cents")),
                          "status": p.get("status")} for p in pays]}


def assemble(t: Tables, ticket_id: int, *, now_micros: int | None = None) -> dict:
    tid = int(ticket_id)
    tickets = [r for r in t.get("ticket", []) if _i(r.get("id")) == tid]
    if not tickets:
        raise LookupError(f"no ticket {tid}")
    ticket = tickets[0]
    mine = {n: sorted((r for r in t.get(n, []) if _i(r.get("ticket_id")) == tid), key=_key) for n in TICKET_TABLES}
    settings = mine["run_settings"][0] if mine["run_settings"] else None
    robot = next((r for r in t.get("robot", []) if _i(r.get("id")) == _i(ticket.get("robot_id"))), None)
    missing: list[str] = []
    # TDM-107: every eval / episode is labelled by the robot it ran on (sim robot or real YAM). The
    # before/after numbers use the ticket's current mode only; the other mode is listed apart.
    from .robot_mode import include_sim_data, robot_mode, row_mode, same_mode_evals

    robots = {_i(r.get("id")): r for r in t.get("robot", [])}
    mode = robot_mode(robot)

    all_events = t.get("event_log", [])
    events = sorted(((e, _payload(e)) for e in all_events), key=lambda x: (_i(x[0]["ts"]), _i(x[0]["id"])))
    events = [(e, p) for e, p in events if _i(p.get("ticket_id")) == tid]

    def of_type(typ: str, **match: Any) -> list[tuple[dict, dict]]:
        return [(e, p) for e, p in events
                if e.get("type") == typ and all(_i(p.get(k), -1) == int(v) for k, v in match.items())]

    # ---- evals
    trials_by_run: dict[int, list[dict]] = {}
    run_ids = {_i(r["id"]) for r in mine["eval_run"]}
    for tr in t.get("eval_trial", []):
        if _i(tr.get("eval_run_id")) in run_ids:
            trials_by_run.setdefault(_i(tr["eval_run_id"]), []).append(tr)
    for rows in trials_by_run.values():
        rows.sort(key=lambda r: _i(r.get("trial_index")))

    def eval_view(r: dict | None) -> dict | None:
        if r is None:
            return None
        fin = of_type("eval.finalized", eval_run_id=_i(r["id"]))
        v = _eval_view(r, trials_by_run.get(_i(r["id"]), []), fin[-1][0] if fin else None)
        v["robot_id"] = _i(r.get("robot_id")) or None
        v["robot_mode"] = row_mode(r, robots)
        return v

    mode_evals = same_mode_evals(mine["eval_run"], robots, mode)  # TDM-107: never mix sim into real numbers
    done_evals = [r for r in mode_evals if r.get("status") == "done"]
    baseline_rows = [r for r in mode_evals if r.get("kind") == "baseline"]
    baseline_row = ([r for r in baseline_rows if r.get("status") == "done"] or baseline_rows or [None])[0]
    if baseline_row is None:
        missing.append(f"no baseline eval_run{f' on the {mode} robot' if mode else ''}: the before number does not exist yet"
                       + (" (evals on the other robot mode are listed under robot_mode.by_mode)"
                          if len(mode_evals) < len(mine["eval_run"]) else ""))
    elif baseline_row.get("status") != "done":
        missing.append(f"baseline eval_run {baseline_row['id']} is {baseline_row.get('status')}, not finalized")

    # ---- training runs, plans, iterations
    runs_by_id = {_i(r["id"]): r for r in mine["training_run"]}
    plans = mine["data_plan"]
    plan_by_id = {_i(p["id"]): p for p in plans}
    iterations = sorted({_i(p.get("iteration")) for p in plans}
                        | {_i(c.get("iteration")) for c in mine["failure_cluster"]}
                        | {_i(r.get("iteration")) for r in mine["training_run"]}
                        | {_i(p.get("iteration")) for e, p in events
                           if e.get("type") in ("diagnose.clusters", "planner.blocked") and p.get("iteration") is not None})

    def iteration_at(ts: int) -> int | None:
        """The iteration of the newest data_plan created at or before ``ts``."""
        best = None
        for p in plans:
            if _i(p.get("created_at")) <= ts:
                best = _i(p.get("iteration"))
        return best

    tasks_by_id = {_i(x["id"]): x for x in mine["task"]}

    def task_iteration(task_id: int) -> tuple[int | None, str]:
        x = tasks_by_id.get(task_id)
        if x is None:
            return None, "unknown task"
        p = plan_by_id.get(_i(x.get("data_plan_id")))
        if p is not None:
            return _i(p.get("iteration")), f"task {task_id} -> data_plan {p['id']}"
        it = iteration_at(_i(x.get("created_at")))
        return it, f"task {task_id} created after the iteration-{it} plan"

    offers_by_id = {_i(o["id"]): o for o in mine["dataset_offer"]}

    # ---- the data: episodes the factory bought, their review and payment
    reviews_by_ep: dict[int, dict] = {}
    for rv in mine["review"]:
        reviews_by_ep[_i(rv.get("episode_id"))] = rv  # sorted by created_at: the newest wins
    payments_by_ep: dict[int, list[dict]] = {}
    for pm in mine["payment"]:
        if _i(pm.get("episode_id")):
            payments_by_ep.setdefault(_i(pm["episode_id"]), []).append(pm)

    data_eps = [e for e in mine["episode"] if e.get("kind") in DATA_KINDS]
    ep_iteration: dict[int, int | None] = {}
    episode_views: list[dict] = []
    for ep in data_eps:
        eid = _i(ep["id"])
        if _i(ep.get("task_id")):
            it, how = task_iteration(_i(ep["task_id"]))
        elif _i(ep.get("dataset_offer_id")) and _i(ep["dataset_offer_id"]) in offers_by_id:
            o = offers_by_id[_i(ep["dataset_offer_id"])]
            it, how = iteration_at(_i(o.get("created_at"))), f"dataset_offer {o['id']}"
        else:
            it, how = iteration_at(_i(ep.get("created_at"))), "no task: by time after the plan"
        if it is None:
            it, how = (iterations[0] if iterations else 0), "before any data_plan"
        ep_iteration[eid] = it
        rv = reviews_by_ep.get(eid)
        pays = payments_by_ep.get(eid, [])
        episode_views.append({
            "episode_id": eid, "iteration": it, "linked_by": how, "kind": ep.get("kind"),
            "task_id": _i(ep.get("task_id")), "contributor_id": _i(ep.get("contributor_id")),
            "dataset_offer_id": _i(ep.get("dataset_offer_id")), "review_status": ep.get("review_status"),
            "review": ({"review_id": _i(rv["id"]), "accepted": bool(rv.get("accepted")), "reason": rv.get("reason"),
                        "reviewer": rv.get("reviewer"), "metrics": _json(rv.get("metrics_json")),
                        "created_at": _i(rv.get("created_at"))} if rv else None),
            "payments": [{"payment_id": _i(p["id"]), "amount_cents": _i(p.get("amount_cents")), "status": p.get("status"),
                          "provider": p.get("provider"), "provider_ref": p.get("provider_ref")} for p in pays],
            "paid_cents": sum(_i(p.get("amount_cents")) for p in pays if p.get("status") in COMMITTED),
            "duration_ms": _i(ep.get("duration_ms")), "frame_count": _i(ep.get("frame_count")),
            "storage_uri": ep.get("storage_uri"), "created_at": _i(ep.get("created_at")),
            "fake": bool(fake_markers(ep)),
            "robot_id": _i(ep.get("robot_id")) or None, "robot_mode": row_mode(ep, robots),  # TDM-107
        })

    # ---- every payment, assigned to an iteration
    payment_views: list[dict] = []
    for pm in mine["payment"]:
        eid, oid, ref = _i(pm.get("episode_id")), _i(pm.get("dataset_offer_id")), str(pm.get("batch_ref") or "")
        if eid:
            what, it = "episode", ep_iteration.get(eid)
        elif oid:
            what, it = "dataset_batch", iteration_at(_i(offers_by_id.get(oid, {}).get("created_at")))
        elif ref.startswith("task:"):
            what, it = "task", task_iteration(_i(ref.split(":", 1)[1]))[0]
        else:
            what, it = "other", None
        if it is None:
            it = iteration_at(_i(pm.get("created_at")))
        payment_views.append({"payment_id": _i(pm["id"]), "for": what, "episode_id": eid, "dataset_offer_id": oid,
                              "batch_ref": pm.get("batch_ref"), "iteration": it, "contributor_id": _i(pm.get("contributor_id")),
                              "amount_cents": _i(pm.get("amount_cents")), "provider": pm.get("provider"),
                              "provider_ref": pm.get("provider_ref"), "status": pm.get("status"), "error": pm.get("error"),
                              "created_at": _i(pm.get("created_at"))})

    # ---- per-iteration blocks
    iter_blocks: list[dict] = []
    for it in iterations:
        diag = of_type("diagnose.clusters", iteration=it)
        de, dp = diag[-1] if diag else (None, {})
        clusters = [c for c in mine["failure_cluster"] if _i(c.get("iteration")) == it]
        it_plans = [p for p in plans if _i(p.get("iteration")) == it]
        plan = it_plans[-1] if it_plans else None
        plan_view = None
        if plan is not None:
            pe = of_type("planner.data_plan", data_plan_id=_i(plan["id"]))
            pp = pe[-1][1] if pe else {}
            evid = pp.get("evidence") if isinstance(pp.get("evidence"), dict) else {}
            model = evid.get("model") if isinstance(evid.get("model"), dict) else {}
            approvals = [{**_ref(e), "approved_by": p.get("approved_by"), "approved_cents": p.get("approved_cents"),
                          "via": p.get("via")} for e, p in of_type("spend.approved", data_plan_id=_i(plan["id"]))]
            plan_view = {
                "data_plan_id": _i(plan["id"]), "all_plan_ids": [_i(p["id"]) for p in it_plans],
                "data_type": plan.get("data_type"), "amount": _i(plan.get("amount")),
                "unit_price_cents": _i(plan.get("unit_price_cents")), "est_cost_cents": _i(plan.get("est_cost_cents")),
                "reasoning": plan.get("reasoning"), "status": plan.get("status"),
                "failure_cluster_ids": _ids(plan.get("failure_cluster_ids")),
                "approved_cents": _i(plan.get("approved_cents")), "approved_by": plan.get("approved_by"),
                "approved_at": _i(plan.get("approved_at")) or None, "approvals": approvals,
                "created_at": _i(plan.get("created_at")),
                "planner_event": _ref(pe[-1][0]) if pe else None,
                "planner": evid.get("planner"),
                # backend: api | droid | claude_code (rdf_common.llm); absent on plans made before the switch existed
                "model": {k: model.get(k) for k in ("requested", "served", "effort", "backend", "backend_model")
                          if k in model or k in ("requested", "served", "effort")} if model else None,
                "guardrail": evid.get("guardrail"), "budget_math": evid.get("budget_math"),
                # A stand-in decision (the TDM-28 scripted driver tags its reasoning "[FAKE ...]"),
                # as opposed to a real planner run that merely quotes [FAKE] trial notes.
                "fake": str(plan.get("reasoning") or "").lstrip().startswith("[FAKE")
                or evid.get("fake") is True or pp.get("fake") is True,
            }
            if pe and pp.get("reasoning") is not None and pp.get("reasoning") != plan.get("reasoning"):
                missing.append(f"iteration {it}: planner.data_plan event reasoning differs from data_plan {plan['id']}")
            if not pe:
                missing.append(f"iteration {it}: data_plan {plan['id']} has no planner.data_plan event")
        else:
            missing.append(f"iteration {it}: no data_plan recorded")
        if de is None and clusters:
            missing.append(f"iteration {it}: no diagnose.clusters event (the clustering reasoning is not on record)")

        plan_ids = {_i(p["id"]) for p in it_plans}
        it_tasks = [x for x in mine["task"] if _i(x.get("data_plan_id")) in plan_ids
                    or (not _i(x.get("data_plan_id")) and iteration_at(_i(x.get("created_at"))) == it)]
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for x in it_tasks:
            by_status[x.get("status")] = by_status.get(x.get("status"), 0) + 1
            by_type[x.get("type")] = by_type.get(x.get("type"), 0) + 1

        eps = [v for v in episode_views if v["iteration"] == it]
        accepted = [v for v in eps if v["review_status"] == "accepted"]
        rejected = [{"episode_id": v["episode_id"], "kind": v["kind"],
                     "reason": (v["review"] or {}).get("reason"), "reviewer": (v["review"] or {}).get("reviewer")}
                    for v in eps if v["review_status"] == "rejected"]
        pays = [p for p in payment_views if p["iteration"] == it]
        data_committed = sum(p["amount_cents"] for p in pays if p["status"] in COMMITTED)
        data_paid = sum(p["amount_cents"] for p in pays if p["status"] == "paid")

        it_runs = [r for r in mine["training_run"] if _i(r.get("iteration")) == it]
        run = ([r for r in it_runs if r.get("status") == "done"] or it_runs or [None])[-1]
        gpu = sum(_i(r.get("gpu_cost_cents")) for r in it_runs)
        it_run_ids = {_i(r["id"]) for r in it_runs}
        cand = [r for r in mode_evals if _i(r.get("training_run_id")) in it_run_ids]
        cand_row = ([r for r in cand if r.get("status") == "done"] or cand or [None])[-1]
        offers = [_tdm95_offer_view(o, mine, events, settings)
                  for o in mine["dataset_offer"] if iteration_at(_i(o.get("created_at"))) == it]

        iter_blocks.append({
            "iteration": it,
            "diagnosis": {
                "event": _ref(de), "model": dp.get("model"), "reasoning": dp.get("reasoning"),
                "backend": (dp.get("usage") or {}).get("backend") if isinstance(dp.get("usage"), dict) else None,
                "source_eval_run_id": dp.get("eval_run_id"),
                "clusters": [{"failure_cluster_id": _i(c["id"]), "label": c.get("label"), "count": _i(c.get("count")),
                              "description": c.get("description") or None,
                              "example_episode_ids": _ids(c.get("example_episode_ids")), "fake": bool(fake_markers(c))}
                             for c in sorted(clusters, key=lambda c: (-_i(c.get("count")), _i(c["id"])))],
            },
            "data_plan": plan_view,
            "tasks": {"count": len(it_tasks), "by_status": by_status, "by_type": by_type,
                      "task_ids": [_i(x["id"]) for x in it_tasks]},
            "episodes_bought": len(eps),
            "episodes_accepted": len(accepted),
            "episodes_sim": sum(1 for v in eps if v["robot_mode"] == "sim"),  # TDM-107
            "episodes_rejected": rejected,
            "episodes": eps,
            "dataset_offers": offers,
            "payments": pays,
            "cost_cents": {"data": data_committed, "data_paid": data_paid, "gpu": gpu, "total": data_committed + gpu},
            "training_run": _training_view(run),
            "training_run_ids": sorted(it_run_ids),
            "eval": eval_view(cand_row),
        })
        if run is not None and run.get("status") == "done" and not _i(run.get("gpu_cost_cents")):
            missing.append(f"iteration {it}: training_run {run['id']} is done but gpu_cost_cents is 0")

    # ---- deployed checkpoint and the final (after) eval
    shipped_ev = of_type("flywheel.shipped")
    sp = shipped_ev[-1][1] if shipped_ev else {}
    deployed = None
    if ticket.get("deployed_checkpoint_uri"):
        deployed = {"uri": ticket["deployed_checkpoint_uri"], "source": "ticket.deployed_checkpoint_uri"}
    elif sp.get("checkpoint_uri"):
        deployed = {"uri": sp["checkpoint_uri"], "source": f"flywheel.shipped event {shipped_ev[-1][0]['id']}"}
    elif ticket.get("status") == "shipped":
        last = next((r for r in reversed(done_evals) if r.get("kind") != "baseline"), None)
        if last is not None:
            deployed = {"uri": last.get("checkpoint_uri"), "source": f"newest finalized candidate eval_run {last['id']}"}
    final_row = None
    if deployed is not None:
        tr_id = _i(sp.get("training_run_id")) or next(
            (_i(r["id"]) for r in mine["training_run"] if r.get("checkpoint_uri") == deployed["uri"]), 0)
        deployed["training_run_id"] = tr_id or None
        cands = [r for r in done_evals if r.get("kind") != "baseline"
                 and (r.get("checkpoint_uri") == deployed["uri"] or (tr_id and _i(r.get("training_run_id")) == tr_id))]
        final_row = cands[-1] if cands else None
        deployed["eval_run_id"] = _i(final_row["id"]) if final_row else None
        deployed["fake"] = bool(fake_markers({"uri": deployed["uri"]})) or bool(
            tr_id and fake_markers(runs_by_id.get(tr_id, {})))
        if ticket.get("status") == "shipped" and not ticket.get("deployed_checkpoint_uri"):
            deployed["note"] = ("ticket.deployed_checkpoint_uri is empty (TDM-70 mark_checkpoint_deployed has not "
                                "run for this ticket); taken from " + deployed["source"])
    else:
        if ticket.get("status") == "shipped":
            missing.append("ticket is shipped but no checkpoint can be traced")
        final_row = next((r for r in reversed(done_evals) if r.get("kind") != "baseline"), None)
    baseline = eval_view(baseline_row)
    final = eval_view(final_row)
    if final is not None:
        final["is_deployed_checkpoint"] = deployed is not None and final["eval_run_id"] == deployed.get("eval_run_id")

    # ---- outcome
    outcome: dict[str, Any] = {"status": ticket.get("status"), "iteration": _i(ticket.get("iteration")),
                               "target_success": ticket.get("target_success"),
                               "success_threshold": (settings or {}).get("success_threshold"),
                               "require_ci_low": (settings or {}).get("require_ci_low")}
    if final_row is not None and final_row.get("status") == "done":
        outcome["met_target"] = meets_threshold(final_row, settings or ticket)
        outcome["verdict"] = explain_threshold(final_row, settings or ticket)
    else:
        outcome["met_target"] = None
        outcome["verdict"] = "no finalized after-training eval yet"
    if baseline and final and baseline["rate"] is not None and final["rate"] is not None:
        outcome["headline"] = f"{baseline['ci_text']} before -> {final['ci_text']} after"
        outcome["delta_rate"] = final["rate"] - baseline["rate"]

    # ---- totals
    by_status: dict[str, int] = {}
    by_for: dict[str, int] = {}
    for p in payment_views:
        by_status[p["status"]] = by_status.get(p["status"], 0) + p["amount_cents"]
        if p["status"] in COMMITTED:
            by_for[p["for"]] = by_for.get(p["for"], 0) + p["amount_cents"]
    data_committed = sum(v for k, v in by_status.items() if k in COMMITTED)
    pre_plan = sum(p["amount_cents"] for p in payment_views if p["iteration"] is None and p["status"] in COMMITTED)
    gpu_total = sum(_i(r.get("gpu_cost_cents")) for r in mine["training_run"])
    providers = sorted({p["provider"] for p in payment_views if p["provider"]})
    total_cost = {
        "data": {"committed_cents": data_committed, "paid_cents": by_status.get("paid", 0),
                 "pending_cents": by_status.get("pending", 0) + by_status.get("awaiting_onboarding", 0),
                 "not_spent_cents": sum(v for k, v in by_status.items() if k in NOT_SPENT),
                 "by_status_cents": by_status, "committed_by_kind_cents": by_for,
                 "before_first_plan_cents": pre_plan,
                 "payment_count": len(payment_views), "providers": providers},
        "gpu": {"cents": gpu_total, "runs": len(mine["training_run"]),
                "run_ids": [_i(r["id"]) for r in mine["training_run"]],
                # TDM-42: estimate (runtime x Modal rates) vs modal_billing; "unlabelled" = written before TDM-42
                "by_source_cents": _tdm42_by_source(mine["training_run"])},
        "total_cents": data_committed + gpu_total,
        "budget_cents": _i(ticket.get("budget_cents")), "approved_cents": _i(ticket.get("approved_cents")),
        "ticket_spent_cents": _i(ticket.get("spent_cents")),
        "note": ("data = payouts paid or reserved (pending/awaiting_onboarding); gpu = sum of training_run.gpu_cost_cents "
                 "(Modal compute: GPU + reserved CPU/memory; see gpu.by_source_cents)"),
    }
    if not data_committed:
        missing.append("no data spend recorded (no paid or reserved payment rows)")
    if mine["training_run"] and not gpu_total:
        missing.append("no GPU cost recorded (every training_run.gpu_cost_cents is 0)")

    # ---- fake / mocked inputs: say so loudly
    fake_components: list[dict] = []

    def flag(component: str, rows: list[dict], id_key: str = "id") -> None:
        hits = [(r, fake_markers(r)) for r in rows]
        hits = [(r, m) for r, m in hits if m]
        if hits:
            fake_components.append({"component": component, "fake_rows": len(hits), "total_rows": len(rows),
                                    "ids": [_i(r.get(id_key)) for r, _ in hits][:20],
                                    "markers": sorted({x for _, m in hits for x in m})[:6]})

    flag("ticket", [ticket])
    if robot is not None:
        flag("robot", [robot])
    flag("eval_trial", [tr for rows in trials_by_run.values() for tr in rows])
    for name in ("episode", "failure_cluster", "data_plan", "review", "training_run", "task", "dataset_offer"):
        flag(name, mine[name])
    fake_events = [e for e, p in events if p.get("fake") is True or str(e.get("actor") or "").startswith("fake")
                   or "[FAKE" in str(e.get("payload_json") or "")]
    if fake_events:
        fake_components.append({"component": "event_log", "fake_rows": len(fake_events), "total_rows": len(events),
                                "ids": [_i(e["id"]) for e in fake_events][:20],
                                "markers": sorted({str(e.get("actor")) for e in fake_events})[:6]})
    is_fake = bool(fake_components)
    banner = None
    if is_fake:
        banner = ("FAKE INPUTS: this report is built from [FAKE]-tagged harness data ("
                  + ", ".join(f"{c['component']} {c['fake_rows']}/{c['total_rows']}" for c in fake_components)
                  + "). Its numbers are not robot results and must not be presented as real.")

    mocked: list[dict] = []
    test_pays = [p for p in payment_views if str(p["provider"] or "").endswith("_test")]
    if test_pays:
        mocked.append({"component": "payments", "detail": f"Stripe TEST MODE ({', '.join(providers)}): "
                       f"{len(test_pays)} payment(s), {usd(sum(p['amount_cents'] for p in test_pays))}; no real money moved",
                       "payment_ids": [p["payment_id"] for p in test_pays]})
    stub_runs = [r for r in mine["training_run"] if str(r.get("modal_call_id") or "").startswith("stub-")]
    if stub_runs:
        mocked.append({"component": "training", "detail": "stub Modal trainer (TDM-28 harness): no GPU job ran",
                       "training_run_ids": [_i(r["id"]) for r in stub_runs]})

    # ---- timeline and the event_log seal
    timeline = []
    for e, p in events:
        typ = str(e.get("type") or "")
        if typ in TIMELINE_TYPES or typ.startswith(TIMELINE_PREFIXES):
            item = {**_ref(e), "at": iso(e["ts"])}
            if typ == "ticket.status_changed":
                item.update({"from": p.get("from"), "to": p.get("to"), "note": p.get("note"),
                             "iteration": p.get("iteration")})
            else:
                for k in ("eval_run_id", "data_plan_id", "training_run_id", "approved_by", "approved_cents",
                          "checkpoint_uri", "uri", "status", "reason"):
                    if p.get(k) is not None and not (k.endswith("_id") and not _i(p[k], 1)):
                        item[k] = p[k]
            timeline.append(item)
    seal = chain_summary(all_events)
    span_types: dict[str, int] = {}
    for e, _ in events:
        span_types[e.get("type")] = span_types.get(e.get("type"), 0) + 1
    ticket_ids = sorted(_i(e["id"]) for e, _ in events)
    event_log = {
        **seal,
        "scope": "every event_log row in id order at generation time (same seal as the /events page)",
        "ticket_span": {"count": len(events), "first_event_id": ticket_ids[0] if ticket_ids else None,
                        "last_event_id": ticket_ids[-1] if ticket_ids else None,
                        "first_ts": _i(events[0][0]["ts"]) if events else None,
                        "last_ts": _i(events[-1][0]["ts"]) if events else None,
                        "first_at": iso(events[0][0]["ts"]) if events else None,
                        "last_at": iso(events[-1][0]["ts"]) if events else None,
                        "by_type": dict(sorted(span_types.items(), key=lambda kv: (-kv[1], str(kv[0]))))},
    }

    rs = settings or {}
    hyper = _json(rs.get("hyperparams_json")) or {}

    # ---- TDM-107: sim vs real, side by side and never mixed
    by_mode: dict[str, dict] = {}
    for m in ("real", "sim"):
        runs = [r for r in mine["eval_run"] if row_mode(r, robots) == m]
        done = [r for r in runs if r.get("status") == "done"]
        eps = [v for v in episode_views if v["robot_mode"] == m]
        b_row = next((r for r in done if r.get("kind") == "baseline"), None)
        cands = [r for r in done if r.get("kind") != "baseline"]
        by_mode[m] = {
            "robot_ids": sorted({_i(r.get("robot_id")) for r in runs} | {v["robot_id"] or 0 for v in eps} - {0}),
            "eval_runs": [eval_view(r) for r in runs],
            "baseline": eval_view(b_row),
            "latest_candidate": eval_view(cands[-1] if cands else None),
            "trials": sum(_i(r.get("trials")) for r in done),
            "successes": sum(_i(r.get("successes")) for r in done),
            "episodes": len(eps),
            "episodes_accepted": sum(1 for v in eps if v["review_status"] == "accepted"),
        }
    switches = [{**_ref(e), **{k: p.get(k) for k in ("from_robot_id", "from_mode", "to_robot_id", "to_mode", "status",
                                                     "iteration", "by", "reason")}}
                for e, p in of_type("ticket.robot_mode_changed")]
    robot_mode_block = {
        "current": mode,
        "headline_mode": mode,
        "note": ("before/after numbers are from the ticket's current robot mode only; "
                 "sim-robot results are never mixed into real-robot success numbers"),
        "include_sim_data": include_sim_data(settings, mode),
        "by_mode": by_mode,
        "switches": switches,
    }
    return {
        "report_version": REPORT_VERSION,
        "ticket_id": tid,
        "generated_at": now_micros if now_micros is not None else int(time.time() * 1_000_000),
        "fake": {"any": is_fake, "banner": banner, "components": fake_components},
        "mocked": mocked,
        "task": {"description": ticket.get("task_description"), "policy_prompt": hyper.get("prompt"),
                 "target_success": ticket.get("target_success"), "budget_cents": _i(ticket.get("budget_cents")),
                 "status": ticket.get("status"), "iteration": _i(ticket.get("iteration")),
                 "created_by": ticket.get("created_by"), "created_at": _i(ticket.get("created_at")),
                 "run_settings": ({k: rs.get(k) for k in ("eval_trials", "success_threshold", "require_ci_low",
                                                          "target_episodes", "max_iterations", "data_type_override")}
                                  | {"hyperparams": hyper}) if settings else None},
        "robot": ({"id": _i(robot["id"]), "name": robot.get("name"), "embodiment": robot.get("embodiment"),
                   "adapter_version": robot.get("adapter_version"), "status": robot.get("status"),
                   "fake": bool(fake_markers(robot)), "robot_mode": mode} if robot else None),
        "outcome": outcome,
        "robot_mode": robot_mode_block,
        "baseline": baseline,
        "final": final,
        "iterations": iter_blocks,
        "total_cost": total_cost,
        "deployed_checkpoint": deployed,
        "agents": [{"agent_session_id": _i(a["id"]), "role": a.get("role"), "name": a.get("name"),
                    "status": a.get("status"), "factory_session_id": a.get("factory_session_id"),
                    "current_step": a.get("current_step")} for a in mine["agent_session"]],
        "timeline": timeline,
        "event_log": event_log,
        "event_log_hash": seal["hash"],
        "saved_reports": [{**_ref(e), "uri": p.get("uri"), "json_uri": p.get("json_uri"),
                           "event_log_hash": p.get("event_log_hash"), "fake": p.get("fake")}
                          for e, p in of_type("report.saved")],
        "missing": missing,
    }


# --------------------------------------------------------------------------- markdown

def _pct(x: Any) -> str:
    return "n/a" if x is None else f"{round(float(x) * 100)}%"


def _eval_line(v: dict | None) -> str:
    if v is None:
        return "not run"
    if v["ci_text"] is None:
        return f"eval_run {v['eval_run_id']} {v['status']} ({v['completed']}/{v['trials']} trials so far)"
    return f"{v['ci_text']} (eval_run {v['eval_run_id']}, checkpoint `{v['checkpoint_uri']}`)"


def _md_text(s: Any) -> str:
    return str(s or "").replace("\r", "").strip()


def _llm_label(backend: str) -> str:
    return {"api": "Claude API", "droid": "Factory droid", "claude_code": "Claude Code (local)"}.get(backend, backend)


def render_markdown(r: dict) -> str:
    out: list[str] = []
    add = out.append
    task = r["task"]
    add(f"# Deployment report: ticket {r['ticket_id']}")
    add("")
    if r["fake"]["any"]:
        add(f"> **WARNING: {r['fake']['banner']}**")
        add("")
    for m in r["mocked"]:
        add(f"> **MOCKED ({m['component']}):** {m['detail']}")
        add("")
    add(f"**Job:** {_md_text(task['description'])}  ")
    if task.get("policy_prompt"):
        add(f"**Policy prompt:** `{task['policy_prompt']}`  ")
    rb = r["robot"]
    rm = r.get("robot_mode") or {}  # TDM-107 (absent in reports saved before it)
    if rb:
        add(f"**Robot:** {rb['name']} ({rb['embodiment']}, adapter {rb['adapter_version']}, id {rb['id']})"
            + (" **[SIM robot: MuJoCo simulation, not the real arm]**" if rm.get("current") == "sim" else "") + "  ")
    o = r["outcome"]
    add(f"**Status:** {o['status']} after {len(r['iterations'])} data iteration(s); target {_pct(o['target_success'])}  ")
    add(f"**Generated:** {iso(r['generated_at'])} ({r['report_version']})")
    add("")

    add("## Success before and after")
    add("")
    if rm.get("current"):
        add(f"These are **{rm['current']}-robot** results only"
            + (" (SIMULATED: not real-arm success numbers)" if rm["current"] == "sim" else "")
            + ". Results on the other robot mode are under *Sim vs real robot*.")
        add("")
    add("| | Result | 95% CI |")
    add("|---|---|---|")
    for name, v in (("Before (baseline)", r["baseline"]), ("After (deployed)", r["final"])):
        ci = "" if not v or not v.get("ci") else f"{_pct(v['ci'][0])}–{_pct(v['ci'][1])}"
        add(f"| {name} | {_eval_line(v)} | {ci} |")
    add("")
    add(f"Verdict: {o['verdict']}.")
    add("")

    if rm.get("by_mode"):
        add("## Sim vs real robot")
        add("")
        add("| Mode | Robots | Finalized eval trials | Baseline | Latest fine-tune eval | Episodes (accepted) |")
        add("|---|---|---|---|---|---|")
        for m in ("real", "sim"):
            bm = rm["by_mode"].get(m) or {}
            add(f"| {'**SIM**' if m == 'sim' else 'real'} | {', '.join(map(str, bm.get('robot_ids') or [])) or '-'} "
                f"| {bm.get('successes', 0)}/{bm.get('trials', 0)} | {_eval_line(bm.get('baseline'))} "
                f"| {_eval_line(bm.get('latest_candidate'))} | {bm.get('episodes', 0)} ({bm.get('episodes_accepted', 0)}) |")
        add("")
        add(f"Sim and real results are never pooled. Training {'includes' if rm.get('include_sim_data') else 'excludes'} "
            "sim-robot episodes (run_settings include_sim_data).")
        add("")
        for sw in rm.get("switches") or []:
            add(f"- {sw.get('at') or iso(sw.get('ts'))}: switched {sw.get('from_mode')} robot {sw.get('from_robot_id')} -> "
                f"{sw.get('to_mode')} robot {sw.get('to_robot_id')} in {sw.get('status')} by {sw.get('by')}"
                + (f" ({_md_text(sw['reason'])})" if sw.get("reason") else "") + f" (event {sw.get('event_id')})")
        if rm.get("switches"):
            add("")

    d = r["deployed_checkpoint"]
    add("## Deployed checkpoint")
    add("")
    if d:
        add(f"`{d['uri']}` (training_run {d.get('training_run_id')}, evaluated by eval_run {d.get('eval_run_id')}; "
            f"source: {d['source']})" + (" **[FAKE]**" if d.get("fake") else ""))
    else:
        add("None yet.")
    add("")

    add("## What data was bought, and why")
    add("")
    for it in r["iterations"]:
        add(f"### Iteration {it['iteration']}")
        add("")
        dg = it["diagnosis"]
        if dg["clusters"]:
            add("Failure clusters" + (f" (from eval_run {dg['source_eval_run_id']})" if dg["source_eval_run_id"] else "") + ":")
            add("")
            for c in dg["clusters"]:
                add(f"- **{_md_text(c['label'])}** x{c['count']}"
                    + (f": {_md_text(c['description'])}" if c["description"] else "")
                    + (f" (examples: episodes {', '.join(map(str, c['example_episode_ids']))})" if c["example_episode_ids"] else ""))
            add("")
        if dg["reasoning"]:
            add(f"Diagnosis reasoning ({dg['model'] or 'model n/a'}"
                + (f", backend {_llm_label(dg['backend'])}" if dg.get("backend") else "")
                + f", event {dg['event']['event_id']}):")
            add("")
            add("> " + _md_text(dg["reasoning"]).replace("\n", "\n> "))
            add("")
        p = it["data_plan"]
        if p:
            add(f"**Decision:** buy {p['amount']} x `{p['data_type']}` at {usd(p['unit_price_cents'])} each "
                f"(estimated {usd(p['est_cost_cents'])}); data_plan {p['data_plan_id']}, status {p['status']}, "
                f"approved {usd(p['approved_cents'])} by {p['approved_by'] or 'nobody yet'}."
                + (f" Planner model: {p['model'].get('served') or p['model'].get('requested')}"
                   + (f" via {_llm_label(p['model']['backend'])}" if p["model"].get("backend") else "") + "."
                   if p.get("model") else "")
                + (" **[FAKE: scripted stand-in, not a planner decision]**" if p["fake"] else ""))
            add("")
            add("Planner reasoning (verbatim from data_plan.reasoning):")
            add("")
            add("> " + _md_text(p["reasoning"]).replace("\n", "\n> "))
            add("")
        else:
            add("No data plan recorded.")
            add("")
        tk = it["tasks"]
        add(f"- Tasks dispatched: {tk['count']} " + (f"({', '.join(f'{k} {v}' for k, v in tk['by_type'].items())}; "
            f"{', '.join(f'{k} {v}' for k, v in tk['by_status'].items())})" if tk["count"] else ""))
        add(f"- Episodes collected: {it['episodes_bought']}, accepted {it['episodes_accepted']}, "
            f"rejected {len(it['episodes_rejected'])}")
        for rj in it["episodes_rejected"]:
            add(f"  - episode {rj['episode_id']} rejected: {_md_text(rj['reason']) or 'no reason recorded'}")
        for of in it["dataset_offers"]:
            add(f"- Dataset offer {of['dataset_offer_id']} (seller {of['seller_id']}, {usd(of['price_per_episode_cents'])}"
                f"/episode, {of['status']}): {_md_text(of.get('line') or '')}"
                + (f"; payout {usd(of['payout_cents'])}" if of.get("payout_cents") else "")
                + (f" ({_md_text(of['origin'])})" if of.get("origin") else ""))
        c = it["cost_cents"]
        add(f"- Cost: data {usd(c['data'])} (paid {usd(c['data_paid'])}), GPU {usd(c['gpu'])}, total {usd(c['total'])}")
        tr = it["training_run"]
        if tr:
            add(f"- Training run {tr['training_run_id']}: {tr['status']}, {tr['episodes']} episodes, "
                f"{tr['duration_s'] if tr['duration_s'] is not None else 'n/a'} s, GPU {usd(tr['gpu_cost_cents'])}"
                + (f" ({_md_text(_tdm42_cost_label(tr['cost']))})" if tr.get("cost") else "")
                + f", checkpoint `{tr['checkpoint_uri']}`" + (" **[FAKE]**" if tr["fake"] else ""))
        add(f"- Eval after this iteration: {_eval_line(it['eval'])}")
        add("")

    tc = r["total_cost"]
    add("## What it cost")
    add("")
    add("| | Amount |")
    add("|---|---|")
    add(f"| Data (paid + reserved payouts) | {usd(tc['data']['committed_cents'])} |")
    add(f"| of which paid | {usd(tc['data']['paid_cents'])} |")
    if tc["data"]["before_first_plan_cents"]:
        add(f"| of which before the first data plan (baseline resets) | {usd(tc['data']['before_first_plan_cents'])} |")
    add(f"| GPU ({tc['gpu']['runs']} training run(s)) | {usd(tc['gpu']['cents'])} |")
    add(f"| **Total** | **{usd(tc['total_cents'])}** |")
    add(f"| Budget / approved | {usd(tc['budget_cents'])} / {usd(tc['approved_cents'])} |")
    add("")
    if tc["data"]["not_spent_cents"]:
        add(f"Not spent (failed, reversed or over budget): {usd(tc['data']['not_spent_cents'])}.")
        add("")

    if r["agents"]:
        add("## Agents")
        add("")
        for a in r["agents"]:
            add(f"- {a['role']} `{a['name']}`: {a['status']}" + (f" (Factory session {a['factory_session_id']})"
                                                                  if a["factory_session_id"] else ""))
        add("")

    add("## Timeline")
    add("")
    for ev in r["timeline"]:
        if ev["type"] == "ticket.status_changed":
            line = f"{ev['from']} -> {ev['to']}" + (f": {_md_text(ev.get('note'))}" if ev.get("note") else "")
        else:
            extra = ", ".join(f"{k} {ev[k]}" for k in ("eval_run_id", "data_plan_id", "training_run_id", "approved_by",
                                                         "checkpoint_uri", "uri") if k in ev)
            line = ev["type"] + (f" ({extra})" if extra else "")
        add(f"- {ev['at']} #{ev['event_id']} {line}")
    add("")

    el = r["event_log"]
    sp = el["ticket_span"]
    add("## Event log seal")
    add("")
    add(f"- `{el['algorithm']}` over {el['count']} rows (ids {el['first_id']}..{el['last_id']}): `{el['hash']}`")
    add(f"- This ticket: {sp['count']} events, ids {sp['first_event_id']}..{sp['last_event_id']}, "
        f"{sp['first_at']} to {sp['last_at']}")
    if el["gaps"]:
        gaps = ", ".join(f"{a}..{b}" for a, b in el["gaps"][:5]) + (" ..." if len(el["gaps"]) > 5 else "")
        add(f"- Id gaps ({len(el['gaps'])}, SpacetimeDB auto-inc allocates ids in blocks, so jumps are expected; the seal covers every row present): {gaps}")
    add("")

    if r["missing"]:
        add("## Gaps in the record")
        add("")
        for m in r["missing"]:
            add(f"- {m}")
        add("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- loading

Sql = Callable[[str], Awaitable[list[dict]]]


async def load_tables(sql: Sql, ticket_id: int) -> Tables:
    """Rows for one ticket. event_log is read whole (the seal hashes every row; ticket events are
    filtered on payload ticket_id in ``assemble``). eval_trial is read per eval run."""
    tid = int(ticket_id)
    tickets = await sql(f"SELECT * FROM ticket WHERE id = {tid}")
    tables: Tables = {"ticket": tickets}
    if not tickets:
        return tables
    queries = [sql(f"SELECT * FROM {n} WHERE ticket_id = {tid}") for n in TICKET_TABLES]
    queries.append(sql("SELECT * FROM robot"))  # TDM-107: every robot an eval / episode ran on (sim vs real)
    queries.append(sql("SELECT * FROM event_log"))
    results = await asyncio.gather(*queries)
    tables.update(zip(TICKET_TABLES, results[:len(TICKET_TABLES)]))
    tables["robot"], tables["event_log"] = results[-2], results[-1]
    trials = await asyncio.gather(*(sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {_i(r['id'])}")
                                    for r in tables["eval_run"]))
    tables["eval_trial"] = [row for rows in trials for row in rows]
    return tables


async def build(sql: Sql, ticket_id: int) -> dict:
    return assemble(await load_tables(sql, ticket_id), ticket_id)
