"""Bus runtime tests against a fake SpacetimeDB client (no network)."""
from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

from rdf_orchestrator.app import create_app
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.stdb import Row, TableSchema, _col_kind


class FakeClient:
    def __init__(self):
        self.state, self.identity, self.queries, self.cb = "stopped", "0xfake", [], {}
        self.calls = []
        self._stop = asyncio.Event()

    def subscribe(self, q): self.queries = q
    def on_snapshot(self, cb): self.cb["snapshot"] = cb
    def on_insert(self, cb): self.cb["insert"] = cb
    def on_update(self, cb): self.cb["update"] = cb
    def on_delete(self, cb): self.cb["delete"] = cb
    def on_state(self, cb): self.cb["state"] = cb
    def key(self, table, row): return row["id"]
    async def run(self): await self._stop.wait()
    def stop(self): self._stop.set()
    async def call_reducer(self, r, a): self.calls.append((r, a)); return 1

    # drive it like the server would
    def snapshot(self, tables):
        self.cb["state"]("live", None)
        self.cb["snapshot"]({t: [Row(r) for r in rows] for t, rows in tables.items()})


def R(**kw):
    return Row(kw)


async def _settle(bus: Bus, n=50):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if bus.health()["inflight"] == 0:
            return


def make():
    reg, fc = Registry(), FakeClient()
    return reg, fc, Bus(reg, fc, workers=2)


def test_where_insert_update_enter_and_existing():
    reg, fc, bus = make()
    seen: list = []

    @reg.on_insert("training_run", where=lambda r: r.status == "queued")
    def ins(ev): seen.append(("ins", ev.row.id, ev.source, threading.current_thread().name.startswith("handler")))

    @reg.on_enter("training_run", where=lambda r: r.status == "queued")
    async def enter(ev): seen.append(("enter", ev.row.id, ev.source))

    @reg.on_update("training_run")
    def upd(ev): seen.append(("upd", ev.row.id, ev.old.status, ev.row.status))

    async def go():
        await bus.start()
        assert fc.queries == ["SELECT * FROM training_run"]
        fc.snapshot({"training_run": [R(id=1, ticket_id=7, status="queued"), R(id=2, ticket_id=7, status="done")]})
        await _settle(bus)
        fc.cb["insert"]("training_run", R(id=3, ticket_id=7, status="queued"), {})
        fc.cb["insert"]("training_run", R(id=4, ticket_id=7, status="running"), {})
        fc.cb["update"]("training_run", R(id=4, ticket_id=7, status="running"), R(id=4, ticket_id=7, status="queued"), {})
        fc.cb["update"]("training_run", R(id=4, ticket_id=7, status="queued"), R(id=4, ticket_id=7, status="queued"), {})
        await _settle(bus)
        await bus.stop()

    asyncio.run(go())
    # include_existing: enter fires for the pre-existing queued row, plain on_insert does not
    assert ("enter", 1, "snapshot") in seen and not any(s[0] == "ins" and s[1] == 1 for s in seen)
    assert ("ins", 3, "live", True) in seen  # sync handler ran on the worker pool
    assert ("enter", 3, "live") in seen
    assert ("enter", 4, "live") in seen  # running -> queued is an entry
    assert sum(1 for s in seen if s[:2] == ("enter", 4)) == 1  # queued -> queued is not
    assert ("upd", 4, "running", "queued") in seen
    assert {r.id for r in bus.rows("training_run")} == {1, 2, 3, 4}


def test_resync_diff_after_reconnect():
    reg, fc, bus = make()
    seen: list = []
    reg.on_insert("event_log")(lambda ev: seen.append(("ins", ev.row.id, ev.source)))
    reg.on_update("event_log")(lambda ev: seen.append(("upd", ev.row.id, ev.source)))
    reg.on_delete("event_log")(lambda ev: seen.append(("del", ev.row.id, ev.source)))

    async def go():
        await bus.start()
        fc.snapshot({"event_log": [R(id=1, v="a"), R(id=2, v="b")]})
        fc.cb["state"]("reconnecting", "ConnectionClosed")
        fc.snapshot({"event_log": [R(id=2, v="B"), R(id=3, v="c")]})  # 1 gone, 2 changed, 3 new
        await _settle(bus)
        h = bus.health()
        await bus.stop()
        return h

    h = asyncio.run(go())
    assert sorted(seen) == [("del", 1, "resync"), ("ins", 3, "resync"), ("upd", 2, "resync")]
    assert h["subscription"]["reconnects"] == 1 and h["status"] == "ok"


def test_handler_errors_counted_and_healthz():
    reg, fc = Registry(), FakeClient()

    @reg.on_insert("ticket", name="boom")
    def boom(ev): raise ValueError("nope")

    app = create_app(client=fc, registry=reg, workers=1)
    with TestClient(app) as c:
        assert c.get("/healthz").json()["subscription"]["state"] == "stopped"
        bus = app.state.bus
        fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), bus._loop)
        fut.result(2)
        bus._loop.call_soon_threadsafe(fc.snapshot, {"ticket": []})
        bus._loop.call_soon_threadsafe(fc.cb["insert"], "ticket", R(id=9, status="new"), {})
        for _ in range(100):
            h = c.get("/healthz").json()
            if h["handler_errors"]:
                break
            threading.Event().wait(0.01)
    assert h["status"] == "ok"
    assert h["handlers"][0]["name"] == "boom" and h["handlers"][0]["errors"] == 1
    assert "ValueError" in h["handlers"][0]["last_error"]
    assert h["last_event"]["table"] == "ticket"


def test_row_normalisation_both_wire_shapes():
    ts = {"Product": {"elements": [{"name": {"some": "__timestamp_micros_since_unix_epoch__"}, "algebraic_type": {"I64": []}}]}}
    opt_ts = {"Sum": {"variants": [{"name": {"some": "some"}, "algebraic_type": ts}, {"name": {"some": "none"}, "algebraic_type": {"Product": {"elements": []}}}]}}
    s = TableSchema("task", ["id", "created_at", "claimed_at"], [_col_kind({"U64": []}), _col_kind(ts), _col_kind(opt_ts)], ["id"])
    obj = s.normalise({"id": 5, "created_at": {"__timestamp_micros_since_unix_epoch__": 10}, "claimed_at": {"some": {"__timestamp_micros_since_unix_epoch__": 11}}})
    pos = s.normalise([5, [10], [0, [11]]])
    assert obj == pos == {"id": 5, "created_at": 10, "claimed_at": 11}
    assert s.normalise([5, [10], [1, []]]).claimed_at is None
    assert s.normalise({"id": 5, "created_at": [10], "claimed_at": {"none": []}})["claimed_at"] is None
    assert s.key(pos) == 5
