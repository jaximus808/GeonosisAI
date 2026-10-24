"""TDM-35: AgentFeed v1 (agents_feed.build_feed / GET /agents) and its TS mirror stay identical.

Input: ``tdm35_feed_input.json`` = TDM-30's recorded proof run (droid.* rows, as event_log rows)
+ the reducer echoes it causes + orchestrator agents' rows + rows that must be ignored, shuffled.
Golden: ``tdm35_feed_golden.json``. Regenerate after an intended change with
``RDF_TDM35_REGEN=1 uv run pytest tests/test_tdm35_agents_feed.py`` and review the diff.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rdf_orchestrator import agents_feed as af

HERE = Path(__file__).parent
REPO = HERE.parents[2]
INPUT = json.loads((HERE / "tdm35_feed_input.json").read_text())
GOLDEN_PATH = HERE / "tdm35_feed_golden.json"
TS_NORMALIZER = REPO / "apps" / "web" / "src" / "lib" / "agentfeed" / "normalize.ts"
CASES = {"all": {}, "ticket2": {"ticket_id": 2}, "limit5": {"limit": 5}}


def _py(case: dict) -> dict:
    return af.build_feed(INPUT["sessions"], INPUT["events"], now_us=INPUT["now_us"], **case)


def _golden() -> dict:
    if os.environ.get("RDF_TDM35_REGEN"):
        GOLDEN_PATH.write_text(json.dumps({k: _py(c) for k, c in CASES.items()}, indent=1, ensure_ascii=False) + "\n")
    return json.loads(GOLDEN_PATH.read_text())


def _agent(feed: dict, key: str) -> dict:
    return next(a for a in feed["agents"] if a["key"] == key)


@pytest.mark.parametrize("name", list(CASES))
def test_python_matches_golden(name):
    assert _py(CASES[name]) == _golden()[name]


def test_ts_mirror_matches_golden():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ts_cases = {k: {"ticketId": c.get("ticket_id"), "limit": c.get("limit")} for k, c in CASES.items()}
    script = f"""
import {{ buildAgentFeed }} from {json.dumps(TS_NORMALIZER.as_uri())};
import {{ readFileSync }} from 'node:fs';
const inp = JSON.parse(readFileSync({json.dumps(str(HERE / 'tdm35_feed_input.json'))}, 'utf8'));
const cases = {json.dumps(ts_cases)};
const out = {{}};
for (const [k, c] of Object.entries(cases)) {{
  const o = {{ nowUs: inp.now_us }};
  if (c.ticketId != null) o.ticketId = c.ticketId;
  if (c.limit != null) o.limit = c.limit;
  out[k] = buildAgentFeed(inp.sessions, inp.events, o);
}}
process.stdout.write(JSON.stringify(out));
"""
    r = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and "Unknown file extension" in r.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert r.returncode == 0, r.stderr[-2000:]
    ts = json.loads(r.stdout)
    golden = _golden()
    for k in CASES:
        assert ts[k] == golden[k], f"TS normalize.ts drifted from agents_feed.py on case {k!r}"


def test_feed_semantics():
    f = _py({})
    keys = [a["key"] for a in f["agents"]]
    # droids first (newest first), then other sessions, then services
    assert keys[:3] == ["session:1", "session:9", "session:3"]
    assert keys[3] == "session:2"
    assert {k for k in keys[4:]} == {"service:clustering", "service:planner", "service:reviewer", "service:qa",
                                    "service:dispatcher", "service:eval", "service:flywheel"}

    d1 = _agent(f, "session:1")
    assert d1["kind"] == "droid" and d1["status"] == "done" and d1["replay"] is False
    assert d1["usage"] == pytest.approx({"credits_total": 10891.0, "turns": 3, "input_tokens": 15413,
                                         "output_tokens": 1837, "seconds": 35.4})
    full = af.build_feed(INPUT["sessions"], INPUT["events"], now_us=INPUT["now_us"], limit=500)
    evs = _agent(full, "session:1")["events"]
    types = [e["type"] for e in evs]
    # reducer echoes that droid.* rows already cover are dropped; resumes and creation stay
    assert not {"agent.step", "agent.waiting_for", "agent.session_updated"} & set(types)
    assert {"agent.session_created", "agent.resumed", "agent.resume_delivering", "agent.resume_delivered"} <= set(types)
    assert [e["ts_us"] for e in evs] == sorted(e["ts_us"] for e in evs)
    assert evs[-1]["kind"] == "done" and evs[-1]["type"] == "droid.turn_completed"
    assert sum(e["kind"] == "tool_call" for e in evs) == 12
    assert len(d1["events"]) == af.DEFAULT_LIMIT and d1["event_count"] == len(evs)

    s2 = _agent(f, "session:2")  # non-droid session: agent.* rows are its whole history
    assert s2["kind"] == "session" and s2["status"] == "waiting_for"
    assert [e["kind"] for e in s2["events"]] == ["info", "step", "waiting"]
    assert s2["waiting_for"]["field"] == "accepted_count"

    s9 = _agent(f, "session:9")  # orphaned events: role and status come from the stream
    assert s9["role"] == "training_droid" and s9["status"] == "failed"
    assert s9["usage"]["credits_total"] == 1200.5 and s9["usage"]["input_tokens"] == 10
    assert _agent(f, "session:3")["events"] == [] and _agent(f, "session:3")["usage"] is None

    pl = _agent(f, "service:planner")
    assert pl["status"] == "active"  # newest row is the data_plan with a non-JSON payload, not the blocked one
    texts = [e["text"] for e in pl["events"]]
    assert "data plan: 5 × recovery (iteration 1, est $15.00) — grasp failures dominate" in texts
    cl = _agent(f, "service:clustering")
    assert [e["text"] for e in cl["events"]] == ["2 failure clusters from eval 4",
                                                  'failure cluster "missed grasp" (3 trials, iteration 1)']
    rv = _agent(f, "service:reviewer")
    assert [e["text"] for e in rv["events"]] == ["episode 11 accepted", "episode 12 rejected: gripper never closed"]
    qa = _agent(f, "service:qa")
    assert qa["status"] == "error" and qa["events"][-1]["is_error"]
    assert _agent(f, "service:flywheel")["status"] == "active"
    every = [e for a in full["agents"] for e in a["events"]]
    assert not [e for e in every if e["actor"].startswith(("robot:", "human:")) or e["type"].startswith("test.")]
    assert not [e for e in every if e["type"] == "ticket.created"]


def test_ticket_filter():
    f = _py({"ticket_id": 2})
    keys = {a["key"] for a in f["agents"]}
    assert "session:2" in keys and "session:3" in keys and "session:1" not in keys
    rv = _agent(f, "service:reviewer")
    assert [e["ticket_id"] for e in rv["events"]] == [2]
    assert "service:qa" in keys and all(e["ticket_id"] == 2 for e in _agent(f, "service:qa")["events"])


def test_replay_from_recording_is_labelled():
    f = af.replay_feed(limit=100)
    assert f["replay"] is True and f["replay_source"] == ["1.events.jsonl"]
    (a,) = f["agents"]
    assert a["replay"] is True and all(e["replay"] for e in a["events"])
    assert a["key"] == "session:1" and a["status"] == "done" and a["usage"]["credits_total"] == 10891.0
    assert a["factory_session_ids"] == ["63583e50-59b4-4305-b908-dd80bf7c8d71"]
    assert a["events"][0]["id"] == "rec:1:1" and a["event_count"] == 40


# ----------------------------------------------------------------------------- HTTP

class _FakeSql:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.queries: list[str] = []

    async def sql(self, query: str) -> list[dict]:
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("down")
        ts = lambda us: {"__timestamp_micros_since_unix_epoch__": us}  # noqa: E731 - rdf_common decoding
        if "agent_session" in query:
            return [{**s, "last_update": ts(s["last_update_us"]), "created_at": ts(0)} for s in INPUT["sessions"]]
        if query.startswith("SELECT id"):
            return [{"id": e["id"]} for e in INPUT["events"]]
        return [{"id": e["id"], "ts": ts(e["ts_us"]), "actor": e["actor"], "type": e["type"],
                 "payload_json": e["payload_json"]} for e in INPUT["events"]]


def _client(fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rdf_orchestrator.agents_api import agents_router, get_agents_stdb

    app = FastAPI()
    app.include_router(agents_router)
    app.dependency_overrides[get_agents_stdb] = lambda: fake
    return TestClient(app)


def test_http_live_and_replay():
    c = _client(_FakeSql())
    live = c.get("/agents").json()
    assert live["v"] == 1 and live["replay"] is False
    assert [a["key"] for a in live["agents"]] == [a["key"] for a in _py({})["agents"]]
    assert _agent(live, "session:1")["usage"]["turns"] == 3
    t2 = c.get("/agents", params={"ticket_id": 2, "limit": 3}).json()
    assert len(_agent(t2, "session:2")["events"]) == 3 and "session:1" not in {a["key"] for a in t2["agents"]}

    rep = c.get("/agents", params={"replay": "1"}).json()
    assert rep["replay"] is True and rep["agents"][0]["replay"] is True

    src = c.get("/agents/replay/source").json()
    assert src["replay"] is True and len(src["events"]) == 40 and src["sessions"][0]["role"] == "data_droid"


def test_http_outage():
    c = _client(_FakeSql(fail=True))
    assert c.get("/agents").status_code == 502
    auto = c.get("/agents", params={"replay": "auto"}).json()
    assert auto["replay"] is True and auto["replay_reason"].startswith("SpacetimeDB unreachable")


def test_http_replay_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("RDF_AGENTS_REPLAY_DIR", str(tmp_path))
    c = _client(_FakeSql())
    assert c.get("/agents", params={"replay": "1"}).status_code == 404
    assert c.get("/agents/replay/source").status_code == 404
