"""TDM-55: short-lived signed tokens for the robot camera stream.

The orchestrator mints a token for the contributor who currently holds a task's claim
(``POST /stream/token``, ``rdf_orchestrator.stream_api``). The robot-client's camera stream server
(``rdf_robot_client.stream``) checks it before sending a single frame and closes the socket when it
expires without a refresh. Both sides share one HMAC key, ``RDF_STREAM_TOKEN_SECRET`` (in ``.env`` on
the laptop and on the Jetson; never in code or logs).

Format (JWT-like, no external deps)::

    base64url(json payload) "." base64url(HMAC-SHA256(secret, "rdf-stream-v1." + payload_b64))

Payload keys: ``v`` (1), ``robot_id`` (SpacetimeDB robot id, 0 = unknown), ``robot_name``,
``task_id`` (0 for an ops token), ``sub`` (``contributor:<id>`` or ``ops:<who>``), ``cams``
(list of camera names, empty = all), ``iat`` / ``exp`` (unix seconds), ``jti``, and (TDM-57)
``claimed_at_us``: the claim generation the token was minted for (``task.claimed_at`` in micros, 0 =
not bound). ``(task_id, claimed_at_us)`` is the TDM-10 lease token; the robot's ``/control`` guard
(``rdf_robot_client.tdm57_lease``) refuses a token whose generation is no longer the live claim.

CLI (ops / testing; prints the token, which is short-lived)::

    python -m rdf_common.stream_token mint --robot-id 1 --ttl 600 --sub ops:jaxon
    python -m rdf_common.stream_token verify <token>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field, SecretStr

from .settings import RdfSettings

_DOMAIN = b"rdf-stream-v1."
DEFAULT_TTL_S = 60          # matches the 60 s arm-control lease; the browser refreshes at half-life
MAX_TTL_S = 24 * 3600


class StreamTokenSettings(RdfSettings):
    rdf_stream_token_secret: SecretStr = Field(min_length=16)


class StreamTokenError(ValueError):
    """Bad, tampered or expired token. ``code`` is safe to show to the client."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.code = code


@dataclass
class StreamClaims:
    robot_id: int
    robot_name: str
    task_id: int
    sub: str
    exp: float
    iat: float
    cams: list[str] = field(default_factory=list)
    jti: str = ""
    claimed_at_us: int = 0  # TDM-57: claim generation (task.claimed_at), 0 = not bound to one

    def allows(self, cam: str) -> bool:
        return not self.cams or cam in self.cams

    def to_payload(self) -> dict[str, Any]:
        return {"v": 1, "robot_id": self.robot_id, "robot_name": self.robot_name, "task_id": self.task_id,
                "sub": self.sub, "cams": self.cams, "iat": int(self.iat), "exp": int(self.exp), "jti": self.jti,
                "claimed_at_us": int(self.claimed_at_us)}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key(secret: str | bytes | SecretStr) -> bytes:
    if isinstance(secret, SecretStr):
        secret = secret.get_secret_value()
    key = secret.encode() if isinstance(secret, str) else secret
    if len(key) < 16:
        raise ValueError("RDF_STREAM_TOKEN_SECRET must be at least 16 bytes")
    return key


def load_secret() -> SecretStr:
    """``RDF_STREAM_TOKEN_SECRET`` from the env or repo-root ``.env`` (exits naming the key if missing)."""
    from .settings import load_settings

    return load_settings(StreamTokenSettings, "stream-token").rdf_stream_token_secret


def mint(secret: str | bytes | SecretStr, *, robot_id: int = 0, robot_name: str = "", task_id: int = 0,
         sub: str, ttl_s: float = DEFAULT_TTL_S, cams: list[str] | None = None,
         not_after: float | None = None, now: float | None = None,
         claimed_at_us: int = 0) -> tuple[str, StreamClaims]:
    """Sign a token valid for ``ttl_s`` (capped at ``not_after``, e.g. the claim's lease expiry)."""
    now = time.time() if now is None else now
    exp = now + min(max(1.0, ttl_s), MAX_TTL_S)
    if not_after is not None:
        exp = min(exp, not_after)
    if exp <= now:
        raise StreamTokenError("expired", "token would already be expired (lease over?)")
    claims = StreamClaims(robot_id=int(robot_id), robot_name=robot_name, task_id=int(task_id), sub=sub,
                          exp=exp, iat=now, cams=list(cams or []), jti=secrets.token_hex(8),
                          claimed_at_us=int(claimed_at_us or 0))
    body = _b64(json.dumps(claims.to_payload(), separators=(",", ":"), sort_keys=True).encode())
    sig = hmac.new(_key(secret), _DOMAIN + body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}", claims


def verify(secret: str | bytes | SecretStr, token: str, *, now: float | None = None,
           leeway_s: float = 2.0) -> StreamClaims:
    """Check signature and expiry; raises ``StreamTokenError`` (code ``malformed|bad_signature|expired``)."""
    if not isinstance(token, str) or token.count(".") != 1 or len(token) > 4096:
        raise StreamTokenError("malformed")
    body, sig = token.split(".")
    try:
        got = _unb64(sig)
    except (ValueError, TypeError):
        raise StreamTokenError("malformed") from None
    want = hmac.new(_key(secret), _DOMAIN + body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(got, want):
        raise StreamTokenError("bad_signature")
    try:
        p = json.loads(_unb64(body))
        claims = StreamClaims(robot_id=int(p.get("robot_id", 0)), robot_name=str(p.get("robot_name", "")),
                              task_id=int(p.get("task_id", 0)), sub=str(p["sub"]), exp=float(p["exp"]),
                              iat=float(p.get("iat", 0)), cams=[str(c) for c in p.get("cams", [])],
                              jti=str(p.get("jti", "")), claimed_at_us=int(p.get("claimed_at_us") or 0))
    except (ValueError, KeyError, TypeError):
        raise StreamTokenError("malformed") from None
    if p.get("v") != 1:
        raise StreamTokenError("malformed", "unknown token version")
    now = time.time() if now is None else now
    if now > claims.exp + leeway_s:
        raise StreamTokenError("expired")
    return claims


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rdf_common.stream_token", description="TDM-55 camera stream tokens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="mint an ops token (prints it; it is short-lived)")
    m.add_argument("--robot-id", type=int, default=0)
    m.add_argument("--robot-name", default="")
    m.add_argument("--task-id", type=int, default=0)
    m.add_argument("--sub", default="ops:cli")
    m.add_argument("--ttl", type=float, default=600)
    m.add_argument("--cams", default="", help="comma-separated, empty = all")
    v = sub.add_parser("verify", help="check a token and print its claims")
    v.add_argument("token")
    a = ap.parse_args(argv)
    secret = load_secret()
    if a.cmd == "mint":
        tok, _ = mint(secret, robot_id=a.robot_id, robot_name=a.robot_name, task_id=a.task_id, sub=a.sub,
                      ttl_s=a.ttl, cams=[c for c in a.cams.split(",") if c])
        print(tok)
        return 0
    try:
        print(json.dumps(verify(secret, a.token).to_payload(), indent=2))
        return 0
    except StreamTokenError as e:
        print(f"invalid: {e.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
