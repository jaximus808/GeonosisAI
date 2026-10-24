"""TDM-24 planner tests: fixture clusters + a fake Anthropic client (no network, no SpacetimeDB).

The real-Claude proof is ``scripts/tdm24_planner_proof.py`` (labelled FIXTURE)."""
from __future__ import annotations

import asyncio
import json
import random
from types import SimpleNamespace

import pytest

from rdf_orchestrator import planner as P
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.stdb import Row


# --------------------------------------------------------------------------- fixtures

def ticket(**kw):
    t = dict(id=7, robot_id=1, task_description="put the cup in the bin", target_success=0.8,
             budget_cents=5000, approved_cents=0, spent_cents=0, status="plan_data", iteration=0)
    t.update(kw)
    return Row(t)


def tables(clusters, *, successes, trials=10, budget=5000, accepted=0, target=5, diag=None, extra_events=(),
           plans=(), offers=(), sellers=0):
    """Bus-mirror-shaped rows for one ticket with a finished baseline eval."""
    fail_eps = []
    eps = iter(range(100, 200))
    trial_rows = []
    for i in range(trials):
        ok = i < successes
        ep = next(eps)
        trial_rows.append(Row(id=500 + i, eval_run_id=3, trial_index=i, success=ok, duration_ms=30000 if ok else 45000,
                              episode_id=ep, failure_note=None if ok else "fixture failure"))
        if not ok:
            fail_eps.append(ep)
    cl_rows, it_eps = [], iter(fail_eps)
    for cid, (label, count) in enumerate(clusters, start=11):
        ex = [next(it_eps) for _ in range(min(count, 3))]
        for _ in range(count - len(ex)):
            next(it_eps, None)
        cl_rows.append(Row(id=cid, ticket_id=7, iteration=0, label=label, description="", count=count,
                           example_episode_ids=ex, created_at=1))
    events = [Row(id=900 + i, actor="x", type=e["type"], payload_json=json.dumps(e["payload"]), ts=1)
              for i, e in enumerate(([{"type": "diagnose.clusters", "payload": diag}] if diag else []) + list(extra_events))]
    return {
        "ticket": [ticket(budget_cents=budget)],
        "run_settings": [Row(ticket_id=7, eval_trials=trials, success_threshold=0.8, require_ci_low=False,
                             budget_cents=budget, target_episodes=target, max_iterations=3,
                             hyperparams_json=json.dumps({"lr": 2.5e-5}))],
        "dataset": [Row(id=1, ticket_id=7, accepted_count=accepted, total_count=accepted)],
        "eval_run": [Row(id=3, ticket_id=7, robot_id=1, training_run_id=0, kind="baseline", checkpoint_uri="pi05_base",
                         status="done", trials=trials, completed=trials, successes=successes,
                         ci_low=0.0 if successes == 0 else 0.1, ci_high=0.28 if successes == 0 else 0.6,
                         created_at=1, finished_at=2)],
        "eval_trial": trial_rows,
        "failure_cluster": cl_rows,
        "data_plan": list(plans),
        "dataset_offer": list(offers),
        "contributor": [Row(id=50 + i, kind="dataset_seller") for i in range(sellers)],
        "event_log": events,
    }


def inputs_for(tbl, t=None):
    return P.gather_inputs(lambda name: tbl.get(name, []), t or tbl["ticket"][0])


NEVER_APPROACHES = [("never approaches the cup", 7), ("freezes at start", 3)]
DROPS_MID_GRASP = [("drops mid-grasp", 5), ("misses the cup", 2)]


class FakeClaude:
    """Stands in for anthropic.Anthropic: .beta.messages.create(**kw) returns scripted plans."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(a):
            a = a(kw)
        return SimpleNamespace(id=f"msg_{len(self.calls)}", model=P.MODEL, stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
                               content=[SimpleNamespace(type="text", text=json.dumps(a))])


def answer(data_type, amount, reasoning, ids=(11,), offer=False, price=0):
    return {"data_type": data_type, "amount": amount, "reasoning": reasoning, "failure_cluster_ids": list(ids),
            "consider_dataset_offer": offer, "offer_max_price_cents": price}


# --------------------------------------------------------------------------- stage + guardrails

@pytest.mark.parametrize("label,phase,stage", [
    ("never approaches the cup", None, "no_approach"),
    ("freezes at start", None, "no_approach"),
    ("wanders away from the table", None, "no_approach"),
    ("drops mid-grasp", None, "late"),
    ("cup slips during lift", None, "late"),
    ("never reaches the bin", None, "late"),
    ("misses the bin on release", None, "late"),
    ("misses the cup", None, "unknown"),
    ("misses the cup", "approach", "no_approach"),
    ("misses the cup", "grasp", "late"),
    ("odd behaviour", "other", "unknown"),
])
def test_classify_stage(label, phase, stage):
    assert P.classify_stage(label, "", phase) == stage


def test_never_approaches_requires_clean_teleop():
    inp = inputs_for(tables(NEVER_APPROACHES, successes=0))
    g = P.guardrail(inp)
    assert (g.rule, g.required_type) == ("R1", "clean_teleop")
    assert g.no_approach_share == 1.0


def test_drops_mid_grasp_requires_recovery_takeover():
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3))
    g = P.guardrail(inp)
    assert (g.rule, g.required_type) == ("R2", "recovery_takeover")
    assert g.late_share == pytest.approx(5 / 7)


def test_never_approaches_with_some_success_is_left_to_the_model():
    g = P.guardrail(inputs_for(tables(NEVER_APPROACHES, successes=2, trials=12)))
    assert g.rule is None and g.required_type is None


def test_mixed_clusters_no_rule():
    g = P.guardrail(inputs_for(tables([("misses the cup", 4), ("drops mid-grasp", 3)], successes=3)))
    assert g.rule is None


# --------------------------------------------------------------------------- full plan with fake Claude

def test_plan_never_approaches_clean_teleop():
    inp = inputs_for(tables(NEVER_APPROACHES, successes=0))
    fake = FakeClaude(answer("clean_teleop", 3, 'All 10 trials failed: "never approaches the cup" (7 of 10) ...',
                             ids=(11, 12)))
    out = P.plan(inp, fake)
    assert out.blocked is None and out.data_type == "clean_teleop" and out.amount == 3
    assert out.unit_price_cents == 1000 and out.failure_cluster_ids == [11, 12]
    assert out.evidence["guardrail"]["rule"] == "R1" and out.evidence["guardrail_agreed_first_try"]
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["schema"] == P.OUTPUT_SCHEMA
    assert '"never approaches the cup"' in kw["messages"][0]["content"]
    args = out.reducer_args()
    assert args[:3] == [7, "clean_teleop", 3] and args[3] == out.reasoning
    ev = json.loads(args[4])
    assert ev["failure_cluster_ids"] == [11, 12] and ev["unit_price_cents"] == 1000


def test_plan_drops_mid_grasp_recovery():
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3))
    fake = FakeClaude(answer("recovery_takeover", 5, 'Most failures are "drops mid-grasp" (5 of 7) ...'))
    out = P.plan(inp, fake)
    assert out.data_type == "recovery_takeover" and out.amount == 5 and out.unit_price_cents == 500


def test_wrong_type_gets_one_corrective_retry():
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3))
    fake = FakeClaude(answer("clean_teleop", 3, 'Clean demos for "drops mid-grasp".'),
                      answer("recovery_takeover", 5, 'Recovery takeovers target "drops mid-grasp" (5 of 7).'))
    out = P.plan(inp, fake)
    assert out.data_type == "recovery_takeover" and "[guardrail]" not in out.reasoning
    assert len(fake.calls) == 2
    retry = fake.calls[1]["messages"][-1]["content"]
    assert "decision rule 2 requires data_type=recovery_takeover" in retry
    assert out.evidence["guardrail_agreed_first_try"] is False


def test_persistent_violation_is_forced_and_marked():
    inp = inputs_for(tables(NEVER_APPROACHES, successes=0))
    fake = FakeClaude(answer("recovery_takeover", 99, 'Takeovers for "never approaches the cup".'))
    out = P.plan(inp, fake)
    b = P.budget(inp)
    assert out.data_type == "clean_teleop"
    assert out.amount == b.max_amount["clean_teleop"]
    assert "[guardrail] data_type set to clean_teleop" in out.reasoning
    assert len(out.evidence["guardrail_forced"]) == 2


def test_reasoning_must_cite_a_cluster_label():
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3))
    fake = FakeClaude(answer("recovery_takeover", 4, "Buy takeovers."),
                      answer("recovery_takeover", 4, 'Takeovers for "drops mid-grasp".'))
    out = P.plan(inp, fake)
    assert len(fake.calls) == 2 and "exact label" in fake.calls[1]["messages"][-1]["content"]
    assert out.evidence["labels_cited"] == ["drops mid-grasp"]


@pytest.mark.parametrize("seed", range(40))
def test_amount_never_exceeds_budget(seed):
    rnd = random.Random(seed)
    budget = rnd.randint(0, 20000)
    approved = rnd.randint(0, budget)
    clusters, successes = rnd.choice([(NEVER_APPROACHES, 0), (DROPS_MID_GRASP, 3)])
    tbl = tables(clusters, successes=successes,
                 budget=budget, accepted=rnd.randint(0, 6), target=rnd.randint(1, 20))
    tbl["ticket"][0]["approved_cents"] = approved
    inp = inputs_for(tbl)
    fake = FakeClaude(lambda kw: answer(rnd.choice(P.DATA_TYPES), rnd.randint(-5, 500),
                                        'For "drops mid-grasp" and "never approaches the cup".'))
    out = P.plan(inp, fake)
    remaining = budget - approved
    if out.blocked:
        b, g = P.budget(inp), P.guardrail(inp)
        assert max(b.max_amount.values()) < 1 or (g.required_type and b.max_amount[g.required_type] < 1)
        assert not fake.calls
        return
    per = out.unit_price_cents + P.PRICES_CENTS["reset"]
    assert 1 <= out.amount and out.amount * per <= remaining
    assert out.amount * out.unit_price_cents <= remaining  # what record_data_plan stores as est_cost
    needed = max(0, inp.target_episodes - inp.dataset_accepted)
    assert out.amount <= (needed if needed else inp.target_episodes)


def test_budget_exhausted_blocks_without_calling_claude():
    tbl = tables(DROPS_MID_GRASP, successes=3, budget=5000)
    tbl["ticket"][0]["spent_cents"] = 4600  # 400c left < 700c per takeover incl. reset
    fake = FakeClaude(answer("recovery_takeover", 1, '"drops mid-grasp"'))
    out = P.plan(inputs_for(tbl), fake)
    assert out.blocked and "cannot pay" in out.blocked and not fake.calls


def test_no_clusters_blocks():
    out = P.plan(inputs_for(tables([], successes=10)), FakeClaude(answer("clean_teleop", 1, "x")))
    assert out.blocked == "no failure clusters recorded for this ticket"


def test_dataset_offer_only_with_clean_teleop():
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3, sellers=1))
    out = P.plan(inp, FakeClaude(answer("recovery_takeover", 4, '"drops mid-grasp"', offer=True, price=300),
                                 answer("recovery_takeover", 4, '"drops mid-grasp"')))
    assert out.evidence["dataset_offer"]["consider"] is False

    inp = inputs_for(tables(NEVER_APPROACHES, successes=0, accepted=2, target=5, sellers=1))
    out = P.plan(inp, FakeClaude(answer("clean_teleop", 3, '"never approaches the cup"', offer=True, price=400)))
    assert out.evidence["dataset_offer"] == {"consider": True, "max_price_per_episode_cents": 400, "max_episodes": 3}
    assert out.amount <= 3  # N - already accepted


def test_override_event_beats_the_rule():
    ov = {"type": P.OVERRIDE_EVENT, "payload": {"ticket_id": 7, "data_type": "clean_teleop"}}
    inp = inputs_for(tables(DROPS_MID_GRASP, successes=3, extra_events=[ov]))
    assert inp.data_type_override == "clean_teleop"
    g = P.guardrail(inp)
    assert (g.rule, g.required_type) == ("override", "clean_teleop")
    out = P.plan(inp, FakeClaude(answer("clean_teleop", 2, 'Override; "drops mid-grasp" argues for takeovers.')))
    assert out.data_type == "clean_teleop"


def test_diagnosis_payload_enriches_clusters():
    diag = {"ticket_id": 7, "iteration": 0, "reasoning": "Grouped by what the wrist cam shows.",
            "clusters": [{"failure_cluster_id": 11, "label": "misses the cup", "phase": "grasp",
                          "recoverable_mid_episode": True, "description": "fingers close beside the cup",
                          "recoverability_reason": "cup still upright"}]}
    inp = inputs_for(tables([("misses the cup", 6), ("never moves", 1)], successes=3, diag=diag))
    c = inp.clusters[0]
    assert (c.phase, c.stage, c.recoverable_mid_episode, c.description) == ("grasp", "late", True,
                                                                            "fingers close beside the cup")
    assert inp.diagnosis_note == "Grouped by what the wrist cam shows."
    assert P.guardrail(inp).required_type == "recovery_takeover"
    f = P.facts(inp, P.budget(inp))
    assert f["failure_clusters"][0]["phase"] == "grasp" and f["latest_eval"]["ci95"] == [0.1, 0.6]
    assert f["failure_clusters"][0]["example_trials"][0]["failure_note"] == "fixture failure"


def test_refusal_raises():
    fake = FakeClaude(answer("clean_teleop", 1, "x"))
    fake.beta.messages.create = lambda **kw: SimpleNamespace(stop_reason="refusal", stop_details=None, content=[])
    with pytest.raises(P.PlannerError):
        P.plan(inputs_for(tables(DROPS_MID_GRASP, successes=3)), fake)


# --------------------------------------------------------------------------- bus handler

class FakeBusClient:
    def __init__(self):
        self.state, self.identity, self.cb, self.calls = "stopped", "0xfake", {}, []
        self._stop = asyncio.Event()

    def subscribe(self, q): self.queries = q
    def on_snapshot(self, cb): self.cb["snapshot"] = cb
    def on_insert(self, cb): self.cb["insert"] = cb
    def on_update(self, cb): self.cb["update"] = cb
    def on_delete(self, cb): self.cb["delete"] = cb
    def on_state(self, cb): self.cb["state"] = cb
    def key(self, table, row): return row.get("id", row.get("ticket_id"))
    async def run(self): await self._stop.wait()
    def stop(self): self._stop.set()
    async def call_reducer(self, r, a): self.calls.append((r, a)); return 1


async def _settle(bus, n=200):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if bus.health()["inflight"] == 0:
            return


def test_handler_records_plan_once():
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.handlers import planner as H

    fake = FakeClaude(answer("recovery_takeover", 5, 'Takeovers for "drops mid-grasp" (5 of 7).'))
    H.set_client_factory(lambda: fake)
    H._claims.clear()
    reg = Registry()
    reg.handlers = [h for h in registry.handlers if h.name == "tdm24_planner"]
    reg.tables = dict(registry.tables)
    tbl = tables(DROPS_MID_GRASP, successes=3)

    async def go():
        fc = FakeBusClient()
        bus = Bus(reg, fc, workers=2)
        await bus.start()
        fc.cb["state"]("live", None)
        fc.cb["snapshot"](tbl)
        await _settle(bus)
        fc.cb["snapshot"](tbl)  # reconnect resync, nothing changed: no second plan
        await _settle(bus)
        # an update that re-enters plan_data on the same iteration is also skipped (in-process claim)
        fc.cb["update"]("ticket", ticket(status="diagnose"), ticket(status="plan_data"), {})
        await _settle(bus)
        await bus.stop()
        return fc.calls

    calls = asyncio.run(go())
    plans = [a for r, a in calls if r == "record_data_plan"]
    assert len(plans) == 1 and len(fake.calls) == 1
    tid, dtype, amount, reasoning, evidence = plans[0]
    assert (tid, dtype, amount) == (7, "recovery_takeover", 5) and "drops mid-grasp" in reasoning
    assert json.loads(evidence)["guardrail"]["rule"] == "R2"


def test_handler_skips_when_plan_exists_and_logs_blocked():
    from rdf_orchestrator.bus import registry
    from rdf_orchestrator.handlers import planner as H

    fake = FakeClaude(answer("recovery_takeover", 5, '"drops mid-grasp"'))
    H.set_client_factory(lambda: fake)
    H._claims.clear()
    reg = Registry()
    reg.handlers = [h for h in registry.handlers if h.name == "tdm24_planner"]
    reg.tables = dict(registry.tables)
    existing = tables(DROPS_MID_GRASP, successes=3,
                      plans=[Row(id=1, ticket_id=7, iteration=0, data_type="recovery_takeover", amount=5,
                                 status="proposed", approved_cents=0)])
    broke = tables(DROPS_MID_GRASP, successes=3, budget=100)

    async def go(tbl):
        fc = FakeBusClient()
        bus = Bus(reg, fc, workers=2)
        await bus.start()
        fc.cb["state"]("live", None)
        fc.cb["snapshot"](tbl)
        await _settle(bus)
        await bus.stop()
        return fc.calls

    assert asyncio.run(go(existing)) == [] and not fake.calls
    H._claims.clear()
    calls = asyncio.run(go(broke))
    assert [r for r, _ in calls] == ["append_event"] and calls[0][1][1] == "planner.blocked"
    assert not fake.calls


def test_run_settings_column_override_beats_event_and_rule():
    """TDM-79: the dashboard's "Data type" (run_settings.data_type_override, TDM-20 PATCH /settings)
    is read first; an override event only applies while the column is empty."""
    ov = {"type": P.OVERRIDE_EVENT, "payload": {"ticket_id": 7, "data_type": "recovery_takeover"}}
    tbl = tables(DROPS_MID_GRASP, successes=3, extra_events=[ov])
    tbl["run_settings"][0]["data_type_override"] = "clean_teleop"
    inp = inputs_for(tbl)
    assert inp.data_type_override == "clean_teleop"
    assert P.guardrail(inp).rule == "override"
    tbl["run_settings"][0]["data_type_override"] = ""  # cleared -> falls back to the event
    assert inputs_for(tbl).data_type_override == "recovery_takeover"
    tbl["run_settings"][0]["data_type_override"] = "bogus"  # not a data type -> ignored
    assert inputs_for(tbl).data_type_override == "recovery_takeover"
