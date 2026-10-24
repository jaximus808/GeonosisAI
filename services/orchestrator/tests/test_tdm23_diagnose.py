"""TDM-23 failure clustering: fixtures + fakes (no network), plus an opt-in live Claude test.

Live:  RDF_LIVE_CLAUDE=1 uv run pytest tests/test_tdm23_diagnose.py -k live -s
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rdf_orchestrator import diagnose as dx
from rdf_orchestrator.bus import Bus, Registry
from rdf_orchestrator.stdb import Row

FIXTURE = Path(__file__).parent / "fixtures" / "tdm23_eval_run.json"


def load_fixture() -> tuple[dx.DiagnoseInputs, dict]:
    d = json.loads(FIXTURE.read_text())
    eps = {t["episode_id"]: {"id": t["episode_id"], "ticket_id": 7, "duration_ms": t["duration_ms"],
                             "cameras_json": json.dumps({"exo": f"modal-vol://rdf-media/tickets/7/episodes/{t['episode_id']}/exo.mp4"})}
           for t in d["trials"] if t["episode_id"]}
    return dx.DiagnoseInputs(d["ticket"], d["eval_run"], d["trials"], eps), d["expected"]


class FakeClusterer:
    model = "fake"

    def __init__(self, md: dx.ModelDiagnosis):
        self.md, self.messages = md, None

    def cluster(self, messages):
        self.messages = messages
        return self.md


def MC(label, idxs, rec=True, phase="grasp"):
    return dx.ModelCluster(label=label, description=f"{label} desc", phase=phase, recoverable_mid_episode=rec,
                           recoverability_reason="why", trial_indices=idxs)


GOOD = dx.ModelDiagnosis(reasoning="three modes", clusters=[
    MC("Drops mid-grasp", [1, 4, 8], phase="lift"), MC("misses the cup", [2, 6, 9]), MC("never moves", [5], False, "start")])


class FakeFrames:
    def __init__(self): self.asked = []

    def frames(self, episode, n):
        self.asked.append(episode["id"])
        return [] if episode["id"] == 45 else [(f"f{i}", b"\xff\xd8fake") for i in range(n)]


class FakeStdb:
    """Simulates the TDM-12 reducers + SQL for failure_cluster / event_log."""

    def __init__(self, existing_clusters=()):
        self.calls, self.clusters, self.events = [], list(existing_clusters), []
        self._next = 100

    def call_reducer(self, name, *args):
        self.calls.append((name, args))
        if name == "record_failure_cluster":
            tid, label, count, ex = args
            assert count > 0 and isinstance(json.loads(ex), list)
            self._next += 7  # auto-inc ids are not sequential
            self.clusters.append({"id": self._next, "ticket_id": tid, "label": label, "count": count,
                                  "example_episode_ids": json.loads(ex)})
        elif name == "append_event":
            self.events.append({"id": len(self.events) + 1, "actor": args[0], "type": args[1], "payload_json": args[2]})

    def sql(self, q):
        assert "failure_cluster" in q
        tid = int(q.rsplit("=", 1)[1])
        return [c for c in self.clusters if c["ticket_id"] == tid]


# --------------------------------------------------------------------------- pure logic

def test_diagnose_maps_trials_to_real_episodes_and_counts():
    inputs, _ = load_fixture()
    fc, fr = FakeClusterer(GOOD), FakeFrames()
    diag = dx.diagnose_eval_run(inputs, claude=fc, frames=fr)
    by = {c.label: c for c in diag.clusters}
    assert list(by) == ["drops mid-grasp", "misses the cup", "never moves"]  # normalised, sorted by count
    assert by["drops mid-grasp"].episode_ids == [41, 44, 48] and by["drops mid-grasp"].count == 3
    assert by["misses the cup"].episode_ids == [42, 46]  # trial 9 has no recording (episode 0)
    assert by["misses the cup"].count == 3
    assert sum(c.count for c in diag.clusters) == len(inputs.failed) == 7
    assert sorted(fr.asked) == [41, 42, 44, 45, 46, 48]  # only failed, recorded episodes
    assert diag.frames_sent == 15 and diag.episodes_without_frames == [45]
    p = diag.payload()
    assert p["recoverable_failures"] == 6 and p["unrecoverable_failures"] == 1
    assert p["failures"] == 7 and p["successes"] == 3 and p["eval_run_id"] == 3 and p["ticket_id"] == 7
    assert set(p["clusters"][0]) >= {"label", "description", "phase", "recoverable_mid_episode",
                                     "recoverability_reason", "count", "trial_indices", "episode_ids",
                                     "example_episode_ids", "failure_cluster_id"}


def test_prompt_contains_notes_and_images_but_not_successes_as_failures():
    inputs, _ = load_fixture()
    fc = FakeClusterer(GOOD)
    dx.diagnose_eval_run(inputs, claude=fc, frames=FakeFrames())
    content = fc.messages[0]["content"]
    texts = "\n".join(b["text"] for b in content if b["type"] == "text")
    assert "Trial 5: FAILED" in texts and "never moved" in texts and "Trial 0: FAILED" not in texts
    assert "3/10 trials succeeded" in texts and "red cup" in texts
    imgs = [b for b in content if b["type"] == "image"]
    assert len(imgs) == 15 and imgs[0]["source"]["media_type"] == "image/jpeg"


def test_reconcile_repairs_bad_model_output():
    inputs, _ = load_fixture()
    md = dx.ModelDiagnosis(reasoning="r", clusters=[
        MC("misses the cup", [2, 6, 99]),   # 99 is not a failed trial
        MC("Misses the cup", [9, 2]),       # same label (merged), 2 duplicated
        MC("drops mid-grasp", [1, 0]),      # 0 succeeded
        MC("empty", []),
    ])
    clusters, repairs = dx.reconcile(inputs, md)
    by = {c.label: c.trial_indices for c in clusters}
    assert by == {"misses the cup": [2, 6, 9], "drops mid-grasp": [1], dx.UNCLASSIFIED_LABEL: [4, 5, 8]}
    assert any("99" in r for r in repairs) and any("duplicate trial 2" in r for r in repairs)
    assert sum(len(v) for v in by.values()) == 7


def test_zero_failures_skips_claude():
    inputs, _ = load_fixture()
    inputs.trials = [dict(t, success=True) for t in inputs.trials]

    class Boom:
        model = "x"
        def cluster(self, m): raise AssertionError("must not call Claude")

    diag = dx.diagnose_eval_run(inputs, claude=Boom())
    assert diag.clusters == []
    st = FakeStdb()
    p = dx.record_diagnosis(diag, st)
    assert [c[0] for c in st.calls] == ["append_event"] and st.events[0]["type"] == dx.EVENT_NO_FAILURES
    assert p["failures"] == 0


def test_record_diagnosis_writes_clusters_then_event_with_ids():
    inputs, _ = load_fixture()
    diag = dx.diagnose_eval_run(inputs, claude=FakeClusterer(GOOD), frames=None)
    st = FakeStdb(existing_clusters=[{"id": 5, "ticket_id": 7, "label": "misses the cup", "count": 2}])
    p = dx.record_diagnosis(diag, st)
    rec = [a for n, a in st.calls if n == "record_failure_cluster"]
    assert rec == [(7, "drops mid-grasp", 3, "[41, 44, 48]"), (7, "misses the cup", 3, "[42, 46]"),
                   (7, "never moves", 1, "[45]")]
    assert st.calls[-1][0] == "append_event" and st.events[-1]["type"] == dx.EVENT_CLUSTERS
    ids = [c["failure_cluster_id"] for c in p["clusters"]]
    assert None not in ids and 5 not in ids and len(set(ids)) == 3  # the old cluster row is not reused
    assert json.loads(st.events[-1]["payload_json"])["clusters"][0]["failure_cluster_id"] == ids[0]
    # planner-side reader
    assert dx.latest_diagnosis(st.events, 7)["eval_run_id"] == 3
    assert dx.latest_diagnosis(st.events, 8) is None


def test_structured_output_parse_and_refusal():
    class Block:
        def __init__(self, t): self.type, self.text = "text", t

    class Resp:
        def __init__(self, stop, text):
            self.stop_reason, self.content, self.model = stop, [Block(text)], "m"
            self.usage = type("U", (), {"input_tokens": 1, "output_tokens": 2})()
            self.stop_details = None

    class Msgs:
        def __init__(self, r): self.r, self.kw = r, None
        def create(self, **kw): self.kw = kw; return self.r

    class Client:
        def __init__(self, r): self.beta = type("B", (), {})(); self.beta.messages = Msgs(r)

    ok = Client(Resp("end_turn", GOOD.model_dump_json()))
    cc = dx.ClaudeClusterer(client=ok)
    assert cc.cluster([{"role": "user", "content": "x"}]).clusters[0].label == "Drops mid-grasp"
    kw = ok.beta.messages.kw
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["schema"] is dx.DIAGNOSIS_SCHEMA and kw["thinking"] == {"type": "adaptive"}
    with pytest.raises(dx.DiagnoseError, match="refused"):
        dx.ClaudeClusterer(client=Client(Resp("refusal", ""))).cluster([])
    with pytest.raises(dx.DiagnoseError, match="validate"):
        dx.ClaudeClusterer(client=Client(Resp("end_turn", '{"reasoning": 1}'))).cluster([])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_extract_frames_from_real_video(tmp_path):
    video = tmp_path / "exo.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=640x480:rate=10",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    frames = dx.extract_frames(video, 4.0)
    assert len(frames) == 3 and all(b[:2] == b"\xff\xd8" for _, b in frames)
    assert frames[0][0].startswith("t=0.8s")
    assert dx.pick_camera({"wrist_left": "a", "exo_cam": "b"}) == ("exo_cam", "b")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_episode_frame_source_downloads_and_samples(tmp_path):
    import functools
    import http.server
    import threading

    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(tmp_path / "exo.mp4")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        signed = []
        src = dx.EpisodeFrameSource(signed_get=lambda uri: signed.append(uri) or f"{base}/exo.mp4")
        ep = {"id": 41, "duration_ms": 3000, "cameras_json": json.dumps(
            {"wrist": "modal-vol://rdf-media/x/wrist.mp4", "exo": "modal-vol://rdf-media/x/exo.mp4"})}
        frames = src.frames(ep, 3)
        assert signed == ["modal-vol://rdf-media/x/exo.mp4"]  # prefers the scene camera, signs via storage
        assert len(frames) == 3 and frames[0][0].startswith("exo t=0.6s") and frames[0][1][:2] == b"\xff\xd8"
        missing = dx.EpisodeFrameSource(signed_get=lambda uri: f"{base}/nope.mp4").frames(ep, 3)
        assert missing == []  # 404 -> text-only, no exception
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- the bus handler

class FakeBusClient:
    def __init__(self):
        self.state, self.identity, self.queries, self.cb = "stopped", "0xfake", [], {}
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
    async def call_reducer(self, r, a): return 1


async def _settle(bus, n=200):
    for _ in range(n):
        await asyncio.sleep(0.01)
        if bus.health()["inflight"] == 0:
            return


def _diagnose_bus():
    from rdf_orchestrator import bus as busmod
    import rdf_orchestrator.handlers.diagnose  # noqa: F401 - registers on the default registry

    reg = Registry()
    hs = [x for x in busmod.registry.handlers if x.name in ("diagnose_failures", "diagnose_on_ticket_diagnose")]
    assert len(hs) == 2
    reg.handlers.extend(hs)
    for t in busmod.registry.tables:
        reg.watch(t)
    client = FakeBusClient()
    return Bus(reg, client, workers=2), client, hs[0]


def _patch(monkeypatch, st, fc):
    import rdf_orchestrator.handlers.diagnose as hd

    monkeypatch.setattr(hd, "make_stdb", lambda: st)
    monkeypatch.setattr(hd, "make_clusterer", lambda: fc)
    monkeypatch.setattr(hd, "make_frames", lambda: None)
    hd._claimed.clear()


def test_candidate_eval_waits_for_ticket_diagnose(monkeypatch):
    inputs, _ = load_fixture()
    st, fc = FakeStdb(), FakeClusterer(GOOD)
    _patch(monkeypatch, st, fc)
    bus, client, _ = _diagnose_bus()
    ticket = Row(dict(inputs.ticket, status="eval"))
    running = Row(dict(inputs.eval_run, kind="candidate", status="running", finished_at=None))

    async def go():
        await bus.start()
        client.cb["state"]("live", None)
        client.cb["snapshot"]({"ticket": [ticket], "eval_run": [running],
                               "eval_trial": [Row(t) for t in inputs.trials],
                               "episode": [Row(e) for e in inputs.episodes.values()], "event_log": []})
        await _settle(bus)
        done = Row(dict(running, status="done", finished_at=5))
        client.cb["update"]("eval_run", running, done, {})
        await _settle(bus)
        assert not st.calls  # deferred: the ticket has not looped to diagnose (iteration not bumped yet)
        client.cb["update"]("ticket", ticket, Row(dict(ticket, status="diagnose", iteration=1)), {})
        await _settle(bus)
        await bus.stop()

    asyncio.run(go())
    # diagnose.started, the rows one by one, then the completion event (the flywheel waits for it)
    assert [c[0] for c in st.calls] == ["append_event"] + ["record_failure_cluster"] * 3 + ["append_event"]
    assert [e["type"] for e in st.events] == [dx.EVENT_STARTED, dx.EVENT_CLUSTERS]
    started = json.loads(st.events[0]["payload_json"])
    assert (started["eval_run_id"], started["iteration"], started["attempt"]) == (3, 1, 1)
    p = json.loads(st.events[-1]["payload_json"])
    assert p["iteration"] == 1 and p["eval_kind"] == "candidate"


def test_handler_on_eval_finalized(monkeypatch):
    inputs, _ = load_fixture()
    st, fc = FakeStdb(), FakeClusterer(GOOD)
    _patch(monkeypatch, st, fc)

    bus, client, h = _diagnose_bus()
    running = Row(dict(inputs.eval_run, status="running", finished_at=None))
    old_done = Row(dict(inputs.eval_run, id=2, status="done", finished_at=1))  # history: skipped on boot

    # Boot snapshot: run 2 (done, open ticket, newest finished) would be picked up as in-flight work,
    # so mark it already diagnosed to check the skip path; run 3 then finalizes live.
    st.events.append({"id": 1, "type": dx.EVENT_CLUSTERS, "payload_json": json.dumps({"eval_run_id": 2})})
    old_events = list(st.events)

    async def go():
        await bus.start()
        client.cb["state"]("live", None)
        client.cb["snapshot"]({
            "ticket": [Row(inputs.ticket)], "eval_run": [running, old_done],
            "eval_trial": [Row(t) for t in inputs.trials],
            "episode": [Row(e) for e in inputs.episodes.values()],
            "event_log": [Row(e) for e in old_events],
        })
        await _settle(bus)
        assert not st.calls
        done = Row(dict(inputs.eval_run, status="done"))
        client.cb["update"]("eval_run", running, done, {})
        client.cb["update"]("eval_run", done, Row(dict(done, ci_low=0.11)), {})
        await _settle(bus)
        await bus.stop()

    asyncio.run(go())
    assert h.errors == 0, h.last_error
    names = [c[0] for c in st.calls]
    assert names == ["append_event"] + ["record_failure_cluster"] * 3 + ["append_event"]
    assert [e["type"] for e in st.events[len(old_events):]] == [dx.EVENT_STARTED, dx.EVENT_CLUSTERS]
    p = json.loads(st.events[-1]["payload_json"])
    assert p["eval_run_id"] == 3 and [c["count"] for c in p["clusters"]] == [3, 3, 1]


# --------------------------------------------------------------------------- live Claude (opt-in)

@pytest.mark.skipif(os.environ.get("RDF_LIVE_CLAUDE") != "1", reason="set RDF_LIVE_CLAUDE=1 to call Claude")
def test_live_claude_clusters_labelled_fixture_stably():
    """One real Claude call per run on the labelled fixture, 3 runs: same cluster count each time, and
    the grouping matches the fixture's ground truth."""
    inputs, expected = load_fixture()
    runs = []
    for i in range(3):
        cc = dx.ClaudeClusterer()
        diag = dx.diagnose_eval_run(inputs, claude=cc, frames=None)
        groups = sorted(sorted(c.trial_indices) for c in diag.clusters)
        rec = {c.trial_indices[0]: c.recoverable_mid_episode for c in diag.clusters}
        print(f"\nrun {i}: usage={cc.last_usage}")
        for c in diag.clusters:
            print(f"  {c.label!r:28} n={c.count} trials={c.trial_indices} eps={c.example_episode_ids} "
                  f"phase={c.phase} recoverable={c.recoverable_mid_episode}")
        print(f"  repairs={diag.repairs}\n  reasoning: {diag.reasoning[:600]}")
        runs.append((len(diag.clusters), groups, rec, diag))
    counts = [r[0] for r in runs]
    assert len(set(counts)) == 1, counts
    for n, groups, rec, diag in runs:
        assert n == expected["clusters"]
        assert groups == sorted(sorted(g) for g in expected["groups"])
        assert {str(k): v for k, v in rec.items()} == expected["recoverable"]
        assert not diag.repairs
