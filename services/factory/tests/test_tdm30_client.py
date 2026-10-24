"""TDM-30 unit tests: mirroring, WAITING_FOR parsing, resume protocols A/B, cloud API quirks.

No network, no credits: SpacetimeDB is a fake that mimics the TDM-13 reducer rules, droids are a
scripted backend, and the cloud backend talks to an httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

from rdf_common.spacetime_client import SpacetimeError
from rdf_factory import FactoryClient, FactoryConfig, ResumeConflict, parse_waiting_for
from rdf_factory.backends import CloudBackend, RawTurn, SessionGone, SessionSpec, _command_allowed, _inside


class FakeStdb:
    """Just enough of the TDM-13 agent_session reducers + append_event."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.events: list[tuple[str, str, dict]] = []
        self.next_id = 1

    async def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name == "upsert_agent_session":
            id_, role, fsid, status, step, nm, ticket = args
            if id_ == 0:
                self.rows[self.next_id] = dict(id=self.next_id, role=role, name=nm or role, ticket_id=ticket,
                                               factory_session_id=fsid or None,
                                               factory_session_ids_json=json.dumps([fsid] if fsid else []),
                                               status=status, current_step=step, waiting_for_json=None,
                                               resume_payload_json=None)
                self.next_id += 1
                return
            r = self.rows[id_]
            if status in ("waiting_for", "resuming") and r["status"] != status:
                raise SpacetimeError(530, f"cannot enter {status}", reducer=name)
            ids = json.loads(r["factory_session_ids_json"])
            if fsid and fsid not in ids:
                ids.append(fsid)
            r.update(status=status, current_step=step, factory_session_id=fsid or r["factory_session_id"],
                     factory_session_ids_json=json.dumps(ids))
            if status != "waiting_for":
                r["waiting_for_json"] = None
        elif name == "agent_wait_for":
            sid, pred, payload = args
            assert "table" in json.loads(pred)
            self.rows[sid].update(status="waiting_for", waiting_for_json=pred, resume_payload_json=payload or None)
        elif name == "agent_resume":
            sid, new, reason = args
            r = self.rows[sid]
            if r["status"] not in ("waiting_for", "paused"):
                raise SpacetimeError(530, f"session {sid} is {r['status']}", reducer=name)
            r.update(status="resuming", waiting_for_json=None)
        elif name == "agent_step":
            sid, text = args
            r = self.rows[sid]
            if r["status"] in ("done", "failed"):
                raise SpacetimeError(530, "terminal", reducer=name)
            if r["status"] in ("starting", "resuming"):
                r["status"] = "running"
            r["current_step"] = text
        elif name == "append_event":
            actor, type_, payload = args
            self.events.append((actor, type_, json.loads(payload)))

    async def sql(self, q):
        m = re.search(r"id = (\d+)", q)
        if m:
            r = self.rows.get(int(m.group(1)))
            return [dict(r)] if r else []
        m = re.search(r"name = '(.*)'", q)
        return [dict(r) for r in self.rows.values() if r["name"] == m.group(1)]

    async def aclose(self):
        pass


class ScriptedBackend:
    """Replies with canned texts; raises SessionGone for ids in `gone`."""

    mode = "local"

    def __init__(self, replies, gone=()):
        self.replies = list(replies)
        self.gone = set(gone)
        self.prompts: list[tuple[str | None, str]] = []
        self.n = 0

    async def run_turn(self, spec, fsid, text, emit, on_session_id):
        self.prompts.append((fsid, text))
        if fsid in self.gone:
            raise SessionGone(fsid)
        if not fsid:
            self.n += 1
            fsid = f"fs-{self.n}"
            await on_session_id(fsid)
        emit("tool_call", "Read README.md", tool="Read", tool_use_id="t1", input={"file_path": "README.md"})
        emit("tool_result", "Read ok", tool="Read", tool_use_id="t1", is_error=False, preview="# hi")
        reply = self.replies.pop(0)
        emit("assistant_text", reply, chars=len(reply))
        return RawTurn(fsid, "success", True, reply, credits=1500.0, seconds=1.0)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


def make_client(tmp_path, backend, stdb=None):
    cfg = FactoryConfig(state_dir=tmp_path / "state", work_root=tmp_path / "sessions", repo_dir=tmp_path,
                        heartbeat_s=0.2)
    stdb = stdb or FakeStdb()
    return FactoryClient(cfg, stdb=stdb, backends={"local": backend}), stdb


def test_parse_waiting_for_forms():
    p = parse_waiting_for("did stuff\nWAITING_FOR dataset:accepted_count>=5", ticket_id=3)
    assert p == {"table": "dataset", "field": "accepted_count", "op": ">=", "value": 5,
                 "raw": "dataset:accepted_count>=5", "ticket_id": 3}
    p = parse_waiting_for('WAITING_FOR {"table":"episode","op":">","value":1}')
    assert p["table"] == "episode" and p["value"] == 1
    p = parse_waiting_for("WAITING_FOR episodes>=5")
    assert p["table"] == "episodes" and p["predicate"] == "episodes>=5"
    assert parse_waiting_for("all good\nDONE") is None


def test_policy_helpers(tmp_path):
    root = str(tmp_path / "w")
    assert _inside(str(tmp_path / "w" / "a.txt"), [root], str(tmp_path))
    assert not _inside(str(tmp_path / "x.txt"), [root], str(tmp_path))
    assert not _inside(str(tmp_path / "w" / ".." / "x.txt"), [root], str(tmp_path))
    assert _command_allowed("uv run pytest -q", ["uv run pytest"])
    assert not _command_allowed("uv run pytest; rm -rf /", ["uv run pytest"])
    assert not _command_allowed("ls", [])


def test_start_wait_resume_done(tmp_path):
    async def go():
        be = ScriptedBackend(["wrote it\nRESUME_PAYLOAD {\"next\": \"train\"}\nWAITING_FOR dataset:accepted_count>=5",
                              "continued\nDONE"])
        fc, db = make_client(tmp_path, be)
        seen = []
        fc.subscribe(None, seen.append)
        r = await fc.start_session("data_droid", "do the thing", ticket_id=7)
        assert r.status == "waiting_for" and r.factory_session_id == "fs-1"
        row = db.rows[r.agent_session_id]
        assert row["status"] == "waiting_for" and json.loads(row["waiting_for_json"])["ticket_id"] == 7
        assert json.loads(row["resume_payload_json"]) == {"next": "train"}
        assert "[rdf-factory droid contract]" in be.prompts[0][1]

        r2 = await fc.resume(r.agent_session_id, "5 accepted episodes now")
        assert r2.protocol == "native_resume" and r2.status == "done"
        assert be.prompts[1][0] == "fs-1"
        assert be.prompts[1][1].startswith("[resume-bridge] 5 accepted episodes now\n\n(End your reply")
        assert db.rows[r.agent_session_id]["status"] == "done"
        assert ("agent_resume", r.agent_session_id, "", "5 accepted episodes now") in db.calls
        steps = [c[2] for c in db.calls if c[0] == "agent_step"]
        assert "Read README.md" in steps
        types = [t for _, t, _ in db.events]
        assert {"droid.session_started", "droid.tool_call", "droid.waiting_for", "droid.turn_completed"} <= set(types)
        assert all(a == "droid:data" for a, _, _ in db.events)
        assert r2.credits_total == 3000.0
        kinds = [e["kind"] for e in seen]
        assert kinds.count("turn_completed") == 2 and all(e["v"] == 1 for e in seen)
        # a second resume on a done session is refused
        with pytest.raises(ResumeConflict):
            await fc.resume(r.agent_session_id, "again")

    asyncio.run(go())


def test_resume_protocol_b_and_claimed_by_bridge(tmp_path):
    async def go():
        be = ScriptedBackend(["WAITING_FOR dataset:accepted_count>=5", "restarted fine\nDONE"], gone={"fs-1"})
        fc, db = make_client(tmp_path, be)
        r = await fc.start_session("training_droid", "train")
        sid = r.agent_session_id
        # the bridge claims first (TDM-27 calls agent_resume itself), then calls resume()
        await db.call_reducer("agent_resume", sid, "", "bridge claim")
        r2 = await fc.resume(sid, "go")
        assert r2.protocol == "restart" and r2.factory_session_id == "fs-2" and r2.status == "done"
        assert json.loads(db.rows[sid]["factory_session_ids_json"]) == ["fs-1", "fs-2"]
        assert "resume_payload" in be.prompts[-1][1] and be.prompts[-1][0] is None
        assert "droid.session_restarted" in [t for _, t, _ in db.events]

    asyncio.run(go())


def test_resume_lost_race(tmp_path):
    async def go():
        be = ScriptedBackend(["idle without marker"])
        fc, db = make_client(tmp_path, be)
        r = await fc.start_session("data_droid", "x")
        assert r.status == "paused"
        db.rows[r.agent_session_id]["status"] = "running"  # someone else woke it
        with pytest.raises(ResumeConflict):
            await fc.resume(r.agent_session_id, "y")

    asyncio.run(go())


# ----------------------------------------------------------------------------- cloud backend


def test_cloud_backend_404_retry_and_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "test-key-not-real")
    state = {"posts": 0, "polls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer test-key-not-real"
        p, m = req.url.path, req.method
        if p.endswith("/computers/c1"):
            return httpx.Response(200, json={"id": "c1", "status": "active"})
        if m == "POST" and p.endswith("/sessions"):
            return httpx.Response(201, json={"sessionId": "s1", "status": "idle"})
        if m == "POST" and p.endswith("/sessions/s1/messages"):
            state["posts"] += 1
            if state["posts"] == 1:
                return httpx.Response(404, json={"detail": "Session does not exist"})
            return httpx.Response(200, json={"status": "pending"})
        if m == "GET" and p.endswith("/sessions/s1"):
            state["polls"] += 1
            st = "running" if state["polls"] < 2 else "idle"
            return httpx.Response(200, json={"status": st, "computerId": "c1", "factoryCredits": 2100})
        if m == "GET" and p.endswith("/sessions/s1/messages"):
            return httpx.Response(200, json={"messages": [
                {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "ok\nDONE"}]},
                {"id": "m1", "role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Create", "input": {"file_path": "/home/x/a.txt"}}]},
            ]})
        if p.endswith("/sessions/gone"):
            return httpx.Response(404, json={"detail": "Session does not exist"})
        return httpx.Response(500, json={"detail": f"unexpected {m} {p}"})

    async def nosleep(_s):
        return None

    async def go():
        cfg = FactoryConfig(state_dir=tmp_path / "state", computer_id="c1")
        be = CloudBackend(cfg, transport=httpx.MockTransport(handler), poll_s=0, sleep=nosleep)
        spec = SessionSpec(agent_session_id=5, role="data_droid", name="n", ticket_id=0, mode="cloud",
                           model="m", reasoning="low", cwd="/home/factory-user/agents/5",
                           workdir="/home/factory-user/agents/5")
        events, ids = [], []

        def emit(kind, text, tool=None, **data):
            events.append((kind, text))

        async def on_sid(s):
            ids.append(s)

        raw = await be.run_turn(spec, None, "hello", emit, on_sid)
        assert raw.success and raw.factory_session_id == "s1" and ids == ["s1"] and state["posts"] == 2
        assert ("tool_call", "Create /home/x/a.txt") in events
        assert raw.text.endswith("DONE") and raw.credits == 2100
        with pytest.raises(SessionGone):
            await be.run_turn(spec, "gone", "hi", emit, on_sid)
        await be.aclose()

    asyncio.run(go())


def test_heartbeat_keeps_current_step_fresh(tmp_path):
    class SlowBackend(ScriptedBackend):
        async def run_turn(self, spec, fsid, text, emit, on_session_id):
            await on_session_id("fs-slow")
            emit("tool_call", "Execute: uv run pytest", tool="Execute", tool_use_id="t9", input={})
            await asyncio.sleep(0.8)  # long tool call, no events
            return RawTurn("fs-slow", "success", True, "DONE", credits=10.0)

    async def go():
        fc, db = make_client(tmp_path, SlowBackend([]))
        r = await fc.start_session("onboarding_droid", "slow")
        steps = [c[2] for c in db.calls if c[0] == "agent_step"]
        assert any("still working" in s and s.startswith("Execute: uv run pytest") for s in steps), steps
        assert r.status == "done" and db.rows[r.agent_session_id]["status"] == "done"

    asyncio.run(go())
