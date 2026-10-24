"""TDM-31 wait-for protocol, end to end against fakes (no network, no credits).

One chain per predicate form:
  droid turn text -> rdf_factory.parse_waiting_for -> FactoryClient mirror -> agent_wait_for
  -> the TDM-27 ResumeBridge (real class) evaluates on each table change -> agent_resume claim
  -> a second FactoryClient (the bridge's resumer) runs the droid's next turn with the bridge's message.

``Tdm31World`` is one in-memory SpacetimeDB for all of it: the TDM-13 agent_session reducers plus the
few game reducers the triggers need (submit_review bumps dataset.accepted_count, set_ticket_status,
set_training_status). Every row change is pushed to the bridge the way the bus would (session_changed /
table_changed). The droid is a scripted backend whose second reply echoes what it was told, so the test
checks the message the droid actually received: predicate, resume_payload and the ticket snapshot.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_common.spacetime_client import SpacetimeError
from rdf_factory import FactoryClient, FactoryConfig
from rdf_factory.backends import RawTurn

from rdf_orchestrator import resume_bridge as rb
from rdf_orchestrator.agents_wait_api import agents_wait_router, get_wait_stdb

NOW_US = lambda: time.time_ns() // 1000  # noqa: E731
TOKEN = "tok-31-payload"


# --------------------------------------------------------------------------- fakes

class Tdm31World:
    def __init__(self):
        self.t: dict[str, dict[int, dict]] = {k: {} for k in rb.BUS_TABLES}
        self.events: list[tuple[str, str, dict]] = []
        self.lock = threading.RLock()
        self.bridge: rb.ResumeBridge | None = None
        self.next_id = 100

    # bus surface for the bridge
    @property
    def registry(self):
        return type("Reg", (), {"tables": {k: object() for k in rb.BUS_TABLES}})()

    client = None

    def rows(self, table):
        with self.lock:
            return [dict(r) for r in self.t.get(table, {}).values()]

    # seeds
    def put(self, table, **row):
        with self.lock:
            self.t[table][row["id"]] = row

    def _notify(self, table, old, new):
        b = self.bridge
        if b is None:
            return
        if table == "agent_session":
            b.session_changed(dict(new), rb.Change(table, "update", new, old, NOW_US()))
        b.table_changed(table, rb.Change(table, "update", dict(new), old, NOW_US()))

    def _update(self, table, id_, **kw):
        with self.lock:
            old = dict(self.t[table][id_])
            self.t[table][id_].update(kw)
            new = dict(self.t[table][id_])
        self._notify(table, old, new)

    # reducers (sync; the async wrapper below serves FactoryClient and the HTTP route)
    def reduce(self, name, *args):
        if name == "upsert_agent_session":
            id_, role, fsid, status, step, nm, ticket = args
            if id_ == 0:
                with self.lock:
                    self.next_id += 1
                    self.t["agent_session"][self.next_id] = dict(
                        id=self.next_id, role=role, name=nm or role, ticket_id=ticket, factory_session_id=fsid or None,
                        factory_session_ids_json=json.dumps([fsid] if fsid else []), status=status, current_step=step,
                        waiting_for_json=None, resume_payload_json=None, last_update=NOW_US())
                return
            r = self.t["agent_session"][id_]
            if status in ("waiting_for", "resuming") and r["status"] != status:
                raise SpacetimeError(530, f"cannot enter {status}", reducer=name)
            ids = json.loads(r["factory_session_ids_json"])
            if fsid and fsid not in ids:
                ids.append(fsid)
            self._update("agent_session", id_, status=status, current_step=step,
                         factory_session_id=fsid or r["factory_session_id"], factory_session_ids_json=json.dumps(ids),
                         waiting_for_json=r["waiting_for_json"] if status == "waiting_for" else None,
                         last_update=NOW_US())
        elif name == "agent_wait_for":
            sid, pred, payload = args
            p = json.loads(pred)
            assert isinstance(p, dict) and ("table" in p or "all" in p or "any" in p)
            if self.t["agent_session"][sid]["status"] in ("done", "failed"):
                raise SpacetimeError(530, f"session {sid} is terminal", reducer=name)
            self._update("agent_session", sid, status="waiting_for", waiting_for_json=pred,
                         resume_payload_json=payload or None, last_update=NOW_US())
        elif name == "agent_resume":
            sid, _new, reason = args
            with self.lock:
                r = self.t["agent_session"][sid]
                if r["status"] not in ("waiting_for", "paused"):
                    raise SpacetimeError(530, f"session {sid} is {r['status']}, not waiting_for/paused", reducer=name)
                sat = r["waiting_for_json"]
            self._update("agent_session", sid, status="resuming", waiting_for_json=None, last_update=NOW_US())
            self.events.append(("factory", "agent.resumed", {"session_id": sid, "reason": reason,
                                                             "satisfied_predicate": json.loads(sat) if sat else None}))
        elif name == "agent_step":
            sid, text = args
            r = self.t["agent_session"][sid]
            if r["status"] in ("done", "failed"):
                raise SpacetimeError(530, "terminal", reducer=name)
            st = "running" if r["status"] in ("starting", "resuming") else r["status"]
            self._update("agent_session", sid, status=st, current_step=text, last_update=NOW_US())
        elif name == "append_event":
            actor, type_, payload = args
            self.events.append((actor, type_, json.loads(payload)))
        elif name == "submit_review":
            eid, accepted = args[0], args[1]
            ep = self.t["episode"][eid]
            self._update("episode", eid, review_status="accepted" if accepted else "rejected")
            if accepted:
                ds = next(d for d in self.t["dataset"].values() if d["ticket_id"] == ep["ticket_id"])
                self._update("dataset", ds["id"], accepted_count=ds["accepted_count"] + 1)
        elif name == "set_ticket_status":
            self._update("ticket", args[0], status=args[1])
        elif name == "set_training_status":
            self._update("training_run", args[0], status=args[1])
        else:
            raise AssertionError(f"unexpected reducer {name}")

    async def call_reducer(self, name, *args):
        self.reduce(name, *args)

    async def sql(self, q):
        m = re.search(r"FROM agent_session WHERE id = (\d+)", q)
        if m:
            r = self.t["agent_session"].get(int(m.group(1)))
            return [dict(r)] if r else []
        m = re.search(r"name = '(.*)'", q)
        return [dict(r) for r in self.t["agent_session"].values() if r["name"] == m.group(1)]

    async def aclose(self):
        pass

    def writer(self):
        world = self

        class W:
            def call_reducer(self, name, *args):
                world.reduce(name, *args)
        return W()

    def ev(self, type_):
        return [p for _, t, p in self.events if t == type_]

    def session(self, sid):
        with self.lock:
            return dict(self.t["agent_session"][sid])


class Tdm31Droid:
    """Scripted droid. Turn 1 parks with the given final line; later turns echo the message they got."""

    mode = "local"

    def __init__(self, park_line: str):
        self.park_line = park_line
        self.prompts: list[tuple[str | None, str]] = []

    async def run_turn(self, spec, fsid, text, emit, on_session_id):
        self.prompts.append((fsid, text))
        if not fsid:
            fsid = "fs-31"
            await on_session_id(fsid)
        if len(self.prompts) == 1:
            reply = (f"Inspected the ticket.\nRESUME_PAYLOAD {{\"done\": \"inspected\", \"next\": \"assemble\", "
                     f"\"token\": \"{TOKEN}\"}}\n{self.park_line}")
        else:
            m = re.search(r'"token": "([^"]+)"', text)
            reply = (f"Resumed. Payload token {m.group(1) if m else 'MISSING'}; next was assemble."
                     f"{' Timed out.' if 'TIMED OUT' in text else ''}\nDONE")
        emit("assistant_text", reply, chars=len(reply))
        return RawTurn(fsid, "success", True, reply, credits=10.0, seconds=0.1)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


def seed(world: Tdm31World) -> None:
    world.put("ticket", id=1, status="review", iteration=0, target_success=0.8)
    world.put("dataset", id=1, ticket_id=1, accepted_count=0, total_count=3, lerobot_uri="")
    for e in (11, 12, 13):
        world.put("episode", id=e, ticket_id=1, review_status="pending")
    world.put("training_run", id=5, ticket_id=1, iteration=0, status="running")
    world.put("ticket", id=2, status="train", iteration=0)  # another ticket: must never satisfy ticket 1's waits
    world.put("dataset", id=2, ticket_id=2, accepted_count=9, total_count=9, lerobot_uri="")


def make(tmp_path, park_line):
    world = Tdm31World()
    seed(world)
    droid = Tdm31Droid(park_line)
    cfg = FactoryConfig(state_dir=tmp_path / "state", work_root=tmp_path / "sessions", repo_dir=tmp_path,
                        heartbeat_s=30)
    starter = FactoryClient(cfg, stdb=world, backends={"local": droid})
    resumer = FactoryClient(cfg, stdb=world, backends={"local": droid})  # the bridge's own client
    bridge = rb.ResumeBridge(world, writer_factory=world.writer, resumer_factory=lambda: resumer, poll_s=0,
                             retry_delays=(0.01,), default_timeout_s=0, bridge_id="bridge-tdm31")
    world.bridge = bridge
    return world, droid, starter, bridge


async def until(pred, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def no_wake_yet(world, bridge, sid):
    time.sleep(0.05)
    return world.session(sid)["status"] == "waiting_for" and not world.ev("agent.resumed")


# --------------------------------------------------------------------------- one test per predicate form

def _review(*eps):
    return [("submit_review", e, True, "ok", "{}") for e in eps]


FORMS = {
    "text": ("WAITING_FOR dataset:accepted_count>=1", [], _review(11), "dataset.accepted_count >= 1"),
    "json_all": ('WAITING_FOR {"all":[{"table":"dataset","field":"accepted_count","op":">=","value":1},'
                 '{"table":"ticket","field":"status","value":"train"}]}',
                 _review(11), [("set_ticket_status", 1, "train", "")], "ticket.status == 'train'"),
    "json_any": ('WAITING_FOR {"any":[{"table":"training_run","id":5,"field":"status","value":"done"},'
                 '{"table":"training_run","id":5,"field":"status","value":"failed"}]}',
                 [], [("set_training_status", 5, "failed")], "training_run.status == 'failed'"),
    "json_agg_where": ('WAITING_FOR {"table":"episode","agg":"count","where":{"review_status":"accepted"},'
                       '"op":">=","value":2}', _review(11), _review(12), "count(episode) >= 2"),
    "text_ticket_table": ("WAITING_FOR ticket:status==train", [], [("set_ticket_status", 1, "train", "")],
                          "ticket.status == 'train'"),
}


@pytest.mark.parametrize("form", list(FORMS))
def test_wait_form_end_to_end(tmp_path, form):
    line, not_enough, trigger, expect = FORMS[form]

    async def go():
        world, droid, starter, bridge = make(tmp_path, line)
        r1 = await starter.start_session("data_droid", "assemble the dataset", ticket_id=1, name=f"tdm31-{form}")
        sid = r1.agent_session_id
        assert r1.status == "waiting_for", r1
        row = world.session(sid)
        assert row["status"] == "waiting_for" and json.loads(row["resume_payload_json"])["token"] == TOKEN
        assert sid in bridge.waiting and bridge.waiting[sid].pred is not None, bridge.health()
        for call in not_enough:  # a partial change must not wake it
            world.reduce(*call)
        assert no_wake_yet(world, bridge, sid), world.events
        for call in trigger:
            world.reduce(*call)
        assert await until(lambda: world.session(sid)["status"] == "done" and world.ev(rb.EV_DELIVERED)), \
            (world.session(sid), world.events)
        msg = droid.prompts[-1][1]
        assert msg.startswith("[resume-bridge] The condition you were waiting for is now true")
        assert expect in msg, msg
        assert f'"token": "{TOKEN}"' in msg  # the saved resume_payload is handed back
        assert "Current SpacetimeDB snapshot for ticket 1:" in msg and '"ticket":{"status":' in msg
        assert f"Payload token {TOKEN}" in droid_reply(world)
        assert len(world.ev("agent.resumed")) == 1
        (d,) = world.ev(rb.EV_DELIVERED)
        assert d["mode"] == "native" and d["timed_out"] is False and d["session_id"] == sid
        await starter.aclose()
        bridge.stop()

    asyncio.run(go())


def droid_reply(world):
    texts = [p["data"].get("full_text") or p["text"] for _, t, p in world.events if t == "droid.assistant_text"]
    return texts[-1] if texts else ""


def test_ticket_scope_ignores_other_tickets(tmp_path):
    """Ticket 2 already has accepted_count 9 and status train: ticket 1's droid keeps waiting."""
    async def go():
        world, droid, starter, bridge = make(tmp_path, "WAITING_FOR dataset:accepted_count>=5")
        r1 = await starter.start_session("data_droid", "x", ticket_id=1, name="tdm31-scope")
        assert json.loads(world.session(r1.agent_session_id)["waiting_for_json"])["ticket_id"] == 1
        assert bridge.check(r1.agent_session_id, rb.Change("dataset", "poll", None, None)) == "false"
        await starter.aclose()

    asyncio.run(go())


@pytest.mark.parametrize("line,why", [
    ("WAITING_FOR dataset:accepted_count>=5 timeout=1s", "Now: dataset.accepted_count >= 5"),
    ("WAITING_FOR the dataset looks ready timeout=1s", "could not be evaluated"),  # free text: only the deadline
])
def test_timeout_wakes_the_droid(tmp_path, line, why):
    async def go():
        world, droid, starter, bridge = make(tmp_path, line)
        r1 = await starter.start_session("data_droid", "x", ticket_id=1, name="tdm31-timeout")
        sid = r1.agent_session_id
        w = bridge.waiting[sid]
        assert w.deadline_us and w.timeout_s() == 1.0
        bridge.resync("poll")
        assert no_wake_yet(world, bridge, sid)
        await asyncio.sleep(1.05)
        bridge.resync("poll")  # the safety poll is what notices a deadline
        assert await until(lambda: world.session(sid)["status"] == "done" and world.ev(rb.EV_DELIVERED)), world.events
        msg = droid.prompts[-1][1]
        assert msg.startswith("[resume-bridge] TIMED OUT:") and why in msg, msg
        assert f'"token": "{TOKEN}"' in msg and "snapshot for ticket 1" in msg
        assert "Timed out." in droid_reply(world)
        (t,) = world.ev(rb.EV_TIMED_OUT)
        assert t["session_id"] == sid and t["timeout_s"] == 1.0
        (d,) = world.ev(rb.EV_DELIVERED)
        assert d["timed_out"] is True
        assert world.ev("agent.resumed")[0]["reason"].startswith("timed out after")
        await starter.aclose()

    asyncio.run(go())


def test_default_timeout_and_opt_out(tmp_path):
    world = Tdm31World()
    b = rb.ResumeBridge(world, writer_factory=world.writer, resumer_factory=lambda: None, poll_s=0,
                        default_timeout_s=60)
    gen = NOW_US()
    assert b._deadline({"table": "dataset"}, gen) == gen + 60_000_000
    assert b._deadline({"table": "dataset", "timeout_s": 0}, gen) is None
    assert b._deadline({"table": "dataset", "timeout_s": 5}, gen) == gen + 5_000_000
    assert b._deadline({"all": [{"table": "x"}], "deadline_us": gen + 7}, gen) == gen + 7
    assert rb.ResumeBridge(world, poll_s=0).default_timeout_s == rb.DEFAULT_TIMEOUT_S
    # timeout_s / deadline_us are wait metadata, never row filters
    leaf = rb.parse_predicate({"table": "dataset", "field": "accepted_count", "op": ">=", "value": 1, "timeout_s": 9})
    assert leaf.filters == ()


# --------------------------------------------------------------------------- POST /agents/{id}/wait

def _app(world):
    app = FastAPI()
    app.include_router(agents_wait_router)

    async def dep():
        return world
    app.dependency_overrides[get_wait_stdb] = dep
    return TestClient(app)


def test_wait_route_parks_and_the_bridge_wakes_it(tmp_path):
    """An operator parks a droid whose turn ended paused (no final line); a review then wakes it."""
    async def go():
        world, droid, starter, bridge = make(tmp_path, "I looked around but forgot the status line.")
        r1 = await starter.start_session("data_droid", "x", ticket_id=1, name="tdm31-route")
        sid = r1.agent_session_id
        assert r1.status == "paused"
        http = _app(world)
        resp = http.post(f"/agents/{sid}/wait", json={"predicate": "dataset:accepted_count>=1", "timeout_s": 600,
                                                      "resume_payload": {"next": "assemble", "token": TOKEN}})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["predicate"]["ticket_id"] == 1 and body["predicate"]["timeout_s"] == 600
        assert body["describe"] == "dataset.accepted_count >= 1 [ticket_id=1]"
        assert world.session(sid)["status"] == "waiting_for" and bridge.waiting[sid].timeout_s() == 600
        world.reduce("submit_review", 11, True, "ok", "{}")
        assert await until(lambda: world.session(sid)["status"] == "done"), world.events
        assert f'"token": "{TOKEN}"' in droid.prompts[-1][1]
        await starter.aclose()

    asyncio.run(go())


def test_wait_route_errors(tmp_path):
    world = Tdm31World()
    seed(world)
    world.reduce("upsert_agent_session", 0, "data_droid", "", "running", "s", "a", 1)
    world.reduce("upsert_agent_session", 0, "data_droid", "", "done", "s", "b", 1)
    live, done = sorted(world.t["agent_session"])
    http = _app(world)
    assert http.post("/agents/999/wait", json={"predicate": "dataset:accepted_count>=1"}).status_code == 404
    assert http.post(f"/agents/{done}/wait", json={"predicate": "dataset:accepted_count>=1"}).status_code == 409
    r = http.post(f"/agents/{live}/wait", json={"predicate": "the dataset is ready"})
    assert r.status_code == 422 and "free-text" in r.text
    r = http.post(f"/agents/{live}/wait", json={"predicate": {"table": "dataset", "agg": "median", "value": 1}})
    assert r.status_code == 422 and "agg" in r.text
    assert world.session(live)["status"] == "running"  # nothing written on a refusal
    r = http.post(f"/agents/{live}/wait", json={"predicate": {"any": [
        {"table": "ticket", "field": "status", "value": "train"}, {"table": "qa_result", "field": "passed", "value": True}]},
        "resume_payload": "free text note"})
    assert r.status_code == 200, r.text
    p = json.loads(world.session(live)["waiting_for_json"])
    assert p["any"][0]["id"] == 1 and "ticket_id" not in p["any"][1]  # ticket keyed by id; qa_result has no ticket
    assert json.loads(world.session(live)["resume_payload_json"]) == {"note": "free text note"}
