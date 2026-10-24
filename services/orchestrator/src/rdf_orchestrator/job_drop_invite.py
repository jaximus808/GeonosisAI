"""TDM-108 per-task invites: a job-drop post grants access to ITS task only, not the whole site.

Every drop link carries a signed invite, ``/j/<task>/<channel>?k=<k>``, and the card can print a short
typeable code for the same (task, channel). The web app's ``/j`` route redeems either one (and checks the
task is still open or claimed) for a scoped access cookie ``rdf_task`` that both gates (Vercel
``apps/web/src/proxy.ts`` + ``lib/tdm110/access.ts``, and the laptop edge ``scripts/demo/public_guard.py``)
accept for that task's pages and APIs only. Everything else still needs ``RDF_PUBLIC_ACCESS_CODE`` or an
operator login.

Formats (byte-for-byte the same in ``apps/web/src/lib/tdm108/invite.ts`` and ``public_guard.py``; the test
vectors in the three test files pin them):

    secret  RDF_DROP_INVITE_SECRET, else RDF_EDGE_SECRET (already shared by Vercel and this laptop)
    key     HMAC-SHA256(secret, "rdf-drop-invite-v1")
    mac     HMAC-SHA256(key, "invite|<task>|<channel>")
    k       base64url(mac) without padding, first 22 characters (132 bits)
    code    6 characters: CODE_ALPHABET[mac[16 + i] % 31] for i in 0..5 (no 0/O, 1/I/L)
    cookie  rdf_task = "t1.<ids>.<ticket>.<robot>.<exp>.<sig>", ids = task ids joined by "-",
            sig = base64url(HMAC-SHA256(key, "task-access|t1.<ids>.<ticket>.<robot>.<exp>"))[:32], 24 h
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

LABEL = b"rdf-drop-invite-v1"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
TASK_COOKIE = "rdf_task"
TASK_TTL_S = 24 * 3600


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def invite_key(secret: str) -> bytes:
    return hmac.new(secret.encode(), LABEL, hashlib.sha256).digest()


def _mac(secret: str, task_id: int, channel: str) -> bytes:
    return hmac.new(invite_key(secret), f"invite|{int(task_id)}|{channel}".encode(), hashlib.sha256).digest()


def invite_k(secret: str, task_id: int, channel: str) -> str:
    return _b64(_mac(secret, task_id, channel))[:22]


def invite_code(secret: str, task_id: int, channel: str) -> str:
    m = _mac(secret, task_id, channel)
    return "".join(CODE_ALPHABET[m[16 + i] % len(CODE_ALPHABET)] for i in range(6))


def check_invite(secret: str, task_id: int, channel: str, *, k: str | None = None, code: str | None = None) -> bool:
    if not secret:
        return False
    if k and hmac.compare_digest(k.encode(), invite_k(secret, task_id, channel).encode()):
        return True
    if code:
        cand = "".join(code.split()).replace("-", "").upper()
        return hmac.compare_digest(cand.encode(), invite_code(secret, task_id, channel).encode())
    return False


def mint_task_cookie(secret: str, ids: list[int], ticket: int, robot: int, now: float | None = None,
                     ttl_s: int = TASK_TTL_S) -> str:
    exp = int((time.time() if now is None else now) + ttl_s)
    body = f"t1.{'-'.join(str(int(i)) for i in ids)}.{int(ticket)}.{int(robot)}.{exp}"
    sig = _b64(hmac.new(invite_key(secret), f"task-access|{body}".encode(), hashlib.sha256).digest())[:32]
    return f"{body}.{sig}"


def verify_task_cookie(secret: str, value: str | None, now: float | None = None) -> dict | None:
    """{ids, ticket, robot, exp} for a valid cookie, else None."""
    if not secret or not value:
        return None
    parts = value.split(".")
    if len(parts) != 6 or parts[0] != "t1":
        return None
    try:
        ids = [int(x) for x in parts[1].split("-")]
        ticket, robot, exp = int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None
    now = time.time() if now is None else now
    if not ids or len(ids) > 32 or exp <= now or exp > now + TASK_TTL_S + 60:
        return None
    want = mint_task_cookie(secret, ids, ticket, robot, now=exp, ttl_s=0)
    if not hmac.compare_digest(want.encode(), value.encode()):
        return None
    return {"ids": ids, "ticket": ticket, "robot": robot, "exp": exp}
