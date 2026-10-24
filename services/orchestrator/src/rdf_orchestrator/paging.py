"""TDM-65 paging: Web Push (VAPID) to contributors' phones when a paid task is created.

Flow::

    dispatcher creates tasks -> Dispatcher.page(...) -> our hook (installed with dsp.set_page_hook)
        -> Pager.page(PageRequest) -> one push per opted-in contributor (rate-limited) -> push service
        -> phone's service worker (apps/web/public/tdm65-sw.js) shows the notification
        -> tap -> /marketplace?task=<id> (the claim page, which scrolls to and highlights the task)

Tasks the dispatcher did not create (a ``create_task`` from a script or the dashboard) are paged by
``handlers/paging.py`` through the same ``Dispatcher.page``, so every page is still recorded as a
``dispatch.paged`` event with this module's result dict (counts only, never endpoints).

Who gets a push: every contributor (kind ``contributor``) with a stored subscription. Subscribing is
the opt-in; the point is to reach phones whose page is closed, and those read as offline once the
presence sweep runs. Online contributors without a subscription still get the marketplace's live
highlight + vibration (TDM-61); the result lists how many that was.

Rate limit: at most one push per contributor per ``RDF_PUSH_RATE_S`` (10 s). Pages inside the window
are dropped for that contributor (the earlier push already brought them to the marketplace) and
reported as ``rate_limited``. A task id is paged at most once.

Subscriptions are sensitive (an endpoint + keys lets anyone push to that phone), so they live in a
private sqlite file on the orchestrator host, outside the repo (``RDF_PUSH_STORE``, default
``~/.local/state/rdf/tdm65_push.sqlite3``, mode 0600), never in a public SpacetimeDB table. They
are only written through ``paging_api.py`` after the browser proves its SpacetimeDB identity.

Keys: ``VAPID_PUBLIC_KEY`` / ``VAPID_PRIVATE_KEY`` (base64url raw P-256) and ``VAPID_SUBJECT`` from
``.env``. Without them the hook still runs and reports ``{"push": "disabled"}``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from pydantic import SecretStr

from rdf_common.settings import RdfSettings

from .log import get_logger

log = get_logger("paging")

DEFAULT_STORE = Path.home() / ".local" / "state" / "rdf" / "tdm65_push.sqlite3"
SEND_WAIT_S = 2.5  # the dispatcher holds a ticket lock while paging; do not block it long
PUSH_TTL_S = 600  # a push for a task that is 10 min old is not worth delivering
TYPE_LABEL = {"reset": "Reset", "recovery_takeover": "Takeover", "clean_teleop": "Teleop demo"}
TYPE_VERB = {"reset": "needs a reset", "recovery_takeover": "needs a takeover",
             "clean_teleop": "needs a teleop demo"}

# Push services we will POST to. Anything else is refused at subscribe time (no SSRF via endpoints).
PUSH_HOST_SUFFIXES = (
    "fcm.googleapis.com", "android.googleapis.com",  # Chrome, Edge-on-Android, Samsung Internet
    "push.services.mozilla.com",  # Firefox
    "push.apple.com",  # Safari (macOS, iOS 16.4+ home-screen apps)
    "notify.windows.com",  # Edge on Windows
)


class PushSettings(RdfSettings):
    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    vapid_subject: str = "mailto:robot-factory@example.invalid"
    rdf_push_store: str | None = None
    rdf_web_public_url: str | None = None
    rdf_push_rate_s: float = 10.0


def endpoint_allowed(endpoint: str) -> bool:
    try:
        u = urlparse(endpoint)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == s or host.endswith("." + s) for s in PUSH_HOST_SUFFIXES)


# --------------------------------------------------------------------------- store

@dataclass
class Subscription:
    endpoint: str
    contributor_id: int
    p256dh: str
    auth: str
    user_agent: str = ""

    def info(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


class PushStore:
    """Private sqlite store of push subscriptions. Thread-safe (one connection, one lock)."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            p.touch(mode=0o600, exist_ok=True)
            os.chmod(p, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS push_subscription (
            endpoint TEXT PRIMARY KEY, contributor_id INTEGER NOT NULL, p256dh TEXT NOT NULL,
            auth TEXT NOT NULL, user_agent TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            last_ok_at REAL, last_error TEXT, fail_count INTEGER NOT NULL DEFAULT 0)""")
        self._db.execute("CREATE INDEX IF NOT EXISTS push_sub_contrib ON push_subscription(contributor_id)")
        self._db.commit()

    def upsert(self, sub: Subscription) -> None:
        """One endpoint belongs to one contributor: re-subscribing from another account moves it."""
        with self._lock:
            self._db.execute(
                """INSERT INTO push_subscription(endpoint, contributor_id, p256dh, auth, user_agent, created_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET contributor_id=excluded.contributor_id,
                   p256dh=excluded.p256dh, auth=excluded.auth, user_agent=excluded.user_agent,
                   fail_count=0, last_error=NULL""",
                (sub.endpoint, sub.contributor_id, sub.p256dh, sub.auth, sub.user_agent[:300], time.time()))
            self._db.commit()

    def delete(self, endpoint: str, contributor_id: int | None = None) -> bool:
        with self._lock:
            if contributor_id is None:
                cur = self._db.execute("DELETE FROM push_subscription WHERE endpoint=?", (endpoint,))
            else:
                cur = self._db.execute("DELETE FROM push_subscription WHERE endpoint=? AND contributor_id=?",
                                       (endpoint, contributor_id))
            self._db.commit()
            return cur.rowcount > 0

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> list[Subscription]:
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [Subscription(r[0], int(r[1]), r[2], r[3], r[4] or "") for r in rows]

    def all(self) -> list[Subscription]:
        return self._rows("SELECT endpoint, contributor_id, p256dh, auth, user_agent FROM push_subscription")

    def for_contributor(self, contributor_id: int) -> list[Subscription]:
        return self._rows("SELECT endpoint, contributor_id, p256dh, auth, user_agent FROM push_subscription "
                          "WHERE contributor_id=?", (contributor_id,))

    def has(self, endpoint: str, contributor_id: int) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM push_subscription WHERE endpoint=? AND contributor_id=?",
                                    (endpoint, contributor_id)).fetchone() is not None

    def mark(self, endpoint: str, ok: bool, error: str | None = None) -> None:
        with self._lock:
            if ok:
                self._db.execute("UPDATE push_subscription SET last_ok_at=?, fail_count=0, last_error=NULL "
                                 "WHERE endpoint=?", (time.time(), endpoint))
            else:
                self._db.execute("UPDATE push_subscription SET fail_count=fail_count+1, last_error=? "
                                 "WHERE endpoint=?", ((error or "")[:300], endpoint))
            self._db.commit()

    def stats(self) -> dict:
        with self._lock:
            n, c = self._db.execute("SELECT COUNT(*), COUNT(DISTINCT contributor_id) FROM push_subscription").fetchone()
        return {"subscriptions": int(n), "contributors": int(c)}


# --------------------------------------------------------------------------- sender

# send(subscription_info, data_json, vapid_private_key, vapid_claims, ttl) -> HTTP status code
Sender = Callable[[dict, str, str, dict, int], int]


def webpush_sender(info: dict, data: str, private_key: str, claims: dict, ttl: int) -> int:
    from pywebpush import WebPushException, webpush

    try:
        r = webpush(info, data=data, vapid_private_key=private_key, vapid_claims=dict(claims), ttl=ttl,
                    timeout=5.0, headers={"Urgency": "high"})
        return int(getattr(r, "status_code", 201) or 201)
    except WebPushException as e:
        if e.response is not None:
            return int(e.response.status_code)
        raise


def notification_for(task_ids: list[int], task_type: str, price_cents: int, ticket_id: int,
                     web_base: str | None = None, *, test: bool = False) -> dict:
    label = TYPE_LABEL.get(task_type, task_type.replace("_", " ").title())
    price = f"${price_cents // 100}" if price_cents % 100 == 0 else f"${price_cents / 100:.2f}"  # as the marketplace shows it
    first = task_ids[0] if task_ids else 0
    path = f"/marketplace?task={first}&src=push" if first else "/marketplace?src=push"
    url = (web_base.rstrip("/") + path) if web_base else path
    if test:
        return {"title": "Push works on this device", "body": "You'll get a notification like this when the "
                "factory posts a paid task. Tap to open the marketplace.", "url": url, "tag": "rdf-test",
                "task_ids": [], "test": True}
    if len(task_ids) > 1:
        title = f"{len(task_ids)} paid {label.lower()} tasks · {price} each"
        body = f"Ticket #{ticket_id} {TYPE_VERB.get(task_type, 'needs help')}. First to claim gets it. (Test mode payouts)"
    else:
        title = f"Paid {label.lower()} · {price}"
        body = f"Ticket #{ticket_id} {TYPE_VERB.get(task_type, 'needs help')}. Tap to claim it first. (Test mode payout)"
    return {"title": title, "body": body, "url": url, "tag": f"rdf-task-{first}", "task_ids": list(task_ids),
            "task_type": task_type, "price_cents": price_cents, "ticket_id": ticket_id}


# --------------------------------------------------------------------------- pager

class Pager:
    def __init__(self, settings: PushSettings | None = None, store: PushStore | None = None, *,
                 sender: Sender = webpush_sender, clock: Callable[[], float] = time.monotonic,
                 send_wait_s: float = SEND_WAIT_S) -> None:
        self.s = settings or PushSettings()
        self.store = store or PushStore(self.s.rdf_push_store or DEFAULT_STORE)
        self.sender = sender
        self.clock = clock
        self.send_wait_s = send_wait_s
        self.rate_s = float(self.s.rdf_push_rate_s)
        self._lock = threading.Lock()
        self._last_push: dict[int, float] = {}
        self._paged_tasks: set[int] = set()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tdm65-push")
        self.recent: list[dict] = []  # last few page results (counts only) for GET /push/status

    @property
    def enabled(self) -> bool:
        return bool(self.s.vapid_public_key and self.s.vapid_private_key
                    and self.s.vapid_private_key.get_secret_value())

    @property
    def public_key(self) -> str | None:
        return self.s.vapid_public_key if self.enabled else None

    def _claims(self) -> dict:
        return {"sub": self.s.vapid_subject}

    def _take_slot(self, contributor_id: int, now: float) -> bool:
        last = self._last_push.get(contributor_id)
        if last is not None and now - last < self.rate_s:
            return False
        self._last_push[contributor_id] = now
        return True

    def _send_all(self, subs: list[Subscription], payload: dict) -> dict:
        data = json.dumps(payload)
        key = self.s.vapid_private_key.get_secret_value() if self.s.vapid_private_key else ""

        def one(sub: Subscription) -> str:
            try:
                code = self.sender(sub.info(), data, key, self._claims(), PUSH_TTL_S)
            except Exception as e:  # noqa: BLE001 - network errors are per-subscription
                self.store.mark(sub.endpoint, False, f"{type(e).__name__}")
                return "error"
            if code in (404, 410):  # the browser unsubscribed or the subscription expired
                self.store.delete(sub.endpoint)
                return "gone"
            if 200 <= code < 300:
                self.store.mark(sub.endpoint, True)
                return "sent"
            self.store.mark(sub.endpoint, False, f"HTTP {code}")
            return "error"

        futs = [self._pool.submit(one, s) for s in subs]
        done, pending = wait(futs, timeout=self.send_wait_s)
        out = {"sent": 0, "gone": 0, "error": 0, "pending": len(pending)}
        for f in done:
            out[f.result()] += 1
        return out

    def page(self, req: Any) -> dict:
        """The dispatcher hook. ``req`` is a ``dispatcher.PageRequest``. Returns a loggable dict."""
        task_ids = [int(t) for t in req.task_ids]
        if not self.enabled:
            return {"push": "disabled", "reason": "VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY not set on the orchestrator",
                    "fallback": "marketplace live highlight"}
        with self._lock:
            fresh = [t for t in task_ids if t not in self._paged_tasks]
            self._paged_tasks.update(fresh)
        if not fresh:
            return {"push": "skipped", "reason": "tasks already paged", "task_ids": task_ids}
        subs = self.store.all()
        by_contrib: dict[int, list[Subscription]] = {}
        for s in subs:
            by_contrib.setdefault(s.contributor_id, []).append(s)
        online = {int(c) for c in req.contributor_ids}
        now = self.clock()
        targets: list[Subscription] = []
        rate_limited: list[int] = []
        with self._lock:
            for cid, cs in sorted(by_contrib.items()):
                if self._take_slot(cid, now):
                    targets += cs
                else:
                    rate_limited.append(cid)
        payload = notification_for(fresh, req.task_type, int(req.price_cents), int(req.ticket_id),
                                   self.s.rdf_web_public_url)
        counts = self._send_all(targets, payload) if targets else {"sent": 0, "gone": 0, "error": 0, "pending": 0}
        result = {"push": "sent" if counts["sent"] or counts["pending"] else "none", "task_ids": fresh,
                  "contributors": sorted({s.contributor_id for s in targets}), "rate_limited": rate_limited,
                  "subscriptions": len(targets), **counts,
                  "online_without_push": sorted(online - set(by_contrib)), "url": payload["url"]}
        log.info("paging.sent", extra={"fields": result})
        self.recent = (self.recent + [{"at": time.time(), **result}])[-20:]
        return result

    def test(self, contributor_id: int) -> dict:
        """Send a test push to one contributor's devices (same rate limit as real pages)."""
        if not self.enabled:
            return {"push": "disabled", "reason": "VAPID keys not set on the orchestrator"}
        subs = self.store.for_contributor(contributor_id)
        if not subs:
            return {"push": "none", "reason": "no subscription for this contributor"}
        with self._lock:
            if not self._take_slot(contributor_id, self.clock()):
                return {"push": "rate_limited", "reason": f"one push per {self.rate_s:g} s per contributor"}
        counts = self._send_all(subs, notification_for([], "", 0, 0, self.s.rdf_web_public_url, test=True))
        return {"push": "sent" if counts["sent"] else "none", "subscriptions": len(subs), **counts}

    def status(self) -> dict:
        return {"enabled": self.enabled, "rate_limit_s": self.rate_s, **self.store.stats(),
                "recent": self.recent[-5:]}


_pager: Pager | None = None
_pager_lock = threading.Lock()


def get_pager() -> Pager:
    global _pager
    with _pager_lock:
        if _pager is None:
            _pager = Pager()
        return _pager


def set_pager(p: Pager | None) -> None:
    """Tests swap in a Pager with a fake sender and an in-memory store."""
    global _pager
    with _pager_lock:
        _pager = p


def page_hook(req: Any) -> dict:
    return get_pager().page(req)


def install() -> None:
    """Install the web-push sender as the dispatcher's page hook (called from handlers/paging.py)."""
    from . import dispatcher as dsp

    dsp.set_page_hook(page_hook)
