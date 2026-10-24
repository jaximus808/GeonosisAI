"""TDM-91: dataset-purchase offers, orchestrator side.

The factory may buy an existing dataset instead of commissioning demos. It only ever pays for
``target_episodes - (episodes already uploaded/accepted)``: that cap is computed and enforced by the
TDM-14 reducers (``create_dataset_offer`` / ``ingest_purchased_episode``), so nothing here can
overbuy, even with two orchestrators. This module is the glue around them:

  1. create   ``create_offer`` (``POST /tickets/{id}/offers``) or ``offer_from_plan`` (the planner's
              ``consider_dataset_offer`` / ``offer_max_price_cents``; TDM-95 calls this). Picks a
              registered seller (``contributor.kind = dataset_seller``), calls ``create_dataset_offer``
              and reads the new row back (``needed_count`` = the N - existing cap).
  2. accept   the seller accepts (``accept_dataset_offer``; from their own identity, or via us).
  3. upload   ``sign_uploads``: signed PUT URLs (``storage.py``) under
              ``tickets/{ticket}/purchased/{offer}/{ref}/{file}`` (raw format: one mp4 per camera +
              ``actions.parquet``), or ``.../purchased/{offer}/lerobot/<path>`` for a LeRobot v2.1
              folder. Only as many episodes as there are open slots get URLs.
  4. ingest   ``ingest``: one ``ingest_purchased_episode`` per episode. A LeRobot delivery (the uploaded
              folder, or ``source: hf://...`` for a public dataset) is first IMPORTED into our episode
              format (TDM-95, ``dataset_import``: ``actions.parquet`` + ``<role>.mp4`` per episode under
              ``.../purchased/{offer}/lerobot-<idx>/``), so QA and the TDM-39 converter read the same
              files. Each becomes ``episode{kind=purchased, review_status=pending}``. The reducer refuses
              deliveries past ``needed_count``; those come back in ``refused`` with its message, and
              every ingest call logs ``dataset_offer.delivery`` (ingested, refused + reasons, import).
  5. QA       nothing to call: the episode entering ``pending`` fires the dataset QA handlers.
              TDM-92 (``dataset_qa``) writes its deterministic ``qa_result`` rows and the summary row
              ``deterministic_qa`` last (rejecting via ``submit_review`` on a failure); TDM-93
              (``handlers/llm_qa.py``) gates on that row, writes ``llm_relevance`` / ``llm_success`` and
              the final ``submit_review``. (The TDM-25 reviewer skips ``purchased``.)
              ``REQUIRED_PURCHASED_QA`` (env ``RDF_OFFER_REQUIRED_QA`` replaces it; ``none`` = empty)
              names checks a non-rejected episode must carry before its offer is auto-settled.
  6. settle   ``handlers/offers.py`` (or ``POST /offers/{id}/finalize``): once every delivered episode
              has a verdict and the offer is filled (or the seller said delivery is complete) ->
              ``finalize_offer`` -> ``reserve_dataset_payment(offer_id, "settle")`` for
              ``accepted_count x price``. TDM-63 then moves the (test-mode) money.

Offer status names (TDM-14 enum): proposed -> accepted -> ingesting -> qa (all slots delivered, under
review) -> done (settled) | cancelled. The verdict maths below mirror ``dataset_offer.ts`` so the API
can show it; the reducers stay the authority.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .log import get_logger

log = get_logger("offers")

ACTOR = "orchestrator"
SETTLE_BATCH_REF = "settle"
EVENT_DELIVERY_COMPLETE = "dataset_offer.delivery_complete"
EVENT_DELIVERY = "dataset_offer.delivery"  # TDM-95: per ingest call: ingested + refused (with reasons) + import record
OPEN_STATUSES = ("proposed", "accepted", "ingesting", "qa")
INGESTABLE = ("accepted", "ingesting", "qa")
CAP_ERRORS = ("is full", "already has its", "cannot take more")
LEROBOT_DIR = "lerobot"

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

# Check names (qa_result.check_name) every non-rejected purchased episode must carry before its offer
# is auto-settled: TDM-92's summary row. (An accepted one also has TDM-93's review, which finalize_offer
# itself requires.) RDF_OFFER_REQUIRED_QA=a,b replaces this set; RDF_OFFER_REQUIRED_QA=none empties it.
REQUIRED_PURCHASED_QA: set[str] = {"deterministic_qa"}


def required_checks() -> set[str]:
    env = os.environ.get("RDF_OFFER_REQUIRED_QA")
    if env is None:
        return set(REQUIRED_PURCHASED_QA)
    return {c.strip() for c in env.split(",") if c.strip() and c.strip().lower() != "none"}


class OfferError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def http(self):
        from fastapi import HTTPException

        return HTTPException(self.status, detail={"code": self.code, "message": self.message, **self.extra})


def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


# --------------------------------------------------------------------------- keys

def purchased_prefix(ticket_id: int, offer_id: int) -> str:
    return f"tickets/{int(ticket_id)}/purchased/{int(offer_id)}/"


def purchased_key(ticket_id: int, offer_id: int, ref: str, filename: str) -> str:
    from .storage import validate_key

    if not _REF_RE.match(ref or ""):
        raise ValueError(f"invalid episode ref {ref!r}: [A-Za-z0-9._-], no slashes")
    return validate_key(f"{purchased_prefix(ticket_id, offer_id)}{ref}/{filename}")


def lerobot_key(ticket_id: int, offer_id: int, rel_path: str) -> str:
    from .storage import validate_key

    return validate_key(f"{purchased_prefix(ticket_id, offer_id)}{LEROBOT_DIR}/{rel_path.lstrip('/')}")


# --------------------------------------------------------------------------- verdict maths (mirrors TDM-14)

def _latest_qa(qa_rows: Iterable[Any]) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for r in qa_rows:
        name = _g(r, "check_name")
        if name not in latest or int(_g(r, "id")) > int(_g(latest[name], "id")):
            latest[name] = r
    return latest


def _latest_review(reviews: Iterable[Any]) -> Any | None:
    best = None
    for r in reviews:
        if best is None or int(_g(r, "id")) > int(_g(best, "id")):
            best = r
    return best


def episode_verdict(ep: Any, qa_rows: Iterable[Any], reviews: Iterable[Any]) -> str:
    """accepted | rejected | pending, exactly as ``tdm14Verdict``."""
    if _g(ep, "review_status") == "rejected":
        return "rejected"
    if any(not _g(r, "passed") for r in _latest_qa(qa_rows).values()):
        return "rejected"
    rev = _latest_review(reviews)
    if rev is not None:
        return "accepted" if _g(rev, "accepted") else "rejected"
    return "accepted" if _g(ep, "review_status") == "accepted" else "pending"


def episode_billable(ep: Any, qa_rows: list[Any], reviews: list[Any]) -> bool:
    """Both QA layers accepted it (``tdm14Billable``): >= 1 QA check, all latest pass, accepted review."""
    if episode_verdict(ep, qa_rows, reviews) != "accepted":
        return False
    rev = _latest_review(reviews)
    return bool(rev is not None and _g(rev, "accepted")) and len(_latest_qa(qa_rows)) > 0


@dataclass
class OfferState:
    offer: Any
    episodes: list[dict] = field(default_factory=list)  # each with verdict, billable, qa, review
    live: int = 0
    accepted: int = 0
    billable: int = 0
    rejected: int = 0
    pending: int = 0
    missing_checks: dict[int, list[str]] = field(default_factory=dict)

    @property
    def needed(self) -> int:
        return int(_g(self.offer, "needed_count", 0))

    @property
    def open_slots(self) -> int:
        return max(0, self.needed - self.live) if _g(self.offer, "status") in OPEN_STATUSES else 0


def offer_state(offer: Any, episodes: Iterable[Any], qa_by_ep: dict[int, list[Any]],
                reviews_by_ep: dict[int, list[Any]], required: set[str] | None = None) -> OfferState:
    oid = int(_g(offer, "id"))
    req = required_checks() if required is None else required
    st = OfferState(offer)
    for ep in sorted((e for e in episodes if int(_g(e, "dataset_offer_id", 0)) == oid and _g(e, "kind") == "purchased"),
                     key=lambda e: int(_g(e, "id"))):
        eid = int(_g(ep, "id"))
        qa, revs = qa_by_ep.get(eid, []), reviews_by_ep.get(eid, [])
        v = episode_verdict(ep, qa, revs)
        b = episode_billable(ep, qa, revs)
        latest = _latest_qa(qa)
        missing = sorted(req - set(latest)) if v != "rejected" else []
        if missing:
            st.missing_checks[eid] = missing
        rev = _latest_review(revs)
        st.episodes.append({
            "episode_id": eid, "storage_uri": _g(ep, "storage_uri"), "verdict": v, "billable": b,
            "review_status": _g(ep, "review_status"),
            "qa": [{"check_name": k, "passed": bool(_g(r, "passed")), "detail_json": _g(r, "detail_json")}
                   for k, r in sorted(latest.items())],
            "review": None if rev is None else {"accepted": bool(_g(rev, "accepted")), "reason": _g(rev, "reason")},
        })
        st.live += v != "rejected"
        st.accepted += v == "accepted"
        st.rejected += v == "rejected"
        st.pending += v == "pending"
        st.billable += b
    return st


def finalize_readiness(st: OfferState, delivery_complete: bool) -> tuple[bool, str]:
    """When the orchestrator may call ``finalize_offer`` on its own."""
    status = _g(st.offer, "status")
    if status not in INGESTABLE:
        return False, f"offer is {status}"
    if not st.episodes:
        return False, "nothing delivered yet"
    if st.pending:
        return False, f"{st.pending} episode(s) still under QA"
    if st.missing_checks:
        return False, f"waiting for QA checks {st.missing_checks}"
    if st.billable >= st.needed:
        return True, f"all {st.needed} needed episodes accepted"
    if delivery_complete:
        return True, f"seller finished delivering; {st.billable}/{st.needed} accepted"
    return False, f"{st.billable}/{st.needed} accepted; waiting for replacements or delivery_complete"


# --------------------------------------------------------------------------- LeRobot v2.1 unpacking

@dataclass
class EpisodeDelivery:
    ref: str
    cameras: dict[str, str]  # camera name -> storage uri
    actions_uri: str
    frame_count: int
    duration_ms: int
    success: bool | None = None
    storage_uri: str = ""  # filled in by the service (the episode's folder uri)


def unpack_lerobot(root_uri: str, info: dict, episodes: list[dict]) -> list[EpisodeDelivery]:
    """One delivery per ``meta/episodes.jsonl`` row: the episode's parquet + one mp4 per video key."""
    fps = float(info.get("fps") or 30)
    chunks = int(info.get("chunks_size") or 1000)
    data_path = info.get("data_path") or "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet"
    video_path = info.get("video_path") or "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4"
    video_keys = sorted(k for k, ft in (info.get("features") or {}).items()
                        if isinstance(ft, dict) and ft.get("dtype") == "video")
    root = root_uri.rstrip("/")
    out = []
    for row in sorted(episodes, key=lambda r: int(r["episode_index"])):
        idx = int(row["episode_index"])
        n = int(row.get("length") or 0)
        fmt = {"episode_chunk": idx // chunks, "episode_index": idx}
        cams = {k.split(".")[-1]: f"{root}/{video_path.format(video_key=k, **fmt)}" for k in video_keys}
        out.append(EpisodeDelivery(ref=f"lerobot-{idx:06d}", cameras=cams, actions_uri=f"{root}/{data_path.format(**fmt)}",
                                   frame_count=n, duration_ms=int(round(n / fps * 1000)) if fps > 0 else 0,
                                   storage_uri=f"{root}/episode_{idx:06d}"))
    return out


# --------------------------------------------------------------------------- service

def _uri_key(uri: str) -> str:
    from .storage import parse_uri

    return parse_uri(uri)[2]


class OffersService:
    """``stdb``: ``AsyncSpacetimeClient`` (or a fake with async ``sql`` / ``call_reducer``).
    ``store``: a ``storage.StorageBackend`` (default ``storage.get_backend()``, resolved lazily).
    ``fetch``: ``uri -> bytes`` for reading a seller's LeRobot meta files (default: signed GET)."""

    def __init__(self, stdb: Any, store: Any = None, fetch: Callable[[str], bytes] | None = None) -> None:
        self.stdb = stdb
        self._store = store
        self._fetch = fetch
        self._create_lock = asyncio.Lock()
        self._ingest_locks: dict[int, asyncio.Lock] = {}

    # ---- plumbing
    @property
    def store(self):
        if self._store is None:
            from .storage import get_backend

            self._store = get_backend()
        return self._store

    def fetch(self, uri: str) -> bytes:
        if self._fetch is not None:
            return self._fetch(uri)
        import httpx

        from .storage import backend_for_uri

        r = httpx.get(backend_for_uri(uri).signed_get(uri).url, timeout=60)
        if r.status_code != 200:
            raise OfferError(422, "fetch_failed", f"could not read {uri}: HTTP {r.status_code}")
        return r.content

    async def _rows(self, table: str, col: str, value: int) -> list[dict]:
        return await self.stdb.sql(f"SELECT * FROM {table} WHERE {col} = {int(value)}")

    async def _call(self, reducer: str, *args: Any) -> None:
        from rdf_common.spacetime_client import SpacetimeError

        try:
            await self.stdb.call_reducer(reducer, *args)
        except SpacetimeError as e:
            raise OfferError(409 if e.status == 530 else 502, f"{reducer}_rejected", e.message) from e

    async def offer(self, offer_id: int) -> dict:
        rows = await self._rows("dataset_offer", "id", offer_id)
        if not rows:
            raise OfferError(404, "offer_not_found", f"No dataset_offer {offer_id}.")
        return rows[0]

    async def _state(self, offer: dict) -> OfferState:
        eps = [e for e in await self._rows("episode", "ticket_id", offer["ticket_id"])
               if int(e.get("dataset_offer_id") or 0) == int(offer["id"])]
        qa: dict[int, list] = {}
        revs: dict[int, list] = {}
        for e in eps:
            qa[int(e["id"])] = await self._rows("qa_result", "episode_id", e["id"])
            revs[int(e["id"])] = await self._rows("review", "episode_id", e["id"])
        return offer_state(offer, eps, qa, revs)

    async def delivery_complete(self, offer_id: int) -> bool:
        rows = await self.stdb.sql(f"SELECT * FROM event_log WHERE type = '{EVENT_DELIVERY_COMPLETE}'")
        return any(_offer_of_event(r) == int(offer_id) for r in rows)

    # ---- sellers
    async def pick_seller(self, seller_id: int | None) -> dict:
        sellers = [c for c in await self.stdb.sql("SELECT * FROM contributor") if c.get("kind") == "dataset_seller"]
        if seller_id is not None:
            for c in sellers:
                if int(c["id"]) == int(seller_id):
                    return c
            raise OfferError(422, "unknown_seller", f"contributor {seller_id} is not a registered dataset_seller.",
                             sellers=[_public_seller(c) for c in sellers])
        if not sellers:
            raise OfferError(422, "no_dataset_seller", "No dataset seller is registered "
                             "(register_contributor with kind dataset_seller).")
        real = [c for c in sellers if not str(c.get("name", "")).startswith("test.")] or sellers
        return min(real, key=lambda c: int(c["id"]))

    # ---- 1. create
    async def create_offer(self, ticket_id: int, price_per_episode_cents: int, *, seller_id: int | None = None,
                           source_uri: str | None = None, accept: bool = False, origin: str = "manual") -> dict:
        if price_per_episode_cents <= 0:
            raise OfferError(422, "bad_price", "price_per_episode_cents must be > 0.")
        tickets = await self._rows("ticket", "id", ticket_id)
        if not tickets:
            raise OfferError(404, "ticket_not_found", f"No ticket {ticket_id}.")
        seller = await self.pick_seller(seller_id)
        async with self._create_lock:
            before = {int(o["id"]) for o in await self._rows("dataset_offer", "ticket_id", ticket_id)}
            await self._call("create_dataset_offer", int(ticket_id), int(seller["id"]), int(price_per_episode_cents),
                             source_uri)
            new = [o for o in await self._rows("dataset_offer", "ticket_id", ticket_id) if int(o["id"]) not in before]
        if not new:
            raise OfferError(502, "offer_not_found", "create_dataset_offer succeeded but the new row was not found.")
        offer = max(new, key=lambda o: int(o["id"]))
        if accept:
            await self._call("accept_dataset_offer", int(offer["id"]))
            offer = await self.offer(int(offer["id"]))
        ticket = tickets[0]
        max_cost = int(offer["needed_count"]) * int(offer["price_per_episode_cents"])
        log.info("offers.created", extra={"fields": {"offer_id": offer["id"], "ticket_id": ticket_id,
                                                    "seller_id": seller["id"], "needed_count": offer["needed_count"],
                                                    "price": price_per_episode_cents, "origin": origin}})
        remaining = int(ticket["budget_cents"]) - int(ticket["spent_cents"])
        return {"offer": offer, "seller": _public_seller(seller), "needed_count": int(offer["needed_count"]),
                "max_cost_cents": max_cost, "budget_remaining_cents": remaining,
                "budget_warning": None if max_cost <= remaining else
                f"buying all {offer['needed_count']} would cost {max_cost}c > remaining budget {remaining}c; "
                "the excess payout would be held as blocked_budget",
                "upload_prefix": purchased_prefix(ticket_id, int(offer["id"])), "origin": origin}

    async def offer_from_plan(self, ticket_id: int, consider_dataset_offer: bool, offer_max_price_cents: int, *,
                              seller_id: int | None = None, source_uri: str | None = None) -> dict | None:
        """Planner entry point (TDM-95): the plan's ``consider_dataset_offer`` / ``offer_max_price_cents``
        (``data_plan`` evidence ``dataset_offer.{consider, max_price_per_episode_cents}``). Returns None
        when the plan does not ask for an offer, an open offer already exists, or no seller is registered."""
        if not consider_dataset_offer or int(offer_max_price_cents or 0) <= 0:
            return None
        open_ = [o for o in await self._rows("dataset_offer", "ticket_id", ticket_id) if o["status"] in OPEN_STATUSES]
        if open_:
            log.info("offers.plan_skip", extra={"fields": {"ticket_id": ticket_id, "reason": "open offer exists",
                                                          "offer_ids": [o["id"] for o in open_]}})
            return None
        try:
            return await self.create_offer(ticket_id, int(offer_max_price_cents), seller_id=seller_id,
                                           source_uri=source_uri, origin="planner")
        except OfferError as e:
            if e.code in ("no_dataset_seller", "create_dataset_offer_rejected"):
                log.info("offers.plan_skip", extra={"fields": {"ticket_id": ticket_id, "reason": e.message}})
                return None
            raise

    async def plan_hint(self, ticket_id: int) -> dict | None:
        """The latest plan's ``evidence.dataset_offer`` ({consider, max_price_per_episode_cents, max_episodes})
        from its ``planner.data_plan`` event, or None."""
        best = None
        for r in await self.stdb.sql("SELECT * FROM event_log WHERE type = 'planner.data_plan'"):
            try:
                p = json.loads(r.get("payload_json") or "{}")
            except ValueError:
                continue
            if int(p.get("ticket_id") or 0) == int(ticket_id) and (best is None or int(r["id"]) > int(best[0]["id"])):
                best = (r, p)
        if best is None:
            return None
        hint = (best[1].get("evidence") or {}).get("dataset_offer")
        return {**hint, "data_plan_id": best[1].get("data_plan_id")} if isinstance(hint, dict) else None

    # ---- 2. accept / cancel
    async def accept(self, offer_id: int) -> dict:
        await self._call("accept_dataset_offer", int(offer_id))
        return await self.summary(offer_id)

    async def cancel(self, offer_id: int, reason: str) -> dict:
        await self._call("cancel_dataset_offer", int(offer_id), reason or "cancelled via orchestrator")
        return await self.summary(offer_id)

    # ---- read
    async def summary(self, offer_id: int) -> dict:
        offer = await self.offer(offer_id)
        st = await self._state(offer)
        done = await self.delivery_complete(offer_id)
        ready, why = finalize_readiness(st, done)
        payments = [p for p in await self._rows("payment", "ticket_id", offer["ticket_id"])
                    if int(p.get("dataset_offer_id") or 0) == int(offer_id)]
        return {"offer": offer, "needed_count": st.needed, "live": st.live, "open_slots": st.open_slots,
                "accepted": st.accepted, "billable": st.billable, "rejected": st.rejected, "pending": st.pending,
                "delivery_complete": done, "ready_to_finalize": ready, "finalize_note": why,
                "payout_cents": st.billable * int(offer["price_per_episode_cents"]) if offer["status"] != "done"
                else int(offer["accepted_count"]) * int(offer["price_per_episode_cents"]),
                "episodes": st.episodes, "payments": payments,
                "upload_prefix": purchased_prefix(offer["ticket_id"], offer_id)}

    async def list_for_ticket(self, ticket_id: int) -> list[dict]:
        return sorted(await self._rows("dataset_offer", "ticket_id", ticket_id), key=lambda o: int(o["id"]))

    # ---- 3. signed uploads
    async def sign_uploads(self, offer_id: int, episodes: list[dict] | None = None,
                           lerobot_files: list[str] | None = None, ttl_s: int | None = None) -> dict:
        from .storage import DEFAULT_PUT_TTL_S, guess_content_type

        offer = await self.offer(offer_id)
        if offer["status"] not in INGESTABLE:
            raise OfferError(409, "offer_not_open", f"dataset_offer {offer_id} is {offer['status']}; "
                             + ("accept it first." if offer["status"] == "proposed" else "uploads are closed."))
        st = await self._state(offer)
        slots = st.open_slots
        known = {e["storage_uri"] for e in st.episodes}
        tid, ttl = int(offer["ticket_id"]), ttl_s or DEFAULT_PUT_TTL_S

        def sign(key: str, ctype: str | None) -> dict:
            s = self.store.signed_put(key, ctype or guess_content_type(key), ttl_s=ttl)
            return {"upload_url": s.url, "method": s.method, "headers": s.headers, "storage_uri": s.storage_uri,
                    "key": s.key, "expires_at": s.expires_at}

        uploads, refused = [], []
        if lerobot_files:
            # meta/* always; data/videos only for the first `slots` episode indexes.
            idx_re = re.compile(r"episode_(\d+)\.(?:parquet|mp4)$")
            wanted = sorted({int(m[1]) for f in lerobot_files if (m := idx_re.search(f))})[:slots]
            for f in lerobot_files:
                m = idx_re.search(f)
                if m and int(m[1]) not in wanted:
                    refused.append({"file": f, "reason": _full_msg(offer, st)})
                    continue
                try:
                    uploads.append({"file": f, **sign(lerobot_key(tid, offer_id, f), None)})
                except ValueError as e:
                    refused.append({"file": f, "reason": str(e)})
        for ep in episodes or []:
            ref = str(ep.get("ref", ""))
            folder = self._folder_uri(tid, offer_id, ref) if _REF_RE.match(ref) else None
            if folder in known:
                refused.append({"ref": ref, "reason": "already ingested"})
                continue
            if slots <= 0:
                refused.append({"ref": ref, "reason": _full_msg(offer, st)})
                continue
            try:
                files = [{"filename": f["filename"], **sign(purchased_key(tid, offer_id, ref, f["filename"]),
                                                            f.get("content_type"))} for f in ep.get("files", [])]
            except (ValueError, KeyError) as e:
                refused.append({"ref": ref, "reason": f"bad file list: {e}"})
                continue
            slots -= 1
            uploads.append({"ref": ref, "files": files})
        return {"offer_id": offer_id, "open_slots": st.open_slots, "uploads": uploads, "refused": refused,
                "upload_prefix": purchased_prefix(tid, offer_id)}

    def _folder_uri(self, ticket_id: int, offer_id: int, ref: str) -> str:
        from .storage import make_uri

        return make_uri(self.store.scheme, self.store.bucket, f"{purchased_prefix(ticket_id, offer_id)}{ref}")

    # ---- 4. ingest
    def _resolve(self, ticket_id: int, offer_id: int, ref: str, value: str) -> str:
        from .storage import make_uri

        if "://" in value:
            key = _uri_key(value)
        else:
            key = purchased_key(ticket_id, offer_id, ref, value)
        if not key.startswith(purchased_prefix(ticket_id, offer_id)):
            raise ValueError(f"{value} is outside this offer's upload prefix {purchased_prefix(ticket_id, offer_id)}")
        return make_uri(self.store.scheme, self.store.bucket, key)

    def _raw_deliveries(self, ticket_id: int, offer_id: int, episodes: list[dict]) -> list[EpisodeDelivery | dict]:
        out: list[EpisodeDelivery | dict] = []
        for ep in episodes:
            ref = str(ep.get("ref", ""))
            try:
                if not _REF_RE.match(ref):
                    raise ValueError(f"invalid episode ref {ref!r}")
                cams = {str(k): self._resolve(ticket_id, offer_id, ref, str(v)) for k, v in (ep.get("cameras") or {}).items()}
                if not cams:
                    raise ValueError("cameras is required: {camera_name: file or uri}")
                actions = self._resolve(ticket_id, offer_id, ref, str(ep.get("actions") or ep.get("actions_uri")
                                                                     or "actions.parquet"))
                fc, dur = int(ep.get("frame_count", 0)), int(ep.get("duration_ms", 0))
                if fc <= 0 or dur <= 0:
                    raise ValueError("frame_count and duration_ms must be > 0")
                out.append(EpisodeDelivery(ref, cams, actions, fc, dur, ep.get("success"),
                                           self._folder_uri(ticket_id, offer_id, ref)))
            except (ValueError, TypeError) as e:
                out.append({"ref": ref, "reason": str(e)})
        return out

    def _download_lerobot(self, ticket_id: int, offer_id: int, dest: Path) -> Path:
        """Copy the seller's uploaded ``.../purchased/{offer}/lerobot/`` folder to ``dest``."""
        from .storage import make_uri

        prefix = f"{purchased_prefix(ticket_id, offer_id)}{LEROBOT_DIR}/"
        objs = [o for o in self.store.list(prefix, limit=10000) if o.key.startswith(prefix)]
        if not any(o.key == f"{prefix}meta/info.json" for o in objs):
            raise OfferError(422, "not_uploaded", f"no LeRobot folder uploaded: {prefix}meta/info.json is missing")
        for o in objs:
            p = dest / o.key[len(prefix):]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(self.fetch(make_uri(self.store.scheme, self.store.bucket, o.key)))
        return dest

    def _lerobot_deliveries(self, ticket_id: int, offer_id: int, manifest: dict, done_refs: set[str],
                            slots: int, needed: int) -> tuple[list[EpisodeDelivery | dict], dict]:
        """TDM-95: a LeRobot delivery (the uploaded folder, or ``source: hf://org/name`` for a public
        dataset) is IMPORTED into our episode format with ``dataset_import`` (TDM-92): one
        ``actions.parquet`` + ``<role>.mp4`` per camera, stored under ``.../purchased/{offer}/lerobot-<idx>/``.
        QA and the TDM-39 converter then read the same files (it never sees a raw LeRobot parquet).
        Only as many new episodes as there are open slots are imported; the rest are refused
        (``offer_full``) without the work. Returns (deliveries | refused dicts, import record)."""
        import tempfile

        from .dataset_import import Source, describe, episode_refs, import_lerobot

        source = manifest.get("source")
        if source is not None and not str(source).startswith("hf://"):
            raise OfferError(422, "bad_source", "source must be a public hf://<org>/<name> dataset "
                             "(or omit it to import the uploaded lerobot/ folder).")
        eps = manifest.get("episodes")
        asked = None if eps is None else [int(e["episode_index"]) if isinstance(e, dict) else int(e) for e in eps]
        with tempfile.TemporaryDirectory(prefix=f"tdm95-offer{offer_id}-") as tmp:
            t = Path(tmp)
            spec = str(source) if source else str(self._download_lerobot(ticket_id, offer_id, t / "src"))
            try:
                src = Source(spec, t / ".cache")
                indexes = [r.index for r in episode_refs(src, describe(src.info()))]
            except Exception as e:  # noqa: BLE001 - a broken delivery is the seller's problem
                raise OfferError(422, "bad_lerobot", f"could not read the LeRobot dataset: {type(e).__name__}: {e}") from e
            if asked is not None:
                indexes = [i for i in asked if i in set(indexes)] + [i for i in asked if i not in set(indexes)]
            out: list[EpisodeDelivery | dict] = []
            todo: list[int] = []
            for idx in indexes:
                ref = f"lerobot-{idx:06d}"
                if ref in done_refs:  # already ingested: a placeholder that hits the dedup branch
                    out.append(EpisodeDelivery(ref, {}, "", 0, 0, storage_uri=self._folder_uri(ticket_id, offer_id, ref)))
                elif len(todo) < max(0, slots):
                    todo.append(idx)
                else:
                    out.append({"ref": ref, "code": "offer_full", "reason": (
                        f"not bought: the offer only buys {needed} episode(s) (target_episodes minus those "
                        "already uploaded) and no slot is open")})
            try:
                imported = import_lerobot(spec, t / "out", episodes=todo, arm=manifest.get("arm"),
                                          camera_map=manifest.get("camera_map"),
                                          assume_success=manifest.get("assume_success")) if todo else []
            except Exception as e:  # noqa: BLE001
                raise OfferError(422, "import_failed", f"dataset_import failed: {type(e).__name__}: {e}") from e
            for ie in sorted(imported, key=lambda x: todo.index(x.source_index)):
                ref = f"lerobot-{ie.source_index:06d}"
                actions_uri = self.store.put(purchased_key(ticket_id, offer_id, ref, "actions.parquet"),
                                             ie.actions_path.read_bytes(), "application/vnd.apache.parquet")
                cams = {role: self.store.put(purchased_key(ticket_id, offer_id, ref, f"{role}.mp4"), p.read_bytes(),
                                             "video/mp4") for role, p in sorted(ie.cameras.items())}
                out.append(EpisodeDelivery(ref, cams, actions_uri, ie.frame_count, ie.duration_ms, ie.success,
                                           self._folder_uri(ticket_id, offer_id, ref)))
            record = {"source": spec if source else f"{purchased_prefix(ticket_id, offer_id)}{LEROBOT_DIR}/",
                      "public_dataset": bool(source), "importer": "rdf_orchestrator.dataset_import",
                      "arm": manifest.get("arm"), "imported": [
                          {"ref": f"lerobot-{ie.source_index:06d}", "source_episode_index": ie.source_index,
                           "frames": ie.frame_count, "cameras": sorted(ie.cameras), "notes": ie.notes[:5]}
                          for ie in imported]}
            return out, record

    def _missing_objects(self, ticket_id: int, offer_id: int, deliveries: list[EpisodeDelivery]) -> dict[str, list[str]]:
        have = {o.key for o in self.store.list(purchased_prefix(ticket_id, offer_id), limit=10000)}
        out: dict[str, list[str]] = {}
        for d in deliveries:
            miss = [u for u in [*d.cameras.values(), d.actions_uri] if _uri_key(u) not in have]
            if miss:
                out[d.ref] = miss
        return out

    async def ingest(self, offer_id: int, manifest: dict) -> dict:
        """``manifest``: ``{"format": "raw", "episodes": [{ref, cameras: {name: file|uri}, actions?,
        frame_count, duration_ms, success?}], "final"?, "verify"?}`` or ``{"format": "lerobot",
        "info"?, "episodes"?: [{episode_index, length}], "final"?, "verify"?}``."""
        lock = self._ingest_locks.setdefault(int(offer_id), asyncio.Lock())
        async with lock:
            return await self._ingest(int(offer_id), manifest)

    async def _ingest(self, offer_id: int, manifest: dict) -> dict:
        from rdf_common.spacetime_client import SpacetimeError

        offer = await self.offer(offer_id)
        if offer["status"] not in INGESTABLE:
            raise OfferError(409, "offer_not_open", f"dataset_offer {offer_id} is {offer['status']}; "
                             + ("accept it first (POST /offers/{id}/accept)." if offer["status"] == "proposed"
                                else "it no longer takes deliveries."))
        tid = int(offer["ticket_id"])
        fmt = manifest.get("format", "raw")
        slots = (await self._state(offer)).open_slots  # advisory; the reducer is the authority
        existing = {e["storage_uri"]: int(e["id"]) for e in await self._rows("episode", "ticket_id", tid)
                    if int(e.get("dataset_offer_id") or 0) == offer_id}
        imported: dict | None = None
        if fmt == "lerobot":
            done_refs = {u.rstrip("/").rsplit("/", 1)[-1] for u in existing}
            items, imported = await asyncio.to_thread(self._lerobot_deliveries, tid, offer_id, manifest, done_refs,
                                                      slots, int(offer["needed_count"]))
            missing: dict[str, list[str]] = {}  # just imported and stored by us
        elif fmt == "raw":
            items = self._raw_deliveries(tid, offer_id, list(manifest.get("episodes") or []))
            deliveries = [d for d in items if isinstance(d, EpisodeDelivery)]
            missing = (await asyncio.to_thread(self._missing_objects, tid, offer_id, deliveries)
                       if manifest.get("verify", True) else {})
        else:
            raise OfferError(422, "bad_format", "format must be raw or lerobot.")

        ingested, already, refused = [], [], []
        full_msg: str | None = None
        for d in items:
            if not isinstance(d, EpisodeDelivery):
                refused.append({"code": "invalid", **d})
                continue
            if d.storage_uri in existing:
                already.append({"ref": d.ref, "episode_id": existing[d.storage_uri]})
                continue
            if full_msg is None and d.ref in missing and slots - len(ingested) <= 0:
                full_msg = (f"not bought: the offer only buys {offer['needed_count']} episode(s) "
                            "(target_episodes minus those already uploaded) and no slot is open")
            if full_msg is not None:
                refused.append({"ref": d.ref, "code": "offer_full", "reason": full_msg})
                continue
            if d.ref in missing:
                refused.append({"ref": d.ref, "code": "not_uploaded", "reason": "files not found in storage: "
                                + ", ".join(missing[d.ref])})
                continue
            try:
                await self.stdb.call_reducer("ingest_purchased_episode", offer_id, d.storage_uri, d.actions_uri,
                                             json.dumps(d.cameras, sort_keys=True), d.frame_count, d.duration_ms,
                                             d.success)
            except SpacetimeError as e:
                if any(s in e.message for s in CAP_ERRORS):
                    full_msg = (f"not bought: the offer only buys {offer['needed_count']} episode(s) "
                                f"(target_episodes minus those already uploaded). Server: {e.message}")
                    refused.append({"ref": d.ref, "code": "offer_full", "reason": full_msg})
                else:
                    refused.append({"ref": d.ref, "code": "ingest_rejected", "reason": e.message})
                continue
            ep_id = await self._episode_id(tid, offer_id, d.storage_uri)
            existing[d.storage_uri] = ep_id
            ingested.append({"ref": d.ref, "episode_id": ep_id, "storage_uri": d.storage_uri})
        if ingested or refused:  # TDM-95: the evidence trail / report see every delivery and every refusal
            note = str(manifest.get("note") or "")[:500]
            try:
                await self._call("append_event", ACTOR, EVENT_DELIVERY, json.dumps({
                    "offer_id": offer_id, "ticket_id": tid, "format": fmt, "needed_count": int(offer["needed_count"]),
                    "ingested": [{"ref": x["ref"], "episode_id": x["episode_id"]} for x in ingested],
                    "refused": [{"ref": x.get("ref"), "code": x.get("code"), "reason": str(x.get("reason", ""))[:300]}
                                for x in refused],
                    "import": imported, "note": note or None}, default=str))
            except OfferError as e:  # the ingest itself happened; the log line below still records it
                log.warning("offers.delivery_event_failed", extra={"fields": {"offer_id": offer_id, "error": e.message}})
        if manifest.get("final"):
            await self.mark_delivery_complete(offer_id, tid)
        log.info("offers.ingest", extra={"fields": {"offer_id": offer_id, "ticket_id": tid, "ingested": len(ingested),
                                                   "already": len(already), "refused": len(refused)}})
        return {"offer_id": offer_id, "ingested": ingested, "already_ingested": already, "refused": refused,
                **{k: v for k, v in (await self.summary(offer_id)).items() if k in (
                    "offer", "needed_count", "live", "open_slots", "delivery_complete")}}

    async def _episode_id(self, ticket_id: int, offer_id: int, storage_uri: str) -> int:
        for e in await self._rows("episode", "ticket_id", ticket_id):
            if int(e.get("dataset_offer_id") or 0) == offer_id and e["storage_uri"] == storage_uri:
                return int(e["id"])
        return 0

    async def mark_delivery_complete(self, offer_id: int, ticket_id: int | None = None) -> None:
        if await self.delivery_complete(offer_id):
            return
        if ticket_id is None:
            ticket_id = int((await self.offer(offer_id))["ticket_id"])
        await self._call("append_event", ACTOR, EVENT_DELIVERY_COMPLETE,
                         json.dumps({"offer_id": int(offer_id), "ticket_id": int(ticket_id)}))

    # ---- 6. settle (the handler does this on its own; this is the manual / forced path)
    async def finalize(self, offer_id: int) -> dict:
        offer = await self.offer(offer_id)
        if offer["status"] != "done":
            await self._call("finalize_offer", int(offer_id))
            offer = await self.offer(offer_id)
        if int(offer["accepted_count"]) > 0:
            await self._call("reserve_dataset_payment", int(offer_id), SETTLE_BATCH_REF)
        out = await self.summary(offer_id)
        payment = next((p for p in out["payments"] if p.get("batch_ref") == SETTLE_BATCH_REF), None)
        return {**out, "payment": payment}


def _offer_of_event(row: Any) -> int | None:
    try:
        p = json.loads(_g(row, "payload_json") or "{}")
        return int(p["offer_id"]) if isinstance(p, dict) and "offer_id" in p else None
    except (ValueError, TypeError):
        return None


def _full_msg(offer: dict, st: OfferState) -> str:
    return (f"not bought: dataset_offer {offer['id']} only buys {st.needed} episode(s) "
            f"(target_episodes minus those already uploaded) and {st.live} are delivered")


def _public_seller(c: dict) -> dict:
    return {"id": c["id"], "name": c.get("name"), "kind": c.get("kind"), "payouts_enabled": c.get("payouts_enabled")}
