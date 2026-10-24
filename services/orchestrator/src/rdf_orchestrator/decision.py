"""TDM-68 evidence trail for the recovery-vs-clean decision.

The demo's key moment has to be traceable from real rows, so this module only *joins* rows that
already exist. It writes nothing, generates no prose, and never fills a gap with a default:
anything missing is listed in ``missing`` (and the dashboard card shows that list).

The chain, per ticket iteration (all links are row ids)::

    eval_run (baseline / candidate, finalized)            event eval.finalized
      -> eval_trial (failed trials, with episode_id)       event eval.trial
        -> failure_cluster (label, count, example_episode_ids)
                                                         events planner.failure_cluster (reducer)
                                                                + diagnose.clusters (TDM-23: the
                                                                  model's grouping reasoning verbatim,
                                                                  eval_run_id, phase, recoverability)
          -> data_plan (data_type, amount, reasoning, failure_cluster_ids)
                                                         event planner.data_plan (reasoning verbatim
                                                                + evidence: guardrail, budget maths,
                                                                  model attempts)
            -> task (data_plan_id)                       event task.created
              -> episode (task_id)  = the data bought
                -> training_run (episode_ids) -> eval_run (training_run_id) = the "after" eval
    plus spend.approved (the human's approval) and planner.blocked / planner.error when no plan exists.

``links`` lists every id check with ok/detail so a broken link is visible, not hidden. ``autonomy``
compares the machine's chain timestamps against human actions on the ticket (actor ``human:*``,
``web``, ``mcp``, ``contributor:*``, ``seller``, or an event type ``human.*``; the ticket's own
``ticket.created`` is the customer's request and does not count). Events whose actor is ``owner`` are
listed separately (``owner_token_actions``), counted neither as machine nor as human: until each
service gets its own identity, the orchestrator itself calls reducers with the owner token, and so
can a person running a script, and the log cannot tell the two apart.

Pure: ``assemble(tables, ticket_id)`` takes rows as dicts (SQL results, snake_case columns,
timestamps as micros). ``load_tables(sql, ticket_id)`` fetches them with any async ``sql(query)``.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

PLAN_TYPES = ("clean_teleop", "recovery_takeover")
HUMAN_ACTOR_KINDS = ("human", "web", "mcp", "contributor", "seller")
OWNER_TOKEN_ACTOR_KINDS = ("owner",)
# Event types written by the machine chain itself (never counted as a human action).
CHAIN_TYPES = ("eval.finalized", "diagnose.clusters", "diagnose.no_failures", "planner.failure_cluster",
               "planner.data_plan", "planner.blocked", "planner.error", "task.created")
TICKET_TABLES = ("run_settings", "eval_run", "failure_cluster", "data_plan", "task", "episode", "training_run",
                 "review")

Tables = dict[str, list[dict]]


# --------------------------------------------------------------------------- small helpers

def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _payload(e: dict) -> dict:
    try:
        p = json.loads(e.get("payload_json") or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def _ref(e: dict | None) -> dict | None:
    """The citation for one event_log row: enough to find it again, nothing more."""
    if e is None:
        return None
    return {"event_id": _i(e["id"]), "ts": _i(e["ts"]), "actor": e.get("actor"), "type": e.get("type")}


def _actor_kind(actor: str | None) -> str:
    a = actor or ""
    return a.split(":", 1)[0]


def _ids(v: Any) -> list[int]:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return [_i(x) for x in (v or []) if _i(x)]


def _cameras(ep: dict) -> list[dict]:
    """cameras_json is {name: uri} or {name: {uri}}; the fake harness nests it under "cameras"."""
    try:
        raw = json.loads(ep.get("cameras_json") or "{}")
    except ValueError:
        return []
    if not isinstance(raw, dict):
        return []
    src = raw.get("cameras") if isinstance(raw.get("cameras"), dict) else raw
    out = []
    for name, v in src.items():
        if name in ("fake", "tag"):
            continue
        uri = v if isinstance(v, str) else v.get("uri") if isinstance(v, dict) else None
        if isinstance(uri, str) and uri:
            out.append({"name": name, "uri": uri})
    return sorted(out, key=lambda c: c["name"])


def _is_fake_episode(ep: dict) -> bool:
    try:
        cams = json.loads(ep.get("cameras_json") or "{}")
    except ValueError:
        cams = {}
    return (isinstance(cams, dict) and cams.get("fake") is True) or "FAKE-" in str(ep.get("storage_uri") or "")


def _episode_view(ep: dict) -> dict:
    return {"episode_id": _i(ep["id"]), "kind": ep.get("kind"), "review_status": ep.get("review_status"),
            "success": ep.get("success"), "duration_ms": _i(ep.get("duration_ms")),
            "frame_count": _i(ep.get("frame_count")), "storage_uri": ep.get("storage_uri"),
            "cameras": _cameras(ep), "task_id": _i(ep.get("task_id")), "contributor_id": _i(ep.get("contributor_id")),
            "checkpoint_uri": ep.get("checkpoint_uri"), "created_at": _i(ep.get("created_at")),
            "fake": _is_fake_episode(ep)}


def _eval_view(r: dict, finalized: dict | None) -> dict:
    return {"eval_run_id": _i(r["id"]), "kind": r.get("kind"), "status": r.get("status"),
            "checkpoint_uri": r.get("checkpoint_uri"), "training_run_id": _i(r.get("training_run_id")),
            "trials": _i(r.get("trials")), "completed": _i(r.get("completed")), "successes": _i(r.get("successes")),
            "ci_low": r.get("ci_low"), "ci_high": r.get("ci_high"), "created_at": _i(r.get("created_at")),
            "finished_at": _i(r.get("finished_at")) or None,
            "summary": (_payload(finalized).get("summary") if finalized else None),
            "finalized_event": _ref(finalized)}


def _check(links: list[dict], name: str, ok: bool | None, detail: str) -> None:
    links.append({"check": name, "ok": ok, "detail": detail})


# --------------------------------------------------------------------------- the assembler

def assemble(t: Tables, ticket_id: int, *, now_micros: int | None = None) -> dict:
    tid = int(ticket_id)
    tickets = [r for r in t.get("ticket", []) if _i(r.get("id")) == tid]
    if not tickets:
        raise LookupError(f"no ticket {tid}")
    ticket = tickets[0]
    mine = {name: [r for r in t.get(name, []) if _i(r.get("ticket_id")) == tid] for name in TICKET_TABLES}

    # Ticket-scoped events (payload ticket_id), oldest first by ts then id.
    events: list[tuple[dict, dict]] = []
    for e in t.get("event_log", []):
        p = _payload(e)
        if _i(p.get("ticket_id")) == tid:
            events.append((e, p))
    events.sort(key=lambda x: (_i(x[0]["ts"]), _i(x[0]["id"])))

    def first(typ: str, **match: Any) -> dict | None:
        for e, p in events:
            if e.get("type") == typ and all(_i(p.get(k), -1) == int(v) for k, v in match.items()):
                return e
        return None

    def all_of(typ: str, **match: Any) -> list[tuple[dict, dict]]:
        return [(e, p) for e, p in events
                if e.get("type") == typ and all(_i(p.get(k), -1) == int(v) for k, v in match.items())]

    evals_by_id = {_i(r["id"]): r for r in mine["eval_run"]}
    eval_views = {rid: _eval_view(r, first("eval.finalized", eval_run_id=rid)) for rid, r in evals_by_id.items()}
    trials_by_run: dict[int, list[dict]] = {}
    for tr in t.get("eval_trial", []):
        if _i(tr.get("eval_run_id")) in evals_by_id:
            trials_by_run.setdefault(_i(tr["eval_run_id"]), []).append(tr)
    for rows in trials_by_run.values():
        rows.sort(key=lambda r: _i(r.get("trial_index")))
    trial_by_episode: dict[int, dict] = {}
    for rid, rows in trials_by_run.items():
        for tr in rows:
            if _i(tr.get("episode_id")):
                trial_by_episode[_i(tr["episode_id"])] = tr
    episodes = {_i(e["id"]): e for e in mine["episode"]}
    other_episodes = {_i(e["id"]): e for e in t.get("episode", []) if _i(e.get("ticket_id")) != tid}

    iterations = sorted({_i(c.get("iteration")) for c in mine["failure_cluster"]}
                        | {_i(p.get("iteration")) for p in mine["data_plan"]}
                        | {_i(p.get("iteration")) for e, p in events
                           if e.get("type") in ("diagnose.clusters", "planner.blocked", "planner.error")
                           and _i(p.get("iteration"), -1) >= 0})

    human_actions, owner_token_actions = [], []
    for e, p in events:
        kind = _actor_kind(e.get("actor"))
        if e.get("type") == "ticket.created" or e.get("type") in CHAIN_TYPES:
            continue
        if kind in HUMAN_ACTOR_KINDS or str(e.get("type") or "").startswith("human."):
            human_actions.append(_ref(e))
        elif kind in OWNER_TOKEN_ACTOR_KINDS:
            owner_token_actions.append(_ref(e))

    decisions = [_decision(it, tid, mine, events, first, all_of, evals_by_id, eval_views, trials_by_run,
                           trial_by_episode, episodes, other_episodes, human_actions, owner_token_actions)
                 for it in iterations]

    missing: list[str] = []
    if not mine["eval_run"]:
        missing.append("no eval_run on this ticket yet (the baseline has not run)")
    if not decisions:
        missing.append("no failure clusters, diagnosis or planner output on this ticket yet")

    return {
        "ticket": {k: ticket.get(k) for k in ("id", "task_description", "status", "iteration", "target_success",
                                              "budget_cents", "approved_cents", "spent_cents", "created_by",
                                              "created_at", "robot_id")},
        "ticket_created_event": _ref(first("ticket.created")),
        "generated_at": now_micros if now_micros is not None else int(time.time() * 1_000_000),
        "evals": sorted(eval_views.values(), key=lambda v: (v["created_at"], v["eval_run_id"])),
        "decisions": decisions,
        "human_actions": human_actions,
        "owner_token_actions": owner_token_actions,
        "missing": missing,
    }


def _decision(it, tid, mine, events, first, all_of, evals_by_id, eval_views, trials_by_run, trial_by_episode,
              episodes, other_episodes, human_actions, owner_token_actions) -> dict:
    links: list[dict] = []
    missing: list[str] = []

    # ---- diagnosis (TDM-23) for this iteration: the newest diagnose.clusters event
    diags = all_of("diagnose.clusters", iteration=it)
    diag_e, diag_p = diags[-1] if diags else (None, {})
    clusters_rows = sorted((c for c in mine["failure_cluster"] if _i(c.get("iteration")) == it),
                           key=lambda c: (-_i(c.get("count")), _i(c["id"])))

    # ---- which eval the clusters came from
    src_id, src_how = None, None
    if diag_p.get("eval_run_id") is not None:
        src_id, src_how = _i(diag_p["eval_run_id"]), "diagnose.clusters.eval_run_id"
    else:
        # No diagnosis event: fall back to the eval run the example episodes are failed trials of.
        runs = {_i(trial_by_episode[e]["eval_run_id"]) for c in clusters_rows for e in _ids(c.get("example_episode_ids"))
                if e in trial_by_episode}
        if len(runs) == 1:
            src_id, src_how = runs.pop(), "example episodes are trials of this run"
    source = eval_views.get(src_id) if src_id is not None else None
    if source is not None:
        source = {**source, "linked_by": src_how,
                  "trials_detail": [{"trial_index": _i(tr.get("trial_index")), "success": bool(tr.get("success")),
                                     "duration_ms": _i(tr.get("duration_ms")), "episode_id": _i(tr.get("episode_id")),
                                     "failure_note": tr.get("failure_note")}
                                    for tr in trials_by_run.get(src_id, [])]}
    if src_id is None:
        missing.append(f"iteration {it}: cannot tell which eval run the clusters came from "
                       "(no diagnose.clusters event, and the example episodes are not trials of one run)")
    elif source is None:
        _check(links, "source eval run exists", False, f"diagnose.clusters names eval_run {src_id}, not on this ticket")
    else:
        _check(links, "source eval run finalized", source["status"] == "done" and source["finalized_event"] is not None,
               f"eval_run {src_id} ({source['kind']}) status {source['status']}, "
               f"eval.finalized event {(source['finalized_event'] or {}).get('event_id')}")

    # ---- clusters, merged with the diagnosis entries (phase, recoverability, trial indices)
    diag_by_id, diag_by_label = {}, {}
    for dc in diag_p.get("clusters") or []:
        if isinstance(dc, dict):
            if dc.get("failure_cluster_id"):
                diag_by_id[_i(dc["failure_cluster_id"])] = dc
            diag_by_label[str(dc.get("label", "")).strip().lower()] = dc
    clusters = []
    for c in clusters_rows:
        cid = _i(c["id"])
        dc = diag_by_id.get(cid) or diag_by_label.get(str(c.get("label", "")).strip().lower())
        ev = first("planner.failure_cluster", failure_cluster_id=cid)
        examples = []
        for eid in _ids(c.get("example_episode_ids")):
            ep = episodes.get(eid)
            tr = trial_by_episode.get(eid)
            view = _episode_view(ep) if ep else {"episode_id": eid, "missing": True}
            view["trial"] = ({"eval_run_id": _i(tr["eval_run_id"]), "trial_index": _i(tr.get("trial_index")),
                              "success": bool(tr.get("success")), "failure_note": tr.get("failure_note"),
                              "duration_ms": _i(tr.get("duration_ms"))} if tr else None)
            examples.append(view)
            if ep is None:
                where = " (it belongs to another ticket)" if eid in other_episodes else ""
                _check(links, f"cluster {cid} example episode {eid} exists", False, f"no episode {eid} on ticket {tid}{where}")
            elif tr is None:
                _check(links, f"cluster {cid} example episode {eid} is an eval trial", False,
                       f"episode {eid} ({ep.get('kind')}) is not the recording of any eval trial on this ticket")
            else:
                ok = (not tr.get("success")) and (src_id is None or _i(tr["eval_run_id"]) == src_id)
                _check(links, f"cluster {cid} example episode {eid} is a failed trial of the source eval", ok,
                       f"eval_run {_i(tr['eval_run_id'])} trial {_i(tr.get('trial_index'))}, "
                       f"success={bool(tr.get('success'))}")
        if not examples:
            _check(links, f"cluster {cid} has example episodes", False,
                   "failure_cluster.example_episode_ids is empty, so there is no clip to show")
        _check(links, f"cluster {cid} has its planner.failure_cluster event", ev is not None,
               f"event {(_ref(ev) or {}).get('event_id')}")
        clusters.append({
            "failure_cluster_id": cid, "label": c.get("label"), "count": _i(c.get("count")),
            "created_at": _i(c.get("created_at")),
            "description": c.get("description") or (dc or {}).get("description") or None,
            "phase": (dc or {}).get("phase"), "recoverable_mid_episode": (dc or {}).get("recoverable_mid_episode"),
            "recoverability_reason": (dc or {}).get("recoverability_reason"),
            "trial_indices": (dc or {}).get("trial_indices"), "episode_ids": (dc or {}).get("episode_ids"),
            "in_diagnosis": dc is not None,
            "event": _ref(ev), "examples": examples,
        })
    if not clusters_rows:
        missing.append(f"iteration {it}: no failure_cluster rows")
    if diag_e is None:
        missing.append(f"iteration {it}: no diagnose.clusters event, so the clustering model's reasoning is not on record")

    diagnosis = None
    if diag_e is not None:
        diagnosis = {"event": _ref(diag_e), "model": diag_p.get("model"), "reasoning": diag_p.get("reasoning"),
                     "eval_run_id": diag_p.get("eval_run_id"), "eval_kind": diag_p.get("eval_kind"),
                     "trials": diag_p.get("trials"), "successes": diag_p.get("successes"),
                     "failures": diag_p.get("failures"), "recoverable_failures": diag_p.get("recoverable_failures"),
                     "unrecoverable_failures": diag_p.get("unrecoverable_failures"), "media": diag_p.get("media")}
        if not diag_p.get("reasoning"):
            missing.append(f"iteration {it}: the diagnose.clusters event carries no reasoning")

    # ---- the plan (insert-only, one per iteration in practice; show the newest, list all ids)
    plans = sorted((p for p in mine["data_plan"] if _i(p.get("iteration")) == it),
                   key=lambda p: (_i(p.get("created_at")), _i(p["id"])))
    plan_row = plans[-1] if plans else None
    plan = alternative = None
    tasks, approvals, bought, trainings, after = [], [], [], [], []
    if plan_row is None:
        blocked = [{"type": e["type"], **_ref(e), "reason": p.get("reason") or p.get("error")}
                   for e, p in events if e.get("type") in ("planner.blocked", "planner.error")
                   and _i(p.get("iteration"), -1) == it]
        missing.append(f"iteration {it}: the planner has not recorded a data_plan"
                       + (f" (latest: {blocked[-1]['type']}: {blocked[-1]['reason']})" if blocked else ""))
    else:
        pid = _i(plan_row["id"])
        pe = first("planner.data_plan", data_plan_id=pid)
        pp = _payload(pe) if pe else {}
        evid = pp.get("evidence") if isinstance(pp.get("evidence"), dict) else {}
        budget_math = evid.get("budget_math") or {}
        guard = evid.get("guardrail") or {}
        model = evid.get("model") or {}
        plan = {
            "data_plan_id": pid, "all_plan_ids": [_i(p["id"]) for p in plans],
            "data_type": plan_row.get("data_type"), "amount": _i(plan_row.get("amount")),
            "unit_price_cents": _i(plan_row.get("unit_price_cents")), "est_cost_cents": _i(plan_row.get("est_cost_cents")),
            "est_cost_incl_resets_cents": evid.get("est_cost_incl_resets_cents"),
            "reasoning": plan_row.get("reasoning"),
            "status": plan_row.get("status"), "approved_cents": _i(plan_row.get("approved_cents")),
            "approved_by": plan_row.get("approved_by"), "approved_at": _i(plan_row.get("approved_at")) or None,
            "created_at": _i(plan_row.get("created_at")),
            "failure_cluster_ids": _ids(plan_row.get("failure_cluster_ids")),
            "event": _ref(pe),
            "event_reasoning_verbatim": (pp.get("reasoning") == plan_row.get("reasoning")) if pe else None,
            "planner": evid.get("planner"), "fixture": evid.get("fixture"),
            "guardrail": guard or None, "guardrail_forced": evid.get("guardrail_forced"),
            "guardrail_agreed_first_try": evid.get("guardrail_agreed_first_try"),
            "labels_cited": evid.get("labels_cited"), "budget_math": budget_math or None,
            "dataset_offer": evid.get("dataset_offer"),
            "model": {"requested": model.get("requested"), "served": model.get("served"), "effort": model.get("effort"),
                      "attempts": model.get("attempts")} if model else None,
            "data_type_override": (evid.get("inputs") or {}).get("data_type_override"),
            "inputs_eval": (evid.get("inputs") or {}).get("latest_eval"),
        }
        _check(links, "data_plan has its planner.data_plan event", pe is not None, f"event {(_ref(pe) or {}).get('event_id')}")
        if pe is not None:
            _check(links, "event_log reasoning is the data_plan reasoning verbatim", plan["event_reasoning_verbatim"],
                   "byte-for-byte comparison of data_plan.reasoning and the event payload")
            if not evid:
                missing.append(f"iteration {it}: the planner.data_plan event has no evidence (inputs, guardrail, budget)")
        cluster_ids = {c["failure_cluster_id"] for c in clusters}
        stray = [c for c in plan["failure_cluster_ids"] if c not in cluster_ids]
        _check(links, "plan targets this iteration's clusters", bool(plan["failure_cluster_ids"]) and not stray,
               f"failure_cluster_ids {plan['failure_cluster_ids']}" + (f", not in iteration {it}: {stray}" if stray else ""))
        in_eval = (plan["inputs_eval"] or {}).get("eval_run_id")
        if in_eval is not None and src_id is not None:
            _check(links, "planner read the same eval the clusters came from", _i(in_eval) == src_id,
                   f"planner inputs latest_eval.eval_run_id={in_eval}, clusters from eval_run {src_id}")

        # The alternative: the plan types are exactly two, so the rejected one is the other.
        if plan["data_type"] in PLAN_TYPES:
            alt = next(x for x in PLAN_TYPES if x != plan["data_type"])
            per = (budget_math.get("per_episode_cents") or {}).get(alt)
            alternative = {
                "data_type": alt,
                "unit_price_cents": ((evid.get("inputs") or {}).get("prices_cents") or {}).get(alt),
                "cost_per_episode_incl_reset_cents": per,
                "max_amount": (budget_math.get("max_amount") or {}).get(alt),
                "affordable": (budget_math.get("affordable") or {}).get(alt),
                "ruled_out_by_rule": guard.get("rule") if guard.get("required_type") == plan["data_type"] else None,
                "rule_why": guard.get("why") if guard.get("required_type") == plan["data_type"] else None,
            }

        approvals = [{**_ref(e), "approved_by": p.get("approved_by"), "approved_cents": p.get("approved_cents"),
                      "via": p.get("via")} for e, p in all_of("spend.approved", data_plan_id=pid)]
        plan_tasks = sorted((x for x in mine["task"] if _i(x.get("data_plan_id")) == pid),
                            key=lambda x: (_i(x.get("created_at")), _i(x["id"])))
        task_ids = set()
        for x in plan_tasks:
            xid = _i(x["id"])
            task_ids.add(xid)
            te = first("task.created", task_id=xid)
            tasks.append({"task_id": xid, "type": x.get("type"), "status": x.get("status"),
                          "price_cents": _i(x.get("price_cents")), "assignee_id": _i(x.get("assignee_id")),
                          "created_at": _i(x.get("created_at")), "completed_at": _i(x.get("completed_at")) or None,
                          "event": _ref(te),
                          "episode_ids": sorted(_i(e["id"]) for e in episodes.values() if _i(e.get("task_id")) == xid)})
            if te is None:
                _check(links, f"task {xid} has its task.created event", False, "no task.created event")
        wrong = [x["task_id"] for x in tasks if x["type"] != plan["data_type"]]
        if tasks:
            _check(links, "dispatched tasks match the plan's data type", not wrong,
                   f"{len(tasks)} task(s) with data_plan_id {pid}" + (f"; other type: {wrong}" if wrong else ""))
        else:
            missing.append(f"iteration {it}: no task with data_plan_id {pid} has been dispatched yet")

        bought = [_episode_view(e) for e in sorted(episodes.values(), key=lambda e: _i(e["id"]))
                  if _i(e.get("task_id")) in task_ids]
        bought_ids = {b["episode_id"] for b in bought}
        for tr in sorted(mine["training_run"], key=lambda r: (_i(r.get("created_at")), _i(r["id"]))):
            used = set(_ids(tr.get("episode_ids")))
            if used & bought_ids:
                trainings.append({"training_run_id": _i(tr["id"]), "iteration": _i(tr.get("iteration")),
                                  "status": tr.get("status"), "checkpoint_uri": tr.get("checkpoint_uri"),
                                  "episodes_total": len(used), "episodes_from_this_plan": len(used & bought_ids),
                                  "gpu_cost_cents": _i(tr.get("gpu_cost_cents")),
                                  "created_at": _i(tr.get("created_at")),
                                  "finished_at": _i(tr.get("finished_at")) or None})
        train_ids = {x["training_run_id"] for x in trainings}
        after = [v for v in eval_views.values() if v["training_run_id"] in train_ids]
        after.sort(key=lambda v: (v["created_at"], v["eval_run_id"]))

    # ---- autonomy: machine chain timestamps vs human actions
    chain = [x for x in ([source.get("finalized_event")] if source else [])
             + [diagnosis["event"] if diagnosis else None]
             + [c["event"] for c in clusters]
             + [plan["event"] if plan else None] if x]
    last_chain = max((x["ts"] for x in chain), default=None)
    start = (source or {}).get("finalized_event")
    before_plan = [h for h in human_actions if plan and plan["event"] and h["ts"] < plan["event"]["ts"]]
    in_window = [h for h in before_plan if start and h["ts"] >= start["ts"]]
    ops_in_window = [o for o in owner_token_actions
                     if plan and plan["event"] and start and start["ts"] <= o["ts"] < plan["event"]["ts"]]
    first_human = human_actions[0] if human_actions else None
    autonomy = {
        "chain_events": chain,
        "last_chain_ts": last_chain,
        "first_human_action": first_human,
        "before_any_human_action": (None if last_chain is None or not (plan and plan["event"])
                                    else first_human is None or last_chain < first_human["ts"]),
        "human_actions_before_plan": before_plan,
        "human_actions_in_decision_window": in_window,
        "owner_token_actions_in_decision_window": ops_in_window,
        "override_in_force": (plan or {}).get("data_type_override"),
    }
    if plan and plan["event"]:
        _check(links, "no human action between the source eval and the plan", not in_window,
               "window from eval.finalized to planner.data_plan" + (f": {len(in_window)} human action(s)" if in_window else ""))

    return {"iteration": it, "source_eval": source, "diagnosis": diagnosis, "clusters": clusters, "plan": plan,
            "alternative": alternative, "approvals": approvals, "tasks": tasks, "data_bought": bought,
            "training_runs": trainings, "after_evals": after, "autonomy": autonomy, "links": links,
            "missing": missing}


# --------------------------------------------------------------------------- loading

Sql = Callable[[str], Awaitable[list[dict]]]


async def load_tables(sql: Sql, ticket_id: int) -> Tables:
    """Rows for one ticket. event_log has no ticket index, so it is read whole (it is small) and
    filtered on payload ticket_id in ``assemble``; eval_trial is read per eval run."""
    tid = int(ticket_id)
    names = ("ticket",) + TICKET_TABLES
    queries = [f"SELECT * FROM ticket WHERE id = {tid}"] + [f"SELECT * FROM {n} WHERE ticket_id = {tid}"
                                                            for n in TICKET_TABLES]
    results = await asyncio.gather(*(sql(q) for q in queries), sql("SELECT * FROM event_log"))
    tables: Tables = dict(zip(names, results[:-1]))
    tables["event_log"] = results[-1]
    runs = [_i(r["id"]) for r in tables["eval_run"]]
    trials = await asyncio.gather(*(sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {r}") for r in runs))
    tables["eval_trial"] = [row for rows in trials for row in rows]
    return tables


async def build(sql: Sql, ticket_id: int) -> dict:
    return assemble(await load_tables(sql, ticket_id), ticket_id)
