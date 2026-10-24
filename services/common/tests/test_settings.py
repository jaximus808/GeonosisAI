"""Tests for the fail-fast loaders. Fake env only; the real .env is never read."""

import pytest

from rdf_common.settings import (
    OrchestratorSettings,
    PaymentsSettings,
    RobotClientSettings,
    load_settings,
)

ALL_KEYS = [
    "ANTHROPIC_API_KEY", "FACTORY_API_KEY", "HF_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "SPACETIME_HOST", "SPACETIME_DB_NAME",
    "SPACETIME_TOKEN", "ORCHESTRATOR_URL", "ROBOT_ID", "JETSON_WS_PORT", "WANDB_API_KEY",
]

SPACETIME = {
    "SPACETIME_HOST": "http://fake-host:3000",
    "SPACETIME_DB_NAME": "fake-db",
    "SPACETIME_TOKEN": "fake-token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


def set_env(monkeypatch, values):
    for k, v in values.items():
        monkeypatch.setenv(k, v)


def test_loads_when_all_required_present(monkeypatch):
    set_env(monkeypatch, {**SPACETIME, "ANTHROPIC_API_KEY": "fake-anthropic"})
    s = load_settings(OrchestratorSettings, "orchestrator", env_file=None)
    assert s.spacetime_db_name == "fake-db"
    assert s.anthropic_api_key.get_secret_value() == "fake-anthropic"
    assert s.orchestrator_url == "http://localhost:8000"
    assert s.modal_token_id is None


def test_missing_var_exits_nonzero_and_names_it(monkeypatch, capsys):
    set_env(monkeypatch, SPACETIME)
    with pytest.raises(SystemExit) as exc:
        load_settings(OrchestratorSettings, "orchestrator", env_file=None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY: missing (required)" in err
    assert "SPACETIME_HOST" not in err


def test_blank_value_counts_as_missing(monkeypatch, capsys):
    set_env(monkeypatch, {**SPACETIME, "ANTHROPIC_API_KEY": "", "SPACETIME_TOKEN": ""})
    with pytest.raises(SystemExit):
        load_settings(OrchestratorSettings, "orchestrator", env_file=None)
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err and "SPACETIME_TOKEN" in err


def test_invalid_value_named_without_echoing_it(monkeypatch, capsys):
    set_env(monkeypatch, {**SPACETIME, "JETSON_WS_PORT": "not-a-port-xyz"})
    with pytest.raises(SystemExit):
        load_settings(RobotClientSettings, "robot-client", env_file=None)
    err = capsys.readouterr().err
    assert "JETSON_WS_PORT" in err
    assert "not-a-port-xyz" not in err


def test_stripe_live_key_rejected_without_echo(monkeypatch, capsys):
    set_env(monkeypatch, {"STRIPE_SECRET_KEY": "sk_live_fakefakefake"})
    with pytest.raises(SystemExit):
        load_settings(PaymentsSettings, "payments", env_file=None)
    err = capsys.readouterr().err
    assert "STRIPE_SECRET_KEY" in err
    assert "sk_live_fakefakefake" not in err


def test_env_file_is_used_and_env_wins(monkeypatch, tmp_path):
    fake = tmp_path / ".env"
    fake.write_text(
        "SPACETIME_HOST=http://from-file\nSPACETIME_DB_NAME=file-db\n"
        "SPACETIME_TOKEN=file-token\nROBOT_ID=file-robot\n"
    )
    monkeypatch.setenv("ROBOT_ID", "env-robot")
    s = load_settings(RobotClientSettings, "robot-client", env_file=fake)
    assert s.spacetime_host == "http://from-file"
    assert s.robot_id == "env-robot"
    assert s.jetson_ws_port == 8765
