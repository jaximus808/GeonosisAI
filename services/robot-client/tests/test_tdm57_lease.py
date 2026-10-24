"""TDM-57: robot-side lease guard for /control (pure rules + re-check, fake clock, no network)."""
from __future__ import annotations

import asyncio
import time

import pytest

from rdf_common.stream_token import mint, verify
from rdf_robot_client.tdm57_lease import ControlLeaseGuard, TaskLease, judge

SECRET = "tdm57-test-secret-0123456789abcdef"
T0 = 1_800_000_000.0
GEN = int((T0 - 5) * 1e6)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def wall(self) -> float:
        return self.t

    def mono(self) -> float:
        return self.t


def row(**kw):
    base = {"id": 7, "robot_id": 1, "status": "claimed", "assignee_id": 3, "claimed_at": GEN,
            "lease_expires_at": int((T0 + 55) * 1e6)}
    base.update(kw)
    return base


def claims(gen=GEN, sub="contributor:3", ttl=60, now=T0):
    tok, _ = mint(SECRET, robot_id=1, task_id=7, sub=sub, ttl_s=ttl, now=now, claimed_at_us=gen)
    return verify(SECRET, tok, now=now)


def test_token_carries_generation():
    c = claims()
    assert c.claimed_at_us == GEN and c.to_payload()["claimed_at_us"] == GEN


@pytest.mark.parametrize("r,code", [
    (row(), "ok"),
    (row(status="cancelled"), "revoked"),
    (row(status="open", assignee_id=0, claimed_at=None, lease_expires_at=None), "released"),
    (row(status="completed"), "completed"),
    (row(lease_expires_at=int((T0 - 1) * 1e6)), "lease_expired"),
    (row(assignee_id=4), "not_holder"),
    (row(claimed_at=GEN + 1), "stale_claim"),
    (row(robot_id=2), "wrong_robot"),
    (None, "no_task"),
])
def test_judge(r, code):
    lease = TaskLease.from_row(r) if r else None
    assert judge(lease, claims(), now_wall=T0, robot_id=1).code == code


def test_judge_refuses_ops_and_unbound_tokens():
    assert judge(TaskLease.from_row(row()), claims(sub="ops:jaxon"), now_wall=T0).code == "no_task"
    assert judge(TaskLease.from_row(row()), claims(gen=0), now_wall=T0).code == "unbound_token"


def make(r_holder, clock, **kw):
    lost = []
    g = ControlLeaseGuard(claims(), lambda tid: r_holder[0], robot_id=1, clock=clock.mono, wall=clock.wall,
                          on_lost=lost.append, **kw)
    return g, lost


def test_lapse_denies_without_io():
    clock, cur = Clock(), [row()]
    g, lost = make(cur, clock)
    assert g.admit().ok
    assert g.check_message({"t": "cmd", "task_id": 7, "claimed_at_us": GEN}).ok
    clock.t = T0 + 4  # still within lease and freshness
    assert g.check_message({"t": "cmd"}).ok
    # Re-checks keep it fresh, but the row's lease runs out (no heartbeat): denied at expiry.
    for _ in range(30):
        clock.t += 2
        g.refresh()
    assert not g.check_message({"t": "cmd"}).ok
    assert lost and lost[0].code == "lease_expired"
    # Latched: a later heartbeat-looking row does not bring control back on this socket.
    cur[0] = row(lease_expires_at=int((clock.t + 60) * 1e6))
    assert not g.refresh().ok


def test_revoke_and_takeover_seen_on_recheck():
    for new, code in [(row(status="cancelled"), "revoked"), (row(status="open"), "released"),
                      (row(assignee_id=9, claimed_at=GEN + 10), "not_holder")]:
        clock, cur = Clock(), [row()]
        g, lost = make(cur, clock)
        assert g.admit().ok
        cur[0] = new
        clock.t += 2
        assert g.refresh().code == code
        assert g.check_message({"t": "cmd"}).code == code
        assert g.check_message({"t": "stop"}).ok  # soft e-stop is never gated
        assert [d.code for d in lost] == [code]


def test_unreachable_db_fails_closed():
    clock = Clock()
    calls = {"n": 0}

    def fetch(tid):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConnectionError("down")
        return row(lease_expires_at=int((T0 + 600) * 1e6))

    g = ControlLeaseGuard(claims(ttl=600), fetch, robot_id=1, clock=clock.mono, wall=clock.wall)
    assert g.admit().ok
    clock.t += 3
    assert g.refresh().ok  # one failed read inside the 5 s window: still ok
    clock.t += 2.5
    assert g.refresh().code == "lease_unverified"


def test_message_must_match_token():
    clock, cur = Clock(), [row()]
    g, _ = make(cur, clock)
    g.admit()
    assert g.check_message({"t": "cmd", "task_id": 8}).code == "wrong_task"
    assert g.check_message({"t": "cmd", "claimed_at_us": GEN + 1}).code == "stale_claim"


def test_token_refresh_rules():
    clock, cur = Clock(), [row()]
    g, _ = make(cur, clock)
    g.admit()
    assert g.update_token(claims(now=T0 + 30)).ok
    assert g.claims.exp == pytest.approx(T0 + 90, abs=1)
    assert g.update_token(claims(sub="contributor:4")).code == "token_mismatch"
    assert g.update_token(claims(gen=GEN + 99)).code == "stale_claim"
    assert g.lost is not None


def test_watch_fires_on_lost_in_loop():
    cur = [row(lease_expires_at=int((time.time() + 60) * 1e6), claimed_at=GEN)]
    tok, _ = mint(SECRET, robot_id=1, task_id=7, sub="contributor:3", ttl_s=60, claimed_at_us=GEN)

    async def go():
        fired = asyncio.Event()
        g = ControlLeaseGuard(verify(SECRET, tok), lambda tid: cur[0], robot_id=1, recheck_s=0.05,
                              on_lost=lambda d: fired.set())
        assert (await g.admit_async()).ok
        w = asyncio.create_task(g.watch())
        await asyncio.sleep(0.12)
        assert not fired.is_set()
        cur[0] = {**cur[0], "status": "cancelled"}
        await asyncio.wait_for(fired.wait(), 1.0)
        assert (await w).code == "revoked"

    asyncio.run(go())


def test_recheck_bound():
    with pytest.raises(ValueError):
        ControlLeaseGuard(claims(), lambda tid: None, recheck_s=6)
