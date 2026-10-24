"""``POST {POLICY_SERVER_URL}/policy/activate`` on the TDM-41 policy server, with its bearer auth.

The one HTTP activator for every caller: the eval orchestrator (TDM-67 ``evaluate.py``, before a
baseline/candidate eval) and the ship step (TDM-70 ``flywheel.ShipStep``, after a deploy). The
server answers 401 without ``Authorization: Bearer $POLICY_SERVER_TOKEN``.

The token comes from the process env, else the repo-root ``.env`` (``rdf_common.settings``). It is
never printed, logged or put in an error message. No token: the request goes out without the
header (a dev server without auth still works) and a 401/403 error says which key to set.
"""
from __future__ import annotations

import os

TOKEN_ENV = "POLICY_SERVER_TOKEN"


def policy_server_token() -> str:
    """POLICY_SERVER_TOKEN from the env, else from the repo .env; "" when unset."""
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if tok:
        return tok
    try:
        from pydantic import SecretStr
        from rdf_common.settings import RdfSettings

        class _PolicyServerSettings(RdfSettings):
            policy_server_token: SecretStr | None = None

        s = _PolicyServerSettings().policy_server_token
        return s.get_secret_value().strip() if s else ""
    except Exception:  # noqa: BLE001 - unreadable .env: behave as unset
        return ""


def auth_headers(token: str | None = None) -> dict[str, str]:
    tok = policy_server_token() if token is None else token.strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def http_activate(url: str, body: dict, timeout: float) -> None:
    """POST ``body`` to ``url`` (the full ``.../policy/activate`` URL). Raises on a non-2xx answer."""
    import httpx

    tok = policy_server_token()
    r = httpx.post(url, json=body, timeout=timeout, headers=auth_headers(tok))
    if r.status_code >= 300:
        text = r.text[:300]
        if tok:
            text = text.replace(tok, "***")  # never echo the secret back into event_log
        hint = ""
        if r.status_code in (401, 403):
            hint = f" ({TOKEN_ENV} {'rejected' if tok else 'not set'})"
        raise RuntimeError(f"HTTP {r.status_code}{hint}: {text}")
