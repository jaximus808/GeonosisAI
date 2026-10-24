"""rdf_common.llm: the droid / claude_code adapters with fake subprocess runners, runtime switching,
and the production lock. No real CLI or API is called here."""
from __future__ import annotations

import base64
import json

import pytest
from pydantic import BaseModel

from rdf_common import llm
from rdf_common.settings import REPO_ROOT

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["color", "n"],
          "properties": {"color": {"type": "string", "enum": ["red", "blue"]}, "n": {"type": "integer", "minimum": 1}}}
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                   "data": base64.standard_b64encode(JPEG).decode()}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for k in ("RDF_LLM_BACKEND", "RDF_DEPLOY_ENV", "FLY_APP_NAME", "FLY_MACHINE_ID", "K_SERVICE",
              "RAILWAY_ENVIRONMENT", "RENDER", "RDF_LLM_CLAUDE_CODE_MODEL", "RDF_LLM_DROID_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RDF_LLM_STATE_FILE", str(tmp_path / "state" / "llm_backend.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("RDF_LLM_CLAUDE_BIN", "/bin/sh")  # exists; the fake runner never runs it
    monkeypatch.setenv("RDF_LLM_DROID_BIN", "/bin/sh")
    real = llm.LLMSettings
    monkeypatch.setattr(llm, "settings", lambda: real(_env_file=None))  # ignore the repo .env
    yield


class FakeRunner:
    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def __call__(self, argv, *, input, cwd, env, timeout):
        files = {}
        from pathlib import Path

        for p in Path(cwd).iterdir():
            files[p.name] = p.read_bytes()
        self.calls.append({"argv": argv, "input": input, "cwd": cwd, "env": env, "timeout": timeout, "files": files})
        o = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
        if isinstance(o, BaseException):
            raise o
        return o


def cc_out(structured=None, *, result=None, refusal=False, is_error=False, subtype="success", status=None):
    ev = [{"type": "system", "subtype": "init"}]
    if refusal:
        ev.append({"type": "assistant", "message": {"stop_reason": "refusal", "stop_details": {"category": "cyber"},
                                                     "content": []}})
    ev.append({"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": status,
               "result": result if result is not None else json.dumps(structured),
               "structured_output": structured, "session_id": "s-1", "num_turns": 2,
               "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
               "total_cost_usd": 0.0123, "modelUsage": {"claude-opus-5-5": {"inputTokens": 10}}})
    return llm.Proc(0, "\n".join(json.dumps(e) for e in ev) + "\n", "")


def droid_out(text, *, is_error=False, rc=0):
    return llm.Proc(rc, json.dumps({"type": "result", "subtype": "failure" if is_error else "success",
                                    "is_error": is_error, "result": text, "session_id": "d-1", "num_turns": 3,
                                    "usage": {"input_tokens": 6, "output_tokens": 50, "factory_credits": 13613}}), "")


def call(client, **kw):
    args = dict(step="test", system="SYS PROMPT", messages=[{"role": "user", "content": [
        {"type": "text", "text": "What colour?"}, IMG]}], schema=SCHEMA, model="claude-opus-5-5", effort="high")
    args.update(kw)
    return client.complete_structured(**args)


# --------------------------------------------------------------------------- claude_code


def test_claude_code_structured_output_no_tools_scratch_dir_no_api_key():
    run = FakeRunner(cc_out({"color": "red", "n": 2}))
    c = llm.LLM(runners={"claude_code": run}, backend="claude_code")
    parsed, usage, meta = call(c)
    assert parsed == {"color": "red", "n": 2}
    a = run.calls[0]
    argv = a["argv"]
    assert argv[argv.index("--tools") + 1] == "" and "--json-schema" in argv and "--safe-mode" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert argv[argv.index("--model") + 1] == "opus" and argv[argv.index("--effort") + 1] == "high"
    assert a["files"]["system.txt"] == b"SYS PROMPT"
    assert "ANTHROPIC_API_KEY" not in a["env"]  # bills the local Claude Code login, not the API
    assert REPO_ROOT not in __import__("pathlib").Path(a["cwd"]).parents
    msg = json.loads(a["input"])
    assert msg["type"] == "user" and any(b["type"] == "image" for b in msg["message"]["content"])
    assert meta["backend"] == "claude_code" and meta["served_model"] == "claude-opus-5-5" and meta["model"] == "opus"
    assert usage["input_tokens"] == 10 and usage["cost_estimate_usd"] == 0.0123 and "claude_code" in usage["cost_basis"]
    assert llm.describe_result(meta) == "claude-opus-5-5 via Claude Code (local)"


def test_claude_code_schema_violation_retries_once_with_feedback():
    run = FakeRunner(cc_out({"color": "green", "n": 0}), cc_out({"color": "blue", "n": 3}))
    r = call(llm.LLM(runners={"claude_code": run}, backend="claude_code"))
    assert r.parsed == {"color": "blue", "n": 3} and r.meta["schema_retries"] == 1 and len(run.calls) == 2
    second = json.loads(run.calls[1]["input"])["message"]["content"][-1]["text"]
    assert "did not match the required JSON schema" in second and "minimum" in second
    assert r.usage["input_tokens"] == 20  # both tries counted


def test_claude_code_schema_violation_twice_raises_schema_error():
    run = FakeRunner(cc_out({"color": "green", "n": 1}))
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"claude_code": run}, backend="claude_code"))
    assert ei.value.reason == "schema" and ei.value.llm_kind == llm.ERROR and len(run.calls) == 2


def test_claude_code_timeout_is_transient():
    run = FakeRunner(TimeoutError("claude did not answer within 1s"))
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"claude_code": run}, backend="claude_code"), timeout_s=1)
    assert ei.value.reason == "timeout" and ei.value.llm_kind == llm.TRANSIENT and run.calls[0]["timeout"] == 1


def test_claude_code_refusal():
    run = FakeRunner(cc_out(None, result="", refusal=True))
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"claude_code": run}, backend="claude_code"))
    e = ei.value
    assert e.reason == "refusal" and e.llm_kind == llm.REFUSAL and e.category == "cyber"
    assert "refus" in str(e) and "category=" in str(e)  # llm_retry.classify's refusal test


def test_claude_code_rate_limit_error_is_transient():
    run = FakeRunner(cc_out(None, result="overloaded", is_error=True, subtype="error_during_execution", status=529))
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"claude_code": run}, backend="claude_code"))
    assert ei.value.llm_kind == llm.TRANSIENT


def test_claude_code_multi_turn_is_flattened():
    run = FakeRunner(cc_out({"color": "red", "n": 1}))
    c = llm.LLM(runners={"claude_code": run}, backend="claude_code")
    call(c, messages=[{"role": "user", "content": "first ask"},
                      {"role": "assistant", "content": [{"type": "text", "text": '{"color": "x"}'}]},
                      {"role": "user", "content": "fix it"}])
    text = " ".join(b["text"] for b in json.loads(run.calls[0]["input"])["message"]["content"] if b["type"] == "text")
    assert "first ask" in text and "Your previous answer" in text and '{"color": "x"}' in text and "fix it" in text


def test_output_model_parses_into_pydantic():
    class Out(BaseModel):
        color: str
        n: int | None

    run = FakeRunner(cc_out({"color": "red", "n": None}))
    r = call(llm.LLM(runners={"claude_code": run}, backend="claude_code"), schema=None, output_model=Out)
    assert isinstance(r.parsed, Out) and r.parsed.n is None


# --------------------------------------------------------------------------- droid


def test_droid_images_as_files_read_only_tools_and_fenced_json(monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fk-test-not-real")
    run = FakeRunner(droid_out('Here you go:\n```json\n{"color": "blue", "n": 4}\n```'))
    r = call(llm.LLM(runners={"droid": run}, backend="droid"))
    assert r.parsed == {"color": "blue", "n": 4}
    a = run.calls[0]
    argv = a["argv"]
    assert argv[1] == "exec" and argv[argv.index("-m") + 1] == "claude-opus-5-5" and argv[argv.index("-o") + 1] == "json"
    assert argv[argv.index("--only-tools") + 1] == "Read" and argv[argv.index("--cwd") + 1] == a["cwd"]
    assert "--auto" not in argv and "--skip-permissions-unsafe" not in argv  # default read-only autonomy
    assert a["files"]["image_01.jpg"] == JPEG and a["files"]["system.md"] == b"SYS PROMPT"
    prompt = a["files"]["prompt.md"].decode()
    assert "image_01.jpg" in prompt and json.dumps(SCHEMA) in prompt and "What colour?" in prompt
    assert a["env"]["FACTORY_API_KEY"] == "fk-test-not-real"
    assert r.usage["factory_credits"] == 13613 and r.meta["backend"] == "droid"


def test_droid_not_json_twice_is_bad_json():
    run = FakeRunner(droid_out("I think it is red."))
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"droid": run}, backend="droid"))
    assert ei.value.reason == "bad_json" and len(run.calls) == 2
    assert "did not match" in run.calls[1]["files"]["prompt.md"].decode()


def test_droid_error_kinds():
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"droid": FakeRunner(droid_out("Rate limit exceeded, try again", is_error=True, rc=1))},
                     backend="droid"))
    assert ei.value.llm_kind == llm.TRANSIENT
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"droid": FakeRunner(droid_out("Authentication failed.", is_error=True, rc=1))},
                     backend="droid"))
    assert ei.value.llm_kind == llm.ERROR
    with pytest.raises(llm.LLMError) as ei:
        call(llm.LLM(runners={"droid": FakeRunner(TimeoutError("slow"))}, backend="droid"))
    assert ei.value.llm_kind == llm.TRANSIENT


# --------------------------------------------------------------------------- switching, lock, logging


def test_runtime_switch_applies_to_the_next_call_and_persists(monkeypatch):
    cc, dr = FakeRunner(cc_out({"color": "red", "n": 1})), FakeRunner(droid_out('{"color": "red", "n": 1}'))
    c = llm.LLM(runners={"claude_code": cc, "droid": dr})
    assert llm.current().backend == "api" and llm.current().source == "default"
    before, after = llm.set_backend("claude_code", by="test")
    assert (before.backend, after.backend, after.source) == ("api", "claude_code", "runtime")
    assert call(c).meta["backend"] == "claude_code"
    llm.set_backend("droid", model="claude-sonnet-5", by="test")
    r = call(c)
    assert r.meta["backend"] == "droid" and dr.calls[0]["argv"][dr.calls[0]["argv"].index("-m") + 1] == "claude-sonnet-5"
    assert len(cc.calls) == 1 and len(dr.calls) == 1
    st = json.loads(llm.state_file().read_text())
    assert st["backend"] == "droid" and st["updated_by"] == "test"
    # a deliberate env change wins over the persisted choice
    monkeypatch.setenv("RDF_LLM_BACKEND", "claude_code")
    assert llm.current().backend == "claude_code" and llm.current().source == "env"


def test_unknown_backend_rejected():
    with pytest.raises(llm.LLMError):
        llm.set_backend("gpt")


@pytest.mark.parametrize("var,val", [("RDF_DEPLOY_ENV", "production"), ("FLY_APP_NAME", "rdf-orchestrator"),
                                     ("RDF_DEPLOY_ENV", "cloud")])
def test_production_lock(monkeypatch, var, val):
    llm.set_backend("claude_code")
    monkeypatch.setenv(var, val)
    sel = llm.current()
    assert sel.backend == "api" and sel.locked and sel.describe()["allowed"] == ["api"]
    with pytest.raises(llm.LLMLocked):
        llm.set_backend("droid")
    with pytest.raises(llm.LLMLocked):
        call(llm.LLM(runners={"droid": FakeRunner(droid_out("{}"))}, backend="droid"))
    monkeypatch.setenv("RDF_LLM_BACKEND", "claude_code")
    with pytest.raises(SystemExit):
        llm.startup_check("test")
    monkeypatch.setenv("RDF_LLM_BACKEND", "api")
    assert llm.startup_check("test").backend == "api"


def test_every_call_is_recorded_with_backend_and_latency():
    seen = []
    llm.add_call_sink(seen.append)
    try:
        call(llm.LLM(runners={"claude_code": FakeRunner(cc_out({"color": "red", "n": 1}))}, backend="claude_code"))
        with pytest.raises(llm.LLMError):
            call(llm.LLM(runners={"claude_code": FakeRunner(TimeoutError("x"))}, backend="claude_code"))
    finally:
        llm.remove_call_sink(seen.append)
    ok, bad = seen
    assert ok["backend"] == "claude_code" and ok["step"] == "test" and ok["ok"] and ok["input_tokens"] == 10
    assert isinstance(ok["latency_ms"], int) and ok["cost_estimate_usd"] == 0.0123
    assert not bad["ok"] and bad["reason"] == "timeout"


def test_api_backend_keeps_the_sdk_call_shape():
    from types import SimpleNamespace

    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(id="msg_1", model="claude-opus-5-5", stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
                               content=[SimpleNamespace(type="text", text='{"color": "red", "n": 1}')])

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    r = call(llm.as_llm(fake))
    kw = calls[0]
    assert kw["fallbacks"] == "default" and kw["betas"] == [llm.FALLBACK_BETA] and kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert r.parsed == {"color": "red", "n": 1} and r.meta["backend"] == "api" and r.id == "msg_1"
    assert r.usage["cost_estimate_usd"] == pytest.approx((1000 * 4 + 100 * 20) / 1e6)


def test_schema_errors_subset():
    s = {"type": "object", "required": ["a"], "properties": {"a": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                                                              "b": {"type": "array", "items": {"type": "string"}}}}
    assert llm.schema_errors({"a": None, "b": ["x"]}, s) == []
    assert llm.schema_errors({"a": "x"}, s) and llm.schema_errors({"b": [1]}, s) and llm.schema_errors({}, s)
