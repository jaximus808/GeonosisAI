"""TDM-32 Onboarding Droid: launcher bounds, the HIL waiting line, the flywheel step. No network, no credits."""
from __future__ import annotations

import asyncio
import json

from rdf_factory import FactoryClient, FactoryConfig
from rdf_factory.backends import RawTurn, _command_allowed
from rdf_factory.droids import onboarding as O

# the robot-client tool's HIL predicate, restated here so the factory tests need no robot-client import
HIL_LINE = 'WAITING_FOR {"table":"event_log","type":"onboarding.hil_confirmed.t1","timeout_s":86400}'


class Stdb:
    def __init__(self):
        self.rows, self.events, self.calls, self.n = {}, [], [], 0

    async def call_reducer(self, name, *a):
        self.calls.append((name, a))
        if name == "upsert_agent_session":
            if a[0] == 0:
                self.n += 1
                self.rows[self.n] = {"id": self.n, "role": a[1], "name": a[5], "status": a[3], "ticket_id": a[6],
                                     "factory_session_id": None, "factory_session_ids_json": "[]"}
            else:
                self.rows[a[0]].update(status=a[3], current_step=a[4])
        elif name == "agent_wait_for":
            self.rows[a[0]].update(status="waiting_for", waiting_for_json=a[1])
        elif name == "agent_resume":
            self.rows[a[0]].update(status="resuming")
        elif name == "append_event":
            self.events.append((a[0], a[1]))

    async def sql(self, q):
        import re

        m = re.search(r"name = '(.*)'", q)
        if m:
            return [dict(r) for r in self.rows.values() if r["name"] == m.group(1)]
        m = re.search(r"id = (\d+)", q)
        r = self.rows.get(int(m.group(1))) if m else None
        return [dict(r)] if r else []

    async def aclose(self):
        pass


class Backend:
    mode = "local"

    def __init__(self, replies):
        self.specs, self.replies = [], list(replies)

    async def run_turn(self, spec, fsid, text, emit, on_session_id):
        self.specs.append((spec, text))
        if not fsid:
            fsid = "fs-1"
            await on_session_id(fsid)
        return RawTurn(fsid, "success", True, self.replies.pop(0), credits=100.0)

    async def interrupt(self, spec, fsid):
        pass

    async def aclose(self):
        pass


def test_tool_prefix_is_the_only_command():
    assert _command_allowed(f"{O.TOOL_PREFIX} check --ctx /tmp/x/droid_context.json", [O.TOOL_PREFIX])
    assert not _command_allowed(f"{O.TOOL_PREFIX} check --ctx a && rm -rf /", [O.TOOL_PREFIX])
    # the human's HIL confirmation is a different module: the droid cannot confirm its own HIL wait
    hil = O.TOOL_PREFIX.replace("onboarding_tool", "onboarding_hil") + " confirm --ticket 1 --by droid"
    assert not _command_allowed(hil, [O.TOOL_PREFIX])
    assert not _command_allowed("uv run --project services/robot-client rdf-robot-client demo-square", [O.TOOL_PREFIX])


def test_launch_bounds_and_hil_park(tmp_path):
    async def go():
        cfg = FactoryConfig(state_dir=tmp_path / "state", work_root=tmp_path / "sessions", repo_dir=tmp_path)
        be, db = Backend(["checked, parked for the human\n" + HIL_LINE, "published\nDONE"]), Stdb()
        fc = FactoryClient(cfg, stdb=db, backends={"local": be})
        r = await O.launch_onboarding_droid(fc, 1, host="http://127.0.0.1:3999", db="local")
        spec, brief = be.specs[0]
        tdir = O.ticket_dir(cfg.work_root, 1)
        assert spec.autonomy == "off" and spec.max_turns == O.MAX_TURNS
        assert spec.allow_commands == [O.TOOL_PREFIX] and str(tdir) in spec.write_roots
        assert len(spec.write_roots) == 2  # its workdir + the ticket dir, nothing else
        ctx = json.loads((tdir / "droid_context.json").read_text())
        assert ctx == {**ctx, "host": "http://127.0.0.1:3999", "db": "local", "ticket_id": 1}
        assert "never move a real arm" in brief and "decided_by: operator" in brief and str(tdir) in brief
        assert r.status == "waiting_for"
        assert r.waiting_for["table"] == "event_log" and r.waiting_for["type"] == "onboarding.hil_confirmed.t1"
        assert r.waiting_for.get("timeout_s") == 86400
        r2 = await fc.resume(r.agent_session_id, "human confirmed the HIL checks")
        assert r2.status == "done" and db.rows[r.agent_session_id]["status"] == "done"

    asyncio.run(go())


# --------------------------------------------------------------------------- flywheel step

class _View:
    def __init__(self, onboarded=False, robot=True):
        self.onboarded = onboarded
        self.robot = {"id": 3, "name": "test.r", "adapter_version": "x" if onboarded else ""} if robot else None


class _Bus:
    def __init__(self, sessions):
        self.s = sessions

    def rows(self, table):
        return self.s if table == "agent_session" else []


class _Ctx:
    def __init__(self, view, sessions=()):
        self.view, self.bus, self.ticket_id = view, _Bus(list(sessions)), 1
        self.logs, self.events = [], []

    def log(self, msg, **f):
        self.logs.append((msg, f))

    async def event(self, t, p, actor=None):
        self.events.append((t, p))


def test_onboarding_step_is_idempotent_and_credit_safe(monkeypatch):
    launched = []

    async def fake_launch(self, ctx):
        launched.append(ctx.ticket_id)

    monkeypatch.setattr(O.OnboardingDroidStep, "_launch", fake_launch)

    async def go():
        step = O.OnboardingDroidStep()
        c = _Ctx(_View(onboarded=True))
        await step.run(c)
        assert "already onboarded" in c.logs[-1][1]["reason"]
        c = _Ctx(_View(), sessions=[{"id": 5, "role": "onboarding_droid", "ticket_id": 1, "status": "failed"}])
        await step.run(c)
        assert "agent_session 5" in c.logs[-1][1]["reason"]  # a finished droid is never restarted on its own
        c = _Ctx(_View(robot=False))
        await step.run(c)
        assert c.logs[-1][1]["reason"] == "ticket has no robot row"
        c = _Ctx(_View(), sessions=[{"id": 6, "role": "training_droid", "ticket_id": 1, "status": "running"}])
        await step.run(c)
        await asyncio.sleep(0)
        assert launched == [1] and c.events[0][0] == "onboarding.droid_starting"

    asyncio.run(go())
