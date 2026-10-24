"""The switchable LLM backend from the orchestrator's side: the planner follows a mid-run switch (fake
CLI runners), GET/PUT /admin/llm (operator key, audit row, production lock), retry classification of
CLI errors, and the fail-fast startup check. No real CLI or API call."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import test_tdm24_planner as T
from rdf_common import llm
from rdf_orchestrator import llm_retry
from rdf_orchestrator import planner as P
from rdf_orchestrator.admin_llm_api import admin_llm_router
from rdf_orchestrator.robot_commands_api import get_operator_key, get_robot_commands_stdb

KEY = "test-operator-key"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in ("RDF_LLM_BACKEND", "RDF_DEPLOY_ENV", "FLY_APP_NAME", "FLY_MACHINE_ID", "K_SERVICE",
              "RAILWAY_ENVIRONMENT", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RDF_LLM_STATE_FILE", str(tmp_path / "llm_backend.json"))
    monkeypatch.setenv("RDF_LLM_CLAUDE_BIN", "/bin/sh")
    monkeypatch.setenv("RDF_LLM_DROID_BIN", "/bin/sh")
    real = llm.LLMSettings
    monkeypatch.setattr(llm, "settings", lambda: real(_env_file=None))


class Runner:
    def __init__(self, make):
        self.make, self.calls = make, []

    def __call__(self, argv, *, input, cwd, env, timeout):
        self.calls.append({"argv": argv, "input": input})
        return self.make()


def plan_answer():
    return T.answer("recovery_takeover", 2, 'Most failures are "drops mid-grasp" late in the lift.', ids=(11,))


def cc():
    a = plan_answer()
    ev = [{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(a), "structured_output": a,
           "session_id": "s", "usage": {"input_tokens": 900, "output_tokens": 300}, "total_cost_usd": 0.02,
           "modelUsage": {"claude-opus-5-5": {}}}]
    return llm.Proc(0, "\n".join(json.dumps(e) for e in ev), "")


def droid():
    return llm.Proc(0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                   "result": json.dumps(plan_answer()), "session_id": "d",
                                   "usage": {"input_tokens": 9, "output_tokens": 99, "factory_credits": 20000}}), "")


def test_planner_follows_a_mid_run_switch():
    rc, rd = Runner(cc), Runner(droid)
    client = llm.LLM(runners={"claude_code": rc, "droid": rd})
    inp = T.inputs_for(T.tables(T.DROPS_MID_GRASP, successes=3))
    llm.set_backend("claude_code", by="test")
    a = P.plan(inp, client)
    assert len(rc.calls) == 1 and not rd.calls
    m = a.evidence["model"]
    assert m["backend"] == "claude_code" and m["backend_label"] == "Claude Code (local)" and m["backend_model"] == "opus"
    assert m["attempts"][0]["usage"]["backend"] == "claude_code"
    assert a.data_type == "recovery_takeover" and a.amount == 2  # same guardrails, same output type

    llm.set_backend("droid", by="test")  # the next planner call, no restart
    b = P.plan(inp, client)
    assert len(rd.calls) == 1 and len(rc.calls) == 1
    assert b.evidence["model"]["backend"] == "droid" and b.evidence["model"]["attempts"][0]["usage"]["factory_credits"] == 20000
    prompt_arg = rd.calls[0]["argv"]
    assert prompt_arg[prompt_arg.index("-m") + 1] == "claude-opus-5-5"


def test_fake_anthropic_client_stays_on_the_api():
    llm.set_backend("claude_code", by="test")
    fake = T.FakeClaude(plan_answer())
    out = P.plan(T.inputs_for(T.tables(T.DROPS_MID_GRASP, successes=3)), fake)
    assert len(fake.calls) == 1 and out.evidence["model"]["backend"] == "api"


def test_classify_cli_errors():
    t = llm.LLMError("claude_code timed out after 120s", reason="timeout", kind=llm.TRANSIENT)
    assert llm_retry.classify(P.PlannerError("x").with_traceback(None)) == llm_retry.ERROR
    try:
        raise P.PlannerError("planner LLM call failed") from t
    except P.PlannerError as e:
        assert llm_retry.classify(e) == llm_retry.TRANSIENT
    r = llm.LLMError("model refused (refusal, category=bio)", reason="refusal", kind=llm.REFUSAL)
    assert llm_retry.classify(r) == llm_retry.REFUSAL


class FakeStdb:
    def __init__(self):
        self.calls = []

    async def call_reducer(self, name, *args):
        self.calls.append((name, args))

    async def sql(self, q):
        return []


def app(stdb, key=KEY):
    a = FastAPI()
    a.include_router(admin_llm_router)
    a.dependency_overrides[get_robot_commands_stdb] = lambda: stdb
    a.dependency_overrides[get_operator_key] = lambda: key
    return TestClient(a)


def test_admin_llm_get_put_with_operator_key_and_audit():
    stdb = FakeStdb()
    c = app(stdb)
    g = c.get("/admin/llm").json()
    assert g["backend"] == "api" and not g["locked"] and g["allowed"] == ["api", "droid", "claude_code"]
    assert c.put("/admin/llm", json={"backend": "claude_code"}).status_code == 401
    assert c.put("/admin/llm", json={"backend": "claude_code"}, headers={"X-RDF-Operator-Key": "nope"}).status_code == 401
    assert c.put("/admin/llm", json={"backend": "gpt"}, headers={"X-RDF-Operator-Key": KEY}).status_code == 422
    r = c.put("/admin/llm", json={"backend": "claude_code", "model": "sonnet"},
              headers={"X-RDF-Operator-Key": KEY, "X-Human-Id": "jaxon"})
    assert r.status_code == 200
    b = r.json()
    assert b["backend"] == "claude_code" and b["effective_model"] == "sonnet" and b["source"] == "runtime"
    assert b["changed"] == {"from": "api", "to": "claude_code", "model": "sonnet", "from_model": None, "by": "jaxon"}
    assert b["audit"] == "logged"
    name, args = stdb.calls[0]
    assert name == "append_event" and args[0] == "human:operator" and args[1] == "llm.backend_changed"
    assert json.loads(args[2])["to"] == "claude_code"
    assert c.get("/admin/llm").json()["backend"] == "claude_code"
    assert llm.current().backend == "claude_code"  # what the next LLM call uses


def test_admin_llm_no_key_configured_is_503():
    assert app(FakeStdb(), key=None).put("/admin/llm", json={"backend": "droid"},
                                         headers={"X-RDF-Operator-Key": KEY}).status_code == 503


@pytest.mark.parametrize("var,val", [("RDF_DEPLOY_ENV", "production"), ("FLY_APP_NAME", "rdf-orchestrator")])
def test_admin_llm_locked_in_production(monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    stdb = FakeStdb()
    c = app(stdb)
    g = c.get("/admin/llm").json()
    assert g["locked"] is True and g["backend"] == "api" and g["allowed"] == ["api"]
    r = c.put("/admin/llm", json={"backend": "claude_code"}, headers={"X-RDF-Operator-Key": KEY})
    assert r.status_code == 403 and "locked in production" in r.json()["detail"]
    assert not stdb.calls


def test_startup_fails_fast_for_non_api_in_production(monkeypatch):
    from rdf_orchestrator.app import create_app

    monkeypatch.setenv("RDF_DEPLOY_ENV", "production")
    monkeypatch.setenv("RDF_LLM_BACKEND", "claude_code")
    with pytest.raises(SystemExit) as ei:
        create_app(client=object())
    assert ei.value.code == 2
    monkeypatch.setenv("RDF_LLM_BACKEND", "api")
    create_app(client=object())  # api is fine
