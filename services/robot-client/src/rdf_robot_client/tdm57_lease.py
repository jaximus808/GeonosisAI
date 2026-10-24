"""TDM-57: robot-side exclusive-control check for the teleop ``/control`` socket (TDM-52 mounts it).

The browser holds a TDM-10 ``claim_task`` lease (60 s, heartbeat every 20 s) and gets a camera/control
token from ``POST /stream/token`` (TDM-55), which the orchestrator mints only for the live claimant. The
token carries ``task_id``, ``sub = contributor:<id>`` and (TDM-57) ``claimed_at_us``, so
``(task_id, claimed_at_us)`` is the lease token of the TDM-10 contract. The token alone is not enough:
it lives up to 60 s and an operator can revoke sooner. So the robot re-reads the ``task`` row itself:

* **admit** (socket auth): the row must say ``status = claimed``, ``lease_expires_at > now``,
  ``assignee_id`` = the token's contributor, ``claimed_at`` = the token's generation, and the task's
  robot must be this robot.
* **re-check every ``recheck_s`` (2 s, the brief says <= 5 s)** in the background (``watch()``). The
  first failed check latches the guard as lost and calls ``on_lost(decision)``: the server holds the
  arm, tells the browser ``{"t":"lease_lost","code":...}`` and closes with ``CLOSE_LEASE_LOST``.
* **per command** (``check_message``, no I/O, 30 Hz safe): token not expired, last re-check ok and
  younger than ``max_stale_s`` (5 s; SpacetimeDB unreachable = fail closed), and the cached
  ``lease_expires_at`` still in the future (expiry is lazy in TDM-10, so the row can still say
  ``claimed`` after the lease ran out). If the message names ``task_id`` / ``claimed_at_us`` (the
  web gate stamps both), they must match the token.

Revocation paths this catches, and how fast (robot side):
  lease lapse (tab hidden/closed, phone dead)  -> at lease_expires_at, no I/O needed
  release_task (holder or operator)            -> next re-check (<= recheck_s)
  cancel_task (operator revoke)                -> next re-check
  someone else claims after a lapse            -> at lease_expires_at already; re-check also sees it
  SpacetimeDB unreachable                      -> max_stale_s after the last good read

``{"t":"stop"}`` (soft e-stop) must bypass this guard: stopping is always allowed.

Use from the ``/control`` handler (TDM-52)::

    from rdf_robot_client.tdm57_lease import ControlLeaseGuard, spacetime_task_fetcher, CLOSE_LEASE_LOST

    fetch = spacetime_task_fetcher(stdb_http)              # rdf_common SpacetimeClient (public table)
    guard = ControlLeaseGuard(claims, fetch, robot_id=robot_id, on_lost=lambda d: hold_and_close(d))
    d = await guard.admit_async()
    if not d.ok: -> send {"t":"error","code":d.code}; close(CLOSE_LEASE_LOST, d.code)
    watcher = asyncio.create_task(guard.watch())
    ...  on {"t":"auth"} refresh: d = guard.update_token(new_claims)
    ...  on {"t":"cmd"}: d = guard.check_message(msg); if not d.ok: hold, reply {"t":"lease_lost",...}
    finally: watcher.cancel()
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from rdf_common.stream_token import StreamClaims

log = logging.getLogger("rdf_robot_client.tdm57_lease")

CLOSE_LEASE_LOST = 4004
RECHECK_S = 2.0
MAX_STALE_S = 5.0

TaskFetcher = Callable[[int], "dict[str, Any] | None"]


@dataclass(frozen=True)
class LeaseDecision:
    ok: bool
    code: str            # "ok" or why not (safe to show to the browser)
    detail: str = ""

    @staticmethod
    def yes() -> "LeaseDecision":
        return LeaseDecision(True, "ok")


@dataclass(frozen=True)
class TaskLease:
    """The parts of a ``task`` row the guard needs (micros since the epoch, 0 = none)."""
    task_id: int
    robot_id: int
    status: str
    assignee_id: int
    claimed_at_us: int
    lease_expires_at_us: int

    @staticmethod
    def from_row(row: dict[str, Any]) -> "TaskLease":
        def us(v: Any) -> int:
            if v is None:
                return 0
            if isinstance(v, dict):  # raw {"__timestamp_micros_since_unix_epoch__": n} / {"some": ...}
                if "some" in v:
                    return us(v["some"])
                if "none" in v:
                    return 0
                return int(next(iter(v.values())))
            return int(v)

        return TaskLease(task_id=int(row["id"]), robot_id=int(row.get("robot_id") or 0),
                         status=str(row.get("status") or ""), assignee_id=int(row.get("assignee_id") or 0),
                         claimed_at_us=us(row.get("claimed_at")), lease_expires_at_us=us(row.get("lease_expires_at")))


def contributor_of(claims: StreamClaims) -> int | None:
    sub = claims.sub or ""
    if not sub.startswith("contributor:"):
        return None
    try:
        return int(sub.split(":", 1)[1])
    except ValueError:
        return None


def judge(lease: TaskLease | None, claims: StreamClaims, *, now_wall: float, robot_id: int | None = None,
          require_generation: bool = True) -> LeaseDecision:
    """Pure rule: may the holder of ``claims`` drive this robot right now, given this task row?"""
    if now_wall > claims.exp:
        return LeaseDecision(False, "token_expired", "control token expired; fetch a new one")
    cid = contributor_of(claims)
    if claims.task_id <= 0 or cid is None:
        return LeaseDecision(False, "no_task", "control needs a task claimant's token, not an ops token")
    if require_generation and claims.claimed_at_us <= 0:
        return LeaseDecision(False, "unbound_token", "token is not bound to a claim generation")
    if lease is None:
        return LeaseDecision(False, "no_task", f"task {claims.task_id} not found")
    if lease.task_id != claims.task_id:
        return LeaseDecision(False, "wrong_task")
    if robot_id is not None and lease.robot_id != robot_id:
        return LeaseDecision(False, "wrong_robot", f"task {lease.task_id} is for robot {lease.robot_id}")
    if lease.status != "claimed":
        code = {"cancelled": "revoked", "completed": "completed", "open": "released"}.get(lease.status, "not_claimed")
        return LeaseDecision(False, code, f"task {lease.task_id} is {lease.status}")
    if lease.lease_expires_at_us <= 0 or lease.lease_expires_at_us / 1e6 <= now_wall:
        return LeaseDecision(False, "lease_expired", "the claim lease ran out")
    if lease.assignee_id != cid:
        return LeaseDecision(False, "not_holder", f"task {lease.task_id} is held by contributor {lease.assignee_id}")
    if claims.claimed_at_us and lease.claimed_at_us != claims.claimed_at_us:
        return LeaseDecision(False, "stale_claim", "the task was claimed again since this token was minted")
    return LeaseDecision.yes()


def spacetime_task_fetcher(client: Any) -> TaskFetcher:
    """``fetch(task_id)`` over ``rdf_common.spacetime_client.SpacetimeClient.sql`` (``task`` is public)."""
    def fetch(task_id: int) -> dict[str, Any] | None:
        rows = client.sql(f"SELECT * FROM task WHERE id = {int(task_id)}")
        return rows[0] if rows else None
    return fetch


class ControlLeaseGuard:
    """Gate for one ``/control`` connection. Thread-safe; ``check_message`` does no I/O."""

    def __init__(self, claims: StreamClaims, fetch: TaskFetcher, *, robot_id: int | None = None,
                 recheck_s: float = RECHECK_S, max_stale_s: float = MAX_STALE_S,
                 require_generation: bool = True, on_lost: Callable[[LeaseDecision], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        if recheck_s > 5.0:
            raise ValueError("recheck_s must be <= 5 s (TDM-57)")
        self.claims, self.fetch, self.robot_id = claims, fetch, robot_id
        self.recheck_s, self.max_stale_s = recheck_s, max(max_stale_s, recheck_s)
        self.require_generation, self.on_lost = require_generation, on_lost
        self.clock, self.wall = clock, wall
        self._lock = threading.Lock()
        self._lease: TaskLease | None = None
        self._last_ok_at: float | None = None      # monotonic time of the last good read
        self._lost: LeaseDecision | None = None
        self.checks = 0
        self.fetch_errors = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- state
    @property
    def lost(self) -> LeaseDecision | None:
        return self._lost

    @property
    def lease(self) -> TaskLease | None:
        return self._lease

    def _lose(self, d: LeaseDecision) -> LeaseDecision:
        fire = False
        with self._lock:
            if self._lost is None:
                self._lost, fire = d, True
        if fire:
            log.warning("control: lease lost task=%s sub=%s (%s: %s)", self.claims.task_id, self.claims.sub,
                        d.code, d.detail)
            if self.on_lost is not None:
                loop = self._loop
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is not None or loop is None:
                    self._fire_lost(d)
                else:  # called from a worker thread (refresh via to_thread): run it on the server's loop
                    loop.call_soon_threadsafe(self._fire_lost, d)
        return self._lost or d

    def _fire_lost(self, d: LeaseDecision) -> None:
        try:
            r = self.on_lost(d) if self.on_lost else None
            if inspect.isawaitable(r):
                asyncio.ensure_future(r)
        except Exception:  # noqa: BLE001 - the guard stays lost either way
            log.exception("control: on_lost callback failed")

    # -- I/O
    def refresh(self) -> LeaseDecision:
        """Read the task row and judge it. Latches lost on a refusal. Fetch errors fail closed later."""
        if self._lost is not None:
            return self._lost
        self.checks += 1
        try:
            row = self.fetch(self.claims.task_id)
        except Exception as e:  # noqa: BLE001
            self.fetch_errors += 1
            log.warning("control: lease re-check failed (%s); stale in %.1fs", type(e).__name__,
                        self._stale_in())
            return self.allowed()
        lease = TaskLease.from_row(row) if row else None
        d = judge(lease, self.claims, now_wall=self.wall(), robot_id=self.robot_id,
                  require_generation=self.require_generation)
        with self._lock:
            self._lease = lease
            if d.ok:
                self._last_ok_at = self.clock()
        return d if d.ok else self._lose(d)

    def admit(self) -> LeaseDecision:
        """First check at socket auth (blocking read, ~100 ms)."""
        return self.refresh()

    async def admit_async(self) -> LeaseDecision:
        self._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(self.refresh)

    async def watch(self) -> LeaseDecision:
        """Re-check every ``recheck_s`` until lost (cancel the task to stop). Returns the loss."""
        self._loop = asyncio.get_running_loop()
        while self._lost is None:
            await asyncio.sleep(self.recheck_s)
            await asyncio.to_thread(self.refresh)
            self.allowed()  # also trips on staleness when reads keep failing
        return self._lost

    def update_token(self, claims: StreamClaims) -> LeaseDecision:
        """A refreshed token on the same socket: same task and contributor, judged against the cache."""
        if self._lost is not None:
            return self._lost
        if claims.task_id != self.claims.task_id or claims.sub != self.claims.sub:
            return LeaseDecision(False, "token_mismatch", "a refreshed token must name the same task and holder")
        if claims.claimed_at_us != self.claims.claimed_at_us:
            # A new claim generation: the holder re-claimed after a lapse. Make them reconnect, so the
            # arm never carries over from one claim to the next without a fresh admit.
            return self._lose(LeaseDecision(False, "stale_claim", "claim generation changed; reconnect"))
        with self._lock:
            self.claims = replace(self.claims, exp=max(self.claims.exp, claims.exp), jti=claims.jti)
        return self.allowed()

    # -- fast path
    def _stale_in(self) -> float:
        if self._last_ok_at is None:
            return 0.0
        return self.max_stale_s - (self.clock() - self._last_ok_at)

    def allowed(self) -> LeaseDecision:
        """No I/O: may a motion command be applied right now?"""
        if self._lost is not None:
            return self._lost
        if self._last_ok_at is None:
            return LeaseDecision(False, "not_admitted")
        if self._stale_in() <= 0:
            return self._lose(LeaseDecision(False, "lease_unverified",
                                            f"could not confirm the lease for {self.max_stale_s:.0f} s"))
        d = judge(self._lease, self.claims, now_wall=self.wall(), robot_id=self.robot_id,
                  require_generation=self.require_generation)
        return d if d.ok else self._lose(d)

    def check_message(self, msg: dict[str, Any]) -> LeaseDecision:
        """Gate one motion message (``cmd`` / ``takeover`` / ``handback`` / ``record_*``). Not ``stop``."""
        if msg.get("t") == "stop":
            return LeaseDecision.yes()
        tid = msg.get("task_id")
        if tid is not None and int(tid) != self.claims.task_id:
            return LeaseDecision(False, "wrong_task", "message names another task")
        gen = msg.get("claimed_at_us")
        if gen is not None and self.claims.claimed_at_us and int(gen) != self.claims.claimed_at_us:
            return LeaseDecision(False, "stale_claim", "message names another claim generation")
        return self.allowed()

    def status(self) -> dict[str, Any]:
        lease = self._lease
        return {"task_id": self.claims.task_id, "sub": self.claims.sub, "claimed_at_us": self.claims.claimed_at_us,
                "token_exp": self.claims.exp, "lost": self._lost.code if self._lost else None,
                "lease_expires_at_us": lease.lease_expires_at_us if lease else None,
                "stale_in_s": round(self._stale_in(), 2), "checks": self.checks, "fetch_errors": self.fetch_errors}
