"""TDM-19 POST /tickets + GET /tickets/{id} with a fake parser and an in-memory fake SpacetimeDB."""
from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdf_common.spacetime_client import SpacetimeError

from rdf_orchestrator.tickets_api import ParsedTicket, TicketsService, get_tickets_service, router

DEFAULT_HYPER = {"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16, "prompt": ""}


class FakeStdb:
    """Just enough of create_ticket and SELECT * FROM t [WHERE col = n] for the router."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {t: [] for t in (
            "robot", "ticket", "run_settings", "dataset", "eval_run", "data_plan", "agent_session", "payment")}
        self.calls: list[tuple] = []
        self.clock = 1_000

    def add_robot(self, rid: int, name: str, status: str) -> None:
        self.tables["robot"].append({"id": rid, "name": name, "status": status, "embodiment": "i2rt_yam"})

    async def sql(self, q: str) -> list[dict]:
        m = re.fullmatch(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (\d+))?", q)
        assert m, q
        rows = self.tables[m[1]]
        return [dict(r) for r in rows if m[2] is None or r[m[2]] == int(m[3])]

    async def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        assert name == "create_ticket"
        robot_id, desc, target, budget, overrides_json = args
        overrides = json.loads(overrides_json) if overrides_json else {}
        if "budget_cents" in overrides:
            raise SpacetimeError(530, "budget_cents is a create_ticket argument, not an override", reducer=name)
        self.clock += 1
        tid = 100 + len(self.tables["ticket"]) * 7  # ids are not sequential
        self.tables["ticket"].append({"id": tid, "robot_id": robot_id, "task_description": desc, "target_success": target,
                                      "budget_cents": budget, "approved_cents": 0, "spent_cents": 0, "status": "created",
                                      "created_at": self.clock})
        hyper = {**DEFAULT_HYPER, **overrides.get("hyperparams", {})}
        self.tables["run_settings"].append({
            "ticket_id": tid, "eval_trials": overrides.get("eval_trials", 10), "success_threshold": target,
            "require_ci_low": False, "budget_cents": budget, "target_episodes": 5, "max_iterations": 3,
            "hyperparams_json": json.dumps(hyper)})
        self.tables["dataset"].append({"id": tid, "ticket_id": tid, "accepted_count": 0, "total_count": 0})


class FakeParser:
    def __init__(self, **fields) -> None:
        self.fields = {"task_description": "Put the cup in the bin.", "target_success": 0.8, "budget_usd": 50.0,
                       "policy_prompt": "put the cup in the bin", "eval_trials": None, **fields}
        self.prompts: list[str] = []

    async def parse(self, prompt: str) -> ParsedTicket:
        self.prompts.append(prompt)
        return ParsedTicket(**self.fields)


def make(parser: FakeParser | None = None, robots=((1, "yam-1", "online"),)) -> tuple[TestClient, FakeStdb]:
    stdb = FakeStdb()
    for r in robots:
        stdb.add_robot(*r)
    svc = TicketsService(stdb, parser or FakeParser())
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_tickets_service] = lambda: svc
    return TestClient(app), stdb


def test_prompt_creates_ticket_with_settings():
    c, stdb = make()
    r = c.post("/tickets", json={"prompt": "Put the cup in the bin, 80% reliable, $50 budget"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["ticket_id"] == 100 and d["robot_selection"] == "only_online_robot"
    assert stdb.calls == [("create_ticket", 1, "Put the cup in the bin.", 0.8, 5000,
                           json.dumps({"hyperparams": {"prompt": "put the cup in the bin"}}))]
    assert d["ticket"]["budget_cents"] == 5000 and d["ticket"]["target_success"] == 0.8
    rs = d["run_settings"]
    assert rs["eval_trials"] == 10 and rs["success_threshold"] == 0.8
    assert rs["hyperparams"]["prompt"] == "put the cup in the bin" and rs["hyperparams"]["steps"] == 2000
    assert d["defaults_applied"] == []


def test_missing_budget_is_422_asking_for_one():
    c, stdb = make(FakeParser(budget_usd=None))
    r = c.post("/tickets", json={"prompt": "Put the cup in the bin, 80% reliable"})
    assert r.status_code == 422
    det = r.json()["detail"]
    assert det["code"] == "budget_required" and "budget" in det["message"].lower()
    assert det["parsed"]["task_description"] == "Put the cup in the bin."
    assert stdb.calls == []


def test_explicit_budget_field_fills_missing_budget_and_target_defaults():
    c, stdb = make(FakeParser(budget_usd=None, target_success=None))
    r = c.post("/tickets", json={"prompt": "put the cup in the bin", "budget_cents": 1234})
    assert r.status_code == 201, r.text
    assert stdb.calls[0][3:5] == (0.8, 1234)
    assert r.json()["defaults_applied"] == ["target_success=0.8"]


def test_overrides_merge_and_eval_trials_from_prompt():
    c, stdb = make(FakeParser(eval_trials=20))
    r = c.post("/tickets", json={"prompt": "x", "settings_overrides": {"max_iterations": 2, "hyperparams": {"steps": 500}}})
    assert r.status_code == 201, r.text
    assert json.loads(stdb.calls[0][5]) == {"max_iterations": 2, "hyperparams": {"steps": 500, "prompt": "put the cup in the bin"},
                                            "eval_trials": 20}
    assert r.json()["run_settings"]["eval_trials"] == 20


def test_reducer_rejection_is_422():
    c, _ = make()
    r = c.post("/tickets", json={"prompt": "x", "settings_overrides": {"budget_cents": 1}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "create_ticket_rejected"


@pytest.mark.parametrize("robots,code", [
    (((1, "yam-1", "offline"),), "no_online_robot"),
    (((1, "yam-1", "online"), (2, "yam-2", "busy")), "multiple_online_robots"),
])
def test_robot_selection_errors(robots, code):
    c, _ = make(robots=robots)
    r = c.post("/tickets", json={"prompt": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


def test_test_robots_ignored_for_auto_pick_but_allowed_explicitly():
    c, stdb = make(robots=((1, "yam-1", "online"), (2, "test.fake-yam", "online")))
    assert c.post("/tickets", json={"prompt": "x"}).json()["robot"]["id"] == 1
    r = c.post("/tickets", json={"prompt": "x", "robot_id": 2})
    assert r.status_code == 201 and r.json()["robot_selection"] == "requested" and stdb.calls[-1][1] == 2
    assert c.post("/tickets", json={"prompt": "x", "robot_id": 9}).json()["detail"]["code"] == "unknown_robot"


def test_two_identical_prompts_get_distinct_ids():
    c, _ = make()
    a = c.post("/tickets", json={"prompt": "x"}).json()["ticket_id"]
    b = c.post("/tickets", json={"prompt": "x"}).json()["ticket_id"]
    assert a != b


def test_get_aggregate():
    c, stdb = make()
    tid = c.post("/tickets", json={"prompt": "x"}).json()["ticket_id"]
    stdb.tables["eval_run"] += [{"id": 9, "ticket_id": tid, "kind": "candidate", "created_at": 50},
                                {"id": 3, "ticket_id": tid, "kind": "baseline", "created_at": 10}]
    stdb.tables["payment"] += [{"id": 1, "ticket_id": tid, "amount_cents": 500, "status": "paid"},
                               {"id": 2, "ticket_id": tid, "amount_cents": 200, "status": "pending"}]
    stdb.tables["ticket"][0]["spent_cents"] = 700
    d = c.get(f"/tickets/{tid}").json()
    assert d["ticket"]["id"] == tid and d["robot"]["name"] == "yam-1"
    assert d["latest_eval_run"]["kind"] == "candidate" and [e["id"] for e in d["eval_runs"]] == [3, 9]
    assert d["spend"] == {"budget_cents": 5000, "approved_cents": 0, "spent_cents": 700, "remaining_cents": 4300,
                          "payments_by_status_cents": {"paid": 500, "pending": 200}, "payment_count": 2}
    assert d["run_settings"]["hyperparams"]["prompt"] == "put the cup in the bin"
    assert d["data_plans"] == [] and d["agent_sessions"] == [] and d["dataset"]["ticket_id"] == tid
    assert c.get("/tickets/424242").status_code == 404


# ---------------------------------------------------------------- TDM-74: preview, then create from it

def test_parse_previews_without_creating():
    parser = FakeParser(budget_usd=None, target_success=None)
    c, stdb = make(parser)
    r = c.post("/tickets/parse", json={"prompt": "Put the cup in the bin",
                                       "settings_overrides": {"max_iterations": 2}})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["missing"] == ["budget"] and b["budget_cents"] is None
    assert b["target_success"] == 0.8 and b["defaults_applied"] == ["target_success=0.8"]
    assert b["settings_overrides"] == {"max_iterations": 2, "hyperparams": {"prompt": "put the cup in the bin"}}
    assert b["robot"]["id"] == 1 and b["robot_selection"] == "only_online_robot" and b["robot_error"] is None
    assert stdb.calls == [] and stdb.tables["ticket"] == []


def test_parse_reports_robot_problem_in_body():
    c, _ = make(robots=((1, "a", "online"), (2, "b", "online")))
    b = c.post("/tickets/parse", json={"prompt": "x"}).json()
    assert b["robot"] is None and b["robot_error"]["code"] == "multiple_online_robots"
    assert {r["id"] for r in b["robot_error"]["robots"]} == {1, 2}


def test_create_with_parsed_skips_claude_and_matches_preview():
    parser = FakeParser()
    c, stdb = make(parser)
    preview = c.post("/tickets/parse", json={"prompt": "Put the cup in the bin, 80% reliable, $50 budget"}).json()
    r = c.post("/tickets", json={"prompt": "Put the cup in the bin, 80% reliable, $50 budget",
                                 "parsed": preview["parsed"], "budget_cents": 4000})
    assert r.status_code == 201, r.text
    assert len(parser.prompts) == 1  # only the preview called the parser
    assert r.json()["ticket"]["budget_cents"] == 4000
    assert r.json()["ticket"]["task_description"] == preview["task_description"]
