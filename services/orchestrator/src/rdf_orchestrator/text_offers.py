"""TDM-113 text job offers: opted-in contributors get paid tasks by iMessage (Inkbox), and the first "YES" wins.

Flow (hooked in next to TDM-65's web push and TDM-108's job drops)::

    /join -> "Get job texts" (phone + consent + "in person near the robot")
        -> POST /texts/optin (browser proves its SpacetimeDB identity) -> TextOffers.opt_in -> welcome text
    dispatcher pages new paid tasks -> page listener -> TextOffers.on_page
        -> up to RDF_TEXT_OFFER_FANOUT (3) opted-in contributors, least recently offered first, skipping anyone
           rate-limited (1 offer / 2 min, 20 / day per number), busy (holds a claimed task) or, for resets, not in person
        -> "$3.50 robot job, about 2 min: recovery takeover on yam sim. Reply YES to claim."
    Inkbox webhook (imessage.received / text.received) -> in-process Inkbox tunnel -> handle_webhook
        -> signature + 300 s timestamp window -> TextOffers.on_inbound
        -> YES: claim_task(task, contributor) as the orchestrator (atomic in SpacetimeDB; first valid YES wins)
           -> reply with the task's TDM-108 per-task invite, <web>/j/<task>/text?k=..., which redeems the scoped
              cookie and lands on /teleop/<task> (or /reset/<task>). Later YESes: "Taken, we'll text you the next one."
        -> STOP / START / HELP are handled here whether or not Inkbox handles them.

Real-arm reset asks (``RDF_RESET_TEXT_CONTRIBUTOR``, reset_done.py): a reset task on a REAL robot is also texted to
that contributor (the operator) as "Reset the scene for TKT-0042 after trial 2/3: put the cube back, then reply
DONE or tap <link>", whatever their in_person flag and the offer rate limit. DONE claims + completes the reset
(the dispatcher then pays it and resumes the robot); YES still claims it and sends the /reset/<task> link.

Lease: arm-control claims last 60 s (TDM-10). A text claim is made before the phone opens the link, so the
orchestrator heartbeats it for up to RDF_TEXT_HOLD_S (180 s) while the claimant opens the page; the teleop page
then heartbeats it itself (useTdm61ClaimHeartbeat).

SAFETY
  * ``RDF_TEXT_MODE=dry_run`` (default): every would-be text is written to the outbox log
    (``~/.local/state/rdf/tdm113_outbox.jsonl``, numbers masked to the last 4 digits) and shown on /ops/drops.
    Nothing is sent. ``live`` sends through Inkbox. The switch is the user's: no code path turns ``live`` on.
  * A number is texted only after it opted in at /join with the consent text (``CONSENT_TEXT``), and never after
    STOP (except the one STOP confirmation). Inbound texts from unknown numbers get no reply.
  * Phone numbers live only in a private sqlite file on this host (``RDF_TEXT_STORE``, default
    ``~/.local/state/rdf/tdm113_texts.sqlite3``, mode 0600). Events in the PUBLIC event_log carry contributor
    ids and a keyed hash, never a number. The Inkbox signing key is in ``tdm113_inkbox.json`` (0600).

Inkbox: Python SDK ``inkbox==0.7.19`` (the same release the user's JS projects pin). One identity
(``RDF_TEXT_HANDLE``, default ``geonosis-jobs``) with iMessage on and the org's dedicated iMessage line attached
when one is free (else the shared router: the person texts ``connect @<handle>`` to it first). SMS is used only
when the identity has a phone number and no iMessage line.

CLI (prints no secrets, sends nothing)::

    uv run python -m rdf_orchestrator.text_offers status
    uv run python -m rdf_orchestrator.text_offers setup        # create/attach the identity, line, webhook (idempotent)
    uv run python -m rdf_orchestrator.text_offers sign-test    # POST a signed test webhook through the tunnel URL
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import SecretStr

from rdf_common.settings import RdfSettings

from .log import get_logger

log = get_logger("text_offers")

ACTOR = "orchestrator:text_offers"
EV_OPT_IN, EV_OPT_OUT, EV_OFFER, EV_CLAIMED, EV_STOP, EV_START, EV_SKIPPED = (
    "text.opt_in", "text.opt_out", "text.offer_sent", "text.claimed", "text.stop", "text.start", "text.skipped")
SRC = "text"  # the TDM-108 attribution channel (/j/<task>/text, job_drop.claimed {src: text})
STATE_DIR = Path.home() / ".local" / "state" / "rdf"
DEFAULT_STORE = STATE_DIR / "tdm113_texts.sqlite3"
DEFAULT_OUTBOX = STATE_DIR / "tdm113_outbox.jsonl"
DEFAULT_INKBOX_STATE = STATE_DIR / "tdm113_inkbox.json"
WEBHOOK_PATH = "/inkbox/webhook"
INBOUND_EVENTS = ("imessage.received", "text.received")
SIGNATURE_WINDOW_S = 300
ARM_CONTROL = ("clean_teleop", "recovery_takeover")
TYPES = ("clean_teleop", "recovery_takeover", "reset")
KIND = {"clean_teleop": "clean demo", "recovery_takeover": "recovery takeover", "reset": "reset"}

CONSENT_TEXT = "GeonosisAI will text you paid robot jobs. Msg frequency varies. Reply STOP to stop."
WELCOME = "You're in. GeonosisAI will text you robot jobs. Reply STOP anytime."
HELP = "GeonosisAI robot jobs: we text you paid tasks, reply YES to claim one. Reply STOP to stop."
STOP_CONFIRM = "You're unsubscribed from GeonosisAI job texts. Reply START to get them again."
TAKEN = "Taken, we'll text you the next one."
NO_OFFER = "No open job right now. We'll text you the next one."
NUDGE = "Reply YES to claim the job, or STOP to stop job texts."
OFFER_FMT = "{price} robot job, about {mins} min: {kind} on {robot}. Reply YES to claim."
REPEAT_CLAIMED_TEXT_S = 30.0  # a YES delivered twice within this window gets one "It's yours" text, not two
CLAIMED_FMT = "It's yours: {kind}, task #{task}. Open {link} in the next 3 minutes to start."
LOGIN_FMT = "Your GeonosisAI sign-in code is {code}. It expires in 10 minutes. Didn't ask for it? Ignore this text."
LOGIN_SENT = "If that number gets GeonosisAI job texts, a 6-digit code is on its way."
EV_RELEASED, EV_TAKEOVER, EV_LOGIN = "text.released", "text.takeover", "text.login"

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
START_WORDS = {"START", "UNSTOP", "RESUME"}
HELP_WORDS = {"HELP", "INFO"}
YES_WORDS = {"YES", "Y", "YEA", "YEAH", "YEP", "YUP", "CLAIM", "OK", "SURE"}
DONE_WORDS = {"DONE", "RESET", "FINISHED", "READY"}
RESET_ASK_FMT = "Reset the scene for {ticket}{trial}: put the cube back, then reply DONE or tap {link}"
RESET_THANKS = "Thanks, scene reset. The robot carries on."
RESET_NONE = "No reset is waiting on you right now."
EV_RESET_ASK, EV_RESET_DONE = "text.reset_ask", "text.reset_done"


class TextSettings(RdfSettings):
    rdf_text_mode: Literal["dry_run", "live"] = "dry_run"
    rdf_text_offer_fanout: int = 3
    rdf_text_store: str | None = None
    rdf_text_outbox: str | None = None
    rdf_text_inkbox_state: str | None = None
    rdf_text_handle: str = "geonosis-jobs"
    rdf_text_rate_s: float = 120.0  # at most one offer per number per 2 min
    rdf_text_daily_max: int = 20  # and 20 per day
    rdf_text_offer_ttl_s: float = 900.0  # an unanswered offer stops counting after 15 min
    rdf_text_hold_s: float = 180.0  # heartbeat a text claim this long while the phone opens the link
    rdf_text_login_ttl_s: float = 600.0  # phone sign-in codes expire after 10 min
    rdf_text_login_gap_s: float = 30.0  # one code per number per 30 s
    rdf_text_login_hourly: int = 5  # and 5 per number per hour
    rdf_text_login_global_10m: int = 30  # and 30 for everyone per 10 min
    rdf_text_login_attempts: int = 5  # wrong guesses before a code is burned
    rdf_reset_text_contributor: int = 0  # >0: real-robot resets are texted to this contributor (the operator)
    rdf_text_webhook_secret: SecretStr | None = None  # override the stored Inkbox signing key (tests / sign-test)
    inkbox_api_key: SecretStr | None = None
    inkbox_base_url: str | None = None


def normalize_phone(raw: str, default_cc: str = "1") -> str | None:
    """E.164 or None. "(415) 555-0123" -> "+14155550123"; "+44 20 7946 0958" -> "+442079460958"."""
    s = (raw or "").strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if not plus:
        if len(digits) == 10:
            digits = default_cc + digits
        elif not (len(digits) == 11 and digits.startswith("1")):
            return None
    if not (8 <= len(digits) <= 15) or digits.startswith("0"):
        return None
    if digits.startswith("1") and len(digits) != 11:
        return None
    return "+" + digits


def last4(phone: str | None) -> str:
    return phone[-4:] if phone else ""


def mask(phone: str | None) -> str:
    return f"***{last4(phone)}" if phone else "-"


def fmt_money(cents: int) -> str:
    return f"${int(cents) // 100}.{int(cents) % 100:02d}"


def keyword(text: str | None) -> str:
    first = (text or "").strip().split(None, 1)
    return re.sub(r"[^A-Z]", "", first[0].upper()) if first else ""


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- store

@dataclass
class OptIn:
    phone: str
    contributor_id: int
    in_person: bool
    status: str  # opted_in | stopped
    consent_at: float
    updated_at: float


@dataclass
class Offer:
    id: int
    task_ids: list[int]
    phone: str
    contributor_id: int
    task_type: str
    ticket_id: int
    sent_at: float
    state: str  # sent | claimed | lost | expired
    claimed_task: int = 0


def _private_file(path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.touch(mode=0o600, exist_ok=True)
    os.chmod(p, 0o600)


class TextStore:
    """Private sqlite store: opt-ins (phone numbers), offers, sends (rate limits), seen webhook ids."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            _private_file(self.path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS optin (phone TEXT PRIMARY KEY, contributor_id INTEGER NOT NULL,
                in_person INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, consent_text TEXT NOT NULL,
                consent_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS optin_contrib ON optin(contributor_id);
            CREATE TABLE IF NOT EXISTS offer (id INTEGER PRIMARY KEY AUTOINCREMENT, task_ids TEXT NOT NULL,
                phone TEXT NOT NULL, contributor_id INTEGER NOT NULL, task_type TEXT NOT NULL, ticket_id INTEGER NOT NULL,
                sent_at REAL NOT NULL, state TEXT NOT NULL, claimed_task INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS offer_phone ON offer(phone, sent_at);
            CREATE TABLE IF NOT EXISTS sends (phone TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sends_phone ON sends(phone, at);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS login_code (phone TEXT PRIMARY KEY, code_hash TEXT NOT NULL,
                expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS device (identity TEXT PRIMARY KEY, contributor_id INTEGER NOT NULL,
                token TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS device_contrib ON device(contributor_id, updated_at);
            CREATE TABLE IF NOT EXISTS merged (from_id INTEGER PRIMARY KEY, into_id INTEGER NOT NULL, at REAL NOT NULL);
        """)
        self._db.commit()

    def _q(self, sql: str, args: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cur

    # opt-ins
    def upsert_optin(self, phone: str, contributor_id: int, in_person: bool, consent_text: str, now: float) -> None:
        """One number per contributor and one contributor per number: re-opting from another account moves it."""
        with self._lock:
            self._db.execute("DELETE FROM optin WHERE contributor_id=? AND phone<>?", (contributor_id, phone))
            self._db.execute(
                """INSERT INTO optin(phone, contributor_id, in_person, status, consent_text, consent_at, updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET contributor_id=excluded.contributor_id,
                   in_person=excluded.in_person, status='opted_in', consent_text=excluded.consent_text,
                   consent_at=excluded.consent_at, updated_at=excluded.updated_at""",
                (phone, contributor_id, int(bool(in_person)), "opted_in", consent_text, now, now))
            self._db.commit()

    def set_status(self, phone: str, status: str, now: float) -> None:
        self._x("UPDATE optin SET status=?, updated_at=? WHERE phone=?", (status, now, phone))

    def _optins(self, where: str = "", args: Iterable[Any] = ()) -> list[OptIn]:
        rows = self._q("SELECT phone, contributor_id, in_person, status, consent_at, updated_at FROM optin " + where, args)
        return [OptIn(r[0], int(r[1]), bool(r[2]), r[3], float(r[4]), float(r[5])) for r in rows]

    def optin(self, phone: str) -> OptIn | None:
        r = self._optins("WHERE phone=?", (phone,))
        return r[0] if r else None

    def optin_for(self, contributor_id: int) -> OptIn | None:
        r = self._optins("WHERE contributor_id=?", (contributor_id,))
        return r[0] if r else None

    def opted_in(self) -> list[OptIn]:
        return self._optins("WHERE status='opted_in'")

    # sends + offers
    def record_send(self, phone: str, kind: str, now: float) -> None:
        self._x("INSERT INTO sends(phone, at, kind) VALUES (?,?,?)", (phone, now, kind))

    def offers_since(self, phone: str, since: float) -> int:
        return int(self._q("SELECT COUNT(*) FROM offer WHERE phone=? AND sent_at>=?", (phone, since))[0][0])

    def last_offer_at(self, phone: str) -> float:
        r = self._q("SELECT MAX(sent_at) FROM offer WHERE phone=?", (phone,))[0][0]
        return float(r or 0.0)

    def add_offer(self, task_ids: list[int], phone: str, contributor_id: int, task_type: str, ticket_id: int,
                  now: float) -> int:
        return int(self._x("INSERT INTO offer(task_ids, phone, contributor_id, task_type, ticket_id, sent_at, state) "
                           "VALUES (?,?,?,?,?,?,'sent')",
                           (json.dumps(task_ids), phone, contributor_id, task_type, ticket_id, now)).lastrowid)

    def _offers(self, where: str, args: Iterable[Any] = ()) -> list[Offer]:
        rows = self._q("SELECT id, task_ids, phone, contributor_id, task_type, ticket_id, sent_at, state, claimed_task "
                       "FROM offer " + where, args)
        return [Offer(int(r[0]), json.loads(r[1]), r[2], int(r[3]), r[4], int(r[5]), float(r[6]), r[7], int(r[8]))
                for r in rows]

    def latest_offers(self, phone: str, since: float) -> list[Offer]:
        return self._offers("WHERE phone=? AND sent_at>=? ORDER BY sent_at DESC, id DESC", (phone, since))

    def open_offers_for_task(self, task_id: int) -> list[Offer]:
        return [o for o in self._offers("WHERE state='sent'") if task_id in o.task_ids]

    def offered_task_ids(self) -> set[int]:
        out: set[int] = set()
        for (ids,) in self._q("SELECT task_ids FROM offer"):
            out.update(int(t) for t in json.loads(ids))
        return out

    def set_offer(self, offer_id: int, state: str, claimed_task: int = 0) -> None:
        self._x("UPDATE offer SET state=?, claimed_task=? WHERE id=?", (state, claimed_task, offer_id))

    def expire_for_phone(self, phone: str) -> None:
        self._x("UPDATE offer SET state='expired' WHERE phone=? AND state='sent'", (phone,))

    # webhook replay protection
    def seen(self, event_id: str, now: float) -> bool:
        """True when this webhook event id was handled before (records it otherwise)."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO seen(id, at) VALUES (?,?)", (event_id, now))
                self._db.execute("DELETE FROM seen WHERE at<?", (now - 7 * 86400,))
                self._db.commit()
                return False
            except sqlite3.IntegrityError:
                return True

    # phone login (6-digit codes; only the keyed hash is stored)
    def put_code(self, phone: str, code_hash: str, expires_at: float, now: float) -> None:
        self._x("INSERT INTO login_code(phone, code_hash, expires_at, attempts, created_at) VALUES (?,?,?,0,?) "
                "ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at, "
                "attempts=0, created_at=excluded.created_at", (phone, code_hash, expires_at, now))

    def get_code(self, phone: str) -> tuple[str, float, int] | None:
        r = self._q("SELECT code_hash, expires_at, attempts FROM login_code WHERE phone=?", (phone,))
        return (r[0][0], float(r[0][1]), int(r[0][2])) if r else None

    def bump_code(self, phone: str) -> None:
        self._x("UPDATE login_code SET attempts=attempts+1 WHERE phone=?", (phone,))

    def drop_code(self, phone: str) -> None:
        self._x("DELETE FROM login_code WHERE phone=?", (phone,))

    def sends_since(self, kind: str, since: float, phone: str | None = None) -> int:
        if phone is None:
            return int(self._q("SELECT COUNT(*) FROM sends WHERE kind=? AND at>=?", (kind, since))[0][0])
        return int(self._q("SELECT COUNT(*) FROM sends WHERE kind=? AND at>=? AND phone=?", (kind, since, phone))[0][0])

    # devices: a browser identity's SpacetimeDB token, seen on a verified /texts/* call. Phone login hands
    # the token of the contributor's device to the new browser so laptop and phone are the same identity.
    def remember_device(self, identity: str, contributor_id: int, token: str, now: float) -> None:
        self._x("INSERT INTO device(identity, contributor_id, token, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(identity) DO UPDATE SET contributor_id=excluded.contributor_id, token=excluded.token, "
                "updated_at=excluded.updated_at", (identity, contributor_id, token, now))

    def devices_for(self, contributor_id: int) -> list[tuple[str, str]]:
        """[(identity, token)] newest first."""
        return [(r[0], r[1]) for r in self._q("SELECT identity, token FROM device WHERE contributor_id=? "
                                              "ORDER BY updated_at DESC", (contributor_id,))]

    def forget_device(self, identity: str) -> None:
        self._x("DELETE FROM device WHERE identity=?", (identity,))

    # merged accounts (scripts/demo/merge_contributors.py): from_id is the same person as into_id
    def canonical(self, contributor_id: int) -> int:
        cid, seen = int(contributor_id), set()
        while cid not in seen:
            seen.add(cid)
            r = self._q("SELECT into_id FROM merged WHERE from_id=?", (cid,))
            if not r:
                return cid
            cid = int(r[0][0])
        return cid

    def merged_from(self, into_id: int) -> list[int]:
        return [int(r[0]) for r in self._q("SELECT from_id FROM merged WHERE into_id=?", (into_id,))]

    def merge(self, from_id: int, into_id: int, now: float, *, move_devices: bool) -> dict:
        """Repoint from_id's opt-in, offers and devices to into_id and record the merge. Idempotent."""
        with self._lock:
            db = self._db
            out = {"optin": 0, "offers": 0, "devices_moved": 0, "devices_dropped": 0}
            src, dst = self.optin_for(from_id), self.optin_for(into_id)
            if src is not None:
                if dst is None or dst.phone == src.phone:
                    out["optin"] = db.execute("UPDATE optin SET contributor_id=?, updated_at=? WHERE contributor_id=?",
                                              (into_id, now, from_id)).rowcount
                else:  # the survivor keeps its own number; the merged account's number stops getting texts
                    db.execute("DELETE FROM optin WHERE contributor_id=?", (from_id,))
            out["offers"] = db.execute("UPDATE offer SET contributor_id=? WHERE contributor_id=?",
                                       (into_id, from_id)).rowcount
            if move_devices:
                out["devices_moved"] = db.execute("UPDATE device SET contributor_id=? WHERE contributor_id=?",
                                                  (into_id, from_id)).rowcount
            else:
                out["devices_dropped"] = db.execute("DELETE FROM device WHERE contributor_id=?", (from_id,)).rowcount
            db.execute("INSERT INTO merged(from_id, into_id, at) VALUES (?,?,?) ON CONFLICT(from_id) DO UPDATE SET "
                       "into_id=excluded.into_id, at=excluded.at", (from_id, into_id, now))
            db.commit()
            return out

    def stats(self, now: float) -> dict:
        n_in, n_person = self._q("SELECT COUNT(*), COALESCE(SUM(in_person),0) FROM optin WHERE status='opted_in'")[0]
        n_stop = self._q("SELECT COUNT(*) FROM optin WHERE status='stopped'")[0][0]
        offers, claimed = self._q("SELECT COUNT(*), COALESCE(SUM(state='claimed'),0) FROM offer WHERE sent_at>=?",
                                  (now - 86400,))[0]
        return {"opted_in": int(n_in), "in_person": int(n_person), "stopped": int(n_stop),
                "offers_24h": int(offers), "claims_24h": int(claimed)}


# --------------------------------------------------------------------------- transports

@dataclass
class SendResult:
    state: str  # dry_run | sent | text_first | blocked | rate_limited | error
    detail: str = ""
    channel: str = ""  # imessage | sms | outbox

    @property
    def ok(self) -> bool:
        return self.state in ("dry_run", "sent")


Transport = Callable[[str, str], SendResult]  # (E.164 phone, text) -> result


class Outbox:
    """Every text (dry run: would-be; live: sent) as a jsonl line with the number masked. Read by /ops/drops."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = str(path) if path else None
        self._lock = threading.Lock()
        self.recent: list[dict] = []
        if self.path:
            _private_file(self.path)

    def write(self, phone: str, text: str, kind: str, mode: str, result: SendResult, now: float) -> dict:
        row = {"at": round(now, 3), "to": mask(phone), "kind": kind, "mode": mode, "state": result.state,
               "channel": result.channel, "text": text}
        if result.detail:
            row["detail"] = result.detail[:200]
        with self._lock:
            self.recent = (self.recent + [row])[-50:]
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        return row

    def tail(self, n: int = 20) -> list[dict]:
        if self.path and not self.recent:
            try:
                lines = Path(self.path).read_text(encoding="utf-8").splitlines()[-n:]
                return [json.loads(x) for x in lines if x.strip()]
            except (OSError, ValueError):
                return []
        return self.recent[-n:]


def dry_run_transport(phone: str, text: str) -> SendResult:
    return SendResult("dry_run", channel="outbox")


class InkboxLink:
    """The Inkbox side: one identity, its iMessage line (or SMS number), webhook subscription and tunnel."""

    def __init__(self, s: TextSettings, *, client: Any = None, state_path: str | Path | None = None) -> None:
        self.s = s
        self._client = client
        self.state_path = Path(state_path or s.rdf_text_inkbox_state or DEFAULT_INKBOX_STATE)
        self._listener: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.listener_status = "off"

    # ---- state (contains the webhook signing key: never print it)
    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict) -> None:
        _private_file(self.state_path)
        self.state_path.write_text(json.dumps(st, indent=1))
        os.chmod(self.state_path, 0o600)

    def summary(self) -> dict:
        st = self.state()
        return {"configured": bool(st.get("identity_id")), "handle": st.get("handle"),
                "channel": st.get("channel"), "line_last4": last4(st.get("line")),
                "line": st.get("line"),  # the number people text: public by design
                "router": st.get("router"), "connect_command": st.get("connect_command"),
                "webhook_url": st.get("webhook_url"), "signing_key": bool(st.get("signing_key")),
                "listener": self.listener_status}

    def client(self) -> Any:
        if self._client is None:
            from inkbox import Inkbox

            if not self.s.inkbox_api_key:
                raise RuntimeError("INKBOX_API_KEY is not set")
            kw = {"api_key": self.s.inkbox_api_key.get_secret_value()}
            if self.s.inkbox_base_url:
                kw["base_url"] = self.s.inkbox_base_url
            self._client = Inkbox(**kw)
        return self._client

    # ---- one-time, idempotent provisioning
    def setup(self) -> dict:
        from inkbox import InkboxAPIError

        ib = self.client()
        handle = self.s.rdf_text_handle
        try:
            ident = ib.get_identity(handle)
            created = False
        except InkboxAPIError as e:
            if e.status_code != 404:
                raise
            try:
                ident = ib.create_identity(handle, display_name="GeonosisAI jobs", imessage_enabled=True,
                                           description="TDM-113 text job offers (Robot Deployment Factory)")
            except InkboxAPIError as e2:
                if e2.status_code == 409:
                    raise RuntimeError(f"Inkbox handle {handle!r} is taken by another org: set RDF_TEXT_HANDLE") from None
                if e2.status_code == 402:
                    raise RuntimeError("Inkbox refused another identity (plan limit). Free one up or upgrade.") from None
                raise
            created = True
        st = self.state()
        st.update({"handle": handle, "identity_id": str(ident.id)})
        line = getattr(ident, "imessage_number", None)
        if line is None:
            free = [n for n in ib.imessages.list_numbers() if n.agent_identity_id is None and str(n.status) == "active"]
            if free:
                ib.identities.update(handle, imessage_enabled=True, imessage_number_id=free[0].id,
                                     idempotency_key=f"tdm113-attach-{free[0].id}")
                ident = ib.get_identity(handle)
                line = getattr(ident, "imessage_number", None)
            elif not getattr(ident, "imessage_enabled", False):
                ib.identities.update(handle, imessage_enabled=True)
        phone = getattr(ident, "phone_number", None)
        if line is not None:
            st.update({"channel": "imessage", "line": line.number, "line_id": str(line.id)})
        elif phone is not None and getattr(phone, "id", None):
            st.update({"channel": "sms", "line": getattr(phone, "number", None), "phone_number_id": str(phone.id)})
        else:
            st.update({"channel": "imessage_shared", "line": None})
        try:
            tri = ib.imessages.get_triage_number()
            st.update({"router": tri.number, "connect_command": f"connect @{handle}"})
        except Exception:  # noqa: BLE001 - only the shared-router fallback needs it
            pass
        tunnel = getattr(ident, "tunnel", None)
        host = getattr(tunnel, "public_host", None) or f"{handle}.inkboxwire.com"
        url = f"https://{host}{WEBHOOK_PATH}"
        st["webhook_url"] = url
        events = ["imessage.received"] + (["text.received"] if st.get("channel") == "sms" else [])
        subs = ib.webhooks.subscriptions.list(agent_identity_id=ident.id, url=url)
        key = None
        if subs:
            st["subscription_id"] = str(subs[0].id)
        else:
            sub = ib.webhooks.subscriptions.create(agent_identity_id=ident.id, url=url, event_types=events)
            st["subscription_id"] = str(sub.id)
            key = sub.signing_key
        if key:
            st["signing_key"] = key
        elif not st.get("signing_key"):
            st["signing_key"] = ib.signing_keys.create_or_rotate(handle).signing_key  # shown once: stored 0600
        st["setup_at"] = time.time()
        self._save(st)
        out = self.summary()
        out["created"] = created
        return out

    # ---- sending
    def send(self, phone: str, text: str) -> SendResult:
        from inkbox import InkboxAPIError

        st = self.state()
        if not st.get("identity_id"):
            return SendResult("error", "Inkbox not set up: run `python -m rdf_orchestrator.text_offers setup`")
        ib = self.client()
        try:
            if st.get("channel") == "sms" and st.get("phone_number_id"):
                ib.texts.send(st["phone_number_id"], to=phone, text=text)
                return SendResult("sent", channel="sms")
            ib.imessages.send(to=phone, text=text, agent_identity_id=st["identity_id"])
            return SendResult("sent", channel="imessage")
        except InkboxAPIError as e:
            code = e.detail.get("error") if isinstance(e.detail, dict) else ""
            if e.status_code in (404, 409) or code == "recipient_not_opted_in":
                return SendResult("text_first", f"HTTP {e.status_code} {code}".strip(), st.get("channel") or "")
            if e.status_code == 403:
                return SendResult("blocked", f"HTTP 403 {code}".strip())
            if e.status_code == 429:
                return SendResult("rate_limited", "HTTP 429")
            return SendResult("error", f"HTTP {e.status_code} {code}".strip())
        except Exception as e:  # noqa: BLE001 - network
            return SendResult("error", type(e).__name__)

    # ---- inbound: the in-process tunnel (no public route on this laptop)
    def start_listener(self, app: Callable) -> bool:
        st = self.state()
        if not st.get("handle") or self._thread is not None:
            return False
        from inkbox.tunnels.client import connect

        def status(s: str) -> None:
            self.listener_status = s
            log.info("text.tunnel", extra={"fields": {"status": s}})

        def run() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            try:
                self._listener = connect(self.client(), name=st["handle"], forward_to=app, on_status=status,
                                         state_dir=STATE_DIR / "tdm113_tunnel" / st["handle"])
                loop.run_until_complete(self._listener.serve_forever())
            except Exception as e:  # noqa: BLE001
                self.listener_status = f"error: {type(e).__name__}"
                log.warning("text.tunnel_failed", extra={"fields": {"error": f"{type(e).__name__}: {str(e)[:200]}"}})
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="tdm113-inkbox-tunnel", daemon=True)
        self._thread.start()
        return True

    def stop_listener(self) -> None:
        lst, loop, self._listener = self._listener, self._loop, None
        if lst is not None and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(lst.aclose(), loop).result(timeout=10)
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        self._thread = None
        self.listener_status = "off"


# --------------------------------------------------------------------------- webhook signature

def sign_webhook(secret: str, body: bytes, *, request_id: str, ts: int) -> dict[str, str]:
    """The headers Inkbox sends: HMAC-SHA256(key, "{request_id}.{timestamp}." + body). For tests / sign-test."""
    key = secret.removeprefix("whsec_")
    sig = hmac.new(key.encode(), f"{request_id}.{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"x-inkbox-signature": f"sha256={sig}", "x-inkbox-request-id": request_id, "x-inkbox-timestamp": str(ts)}


def verify_signature(raw: bytes, headers: dict[str, str], secret: str | None, now: float,
                     window_s: int = SIGNATURE_WINDOW_S) -> tuple[bool, str]:
    """Inkbox webhook check: the SDK's verify_webhook plus the 300 s timestamp window (the SDK does not check it)."""
    if not secret:
        return False, "no signing key"
    h = {k.lower(): v for k, v in headers.items()}
    try:
        ts = float(h.get("x-inkbox-timestamp", ""))
    except ValueError:
        return False, "missing X-Inkbox-* signature headers"
    if not h.get("x-inkbox-signature") or not h.get("x-inkbox-request-id"):
        return False, "missing X-Inkbox-* signature headers"
    if abs(now - ts) > window_s:
        return False, "timestamp outside 300s window"
    try:
        from inkbox import verify_webhook

        ok = verify_webhook(payload=raw, headers=h, secret=secret)
    except Exception:  # noqa: BLE001
        ok = False
    return (True, "") if ok else (False, "signature mismatch")


def inbound_of(body: dict) -> tuple[str | None, str | None, str]:
    """(remote number, text, channel) of an inbound one-to-one message webhook, else (None, None, reason)."""
    et = body.get("event_type")
    data = body.get("data") or {}
    if et == "imessage.received":
        m = data.get("message") or {}
        if m.get("direction") != "inbound" or m.get("is_group"):
            return None, None, "not inbound 1:1"
        return m.get("remote_number"), m.get("content") or "", "imessage"
    if et == "text.received":
        m = data.get("text_message") or {}
        if m.get("direction") != "inbound":
            return None, None, "not inbound"
        return m.get("remote_phone_number"), m.get("text") or "", "sms"
    return None, None, f"ignored {et}"


# --------------------------------------------------------------------------- offers

Append = Callable[[str, dict], None]
Claim = Callable[[int, int], None]  # claim_task(task_id, contributor_id); raises when the claim fails
Heartbeat = Callable[[int], None]
Rows = Callable[[str], list]


def _default_writer() -> Any:
    from . import dispatcher as dsp

    return dsp.make_writer()


class TextOffers:
    def __init__(self, settings: TextSettings | None = None, *, store: TextStore | None = None,
                 transport: Transport | None = None, link: InkboxLink | None = None, outbox: Outbox | None = None,
                 append: Append | None = None, claim: Claim | None = None, heartbeat: Heartbeat | None = None,
                 rows: Rows | None = None, clock: Callable[[], float] = time.time, inline: bool = False,
                 drop_settings: Any = None, record_claim: Callable[[int, int, str], Any] | None = None,
                 release: Callable[[int], None] | None = None,
                 reset_call: Callable[..., Any] | None = None) -> None:
        self.s = settings or TextSettings()
        self.store = store or TextStore(self.s.rdf_text_store or DEFAULT_STORE)
        self.link = link or InkboxLink(self.s)
        self.outbox = outbox or Outbox(self.s.rdf_text_outbox or DEFAULT_OUTBOX)
        self._transport = transport
        self.clock = clock
        self.inline = inline
        self.rows: Rows = rows or (lambda t: [])
        self._writer: Any = None
        self._append = append
        self._claim = claim
        self._heartbeat = heartbeat
        self._drop_settings = drop_settings
        self._record_claim = record_claim
        self._release = release
        self._reset_call = reset_call  # reducer caller for DONE (tests); default: the bus writer
        self._lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._claimed_texted: dict[str, float] = {}  # phone -> when the last "It's yours" text went out
        self._offered: set[int] = set()
        self._holds: dict[int, tuple[int, float]] = {}  # task_id -> (contributor_id, hold until)
        self._hold_thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tdm113-text")

    # ---- plumbing
    @property
    def mode(self) -> str:
        return self.s.rdf_text_mode

    def w(self) -> Any:
        if self._writer is None:
            self._writer = _default_writer()
        return self._writer

    def event(self, type_: str, payload: dict) -> None:
        try:
            if self._append is not None:
                self._append(type_, payload)
            else:
                self.w().call_reducer("append_event", ACTOR, type_,
                                      json.dumps(payload, default=str, separators=(",", ":")))
        except Exception as e:  # noqa: BLE001 - the event log is best effort here
            log.warning("text.event_failed", extra={"fields": {"type": type_, "error": type(e).__name__}})

    def claim_task(self, task_id: int, contributor_id: int) -> None:
        if self._claim is not None:
            return self._claim(task_id, contributor_id)
        self.w().call_reducer("claim_task", task_id, contributor_id)

    def release_task(self, task_id: int) -> None:
        if self._release is not None:
            return self._release(task_id)
        self.w().call_reducer("release_task", task_id)

    def heartbeat_claim(self, task_id: int) -> None:
        if self._heartbeat is not None:
            return self._heartbeat(task_id)
        self.w().call_reducer("heartbeat_claim", task_id)

    def transport(self) -> Transport:
        if self._transport is not None:
            return self._transport
        return self.link.send if self.mode == "live" else dry_run_transport

    def attribute_claim(self, task_id: int, contributor_id: int) -> None:
        """job_drop.claimed {src: text} for /ops/drops attribution (TDM-108's writer unless one was injected)."""
        try:
            if self._record_claim is not None:
                self._record_claim(task_id, contributor_id, SRC)
                return
            from . import job_drops as jd

            d = jd._listener_dropper()
            if d is not None:
                d.record_claim(task_id, contributor_id, SRC)
        except Exception:  # noqa: BLE001
            pass

    def drop_settings(self) -> Any:
        if self._drop_settings is None:
            from .job_drops import DropSettings

            self._drop_settings = DropSettings()
        return self._drop_settings

    def phash(self, phone: str) -> str:
        """Keyed hash for the PUBLIC event log (an unkeyed hash of a phone number is trivially reversed)."""
        key = (self.drop_settings().invite_secret or "rdf-tdm113").encode()
        return hmac.new(key, phone.encode(), hashlib.sha256).hexdigest()[:12]

    def task_link(self, task_id: int) -> str:
        from . import job_drops as jd

        ds = self.drop_settings()
        return jd.invite_link(ds, jd.public_base(ds), task_id, SRC)

    def _text(self, phone: str, text: str, kind: str, *, allow_stopped: bool = False,
              outbox_text: str | None = None) -> SendResult:
        """THE only path that texts anyone: the number must be opted in (STOP's confirmation excepted)."""
        row = self.store.optin(phone)
        if row is None or (row.status != "opted_in" and not allow_stopped):
            log.warning("text.refused", extra={"fields": {"kind": kind, "to": mask(phone), "reason": "not opted in"}})
            return SendResult("blocked", "not opted in")
        now = self.clock()
        try:
            res = self.transport()(phone, text)
        except Exception as e:  # noqa: BLE001
            res = SendResult("error", type(e).__name__)
        self.store.record_send(phone, kind, now)
        self.outbox.write(phone, outbox_text if outbox_text is not None else text, kind, self.mode, res, now)
        log.info("text.sent" if self.mode == "live" else "text.dry_run",
                 extra={"fields": {"kind": kind, "to": mask(phone), "state": res.state, "contributor_id": row.contributor_id}})
        return res

    # ---- opt-in (from /join, after the browser proved its contributor identity)
    def opt_in(self, contributor_id: int, phone_raw: str, in_person: bool, consent_text: str) -> dict:
        phone = normalize_phone(phone_raw)
        if phone is None:
            return {"ok": False, "code": "bad_phone", "detail": "Enter a mobile number, e.g. (415) 555-0123."}
        if consent_text.strip() != CONSENT_TEXT:
            return {"ok": False, "code": "consent_required", "detail": "Tick the consent box to get job texts."}
        now = self.clock()
        self.store.upsert_optin(phone, int(contributor_id), bool(in_person), CONSENT_TEXT, now)
        self.event(EV_OPT_IN, {"contributor_id": int(contributor_id), "in_person": bool(in_person),
                               "ph": self.phash(phone), "mode": self.mode})
        res = self._text(phone, WELCOME, "welcome")
        return {"ok": True, **self.state_for(int(contributor_id)), "welcome": self._welcome_view(res)}

    def _welcome_view(self, res: SendResult) -> dict:
        out: dict[str, Any] = {"state": res.state}
        if res.state == "text_first":
            link = self.link.summary()
            if link.get("line"):
                out["hint"] = f"Text START to {link['line']} once, then tap Resend."
            elif link.get("router"):
                out["hint"] = f"Text \"{link.get('connect_command')}\" to {link['router']} once, then tap Resend."
        return out

    def resend_welcome(self, contributor_id: int) -> dict:
        row = self.store.optin_for(contributor_id)
        if row is None or row.status != "opted_in":
            return {"ok": False, "code": "not_opted_in"}
        if self.store._q("SELECT COUNT(*) FROM sends WHERE phone=? AND kind='welcome' AND at>=?",
                         (row.phone, self.clock() - 60))[0][0] >= 2:
            return {"ok": False, "code": "rate_limited", "detail": "Wait a minute before resending."}
        return {"ok": True, "welcome": self._welcome_view(self._text(row.phone, WELCOME, "welcome"))}

    def opt_out(self, contributor_id: int) -> dict:
        row = self.store.optin_for(contributor_id)
        if row is None:
            return {"ok": True, "opted_in": False}
        self.store.set_status(row.phone, "stopped", self.clock())
        self.store.expire_for_phone(row.phone)
        self.event(EV_OPT_OUT, {"contributor_id": contributor_id, "ph": self.phash(row.phone), "via": "web"})
        return {"ok": True, "opted_in": False}

    def state_for(self, contributor_id: int) -> dict:
        row = self.store.optin_for(contributor_id)
        link = self.link.summary()
        base = {"mode": self.mode, "consent_text": CONSENT_TEXT, "line": link.get("line") if link.get("configured") else None}
        if row is None:
            return {**base, "opted_in": False}
        return {**base, "opted_in": row.status == "opted_in", "stopped": row.status == "stopped",
                "last4": last4(row.phone), "in_person": row.in_person}

    # ---- offers (dispatcher page listener)
    def on_page(self, req: Any, rows: Rows) -> dict:
        ids = [int(t) for t in req.task_ids]
        with self._lock:
            fresh = [t for t in ids if t not in self._offered]
            self._offered.update(fresh)
        if not fresh:
            return {"text": "skipped", "reason": "already offered"}
        if self.inline:
            return self._offer(fresh, rows)
        self._pool.submit(self._offer_safe, fresh, rows)
        return {"text": "queued", "mode": self.mode, "task_ids": fresh}

    def _offer_safe(self, ids: list[int], rows: Rows) -> None:
        try:
            self._offer(ids, rows)
        except Exception as e:  # noqa: BLE001
            log.exception("text.offer_error", extra={"fields": {"task_ids": ids, "error": type(e).__name__}})

    def _busy(self, rows: Rows) -> set[int]:
        return {_i(t.get("assignee_id")) for t in rows("task") if t.get("status") == "claimed"}

    def pick(self, task_type: str, rows: Rows) -> tuple[list[OptIn], dict[str, int]]:
        now = self.clock()
        busy = self._busy(rows)
        skipped = {"not_in_person": 0, "busy": 0, "rate_limited": 0, "daily_cap": 0}
        ok: list[tuple[float, OptIn]] = []
        for o in self.store.opted_in():
            if task_type == "reset" and not o.in_person:
                skipped["not_in_person"] += 1
                continue
            if o.contributor_id in busy:
                skipped["busy"] += 1
                continue
            last = self.store.last_offer_at(o.phone)
            if last and now - last < self.s.rdf_text_rate_s:
                skipped["rate_limited"] += 1
                continue
            if self.store.offers_since(o.phone, now - 86400) >= self.s.rdf_text_daily_max:
                skipped["daily_cap"] += 1
                continue
            ok.append((last, o))
        ok.sort(key=lambda x: (x[0], x[1].contributor_id))  # least recently offered first
        return [o for _, o in ok[: max(0, int(self.s.rdf_text_offer_fanout))]], skipped

    def offer_text(self, f: Any) -> str:
        robot = "yam sim" if f.sim else "the real YAM arm"
        from .job_drops import MINUTES

        return OFFER_FMT.format(price=fmt_money(f.price_cents), mins=MINUTES.get(f.task_type, 2),
                                kind=KIND.get(f.task_type, f.task_type.replace("_", " ")), robot=robot)

    def _offer(self, ids: list[int], rows: Rows) -> dict:
        from . import job_drops as jd

        f, skipped_tasks = jd.facts_from_rows(ids, rows, set(TYPES))
        if f is None:
            self.event(EV_SKIPPED, {"task_ids": ids, "reasons": {str(k): v for k, v in skipped_tasks.items()}})
            return {"text": "skipped", "reasons": {str(k): v for k, v in skipped_tasks.items()}}
        ask = self.reset_ask(f, rows) if f.task_type == "reset" and not f.sim else None
        targets, skipped = self.pick(f.task_type, rows)
        if ask and ask.get("contributor_id"):
            targets = [o for o in targets if o.contributor_id != ask["contributor_id"]]
        if not targets and ask and ask.get("state") in ("sent", "dry_run"):
            return {"text": "reset_ask", **ask}
        if not targets:
            self.event(EV_SKIPPED, {"task_ids": f.task_ids, "ticket_id": f.ticket_id, "reason": "no_recipient", **skipped})
            return {"text": "none", "task_ids": f.task_ids, **skipped}
        body = self.offer_text(f)
        now = self.clock()
        sent: list[int] = []
        states: dict[str, int] = {}
        for o in targets:
            self.store.add_offer(f.task_ids, o.phone, o.contributor_id, f.task_type, f.ticket_id, now)
            res = self._text(o.phone, body, "offer")
            states[res.state] = states.get(res.state, 0) + 1
            if res.ok:
                sent.append(o.contributor_id)
        out = {"task_id": f.task_ids[0], "task_ids": f.task_ids, "ticket_id": f.ticket_id, "task_type": f.task_type,
               "price_cents": f.price_cents, "mode": self.mode, "contributors": sent, "count": len(sent),
               "states": states, "skipped": skipped, "src": SRC}
        self.event(EV_OFFER, out)
        return {"text": "sent" if sent else "failed", **out}

    def reset_ask(self, f: Any, rows: Rows) -> dict | None:
        """A real robot's reset task: text the operator (RDF_RESET_TEXT_CONTRIBUTOR) a direct ask with DONE."""
        cid = int(self.s.rdf_reset_text_contributor or 0)
        if cid <= 0:
            return None
        row = self.store.optin_for(cid)
        if row is None or row.status != "opted_in":
            self.event(EV_SKIPPED, {"task_ids": f.task_ids, "ticket_id": f.ticket_id, "reason": "operator_not_opted_in",
                                    "contributor_id": cid})
            return {"contributor_id": cid, "state": "not_opted_in"}
        from . import reset_done as rdone

        task = next((dict(t) for t in rows("task") if _i(t.get("id")) == f.task_ids[0]), {})
        words = rdone.trial_words(rdone.trial_of(task, rows))
        body = RESET_ASK_FMT.format(ticket=rdone.tkt(f.ticket_id), trial=f" {words}" if words else "",
                                    link=self.task_link(f.task_ids[0]))
        self.store.add_offer(f.task_ids, row.phone, cid, "reset", f.ticket_id, self.clock())
        res = self._text(row.phone, body, "reset_ask")
        out = {"task_id": f.task_ids[0], "ticket_id": f.ticket_id, "contributor_id": cid, "state": res.state,
               "mode": self.mode, "trial": words}
        self.event(EV_RESET_ASK, out)
        return out

    def reset_done_from_text(self, row: OptIn, rows: Rows) -> dict:
        """DONE: finish the newest reset this number was asked to do (claim + complete as them)."""
        from . import reset_done as rdone

        now = self.clock()
        status = {_i(t.get("id")): dict(t) for t in rows("task")}
        for o in self.store.latest_offers(row.phone, now - self.s.rdf_text_offer_ttl_s):
            if o.task_type != "reset" or o.state not in ("sent", "claimed"):
                continue
            for tid in o.task_ids:
                t = status.get(tid)
                if t is None or t.get("status") not in ("open", "claimed"):
                    continue
                holder = _i(t.get("assignee_id"))
                if t.get("status") == "claimed" and not (holder == row.contributor_id
                                                         or self.same_person(holder, row.contributor_id)):
                    continue  # someone else is resetting it
                try:
                    out = rdone.run_sync(self._reducer, t, row.contributor_id, via="text")
                except Exception as e:  # noqa: BLE001
                    log.warning("text.reset_done_failed", extra={"fields": {"task_id": tid, "error": type(e).__name__}})
                    continue
                self.store.set_offer(o.id, "claimed", tid)
                self.event(EV_RESET_DONE, {"task_id": tid, "ticket_id": o.ticket_id, "contributor_id": row.contributor_id,
                                           "mode": out.get("mode"), "src": SRC})
                self._text(row.phone, RESET_THANKS, "reset_done")
                return {"action": "reset_done", "task_id": tid, "mode": out.get("mode")}
        self._text(row.phone, RESET_NONE, "reset_none")
        return {"action": "no_reset"}

    def _reducer(self, name: str, *args: Any) -> Any:
        if name == "claim_task" and self._claim is not None:
            return self._claim(*args)
        if self._reset_call is not None:
            return self._reset_call(name, *args)
        return self.w().call_reducer(name, *args)

    def task_changed(self, task_id: int, rows: Rows) -> None:
        """A task left 'open': unanswered offers whose tasks are all gone expire (a later YES gets TAKEN)."""
        status = {_i(t.get("id")): t.get("status") for t in rows("task")}
        for o in self.store.open_offers_for_task(int(task_id)):
            if all(status.get(t, "gone") != "open" for t in o.task_ids):
                self.store.set_offer(o.id, "expired")

    # ---- inbound texts
    def on_inbound(self, phone_raw: str | None, text: str | None, rows: Rows | None = None) -> dict:
        phone = normalize_phone(phone_raw or "")
        row = self.store.optin(phone) if phone else None
        if row is None:
            log.info("text.inbound_unknown", extra={"fields": {"from": mask(phone)}})
            return {"action": "ignored", "reason": "unknown number"}  # never text a number that did not opt in
        kw = keyword(text)
        now = self.clock()
        if kw in STOP_WORDS:
            if row.status == "opted_in":
                self.store.set_status(row.phone, "stopped", now)
                self.store.expire_for_phone(row.phone)
                self.event(EV_STOP, {"contributor_id": row.contributor_id, "ph": self.phash(row.phone), "via": "text"})
                self._text(row.phone, STOP_CONFIRM, "stop_confirm", allow_stopped=True)
            return {"action": "stopped"}
        if kw in START_WORDS:
            if row.status != "opted_in":
                self.store.set_status(row.phone, "opted_in", now)
                self.event(EV_START, {"contributor_id": row.contributor_id, "ph": self.phash(row.phone)})
            self._text(row.phone, WELCOME, "welcome")
            return {"action": "started"}
        if row.status != "opted_in":
            return {"action": "ignored", "reason": "stopped"}
        if kw in HELP_WORDS:
            self._text(row.phone, HELP, "help")
            return {"action": "help"}
        if kw in YES_WORDS:
            return self.claim_from_text(row, rows or self.rows)
        if kw in DONE_WORDS:
            return self.reset_done_from_text(row, rows or self.rows)
        if self.store.latest_offers(row.phone, now - self.s.rdf_text_offer_ttl_s):
            self._text(row.phone, NUDGE, "nudge")
            return {"action": "nudged"}
        return {"action": "ignored", "reason": "no keyword"}

    def claim_from_text(self, row: OptIn, rows: Rows) -> dict:
        now = self.clock()
        with self._claim_lock:  # one YES at a time in this process; SpacetimeDB orders claims across processes
            recent = self.store.latest_offers(row.phone, now - self.s.rdf_text_offer_ttl_s)
            live = [o for o in recent if o.state == "sent"]
            if not live and recent and recent[0].state == "claimed":  # a second YES: send the link again
                o = recent[0]
                # ...but not for the same YES delivered twice (two webhook events a second apart): once per 30 s
                last = self._claimed_texted.get(row.phone, 0.0)
                if now - last < REPEAT_CLAIMED_TEXT_S:
                    return {"action": "claimed", "task_id": o.claimed_task, "repeat": True, "texted": False}
                self._claimed_texted[row.phone] = now
                self._text(row.phone, CLAIMED_FMT.format(kind=KIND.get(o.task_type, o.task_type), task=o.claimed_task,
                                                         link=self.task_link(o.claimed_task)), "claimed")
                return {"action": "claimed", "task_id": o.claimed_task, "repeat": True}
            if not live:
                self._text(row.phone, TAKEN if recent else NO_OFFER, "taken" if recent else "no_offer")
                return {"action": "taken" if recent else "no_offer"}
            offer = live[0]
            status = {_i(t.get("id")): t.get("status") for t in rows("task")}
            won = 0
            errors: list[str] = []
            for tid in offer.task_ids:
                if status and status.get(tid, "open") not in ("open", "claimed"):
                    continue
                try:
                    self.claim_task(tid, row.contributor_id)
                    won = tid
                    break
                except Exception as e:  # noqa: BLE001 - the reducer refused: someone else holds it
                    errors.append(str(e)[:120])
            if not won:
                self.store.set_offer(offer.id, "lost")
                self._text(row.phone, TAKEN, "taken")
                return {"action": "taken", "errors": len(errors)}
            self.store.set_offer(offer.id, "claimed", won)
        if offer.task_type in ARM_CONTROL:
            self.hold(won, row.contributor_id)
        self.event(EV_CLAIMED, {"task_id": won, "contributor_id": row.contributor_id, "ticket_id": offer.ticket_id,
                                "task_type": offer.task_type, "src": SRC, "offer_age_s": round(now - offer.sent_at, 1)})
        self.attribute_claim(won, row.contributor_id)
        link = self.task_link(won)
        self._claimed_texted[row.phone] = now
        self._text(row.phone, CLAIMED_FMT.format(kind=KIND.get(offer.task_type, offer.task_type), task=won, link=link),
                   "claimed")
        return {"action": "claimed", "task_id": won}

    # ---- keep a text claim alive while the phone opens the link
    def hold(self, task_id: int, contributor_id: int) -> None:
        with self._lock:
            self._holds[int(task_id)] = (int(contributor_id), self.clock() + self.s.rdf_text_hold_s)
            if self._hold_thread is None and not self.inline:
                self._hold_thread = threading.Thread(target=self._hold_loop, name="tdm113-hold", daemon=True)
                self._hold_thread.start()

    def drop_hold(self, task_id: int) -> bool:
        """Stop heartbeating a text claim (its holder gave it back, or another device took it over)."""
        with self._lock:
            return self._holds.pop(int(task_id), None) is not None

    def holding(self, task_id: int) -> bool:
        with self._lock:
            return int(task_id) in self._holds

    # ---- same person on two devices / two accounts (merged by scripts/demo/merge_contributors.py)
    def same_person(self, a: int, b: int) -> bool:
        return bool(a) and bool(b) and self.store.canonical(int(a)) == self.store.canonical(int(b))

    def give_back(self, caller_id: int, task: dict | None) -> dict:
        """The "Give back control" button: release_task as the orchestrator when the caller is the holder (or the
        holder's other, merged account), and stop the text-offer hold so nothing heartbeats it again."""
        if task is None:
            return {"ok": False, "code": "no_task", "detail": "That task does not exist."}
        tid, holder = _i(task.get("id")), _i(task.get("assignee_id"))
        if task.get("status") != "claimed":
            dropped = self.drop_hold(tid)
            return {"ok": True, "released": False, "task_id": tid, "hold_dropped": dropped,
                    "detail": f"Task {tid} is {task.get('status')}: nobody holds it."}
        if not self.same_person(caller_id, holder):
            return {"ok": False, "code": "not_holder", "detail": "Only the person driving can give control back."}
        dropped = self.drop_hold(tid)
        self.release_task(tid)
        self.event(EV_RELEASED, {"task_id": tid, "contributor_id": int(caller_id), "holder_id": holder,
                                 "robot_id": _i(task.get("robot_id")), "hold_dropped": dropped, "via": "web"})
        return {"ok": True, "released": True, "task_id": tid, "holder_id": holder, "hold_dropped": dropped}

    def take_over(self, caller_id: int, task: dict | None) -> dict:
        """A second device of the same person opened the job link: move the claim to this device instead of
        refusing it. release_task + claim_task(caller) as the orchestrator gives a new claim generation, so the
        first device loses control (its commands carry the old claimed_at and the robot refuses them)."""
        if task is None:
            return {"ok": False, "code": "no_task", "detail": "That task does not exist."}
        tid, holder = _i(task.get("id")), _i(task.get("assignee_id"))
        if task.get("status") not in ("claimed", "open"):
            return {"ok": False, "code": "closed", "detail": f"Task {tid} is {task.get('status')}."}
        if task.get("status") == "claimed" and not self.same_person(caller_id, holder):
            return {"ok": False, "code": "taken", "detail": "Someone else is driving this task."}
        dropped = self.drop_hold(tid)
        if task.get("status") == "claimed":
            try:
                self.release_task(tid)
            except Exception as e:  # noqa: BLE001 - lapsed in between: the claim below decides
                log.info("text.takeover_release", extra={"fields": {"task_id": tid, "error": str(e)[:120]}})
        self.claim_task(tid, int(caller_id))
        self.event(EV_TAKEOVER, {"task_id": tid, "contributor_id": int(caller_id), "previous_holder_id": holder,
                                 "robot_id": _i(task.get("robot_id")), "hold_dropped": dropped})
        return {"ok": True, "task_id": tid, "contributor_id": int(caller_id), "previous_holder_id": holder}

    # ---- phone sign-in: a 6-digit code by text binds this browser to the contributor with that number
    def _code_hash(self, phone: str, code: str) -> str:
        key = (self.drop_settings().invite_secret or "rdf-tdm113").encode()
        return hmac.new(key, f"login:{phone}:{code}".encode(), hashlib.sha256).hexdigest()

    def login_start(self, phone_raw: str) -> dict:
        """Text a code to an opted-in number. The answer is the same whether or not the number is known, so the
        form can't be used to find out who gets job texts. Rate limits: 1 / 30 s and 5 / hour per number,
        30 / 10 min overall."""
        import secrets

        phone = normalize_phone(phone_raw)
        if phone is None:
            return {"ok": False, "code": "bad_phone", "detail": "Enter a mobile number, e.g. (415) 555-0123."}
        now = self.clock()
        s = self.s
        if self.store.sends_since("login_code", now - 600) >= s.rdf_text_login_global_10m:
            log.warning("text.login_rate_global", extra={"fields": {"to": mask(phone)}})
            return {"ok": False, "code": "rate_limited", "detail": "Too many sign-in codes right now. Try again in a few minutes."}
        if (self.store.sends_since("login_code", now - s.rdf_text_login_gap_s, phone)
                or self.store.sends_since("login_code", now - 3600, phone) >= s.rdf_text_login_hourly):
            log.info("text.login_rate", extra={"fields": {"to": mask(phone)}})
            return {"ok": False, "code": "rate_limited", "detail": "A code was sent a moment ago. Wait a bit before asking again."}
        row = self.store.optin(phone)
        if row is None or row.status != "opted_in":
            log.info("text.login_unknown", extra={"fields": {"to": mask(phone)}})
            self.store.record_send(phone, "login_code", now)  # rate-limit unknown numbers the same way; nothing is sent
            return {"ok": True, "detail": LOGIN_SENT}
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.store.put_code(phone, self._code_hash(phone, code), now + s.rdf_text_login_ttl_s, now)
        res = self._text(phone, LOGIN_FMT.format(code=code), "login_code",
                         outbox_text=None if self.mode == "dry_run" else LOGIN_FMT.format(code="******"))
        self.event(EV_LOGIN, {"step": "code_sent", "contributor_id": row.contributor_id, "ph": self.phash(phone),
                              "state": res.state})
        return {"ok": True, "detail": LOGIN_SENT, "mode": self.mode}

    def login_check(self, phone_raw: str, code: str) -> dict:
        """-> {ok, contributor_id} for the right code (single use), else {ok: False, code, detail}."""
        phone = normalize_phone(phone_raw)
        bad = {"ok": False, "code": "bad_code", "detail": "That code is wrong or has expired. Ask for a new one."}
        if phone is None or not re.fullmatch(r"\d{6}", (code or "").strip()):
            return bad
        got = self.store.get_code(phone)
        now = self.clock()
        if got is None:
            return bad
        h, exp, attempts = got
        if now >= exp or attempts >= self.s.rdf_text_login_attempts:
            self.store.drop_code(phone)
            return bad
        if not hmac.compare_digest(h, self._code_hash(phone, code.strip())):
            self.store.bump_code(phone)
            log.info("text.login_wrong_code", extra={"fields": {"to": mask(phone), "attempt": attempts + 1}})
            return bad
        self.store.drop_code(phone)
        row = self.store.optin(phone)
        if row is None or row.status != "opted_in":
            return bad
        cid = self.store.canonical(row.contributor_id)
        self.event(EV_LOGIN, {"step": "verified", "contributor_id": cid, "ph": self.phash(phone)})
        return {"ok": True, "contributor_id": cid}

    def _hold_loop(self) -> None:
        while True:
            time.sleep(20)
            if not self.tick_holds():
                with self._lock:
                    if not self._holds:
                        self._hold_thread = None
                        return

    def tick_holds(self, rows: Rows | None = None) -> int:
        rows = rows or self.rows
        now = self.clock()
        by_id = {_i(t.get("id")): t for t in rows("task")}
        with self._lock:
            items = list(self._holds.items())
        kept = 0
        for tid, (cid, until) in items:
            t = by_id.get(tid)
            if now >= until or (t is not None and (t.get("status") != "claimed" or _i(t.get("assignee_id")) != cid)):
                with self._lock:
                    self._holds.pop(tid, None)
                continue
            try:
                self.heartbeat_claim(tid)
                kept += 1
            except Exception as e:  # noqa: BLE001 - lapsed or released: stop holding
                log.info("text.hold_end", extra={"fields": {"task_id": tid, "error": str(e)[:120]}})
                with self._lock:
                    self._holds.pop(tid, None)
        return kept

    # ---- webhook (tunnel + POST /texts/webhook)
    def signing_secret(self) -> str | None:
        if self.s.rdf_text_webhook_secret and self.s.rdf_text_webhook_secret.get_secret_value():
            return self.s.rdf_text_webhook_secret.get_secret_value()
        return self.link.state().get("signing_key")

    def handle_webhook(self, raw: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        ok, why = verify_signature(raw, headers, self.signing_secret(), self.clock())
        if not ok:
            log.warning("text.webhook_rejected", extra={"fields": {"reason": why}})
            return 401, {"ok": False, "reason": why}
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"ok": False, "reason": "bad json"}
        eid = str(body.get("id") or "")
        if eid and self.store.seen(eid, self.clock()):
            return 200, {"ok": True, "duplicate": True}
        phone, text, why = inbound_of(body)
        if phone is None:
            return 200, {"ok": True, "ignored": why}
        if self.inline:
            return 200, {"ok": True, **self.on_inbound(phone, text)}
        self._pool.submit(self._inbound_safe, phone, text)
        return 200, {"ok": True, "queued": True}

    def _inbound_safe(self, phone: str, text: str | None) -> None:
        try:
            self.on_inbound(phone, text)
        except Exception as e:  # noqa: BLE001
            log.exception("text.inbound_error", extra={"fields": {"error": type(e).__name__}})

    def asgi(self) -> Callable:
        """A minimal ASGI app for the Inkbox tunnel: only POST /inkbox/webhook, nothing else of the orchestrator."""
        async def app(scope: dict, receive: Callable, send: Callable) -> None:
            if scope.get("type") != "http":
                return
            path, method = scope.get("path"), scope.get("method")
            chunks, more = [], True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
                if sum(len(c) for c in chunks) > 256_000:
                    break
            if path != WEBHOOK_PATH or method != "POST":
                code, out = (404, {"ok": False}) if path != WEBHOOK_PATH else (405, {"ok": False})
            else:
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
                code, out = await asyncio.to_thread(self.handle_webhook, b"".join(chunks), headers)
            data = json.dumps(out).encode()
            await send({"type": "http.response.start", "status": code,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
            await send({"type": "http.response.body", "body": data})

        return app

    def start(self) -> dict:
        """Orchestrator startup (app lifespan): live mode connects the Inkbox tunnel for inbound texts."""
        if self.mode != "live":
            return {"text": "dry_run", "listener": "off"}
        if not self.link.state().get("identity_id"):
            try:
                self.link.setup()
            except Exception as e:  # noqa: BLE001
                log.warning("text.setup_failed", extra={"fields": {"error": f"{type(e).__name__}: {str(e)[:200]}"}})
                return {"text": "live", "listener": "setup_failed"}
        started = self.link.start_listener(self.asgi())
        return {"text": "live", "listener": "starting" if started else self.link.listener_status}

    def stop(self) -> None:
        self.link.stop_listener()

    def status(self) -> dict:
        return {"mode": self.mode, "fanout": self.s.rdf_text_offer_fanout, "rate_s": self.s.rdf_text_rate_s,
                "daily_max": self.s.rdf_text_daily_max, "hold_s": self.s.rdf_text_hold_s,
                **self.store.stats(self.clock()), "inkbox": self.link.summary(), "outbox": self.outbox.tail(20)}


# --------------------------------------------------------------------------- singleton + hooks

_offers: TextOffers | None = None
_offers_lock = threading.Lock()


def get_offers() -> TextOffers:
    global _offers
    with _offers_lock:
        if _offers is None:
            _offers = TextOffers()
        return _offers


def set_offers(o: TextOffers | None) -> None:
    global _offers
    with _offers_lock:
        _offers = o


def _listener_offers() -> TextOffers | None:
    """Off under pytest unless a test installed one (other tests import the handlers package)."""
    if _offers is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    return get_offers()


def page_listener(req: Any, rows: Rows) -> dict | None:
    o = _listener_offers()
    if o is None:
        return None
    o.rows = rows
    return o.on_page(req, rows)


def task_changed(task_id: int, rows: Rows) -> None:
    o = _listener_offers()
    if o is not None:
        o.rows = rows
        o.task_changed(task_id, rows)


def install() -> None:
    from . import dispatcher as dsp

    dsp.add_page_listener(page_listener)


def start(rows: Rows | None = None) -> dict:
    o = _listener_offers()
    if o is None:
        return {"text": "off"}
    if rows is not None:
        o.rows = rows
    try:
        return o.start()
    except Exception as e:  # noqa: BLE001 - texting must never stop the orchestrator from starting
        log.warning("text.start_failed", extra={"fields": {"error": f"{type(e).__name__}: {str(e)[:200]}"}})
        return {"text": "error"}


def stop() -> None:
    if _offers is not None:
        _offers.stop()


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_orchestrator.text_offers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="mode, opt-in counts, Inkbox setup (no numbers, no secrets)")
    sub.add_parser("setup", help="create/attach the Inkbox identity, iMessage line and webhook (idempotent; texts no one)")
    st = sub.add_parser("sign-test", help="POST a signed test webhook to a URL (default: the tunnel URL)")
    st.add_argument("--url", default=None)
    a = ap.parse_args(argv)
    s = TextSettings()
    link = InkboxLink(s)
    if a.cmd == "status":
        o = TextOffers(s, link=link)
        out = o.status()
        out["outbox"] = out["outbox"][-5:]
        print(json.dumps(out, indent=1, default=str))
        return 0
    if a.cmd == "setup":
        print(json.dumps(link.setup(), indent=1, default=str))
        return 0
    if a.cmd == "sign-test":
        import httpx

        secret = (s.rdf_text_webhook_secret.get_secret_value() if s.rdf_text_webhook_secret else None) or \
            link.state().get("signing_key")
        url = a.url or link.state().get("webhook_url")
        if not secret or not url:
            print("run setup first (no signing key / webhook url)", file=sys.stderr)
            return 2
        # a delivery-status event: verified and then ignored, so it texts no one
        body = json.dumps({"id": f"evt_tdm113_signtest_{int(time.time())}", "event_type": "imessage.sent",
                           "timestamp": "", "data": {"message": None}}).encode()
        ts = int(time.time())
        good = httpx.post(url, content=body, headers={**sign_webhook(secret, body, request_id="tdm113-good", ts=ts),
                                                      "content-type": "application/json"}, timeout=20)
        bad = httpx.post(url, content=body, headers={**sign_webhook("whsec_wrong", body, request_id="tdm113-bad", ts=ts),
                                                     "content-type": "application/json"}, timeout=20)
        print(json.dumps({"url": url, "signed": [good.status_code, good.text[:200]],
                          "forged": [bad.status_code, bad.text[:200]]}, indent=1))
        return 0 if good.status_code == 200 and bad.status_code == 401 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
