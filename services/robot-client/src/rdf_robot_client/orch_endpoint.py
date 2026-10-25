"""Find the orchestrator the way the Vercel web app does (TDM-110 hybrid deploy), so a robot never needs a
hand-copied ``ORCHESTRATOR_URL`` after the laptop's quick tunnel restarts.

    ep = orchestrator_endpoint_for(client)    # services["orch_endpoint"]; started once per process
    ep.base()     -> "https://<random>.trycloudflare.com" (proven) | None (not found yet / backend down)
    ep.headers()  -> {"x-rdf-edge-secret": ...} for requests to base() ONLY (never to signed media URLs)

How (mirrors apps/web/src/lib/tdm110/edge.ts):
  1. ``make demo-hybrid`` (scripts/demo/hybrid.py) appends event_log ``orchestrator.endpoint`` {url, robot_url,
     ...} (or {url: null, state: "down"} on stop) as actor ``orchestrator``. append_event refuses anonymous
     callers and a non-orchestrator service may not log as that actor, so only the orchestrator (or the owner)
     writes a row this accepts.
  2. Here: the newest such row over SpacetimeDB HTTP SQL (event_log is public; the robot_client token reads it),
     at start and every ``RDF_ORCH_DISCOVERY_S`` (default 30) s, with an ``id >`` cursor after the first read.
  3. Before the URL gets the secret, ``GET <url>/_rdf/proof?nonce=<random>`` must answer
     HMAC-SHA256(RDF_EDGE_SECRET, "rdf-edge-proof-v1\\n<nonce>\\n<url>") (scripts/demo/public_guard.py
     edge_proof). A URL that fails never sees the secret. Re-proved every 5 min.
  4. The edge (no --web) forwards any orchestrator route that carries ``x-rdf-edge-secret``, so the uploader's
     POST /uploads/sign-batch needs no ``--api`` secret path. The signed PUTs go straight to the media store.

``ORCHESTRATOR_URL`` set = an explicit override: no discovery (the secret header is still added when
RDF_EDGE_SECRET is set, as Vercel does; the edge strips it before forwarding). Neither set = the old default
``http://localhost:8000``. Logs carry the URL, the event id and the outcome, never the secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

EVENT_TYPE = "orchestrator.endpoint"
EVENT_ACTOR = "orchestrator"
SECRET_HEADER = "x-rdf-edge-secret"
PROOF_LABEL = "rdf-edge-proof-v1"
DEFAULT_INTERVAL_S = 30.0
REPROVE_S = 300.0
LEGACY_DEFAULT = "http://localhost:8000"
_URL_OK = (re.compile(r"^https://[a-z0-9.-]+(:\d+)?$", re.I), re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$"))


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "orch-endpoint", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


def redact(url: str | None) -> str | None:
    """A URL safe to log: an ``/_api/<secret>`` capability path (demo-public --api) is masked."""
    return re.sub(r"/_api/[^/?#]+", "/_api/<secret>", url) if url else url


def edge_proof(secret: str, nonce: str, url: str) -> str:
    """Same as scripts/demo/public_guard.py edge_proof and edge.ts tdm110Proof (pinned by a test)."""
    msg = f"{PROOF_LABEL}\n{nonce}\n{url.rstrip('/')}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class EndpointRow:
    id: int
    url: str | None          # None: the backend published state=down, or the URL is not acceptable
    tunnel: str | None = None


def pick_endpoint(rows: list[dict[str, Any]]) -> EndpointRow | None:
    """The newest acceptable ``orchestrator.endpoint`` row (edge.ts tdm110PickEndpoint)."""
    best: EndpointRow | None = None
    for r in rows:
        if r.get("type") != EVENT_TYPE or r.get("actor") != EVENT_ACTOR:
            continue
        try:
            rid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if best is not None and rid <= best.id:
            continue
        try:
            p = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict):
            continue
        raw = p.get("url").rstrip("/") if isinstance(p.get("url"), str) else ""
        url = raw if any(rx.match(raw) for rx in _URL_OK) else None
        best = EndpointRow(rid, url, p.get("tunnel") if isinstance(p.get("tunnel"), str) else None)
    return best


def _default_get(url: str, timeout: float) -> tuple[int, Any]:
    import httpx

    r = httpx.get(url, timeout=timeout, follow_redirects=False, headers={"cache-control": "no-store"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


class OrchestratorEndpoint:
    """The orchestrator base URL for this robot: explicit, or discovered + proven. Thread-safe."""

    def __init__(self, *, secret: str = "", explicit: str | None = None,
                 sql: Callable[[str], list[dict[str, Any]]] | None = None,
                 http_get: Callable[[str, float], tuple[int, Any]] | None = None,
                 interval_s: float = DEFAULT_INTERVAL_S, reprove_s: float = REPROVE_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.secret = (secret or "").strip()
        self.explicit = (explicit or "").strip().rstrip("/") or None
        self._sql = sql
        self._get = http_get or _default_get
        self.interval_s, self.reprove_s, self._clock = interval_s, reprove_s, clock
        self._lock = threading.Lock()
        self._url: str | None = None          # proven
        self._candidate: str | None = None    # newest published
        self._last_id = 0
        self._proved_at = 0.0
        self._error: str | None = None
        self._said: str | None = None         # last logged state, so a 30 s poll does not spam the journal
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tunnel: str | None = None
        self._start_lock = threading.Lock()
        self._started_explicit = False
        if self.explicit is None and not self.secret:
            self.explicit = LEGACY_DEFAULT  # nothing to discover with: the pre-discovery behaviour
        elif self.explicit is None and len(self.secret) < 24:
            self._sql = None  # the edge itself refuses secrets this short
            self._error = "RDF_EDGE_SECRET is shorter than 24 characters; discovery is off"

    # ---- construction
    @classmethod
    def fixed(cls, url: str) -> OrchestratorEndpoint:
        return cls(explicit=url)

    @classmethod
    def from_env(cls, stdb_config: Any = None, env: dict[str, str] | None = None) -> OrchestratorEndpoint:
        """ORCHESTRATOR_URL (override), else discovery with RDF_EDGE_SECRET over the robot's SpacetimeDB."""
        env = os.environ if env is None else env
        explicit = (env.get("ORCHESTRATOR_URL") or "").strip() or None
        secret = (env.get("RDF_EDGE_SECRET") or "").strip()
        sql = None
        if explicit is None and secret:
            from rdf_common.spacetime_client import SpacetimeClient

            cli = SpacetimeClient(config=stdb_config) if stdb_config is not None else SpacetimeClient()
            sql = lambda q: cli.sql(q, decode=False)  # noqa: E731  (event_log is all plain columns)
        interval = float(env.get("RDF_ORCH_DISCOVERY_S") or DEFAULT_INTERVAL_S)
        return cls(secret=secret, explicit=explicit, sql=sql, interval_s=max(0.5, interval))

    @property
    def discovering(self) -> bool:
        return self.explicit is None

    # ---- what callers use
    def base(self) -> str | None:
        if self.explicit is not None:
            return self.explicit
        with self._lock:
            return self._url

    def headers(self) -> dict[str, str]:
        """For requests to base() only. Empty without a secret (or before a URL is proven)."""
        if not self.secret or self.base() is None:
            return {}
        return {SECRET_HEADER: self.secret}

    def describe(self) -> dict[str, Any]:
        with self._lock:
            url = self.explicit if self.explicit is not None else self._url
            return {"mode": "explicit" if self.explicit else "discovery", "url": redact(url),
                    "candidate": self._candidate, "event_id": self._last_id or None, "error": self._error}

    def nudge(self) -> None:
        """Poll again now (an upload just failed: the tunnel may have rotated)."""
        self._wake.set()

    # ---- discovery
    def refresh(self) -> str | None:
        """One poll: read the newest row, prove a new (or stale) candidate. Returns the proven URL."""
        if not self.discovering or self._sql is None:
            return self.base()
        where = f"type = '{EVENT_TYPE}'" + (f" AND id > {self._last_id}" if self._last_id else "")
        try:
            pick = pick_endpoint(self._sql(f"SELECT * FROM event_log WHERE {where}"))
        except Exception as e:  # noqa: BLE001  (a DB blip keeps the last proven URL)
            self._say(f"sql:{type(e).__name__}", "orchestrator endpoint lookup failed (keeping the last URL)",
                      error=str(e)[:200])
            return self.base()
        with self._lock:
            if pick is not None:
                self._last_id = pick.id
                if pick.url != self._candidate:
                    self._candidate, self._url, self._proved_at = pick.url, None, 0.0
                    self._tunnel = pick.tunnel
            cand = self._candidate
            need = cand is not None and (self._url is None or self._clock() - self._proved_at > self.reprove_s)
        if cand is None:
            with self._lock:
                self._error = "backend offline (published state=down)" if self._last_id else "no backend published yet"
            self._say(f"none:{self._last_id}", "no orchestrator endpoint yet" if not self._last_id
                      else "orchestrator endpoint is down (state=down published); uploads wait",
                      event_id=self._last_id or None)
            return None
        if not need:
            return self.base()
        ok, why = self._prove(cand)
        with self._lock:
            if self._candidate != cand:  # a newer row landed meanwhile; the next poll handles it
                return self._url
            if ok:
                fresh = self._url != cand
                self._url, self._proved_at, self._error = cand, self._clock(), None
            else:
                self._url, self._proved_at, self._error = None, 0.0, why
        if ok and fresh:
            self._say(f"ok:{cand}", "orchestrator endpoint discovered", url=cand, event_id=self._last_id,
                      tunnel=self._tunnel, proof="ok")
        elif not ok:
            self._say(f"bad:{cand}:{why}", "orchestrator endpoint failed the edge proof; not sending it the secret",
                      url=cand, event_id=self._last_id, error=why)
        return self.base()

    def _prove(self, url: str) -> tuple[bool, str]:
        nonce = secrets.token_urlsafe(18)
        try:
            status, body = self._get(f"{url}/_rdf/proof?nonce={nonce}", 6.0)
        except Exception as e:  # noqa: BLE001
            return False, f"not reachable ({type(e).__name__}: {str(e)[:120]})"
        got = body.get("proof") if isinstance(body, dict) else None
        if status != 200 or not isinstance(got, str):
            return False, f"HTTP {status}, no proof"
        if not hmac.compare_digest(got.encode(), edge_proof(self.secret, nonce, url).encode()):
            return False, f"HTTP {status}, wrong proof"
        return True, ""

    def _say(self, key: str, msg: str, **fields: Any) -> None:
        if key != self._said:
            self._said = key
            _log(msg, **fields)

    # ---- background poll
    def start(self) -> OrchestratorEndpoint:
        if not self.discovering:
            with self._start_lock:
                if self._started_explicit:
                    return self
                self._started_explicit = True
            _log("orchestrator endpoint (explicit ORCHESTRATOR_URL or the localhost default: discovery off)",
                 url=redact(self.explicit), edge_secret_header=bool(self.secret))
            return self
        if self._sql is None:
            _log("orchestrator discovery off", error=self._error or "no SpacetimeDB client")
            return self
        with self._start_lock:
            if self._thread is not None:
                return self
            self._thread = threading.Thread(target=self._run, name="orch-endpoint", daemon=True)
        _log("orchestrator discovery on", every_s=self.interval_s, event=EVENT_TYPE)
        self.refresh()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.interval_s)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001  (never let the poller die)
                _log("orchestrator discovery poll crashed (will retry)", error=repr(e)[:200])


_SERVICE_LOCK = threading.Lock()


def orchestrator_endpoint_for(client: Any) -> OrchestratorEndpoint:
    """The process's shared endpoint (``client.services["orch_endpoint"]``), built + started on first use."""
    services: dict[str, Any] = client.services
    with _SERVICE_LOCK:
        ep = services.get("orch_endpoint")
        if ep is None:
            ep = OrchestratorEndpoint.from_env(getattr(client, "stdb", None))
            services["orch_endpoint"] = ep
    return ep.start()  # idempotent; the first poll runs outside the services lock


def start_orchestrator_discovery(client: Any) -> None:
    """At robot-client start (main.py, tdm119_teleop): find the orchestrator now, off the startup path, so the
    first upload does not wait for it. Never raises."""
    def _go() -> None:
        try:
            orchestrator_endpoint_for(client)
        except Exception as e:  # noqa: BLE001  (uploads retry discovery lazily via recorder_for)
            _log("orchestrator discovery did not start", error=repr(e)[:300])

    threading.Thread(target=_go, name="orch-endpoint-start", daemon=True).start()
