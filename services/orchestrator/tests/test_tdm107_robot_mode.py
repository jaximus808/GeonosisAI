"""TDM-107: per-ticket robot mode (sim robot | real YAM), switching, and keeping sim out of real numbers."""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdf_common.spacetime_client import SpacetimeError

from rdf_orchestrator import report
from rdf_orchestrator import robot_mode as rm
from rdf_orchestrator.flywheel import TicketView, decide
from rdf_orchestrator.tickets_api import CreateTicketRequest, ParsedTicket, TicketsService

SIM_CFG = json.dumps({"sim": True, "cameras": "mujoco", "rendered": "mujoco"})
REAL_CFG = json.dumps({"cameras": "config"})


def tdm107_run(coro):
    return asyncio.run(coro)


def tdm107_robot(rid: int, name: str, status: str = "online", sim: bool = False) -> dict:
    return {"id": rid, "name": name, "status": status, "embodiment": "i2rt_yam", "adapter_version": "v1",
            "camera_config_json": SIM_CFG if sim else REAL_CFG}


class Tdm107Stdb:
    """In-memory SELECT * FROM t [WHERE col = n | WHERE type = 's'] + the reducers the switch calls."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {t: [] for t in (
            "robot", "ticket", "run_settings", "eval_run", "task", "event_log", "dataset", "data_plan",
            "agent_session", "payment")}
        self.calls: list[tuple] = []
        self.missing_reducer = False

    async def sql(self, q: str) -> list[dict]:
        m = re.fullmatch(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (?:(\d+)|'([^']*)'))?", q)
        assert m, q
        rows = self.tables[m[1]]
        if m[2] is None:
            return [dict(r) for r in rows]
        want = int(m[3]) if m[3] is not None else m[4]
        return [dict(r) for r in rows if r.get(m[2]) == want]

    async def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name == "set_ticket_robot":
            if self.missing_reducer:
                raise SpacetimeError(404, "No such reducer", reducer=name)
            tid, rid, by, reason = args
            t = next(t for t in self.tables["ticket"] if t["id"] == tid)
            frm = next(r for r in self.tables["robot"] if r["id"] == t["robot_id"])
            to = next(r for r in self.tables["robot"] if r["id"] == rid)
            t["robot_id"] = rid
            self.tables["event_log"].append({"id": len(self.tables["event_log"]) + 1, "ts": 5, "actor": "orchestrator",
                                             "type": "ticket.robot_mode_changed", "payload_json": json.dumps({
                                                 "ticket_id": tid, "from_robot_id": frm["id"],
                                                 "from_mode": rm.robot_mode(frm), "to_robot_id": rid,
                                                 "to_mode": rm.robot_mode(to), "by": by, "reason": reason})})
        elif name == "cancel_task":
            next(t for t in self.tables["task"] if t["id"] == args[0])["status"] = "cancelled"
        else:
            raise AssertionError(name)


def tdm107_world(ticket_status: str = "dispatch") -> Tdm107Stdb:
    s = Tdm107Stdb()
    s.tables["robot"] = [tdm107_robot(1, "yam-1", "offline"), tdm107_robot(2, "sim-yam-mujoco", sim=True),
                         tdm107_robot(3, "test.sim-yam-1", sim=True)]
    s.tables["ticket"] = [{"id": 7, "robot_id": 2, "status": ticket_status, "iteration": 0, "task_description": "x"}]
    s.tables["run_settings"] = [{"ticket_id": 7, "hyperparams_json": json.dumps({"prompt": "p"})}]
    s.tables["task"] = [
        {"id": 50, "ticket_id": 7, "robot_id": 2, "type": "clean_teleop", "status": "open"},
        {"id": 51, "ticket_id": 7, "robot_id": 2, "type": "clean_teleop", "status": "claimed"},
        {"id": 52, "ticket_id": 7, "robot_id": 2, "type": "reset", "status": "open"},
    ]
    return s


# --------------------------------------------------------------------------- labels

def test_mode_comes_from_the_robot_row():
    assert rm.robot_mode(tdm107_robot(1, "yam-1")) == "real"
    assert rm.robot_mode(tdm107_robot(2, "sim", sim=True)) == "sim"
    assert rm.robot_mode({"camera_config_json": "not json"}) == "real"
    assert rm.robot_mode({"camera_config_json": json.dumps({"sim": "yes"})}) == "real"  # only literal true
    assert rm.robot_mode(None) is None


def test_include_sim_data_defaults_by_mode_and_can_be_set():
    assert rm.include_sim_data(None, "sim") is True
    assert rm.include_sim_data(None, "real") is False
    on = {"hyperparams_json": json.dumps({"include_sim_data": True})}
    assert rm.include_sim_data(on, "real") is True
    off = {"hyperparams_json": json.dumps({"include_sim_data": False})}
    assert rm.include_sim_data(off, "sim") is False


# --------------------------------------------------------------------------- creation picks by mode

def test_create_in_sim_mode_picks_the_sim_robot():
    s = tdm107_world()
    s.tables["robot"][0]["status"] = "online"  # both modes online: only the mode decides
    svc = TicketsService(s, parser=None)
    robot, how = tdm107_run(svc.pick_robot(None, "sim"))
    assert robot["id"] == 2 and how == "only_online_sim_robot"  # test.* robots are never auto-picked
    robot, _ = tdm107_run(svc.pick_robot(None, "real"))
    assert robot["id"] == 1
    with pytest.raises(Exception) as e:
        tdm107_run(svc.pick_robot(1, "sim"))
    assert e.value.code == "robot_mode_mismatch"


def test_preview_reports_the_mode():
    s = tdm107_world()
    svc = TicketsService(s, parser=None)
    parsed = ParsedTicket(task_description="Put the cube on the plate.", target_success=0.8, budget_usd=20,
                          policy_prompt="put the cube on the plate", eval_trials=None)
    out = tdm107_run(svc.preview(CreateTicketRequest(prompt="cube", robot_mode="sim", parsed=parsed)))
    assert out["robot"]["id"] == 2 and out["robot_mode"] == "sim" and out["robot"]["robot_mode"] == "sim"
    out = tdm107_run(svc.preview(CreateTicketRequest(prompt="cube", robot_mode="real", parsed=parsed)))
    assert out["robot"] is None and out["robot_error"]["code"] == "no_online_robot"  # the YAM is offline


# --------------------------------------------------------------------------- switching

def test_switch_to_real_moves_the_ticket_and_reposts_open_tasks():
    s = tdm107_world()
    out = tdm107_run(rm.RobotModeService(s).switch(7, "real", by="human:jax", reason="the YAM arrived"))
    assert out["changed"] and out["robot_mode"] == "real" and out["robot"]["id"] == 1
    assert out["selection"] == "only_real_robot_offline"  # offline is fine: new tasks wait for it
    assert ("set_ticket_robot", 7, 1, "human:jax", "the YAM arrived") in s.calls
    assert out["cancelled_task_ids"] == [50]  # open collection task only; claimed + reset tasks are left
    assert [h["to_mode"] for h in out["history"]] == ["real"]
    again = tdm107_run(rm.RobotModeService(s).switch(7, "real"))
    assert again["changed"] is False


def test_switch_refused_while_an_eval_runs_or_when_frozen():
    s = tdm107_world()
    s.tables["eval_run"] = [{"id": 9, "ticket_id": 7, "robot_id": 2, "status": "running"}]
    with pytest.raises(rm.RobotModeError) as e:
        tdm107_run(rm.RobotModeService(s).switch(7, "real"))
    assert e.value.code == "eval_running" and e.value.status == 409
    s = tdm107_world("shipped")
    with pytest.raises(rm.RobotModeError) as e:
        tdm107_run(rm.RobotModeService(s).switch(7, "real"))
    assert e.value.code == "ticket_frozen"
    assert not [c for c in s.calls if c[0] == "set_ticket_robot"]


def test_switch_without_the_reducer_changes_nothing():
    s = tdm107_world()
    s.missing_reducer = True
    with pytest.raises(rm.RobotModeError) as e:
        tdm107_run(rm.RobotModeService(s).switch(7, "real"))
    assert e.value.code == "reducer_not_published" and e.value.status == 503
    assert s.tables["ticket"][0]["robot_id"] == 2 and s.tables["task"][0]["status"] == "open"


def test_switch_route_is_operator_only():
    s = tdm107_world()
    app = FastAPI()
    app.include_router(rm.robot_mode_router)
    app.dependency_overrides[rm.get_robot_mode_service] = lambda: rm.RobotModeService(s)
    app.dependency_overrides[rm.get_robot_mode_operator_key] = lambda: "k" * 32
    c = TestClient(app)
    assert c.get("/tickets/7/robot_mode").json()["robot_mode"] == "sim"
    assert c.post("/tickets/7/robot_mode", json={"mode": "real"}).status_code == 401
    assert c.post("/tickets/7/robot_mode", json={"mode": "real"}, headers={"X-RDF-Operator-Key": "nope"}).status_code == 401
    r = c.post("/tickets/7/robot_mode", json={"mode": "real"}, headers={"X-RDF-Operator-Key": "k" * 32, "X-Human-Id": "jax"})
    assert r.status_code == 200 and r.json()["robot_mode"] == "real"
    assert c.post("/tickets/7/robot_mode", json={"mode": "lab"}, headers={"X-RDF-Operator-Key": "k" * 32}).status_code == 422
    app.dependency_overrides[rm.get_robot_mode_operator_key] = lambda: None
    assert c.post("/tickets/7/robot_mode", json={"mode": "sim"}).status_code == 503  # fail closed


# --------------------------------------------------------------------------- flywheel never mixes modes

class Tdm107Bus:
    def __init__(self, tables: dict[str, list[dict]]) -> None:
        self.tables = tables

    def rows(self, t):
        return list(self.tables.get(t, []))

    def get(self, t, k):
        key = "ticket_id" if t == "run_settings" else "id"
        return next((r for r in self.tables.get(t, []) if r.get(key) == k), None)


def tdm107_view(robot_id: int, settings_hp: dict | None = None) -> TicketView:
    eps = [{"id": 1, "ticket_id": 7, "robot_id": 2, "kind": "clean_teleop", "review_status": "accepted"},
           {"id": 2, "ticket_id": 7, "robot_id": 1, "kind": "clean_teleop", "review_status": "accepted"},
           {"id": 3, "ticket_id": 7, "robot_id": 2, "kind": "eval", "review_status": "not_reviewed"}]
    evals = [{"id": 10, "ticket_id": 7, "robot_id": 2, "kind": "baseline", "status": "done", "successes": 3,
              "trials": 10, "training_run_id": 0, "checkpoint_uri": "base"}]
    bus = Tdm107Bus({
        "ticket": [{"id": 7, "robot_id": robot_id, "status": "baseline", "iteration": 0}],
        "robot": [tdm107_robot(1, "yam-1"), tdm107_robot(2, "sim-yam-mujoco", sim=True)],
        "run_settings": [{"ticket_id": 7, "hyperparams_json": json.dumps(settings_hp or {})}],
        "episode": eps, "eval_run": evals,
    })
    return TicketView.from_bus(bus, 7)


def test_a_sim_baseline_does_not_count_for_a_real_ticket():
    sim = tdm107_view(robot_id=2)
    assert sim.robot_mode == "sim" and sim.baseline_eval["id"] == 10
    assert decide(sim).to == "diagnose"
    real = tdm107_view(robot_id=1)  # the same ticket after switching to the YAM
    assert real.robot_mode == "real" and real.baseline_eval is None
    assert decide(real).to is None  # waits for a real-robot baseline


def test_training_set_leaves_sim_episodes_out_of_a_real_ticket_unless_asked():
    assert tdm107_view(robot_id=2).accepted_episode_ids == [1, 2]
    real = tdm107_view(robot_id=1)
    assert real.accepted_episode_ids == [2] and real.excluded_sim_episode_ids == [1]
    assert tdm107_view(1, {"include_sim_data": True}).accepted_episode_ids == [1, 2]
    assert tdm107_view(2, {"include_sim_data": False}).accepted_episode_ids == [2]


# --------------------------------------------------------------------------- the report splits sim and real

def test_report_separates_sim_from_real():
    t = {
        "ticket": [{"id": 7, "robot_id": 1, "status": "eval", "iteration": 0, "task_description": "cube",
                    "target_success": 0.8, "budget_cents": 1000, "approved_cents": 0, "spent_cents": 0,
                    "created_at": 1, "created_by": "web"}],
        "robot": [tdm107_robot(1, "yam-1"), tdm107_robot(2, "sim-yam-mujoco", sim=True)],
        "run_settings": [{"ticket_id": 7, "success_threshold": 0.8, "require_ci_low": False, "hyperparams_json": "{}",
                          "created_at": 1}],
        "eval_run": [
            {"id": 10, "ticket_id": 7, "robot_id": 2, "kind": "baseline", "status": "done", "successes": 9,
             "trials": 10, "completed": 10, "ci_low": 0.6, "ci_high": 0.98, "training_run_id": 0,
             "checkpoint_uri": "base", "created_at": 2},
            {"id": 11, "ticket_id": 7, "robot_id": 1, "kind": "baseline", "status": "done", "successes": 2,
             "trials": 10, "completed": 10, "ci_low": 0.05, "ci_high": 0.5, "training_run_id": 0,
             "checkpoint_uri": "base", "created_at": 3},
        ],
        "eval_trial": [],
        "episode": [{"id": 1, "ticket_id": 7, "robot_id": 2, "kind": "clean_teleop", "review_status": "accepted",
                     "created_at": 2}],
        "event_log": [{"id": 1, "ts": 4, "actor": "orchestrator", "type": "ticket.robot_mode_changed",
                       "payload_json": json.dumps({"ticket_id": 7, "from_robot_id": 2, "from_mode": "sim",
                                                   "to_robot_id": 1, "to_mode": "real", "by": "human:jax"})}],
    }
    r = report.assemble(t, 7, now_micros=10)
    assert r["robot_mode"]["current"] == "real"
    assert r["baseline"]["eval_run_id"] == 11 and r["baseline"]["robot_mode"] == "real"  # never the 9/10 sim run
    sim, real = r["robot_mode"]["by_mode"]["sim"], r["robot_mode"]["by_mode"]["real"]
    assert (sim["successes"], sim["trials"], sim["robot_ids"]) == (9, 10, [2])
    assert (real["successes"], real["trials"], real["robot_ids"]) == (2, 10, [1])
    assert sim["episodes"] == 1 and real["episodes"] == 0
    assert r["robot_mode"]["include_sim_data"] is False
    assert [s["to_mode"] for s in r["robot_mode"]["switches"]] == ["real"]
    md = report.render_markdown(r)
    assert "## Sim vs real robot" in md and "**SIM**" in md and "real-robot** results only" in md
