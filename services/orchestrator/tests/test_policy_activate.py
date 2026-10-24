"""Shared /policy/activate helper: the TDM-41 bearer token goes out on every caller, never into logs."""
from __future__ import annotations

import inspect
import json

import pytest

from rdf_orchestrator import policy_activate as pa

TOKEN = "tok-test-not-a-real-secret-123"


class Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text = status, text


@pytest.fixture
def posts(monkeypatch):
    import httpx

    seen: list[dict] = []
    answer = {"resp": Resp(200, "{}")}

    def fake_post(url, json=None, timeout=None, headers=None):
        seen.append({"url": url, "json": json, "timeout": timeout, "headers": dict(headers or {})})
        return answer["resp"]

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen, answer


def test_sends_bearer_token_from_env(monkeypatch, posts):
    seen, _ = posts
    monkeypatch.setenv("POLICY_SERVER_TOKEN", TOKEN)
    pa.http_activate("https://policy.test/policy/activate", {"checkpoint_uri": "modal-vol://ck/1"}, 5.0)
    assert seen == [{"url": "https://policy.test/policy/activate", "json": {"checkpoint_uri": "modal-vol://ck/1"},
                     "timeout": 5.0, "headers": {"Authorization": f"Bearer {TOKEN}"}}]


def test_token_falls_back_to_dotenv(monkeypatch, tmp_path):
    from rdf_common import settings

    env = tmp_path / ".env"
    env.write_text(f"POLICY_SERVER_TOKEN={TOKEN}\n")
    monkeypatch.delenv("POLICY_SERVER_TOKEN", raising=False)
    monkeypatch.setitem(settings.RdfSettings.model_config, "env_file", env)
    assert pa.policy_server_token() == TOKEN
    env.write_text("OTHER=1\n")
    assert pa.policy_server_token() == "" and pa.auth_headers() == {}


def test_401_names_the_key_and_never_echoes_the_token(monkeypatch, posts):
    seen, answer = posts
    monkeypatch.setenv("POLICY_SERVER_TOKEN", TOKEN)
    answer["resp"] = Resp(401, f"bad token {TOKEN}")
    with pytest.raises(RuntimeError) as ei:
        pa.http_activate("https://policy.test/policy/activate", {}, 1.0)
    msg = str(ei.value)
    assert "HTTP 401" in msg and "POLICY_SERVER_TOKEN rejected" in msg and TOKEN not in msg
    monkeypatch.setattr(pa, "policy_server_token", lambda: "")
    with pytest.raises(RuntimeError, match="POLICY_SERVER_TOKEN not set"):
        pa.http_activate("https://policy.test/policy/activate", {}, 1.0)
    assert seen[-1]["headers"] == {}


def test_eval_and_ship_both_use_the_shared_helper(monkeypatch, posts):
    """evaluate.py (TDM-67) used to POST without Authorization; both callers now go through one helper."""
    from rdf_orchestrator import evaluate, flywheel

    seen, _ = posts
    monkeypatch.setenv("POLICY_SERVER_TOKEN", TOKEN)
    assert inspect.signature(evaluate.EvalOrchestrator).parameters["activate"].default is pa.http_activate
    evaluate._http_activate("http://policy:9000/policy/activate", {"kind": "baseline"}, 3.0)
    flywheel.ShipStep().activate("http://policy:9000/policy/activate", {"kind": "deployed"}, 3.0)
    assert [s["headers"] for s in seen] == [{"Authorization": f"Bearer {TOKEN}"}] * 2


def test_eval_failure_event_has_no_token(monkeypatch, posts):
    """The eval orchestrator's activate-failed event carries the error text, never the secret."""
    from rdf_orchestrator import evaluate

    _, answer = posts
    monkeypatch.setenv("POLICY_SERVER_TOKEN", TOKEN)
    answer["resp"] = Resp(403, f"forbidden for {TOKEN}")
    events: list[tuple[str, dict]] = []
    e = evaluate.EvalOrchestrator.__new__(evaluate.EvalOrchestrator)
    e.cfg = evaluate.EvalConfig(policy_server_url="http://policy:9000")
    e.activate = evaluate._http_activate
    e.event = lambda t, p: events.append((t, p))
    base = {"ticket_id": 1, "robot_id": 1, "kind": "baseline", "checkpoint_uri": "gs://x"}
    assert e._activate_policy(base) is False
    assert events[0][0] == "eval.policy_activate_failed"
    assert TOKEN not in json.dumps(events) and "HTTP 403" in events[0][1]["error"]
