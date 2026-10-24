"""TDM-27 resume bridge: predicate semantics + the bus flow against fakes (no network).

The fake writer models the TDM-13 reducers that matter: ``agent_resume`` is the atomic claim (only
from waiting_for/paused), ``upsert_agent_session`` appends a Protocol B session id, and
``append_event`` records events. Several bridges can share one ``Tdm27AgentDB`` to race.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from rdf_orchestrator import resume_bridge as rb
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.handlers import resume_bridge as rb_handlers
from rdf_orchestrator.stdb import Row

R = lambda **kw: Row(kw)  # noqa: E731
NOW_US = lambda: int(time.time() * 1e6)  # noqa: E731


# --------------------------------------------------------------------------- fakes

class Tdm27Client:
    """Bus client fake: tests drive snapshot/insert/update/delete like the server would."""

    def __init__(self, schema: set[str] | None = None):
        self.state, self.identity, self.queries, self.cb = "stopped", "0xfake27", [], {}
        self._stop = asyncio.Event()
        self.schema = {t: object() for t in (schema or set())}
        self.host = self.db = self.token = None

    def subscribe(self, q): self.queries = q
    def on_snapshot(self, cb): self.cb["snapshot"] = cb
    def on_insert(self, cb): self.cb["insert"] = cb
    def on_update(self, cb): self.cb["update"] = cb
    def on_delete(self, cb): self.cb["delete"] = cb
    def on_state(self, cb): self.cb["state"] = cb
    def key(self, table, row): return row["id"]
    async def run(self): await self._stop.wait()
    def stop(self): self._stop.set()
    async def call_reducer(self, r, a): return 1

    def snapshot(self, tables):
        self.cb["state"]("live", None)
        self.cb["snapshot"]({t: [Row(r) for r in rows] for t, rows in tables.items()})


class Tdm27AgentDB:
    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.events: list[tuple[str, dict]] = []
        self.calls: list[tuple] = []
        self.lock = threading.Lock()

    def writer(self):
        db = self

        class W:
            def call_reducer(self, name, *args):
                with db.lock:
                    db.calls.append((name, *args))
                    if name == "agent_resume":
                        sid, new_id, reason = args
                        s = db.sessions[sid]
                        if s["status"] not in ("waiting_for", "paused"):
                            raise RuntimeError(f"reducer 'agent_resume' failed (530): session {sid} is {s['status']}, not waiting_for/paused")
                        s.update(status="resuming", waiting_for_json=None, reason=reason)
                        db.events.append(("agent.resumed", {"session_id": sid, "mode": "restart" if new_id else "native"}))
                    elif name == "upsert_agent_session":
                        sid, role, fsid, status, step, _n, _t = args
                        s = db.sessions[sid]
                        if fsid:
                            s.setdefault("ids", []).append(fsid)
                            s["factory_session_id"] = fsid
                        s.update(status=status, current_step=step)
                    elif name == "append_event":
                        db.events.append((args[1], json.loads(args[2])))
        return W()

    def ev(self, type_):
        return [p for t, p in self.events if t == type_]


class Tdm27Resumer:
    def __init__(self, result=None, fail=0):
        self.calls: list[tuple[int, str, float]] = []
        self.result, self.fail = result, fail

    def resume(self, agent_session_id, text):
        self.calls.append((agent_session_id, text, time.time()))
        if self.fail:
            self.fail -= 1
            raise ConnectionError("factory down")
        return self.result


def session(id=1, status="waiting_for", pred=None, ticket_id=1, last_update=None, **kw):
    return R(id=id, ticket_id=ticket_id, role="planner", name="droid", factory_session_id=kw.pop("fsid", "fs-1"),
             factory_session_ids_json='["fs-1"]', status=status, current_step="",
             waiting_for_json=json.dumps(pred) if pred is not None else None,
             resume_payload_json=kw.pop("payload", '{"next":"train"}'), created_at=1,
             last_update=last_update or NOW_US() - 2_000_000, **kw)


TR_DONE = {"table": "training_run", "ticket_id": 1, "field": "status", "op": "==", "value": "done"}


def make_bridge(db: Tdm27AgentDB, resumer=None, schema=None, **kw):
    reg, fc = Registry(), Tdm27Client(schema)
    rb_handlers.register(reg)
    bus = Bus(reg, fc, workers=2)
    resumer = resumer if resumer is not None else Tdm27Resumer()
    b = rb.ResumeBridge(bus, writer_factory=db.writer, resumer_factory=lambda: resumer, poll_s=0,
                        retry_delays=(0.01, 0.01), **kw)
    rb._instances[id(bus)] = b
    return bus, fc, b, resumer


async def settle(*bridges, t=2.0):
    end = time.time() + t
    while time.time() < end:
        await asyncio.sleep(0.02)
        if all(not b.inflight and b.pool._work_queue.empty() for b in bridges):
            await asyncio.sleep(0.05)
            if all(not b.inflight for b in bridges):
                return


def sync_db(db: Tdm27AgentDB, rows):
    for r in rows:
        db.sessions[r["id"]] = dict(r)


# --------------------------------------------------------------------------- predicate semantics

def rows_from(tables):
    return lambda t: tables.get(t)


def test_predicate_leaf_ops_filters_and_coercion():
    tables = {"dataset": [R(id=1, ticket_id=1, accepted_count=1), R(id=2, ticket_id=2, accepted_count=5)],
              "training_run": [R(id=3, ticket_id=1, status="running"), R(id=4, ticket_id=1, status="done")]}
    f = rows_from(tables)
    P = rb.parse_predicate
    assert not rb.evaluate(P({"table": "dataset", "ticket_id": 1, "field": "accepted_count", "op": ">=", "value": 2}), f).ok
    assert rb.evaluate(P({"table": "dataset", "ticket_id": 2, "field": "accepted_count", "op": ">=", "value": 2}), f).ok
    assert rb.evaluate(P({"table": "dataset", "ticket_id": "2", "field": "accepted_count", "op": ">", "value": "4"}), f).ok
    assert rb.evaluate(P({"table": "dataset", "where": {"ticket_id": 2}, "field": "accepted_count", "value": 5}), f).ok
    # ANY filtered row satisfies
    ev = rb.evaluate(P(json.dumps(TR_DONE)), f)
    assert ev.ok and [r["id"] for r in ev.matched[0][1]] == [4]
    assert rb.evaluate(P({"table": "training_run", "ticket_id": 1, "field": "status", "op": "!=", "value": "failed"}), f).ok
    # incomparable is false, not an error
    assert not rb.evaluate(P({"table": "training_run", "field": "status", "op": ">", "value": 3}), f).ok
    # missing field in rows -> false
    assert not rb.evaluate(P({"table": "dataset", "field": "nope", "value": 1}), f).ok


def test_predicate_aggregates_exists_and_groups():
    tables = {"episode": [R(id=i, ticket_id=1, review_status="accepted" if i < 3 else "pending", frame_count=100)
                          for i in range(1, 6)], "task": []}
    f = rows_from(tables)
    P = rb.parse_predicate
    assert rb.evaluate(P({"table": "episode", "ticket_id": 1, "review_status": "accepted", "agg": "count", "op": ">=", "value": 2}), f).ok
    assert not rb.evaluate(P({"table": "episode", "review_status": "accepted", "agg": "count", "op": ">=", "value": 3}), f).ok
    assert rb.evaluate(P({"table": "episode", "agg": "sum", "field": "frame_count", "op": "==", "value": 500}), f).ok
    assert rb.evaluate(P({"table": "episode", "id": 5}), f).ok  # exists
    assert not rb.evaluate(P({"table": "task", "id": 5}), f).ok
    assert rb.evaluate(P({"table": "task", "agg": "count", "op": "==", "value": 0}), f).ok
    both = {"all": [{"table": "episode", "id": 1}, {"any": [{"table": "task", "id": 9}, {"table": "episode", "id": 2}]}]}
    n = P(both)
    assert rb.evaluate(n, f).ok and rb.tables_of(n) == {"episode", "task"}
    assert not rb.evaluate(P({"all": [{"table": "episode", "id": 1}, {"table": "task", "id": 9}]}), f).ok
    # table not available yet -> false and reported as missing
    ev = rb.evaluate(P({"table": "contributor_presence", "contributor_id": 1}), f)
    assert not ev.ok and ev.missing == {"contributor_presence"}


@pytest.mark.parametrize("bad", ["not json", "[]", {}, {"all": []}, {"table": "x", "op": "~"},
                                 {"table": "x", "agg": "avg", "value": 1}, {"table": "x", "field": "f"},
                                 {"table": "x", "agg": "sum", "value": 1}])
def test_predicate_malformed(bad):
    with pytest.raises(rb.PredicateError):
        rb.parse_predicate(bad)


def test_new_session_id_normalisation():
    assert rb.new_session_id(None, "fs-1") is None
    assert rb.new_session_id("reply text", "fs-1") is None
    assert rb.new_session_id({"new_factory_session_id": "fs-2"}, "fs-1") == "fs-2"
    assert rb.new_session_id({"mode": "restart", "factory_session_id": "fs-3"}, "fs-1") == "fs-3"
    assert rb.new_session_id({"mode": "native", "factory_session_id": "fs-1"}, "fs-1") is None
    assert rb.new_session_id({"new_factory_session_id": "fs-1"}, "fs-1") is None


# --------------------------------------------------------------------------- bus flow

def test_resumes_when_predicate_becomes_true_on_update():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db)
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="running")],
                     "ticket": [R(id=1)]})
        await settle(b)
        assert not res.calls and b.waiting[1].pred is not None
        t0 = time.time()
        fc.cb["update"]("training_run", R(id=3, ticket_id=1, status="running"), R(id=3, ticket_id=1, status="done"),
                        {"timestamp": {"__timestamp_micros_since_unix_epoch__": NOW_US()}})
        await settle(b)
        await bus.stop()
        return t0

    t0 = asyncio.run(go())
    assert len(res.calls) == 1
    sid, text, at = res.calls[0]
    assert sid == 1 and at - t0 < 0.5
    assert "training_run.status == 'done'" in text and "status: 'running' -> 'done'" in text
    assert '"next":"train"' in text  # resume_payload handed over
    assert db.sessions[1]["status"] == "resuming"
    assert [c[0] for c in db.calls].count("agent_resume") == 1
    (d,) = db.ev(rb.EV_DELIVERED)
    assert d["mode"] == "native" and d["predicate"] == TR_DONE and d["elapsed_s"] >= 1.5
    assert d["detect_ms"] is not None and "training_run 3 updated" in d["changed"]


def test_already_true_at_wait_time_and_on_boot():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db)
    running = session(status="running", pred=None)
    waiting = session(pred=TR_DONE, last_update=NOW_US())
    boot_waiting = session(id=2, pred={"table": "dataset", "ticket_id": 1, "field": "accepted_count", "op": ">=", "value": 2})
    sync_db(db, [waiting, boot_waiting])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [running, boot_waiting], "training_run": [R(id=3, ticket_id=1, status="done")],
                     "dataset": [R(id=1, ticket_id=1, accepted_count=2)], "ticket": [R(id=1)]})
        await settle(b)
        assert [c[0] for c in res.calls] == [2]  # boot: session 2 was already satisfied
        fc.cb["update"]("agent_session", running, waiting, {})  # session 1 starts waiting on a true predicate
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert sorted(c[0] for c in res.calls) == [1, 2]
    changed = {p["session_id"]: p["changed"] for p in db.ev(rb.EV_DELIVERED)}
    assert changed == {1: "already true when the wait started", 2: "found true on orchestrator boot"}


def test_resync_after_reconnect_resumes():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db)
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="running")], "ticket": [R(id=1)]})
        await settle(b)
        fc.cb["state"]("reconnecting", "ConnectionClosed")  # the update happened while we were away
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert len(res.calls) == 1


def test_two_bridges_race_only_one_resumes():
    db = Tdm27AgentDB()
    buses = [make_bridge(db) for _ in range(3)]
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        for bus, fc, _b, _r in buses:
            await bus.start()
            fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="running")], "ticket": [R(id=1)]})
        for _bus, fc, _b, _r in buses:  # every bridge sees the same transaction
            fc.cb["update"]("training_run", R(id=3, ticket_id=1, status="running"), R(id=3, ticket_id=1, status="done"), {})
            fc.cb["insert"]("event_log", R(id=99, type="x"), {})  # unrelated noise
        await settle(*[x[2] for x in buses])
        for bus, *_ in buses:
            await bus.stop()

    asyncio.run(go())
    assert sum(len(r.calls) for *_x, r in buses) == 1
    assert sum(b.stats["lost"] for _bus, _fc, b, _r in buses) == 2
    assert [c[0] for c in db.calls].count("agent_resume") == 3 and len(db.ev(rb.EV_DELIVERED)) == 1


def test_one_bridge_claims_a_wait_once_despite_repeated_events():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db)
    s = session(pred={"table": "episode", "ticket_id": 1, "agg": "count", "op": ">=", "value": 1})
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "episode": [], "ticket": [R(id=1)]})
        for i in range(1, 6):
            fc.cb["insert"]("episode", R(id=i, ticket_id=1), {})
        await settle(b)
        b.resync("poll")
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert len(res.calls) == 1 and [c[0] for c in db.calls].count("agent_resume") == 1


def test_protocol_b_restart_records_new_session():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db, resumer=Tdm27Resumer(result={"new_factory_session_id": "fs-new"}))
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    ups = [c for c in db.calls if c[0] == "upsert_agent_session"]
    assert ups and ups[0][3] == "fs-new" and ups[0][4] == "resuming"
    assert db.sessions[1]["ids"] == ["fs-new"]
    (d,) = db.ev(rb.EV_DELIVERED)
    assert d["mode"] == "restart" and d["factory_session_id"] == "fs-new" and d["previous_factory_session_id"] == "fs-1"


def test_unknown_ticket_and_unknown_table_log_errors_not_crash():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db, schema={"agent_session", "dataset", "ticket", "event_log"})
    s1 = session(id=1, pred={"table": "dataset", "ticket_id": 999, "field": "accepted_count", "op": ">=", "value": 2})
    s2 = session(id=2, pred={"table": "no_such_table", "id": 1})
    s3 = session(id=3, pred=TR_DONE)
    s3["waiting_for_json"] = "{not json"
    sync_db(db, [s1, s2, s3])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s1, s2, s3], "dataset": [R(id=1, ticket_id=1, accepted_count=9)], "ticket": [R(id=1)]})
        await settle(b)
        fc.cb["update"]("dataset", R(id=1, ticket_id=1, accepted_count=9), R(id=1, ticket_id=1, accepted_count=10), {})
        fc.cb["insert"]("dataset", R(id=2, ticket_id=5, accepted_count=10), {})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert not res.calls
    errs = {p["session_id"]: p["error"] for p in db.ev(rb.EV_WAIT_ERROR)}
    assert errs[1] == "ticket 999 does not exist" and "no such table: no_such_table" in errs[2]
    assert "not valid JSON" in errs[3]
    assert len(db.ev(rb.EV_WAIT_ERROR)) == 3  # once per wait, not once per event
    assert set(b.waiting) == {1, 2, 3}  # still waiting


def test_side_subscription_for_tables_not_on_the_bus():
    db = Tdm27AgentDB()
    side_clients: list[Tdm27Client] = []

    def side_factory(bus, queries):
        c = Tdm27Client()
        c.subscribe(queries)
        side_clients.append(c)
        return c

    old = rb.make_side_client
    rb.make_side_client = side_factory
    try:
        bus, fc, b, res = make_bridge(db)
        s = session(pred={"table": "contributor_presence", "contributor_id": 4, "field": "page", "value": "tasks"})
        sync_db(db, [s])

        async def go():
            await bus.start()
            fc.snapshot({"agent_session": [s], "ticket": [R(id=1)]})
            await settle(b)
            for _ in range(50):
                if side_clients and "snapshot" in side_clients[-1].cb:
                    break
                await asyncio.sleep(0.02)
            sc = side_clients[-1]
            assert sc.queries == ["SELECT * FROM contributor_presence"]
            sc.snapshot({"contributor_presence": [R(id=4, contributor_id=4, page="home")]})
            await settle(b)
            assert not res.calls
            sc.cb["update"]("contributor_presence", R(id=4, contributor_id=4, page="home"),
                            R(id=4, contributor_id=4, page="tasks"), {})
            await settle(b)
            await bus.stop()

        asyncio.run(go())
    finally:
        rb.make_side_client = old
    assert len(res.calls) == 1 and "page: 'home' -> 'tasks'" in res.calls[0][1]


def test_no_resumer_never_claims():
    db = Tdm27AgentDB()
    reg, fc = Registry(), Tdm27Client()
    rb_handlers.register(reg)
    bus = Bus(reg, fc, workers=2)
    b = rb.ResumeBridge(bus, writer_factory=db.writer, resumer_factory=lambda: None, poll_s=0)
    rb._instances[id(bus)] = b
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert not [c for c in db.calls if c[0] == "agent_resume"] and db.sessions[1]["status"] == "waiting_for"


def test_resume_failure_pauses_and_logs():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db, resumer=Tdm27Resumer(fail=10))
    s = session(pred=TR_DONE)
    sync_db(db, [s])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert len(res.calls) == 3  # first try + 2 retries
    assert db.sessions[1]["status"] == "paused" and "factory down" in db.sessions[1]["current_step"]
    assert db.ev(rb.EV_FAILED) and not db.ev(rb.EV_DELIVERED)


def test_poll_resync_and_orphan_recovery():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db, orphan_s=60)
    old_us = NOW_US() - 300_000_000
    orphan = session(id=5, status="resuming", pred=None, last_update=old_us)
    done_one = session(id=6, status="resuming", pred=None, last_update=old_us)
    unseen = session(id=7, pred=TR_DONE)
    sync_db(db, [orphan, done_one, unseen])
    evs = [R(id=10, type="agent.resumed", payload_json=json.dumps({"session_id": 5, "mode": "native", "reason": "r",
                                                                     "satisfied_predicate": TR_DONE})),
           R(id=11, type="agent.resumed", payload_json=json.dumps({"session_id": 6, "mode": "native"})),
           R(id=12, type=rb.EV_DELIVERED, payload_json=json.dumps({"session_id": 6}))]

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [orphan, done_one], "event_log": evs,
                     "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        bus.mirror["agent_session"][7] = unseen  # a row whose event we never got
        b.resync("poll")
        await settle(b)
        b.resync("poll")  # second poll: nothing new
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert sorted(c[0] for c in res.calls) == [5, 7]
    rec = [p for p in db.ev(rb.EV_DELIVERED) if p.get("recovered")]
    assert len(rec) == 1 and rec[0]["session_id"] == 5
    assert next(p for p in db.ev(rb.EV_DELIVERED) if p["session_id"] == 7)["changed"] == "found true on the safety poll"


def test_wait_again_after_resume_is_a_new_claim():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db)
    w1 = session(pred=TR_DONE, last_update=NOW_US() - 5_000_000)
    sync_db(db, [w1])

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [w1], "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        r = session(status="running", pred=None)
        fc.cb["update"]("agent_session", w1, r, {})
        w2 = session(pred={"table": "training_run", "id": 3, "field": "status", "value": "done"}, last_update=NOW_US())
        db.sessions[1].update(status="waiting_for")
        fc.cb["update"]("agent_session", r, w2, {})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    assert len(res.calls) == 2


# --------------------------------------------------------------------------- TDM-30 client shape

class Tdm27TurnResult:
    def __init__(self, protocol, fsid, status="waiting_for", ok=True):
        self.protocol, self.factory_session_id, self.status, self.ok = protocol, fsid, status, ok


class Tdm27AsyncClient:
    """Shaped like rdf_factory.FactoryClient.resume: async, keyword-only claim/reason, TurnResult."""

    def __init__(self, protocol="native_resume", fsid="fs-1", exc=None):
        self.calls, self.protocol, self.fsid, self.exc = [], protocol, fsid, exc
        self.loops = set()

    async def resume(self, agent_session_id, text, *, reason="", claim=True, finish_on_idle=False):
        self.loops.add(id(asyncio.get_running_loop()))
        self.calls.append((agent_session_id, text, reason, claim))
        if self.exc:
            raise self.exc
        return Tdm27TurnResult(self.protocol, self.fsid)


def _run_one(client, pred=TR_DONE, n_sessions=1):
    db = Tdm27AgentDB()
    reg, fc = Registry(), Tdm27Client()
    rb_handlers.register(reg)
    bus = Bus(reg, fc, workers=2)
    b = rb.ResumeBridge(bus, writer_factory=db.writer, resumer_factory=lambda: client, poll_s=0, retry_delays=(0.01,))
    rb._instances[id(bus)] = b
    ss = [session(id=i, pred=pred) for i in range(1, n_sessions + 1)]
    sync_db(db, ss)

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": ss, "training_run": [R(id=3, ticket_id=1, status="done")], "ticket": [R(id=1)]})
        await settle(b)
        await bus.stop()

    asyncio.run(go())
    return db, b


def test_async_tdm30_client_native_gets_claim_false_and_one_loop():
    client = Tdm27AsyncClient()
    db, _b = _run_one(client, n_sessions=3)
    assert len(client.calls) == 3 and all(c[3] is False for c in client.calls)  # bridge already claimed
    assert all(c[2].startswith("predicate true: training_run.status == 'done'") for c in client.calls)
    assert len(client.loops) == 1  # every call on the same private loop
    assert not client.calls[0][1].startswith("[resume-bridge]")  # the client adds the prefix
    d = db.ev(rb.EV_DELIVERED)
    assert len(d) == 3 and {p["mode"] for p in d} == {"native"} and d[0]["turn_status"] == "waiting_for"
    assert len(db.ev(rb.EV_DELIVERING)) == 3


def test_async_tdm30_client_restart_is_recorded_by_the_client_not_the_bridge():
    client = Tdm27AsyncClient(protocol="restart", fsid="fs-b")
    db, _b = _run_one(client)
    assert not [c for c in db.calls if c[0] == "upsert_agent_session"]
    (d,) = db.ev(rb.EV_DELIVERED)
    assert d["mode"] == "restart" and d["factory_session_id"] == "fs-b"


def test_non_transport_error_is_not_retried_and_conflict_is_not_paused():
    class ResumeConflict(RuntimeError):
        pass

    client = Tdm27AsyncClient(exc=ResumeConflict("agent_session 1 is running"))
    db, _b = _run_one(client)
    assert len(client.calls) == 1 and db.sessions[1]["status"] == "resuming"  # not paused
    assert db.ev(rb.EV_FAILED)
    client = Tdm27AsyncClient(exc=ValueError("bad"))
    db, _b = _run_one(client)
    assert len(client.calls) == 1 and db.sessions[1]["status"] == "paused"


def test_orphan_closed_by_delivering_event_is_left_alone():
    db = Tdm27AgentDB()
    bus, fc, b, res = make_bridge(db, orphan_s=60)
    s = session(id=5, status="resuming", pred=None, last_update=NOW_US() - 300_000_000)
    sync_db(db, [s])
    evs = [R(id=10, type="agent.resumed", payload_json=json.dumps({"session_id": 5, "mode": "native"})),
           R(id=11, type=rb.EV_DELIVERING, payload_json=json.dumps({"session_id": 5}))]

    async def go():
        await bus.start()
        fc.snapshot({"agent_session": [s], "event_log": evs})
        await settle(b)
        assert b.recover_orphans() == 0
        await bus.stop()

    asyncio.run(go())
    assert not res.calls


def test_tdm30_parsed_waiting_for_shapes():
    tables = {"dataset": [R(id=1, ticket_id=7, accepted_count=5)]}
    # WAITING_FOR dataset:accepted_count>=5 as stored by rdf_factory.parse_waiting_for(ticket_id=7)
    p = rb.parse_predicate({"table": "dataset", "field": "accepted_count", "op": ">=", "value": 5,
                            "raw": "dataset:accepted_count>=5", "ticket_id": 7})
    assert p.filters == (("ticket_id", 7),) and rb.evaluate(p, rows_from(tables)).ok
    with pytest.raises(rb.PredicateError, match="free-text"):
        rb.parse_predicate({"table": "dataset", "predicate": "dataset has 5 accepted", "raw": "dataset has 5 accepted",
                            "ticket_id": 7})
