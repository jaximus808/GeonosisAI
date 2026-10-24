"""TDM-48 recorder tests.

Fast (no network): clock/interpolation units, a SimArm + synthetic-camera takeover recording that
must pass ``rdf_common.review_checks``, and the upload queue's retry/idempotency against fakes.

Integration (needs the ``spacetime`` CLI; skipped otherwise): a throwaway local SpacetimeDB
(127.0.0.1:$TDM48_PORT, default a free port, data under $TMPDIR/tdm-48-<pid>/), the REAL orchestrator media API
(``rdf_orchestrator.media_api`` under uvicorn) signing for a local stand-in of the Modal media store
(same HMAC protocol, files in a temp dir), and the REAL ``RobotClient`` + SimArm streaming ticks:

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm48_recorder.py -v

Never touches maincloud or the real Modal Volume.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from rdf_robot_client.adapters.base import ControlTick
from rdf_robot_client.recorder import (
    ClockAnchor,
    EpisodeUploader,
    LocalArmDriver,
    LocalEpisode,
    Recorder,
    check_local,
    count_video_frames,
    recorder_for,
    sim_motion,
    tick_at,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# A free port + a per-process data dir by default: several workers run this suite in one checkout,
# and a fixed port lets one run's LocalStack reap another's server mid-test.
PORT = int(os.environ.get("TDM48_PORT") or _free_port())
DB = "tdm48rec"
SIGNING_KEY = "tdm48-test-signing-key-0123456789"


def wait_for(pred, timeout=10.0, what="condition", every=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def _tick(t_ns, q, src="policy", g=0.5):
    return ControlTick(timestamp_us=0, joint_pos=tuple(q), gripper_pos=g, ik_target_pos=(0.1, 0.2, 0.3),
                       ik_target_quat=(1.0, 0.0, 0.0, 0.0), gripper_cmd=g, control_source=src,
                       joint_cmd=tuple(q), t_ns=t_ns)


# ============================================================================ units

def test_tick_interpolation_and_clock_anchor():
    a, b = _tick(1_000_000, [0.0, 1.0], "policy", 0.0), _tick(2_000_000, [1.0, 3.0], "human", 1.0)
    t, how = tick_at(1_250_000, a, b, max_gap_ns=10_000_000)
    assert how == "interp" and t.joint_pos == pytest.approx((0.25, 1.5)) and t.gripper_pos == pytest.approx(0.25)
    assert t.control_source == "policy"  # the command in force at the frame, not the next one
    assert tick_at(1_250_000, a, b, max_gap_ns=500_000)[1] == "prev"  # gap too large: no interpolation
    assert tick_at(500_000, None, a, 10)[1] == "next"
    anc = ClockAnchor.now()
    assert anc.err_us < 1000
    assert anc.wall_us_of(anc.mono_ns + 1_500_000_000) - anc.wall_us == 1_500_000
    assert abs(anc.wall_us_of(time.monotonic_ns()) - time.time_ns() // 1000) < 5000


# ============================================================================ sim recording

@pytest.fixture(scope="module")
def sim_rig():
    from rdf_robot_client.cameras import CameraRig

    rig = CameraRig.from_config(synthetic=True).start()
    assert rig.wait_ready(10)
    yield rig
    rig.stop()


@pytest.fixture
def tmp_root():
    d = Path(tempfile.mkdtemp(prefix="tdm-48-eps-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def record_sim_takeover(rig, root, *, seconds=7.0, takeover_at=2.5, handback_at=5.5, uploader=None,
                        ticket_id=0, kind="recovery_takeover") -> LocalEpisode:
    from rdf_robot_client.adapters.sim import SimArm

    arm = SimArm()
    arm.connect()

    mode = {"src": "policy"}  # what the "control loop" streams; the test flips it at the marks

    drv = LocalArmDriver(arm, sim_motion(arm.home_joints), lambda t: mode["src"]).start()
    rec = Recorder(drv, rig, uploader=uploader, root=root)
    rec.start(ticket_id, kind, note="tdm48 test")
    t0 = time.monotonic()
    tk = hb = None
    while time.monotonic() - t0 < seconds:
        t = time.monotonic() - t0
        if kind == "recovery_takeover" and tk is None and t >= takeover_at:
            mode["src"] = "idle"  # pause_for_takeover: arm held while the operator connects
            tk = rec.mark_takeover()
        if tk is not None and hb is None and mode["src"] == "idle" and t >= takeover_at + 0.4:
            mode["src"] = "human"
        if tk is not None and hb is None and t >= handback_at:
            mode["src"] = "policy"
            hb = rec.mark_handback()
        time.sleep(0.005)
    ep = rec.stop(success=True)
    drv.stop()
    arm.disconnect()
    assert ep.meta["takeover_ts"] == tk and ep.meta["handback_ts"] == hb
    return ep


def test_sim_takeover_episode_passes_review_checks(sim_rig, tmp_root):
    from rdf_common.episode_format import REQUIRED_COLUMNS, read_actions

    ep = record_sim_takeover(sim_rig, tmp_root)
    m = ep.meta
    assert m["status"] == "recorded", m["problems"]
    assert m["fixture"] is True and "[SIM]" in m["note"]
    act = read_actions(ep.actions_path)
    assert set(REQUIRED_COLUMNS) <= set(act.columns) and "joint_cmd" in act.columns
    assert act.n == m["frame_count"] and abs(act.n - 7 * 30) <= 6
    assert list(act.frame_index) == list(range(act.n))
    # one video frame per parquet row, every camera, H.264
    for cam, p in ep.video_paths.items():
        assert count_video_frames(p) == act.n, cam
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,pix_fmt", "-of", "csv=p=0", str(p)], capture_output=True, text=True)
        assert probe.stdout.strip() == "h264,yuv420p"
    # clocks: 30 fps on the wall clock, takeover/handback inside the span, sources switch at the marks
    dt = (act.timestamp_us[1:] - act.timestamp_us[:-1]) / 1000
    assert 30 < float(sorted(dt)[len(dt) // 2]) < 37
    tk, hb = m["takeover_ts"], m["handback_ts"]
    assert act.timestamp_us[0] < tk < hb < act.timestamp_us[-1]
    src = act.control_source
    for t, s in zip(act.timestamp_us, src):
        if t < tk - 40_000:
            assert s == "policy"
        elif tk + 40_000 <= t < hb - 40_000:
            assert s == "human"  # including the idle pause, relabeled inside the takeover window
        elif t >= hb + 40_000:
            assert s == "policy"
    assert m["takeover"]["idle_relabeled_human"] > 0
    assert m["duration_ms"] == round((act.timestamp_us[-1] - act.timestamp_us[0]) / 1000)
    report = check_local(ep)
    assert report.passed, report.reason()
    assert {r.name for r in report.results} >= {"takeover_timestamps", "failure_before_takeover", "jerk"}
    # the sim flag is honest: without allow_fixtures the reviewer rejects it as synthetic
    from rdf_common.review_checks import ReviewThresholds, run_checks

    strict = run_checks(ep.episode_row(), act, ReviewThresholds())
    assert not strict.passed and strict.get("actions").reason.startswith("actions are synthetic")


def test_inferred_takeover_without_marks(sim_rig, tmp_root):
    """No mark_* calls: the takeover window comes from the control_source stream."""
    from rdf_robot_client.adapters.sim import SimArm

    arm = SimArm()
    arm.connect()
    drv = LocalArmDriver(arm, sim_motion(arm.home_joints),
                         lambda t: "policy" if t < 1.5 or t >= 3.5 else "human").start()
    rec = Recorder(drv, sim_rig, root=tmp_root)
    rec.start(0, "recovery_takeover")
    time.sleep(4.5)
    ep = rec.stop(success=True)
    drv.stop()
    assert ep.meta["takeover"]["inferred"]["takeover"].startswith("control_source")
    assert ep.meta["takeover_ts"] and ep.meta["handback_ts"] > ep.meta["takeover_ts"]
    assert check_local(ep).passed, check_local(ep).reason()


# ============================================================================ upload queue (fakes)

class FakeStore:
    """Stand-in for services/media-store: PUT/GET /o/{key}?exp&sig with the same HMAC."""

    def __init__(self, key: str = SIGNING_KEY, fail_first: int = 0) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tdm-48-store-"))
        self.key, self.fail_left, self.puts = key, fail_first, 0
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self, method):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                k = unquote(u.path[len("/o/"):])
                exp = int(q["exp"][0])
                want = hmac.new(store.key.encode(), f"{method}\n{k}\n{exp}".encode(), hashlib.sha256).hexdigest()
                return k if hmac.compare_digest(want, q["sig"][0]) and exp > time.time() else None

            def do_PUT(self):
                k = self._auth("PUT")
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                if store.fail_left > 0:
                    store.fail_left -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                if not k:
                    self.send_response(403)
                    self.end_headers()
                    return
                p = store.dir / k
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
                store.puts += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def do_GET(self):
                k = self._auth("GET")
                p = store.dir / (k or "")
                if not k or not p.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)


class FakeSignApi:
    """Minimal /uploads/sign-batch with the orchestrator's key layout (unit test only)."""

    def __init__(self, store: FakeStore, down_for: int = 0) -> None:
        self.down_left = down_for
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if api.down_left > 0:
                    api.down_left -= 1
                    self.send_response(502)
                    self.end_headers()
                    return
                out = []
                for f in body["files"]:
                    key = f"tickets/{body['ticket_id']}/episodes/{body['episode_id']}/{f['filename']}"
                    exp = int(time.time()) + 900
                    sig = hmac.new(store.key.encode(), f"PUT\n{key}\n{exp}".encode(), hashlib.sha256).hexdigest()
                    out.append({"upload_url": f"{store.url}/o/{key}?exp={exp}&sig={sig}", "method": "PUT",
                                "headers": {"Content-Type": f["content_type"]},
                                "storage_uri": f"modal-vol://rdf-media/{key}", "key": key, "expires_at": exp})
                data = json.dumps({"uploads": out}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeStdb:
    def __init__(self, lose_first_reply: bool = False) -> None:
        self.rows: list[dict] = []
        self.calls = 0
        self.lose_first_reply = lose_first_reply

    def call_reducer(self, name, *args):
        assert name == "create_episode"
        self.calls += 1
        self.rows.append({"id": len(self.rows) + 1, "storage_uri": args[4], "args": args})
        if self.lose_first_reply and self.calls == 1:
            raise ConnectionError("reply lost (the reducer ran)")

    def sql(self, q):
        uri = q.split("storage_uri = '", 1)[1].rstrip("'")
        return [r for r in self.rows if r["storage_uri"] == uri]


def test_upload_queue_retries_and_is_idempotent(sim_rig, tmp_root):
    store = FakeStore(fail_first=1)
    api = FakeSignApi(store, down_for=2)  # two network blips on signing, then one 503 on a PUT
    stdb = FakeStdb(lose_first_reply=True)  # and the first create_episode reply is lost
    up = EpisodeUploader(api.url, stdb, root=tmp_root, max_backoff_s=0.3)
    try:
        ep = record_sim_takeover(sim_rig, tmp_root, seconds=4.0, takeover_at=1.5, handback_at=3.0, ticket_id=5)
        up.start()  # boot rescan picks up the finished recording
        assert up.wait_idle(60)
        st = up.status(ep.key)
        assert st["state"] == "done", st
        assert st["attempts"] >= 4
        assert len(stdb.rows) == 1, "create_episode must not be duplicated after a lost reply"
        args = stdb.rows[0]["args"]
        (tid, _task, _contrib, kind, storage_uri, actions_uri, cameras_json, frames, dur_ms, tk, hb, success, _ck) = args
        assert (tid, kind, success) == (5, "recovery_takeover", True)
        assert storage_uri == f"modal-vol://rdf-media/tickets/5/episodes/{ep.key}"
        assert actions_uri == storage_uri + "/actions.parquet"
        assert json.loads(cameras_json) == {c: f"{storage_uri}/{c}.mp4" for c in ("top", "wrist")}
        assert (frames, dur_ms, tk, hb) == (ep.meta["frame_count"], ep.meta["duration_ms"],
                                             ep.meta["takeover_ts"], ep.meta["handback_ts"])
        for f in ("top.mp4", "wrist.mp4", "actions.parquet", "meta.json"):
            assert (store.dir / f"tickets/5/episodes/{ep.key}/{f}").read_bytes() == (ep.dir / f).read_bytes()
        # a restarted uploader does not redo finished work
        up.stop()
        up2 = EpisodeUploader(api.url, stdb, root=tmp_root).start()
        assert up2.wait_idle(5) and len(stdb.rows) == 1
        up2.stop()
    finally:
        up.stop()
        api.close()
        store.close()


def test_permanent_refusal_keeps_files(sim_rig, tmp_root):
    from rdf_common.spacetime_client import SpacetimeError

    class Refuse(FakeStdb):
        def call_reducer(self, name, *args):
            raise SpacetimeError(530, "ticket 9 is shipped; no new episodes", reducer=name)

    store = FakeStore()
    api = FakeSignApi(store)
    up = EpisodeUploader(api.url, Refuse(), root=tmp_root, max_backoff_s=0.2)
    try:
        ep = record_sim_takeover(sim_rig, tmp_root, seconds=3.0, takeover_at=1.0, handback_at=2.2, ticket_id=9)
        st = up.upload_now(ep.dir)
        assert st["state"] == "failed" and "shipped" in st["error"]
        assert ep.actions_path.exists()
    finally:
        up.stop()
        api.close()
        store.close()


# ============================================================================ integration

@pytest.fixture(scope="module")
def stack():
    if not shutil.which("spacetime"):
        pytest.skip("spacetime CLI not installed")
    from harness import LocalStack  # TDM-28

    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / f"tdm-48-{os.getpid()}")
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        yield st
    finally:
        st.stop()


@pytest.fixture(scope="module")
def media_api():
    """The real orchestrator media API (TDM-4), signing for the local FakeStore."""
    store = FakeStore()
    port = _free_port()
    env = {**os.environ, "RDF_MEDIA_BACKEND": "modal", "RDF_MEDIA_STORE_URL": store.url,
           "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY, "RDF_MEDIA_BUCKET": "rdf-media"}
    log = open(Path(tempfile.gettempdir()) / f"tdm-48-media-api-{os.getpid()}.log", "wb")
    proc = subprocess.Popen(["uv", "run", "uvicorn", "rdf_orchestrator.media_api:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=REPO / "services" / "orchestrator", env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        import httpx

        def up():
            try:
                return httpx.get(f"{url}/media/healthz", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(up, 60, "media API")
        yield {"url": url, "store": store, "port": port}
    finally:
        proc.terminate()
        proc.wait(10)
        store.close()


def test_end_to_end_robot_client_upload_create_episode(stack, media_api, sim_rig, tmp_root):
    """Real RobotClient ticks (sim_demo_rollout run_eval) + a takeover recording -> media API ->
    local store -> create_episode on the local SpacetimeDB; the row + uploaded files pass the checks."""
    import httpx
    from rdf_common.episode_format import read_actions
    from rdf_common.review_checks import ReviewThresholds, run_checks
    from rdf_common.spacetime_client import SpacetimeConfig

    from rdf_robot_client.adapters import make_adapter
    from rdf_robot_client.handlers import sim_demo_rollout
    from rdf_robot_client.loop import RobotClient

    orch = stack.client("orchestrator")
    cfg = SpacetimeConfig.resolve(stack.host, DB, stack.token("robot_client"))
    client = RobotClient(make_adapter("sim"), name="test.tdm48-sim", stdb=cfg, camera_config={"sim": True},
                         heartbeat_s=1.0, clear_estop_on_boot=True, services={"cameras": sim_rig},
                         handlers={"run_eval": sim_demo_rollout}).start()
    rec = recorder_for(client, orchestrator_url=media_api["url"], root=tmp_root)  # what job handlers call
    assert rec is not None and client.services["recorder"] is rec and recorder_for(client) is rec
    try:
        rid = client.robot_id
        orch.call_reducer("create_ticket", rid, "test.tdm48: put the cup in the bin", 0.8, 5000, "")
        tid = max(int(r["id"]) for r in orch.sql(f"SELECT * FROM ticket WHERE robot_id = {rid}"))

        # 1) an eval rollout through the real command loop: ticks come from run_control_loop
        rec.start(tid, "eval", checkpoint_uri="sim://none")
        orch.call_reducer("issue_robot_command", rid, "run_eval", json.dumps({"sim_seconds": 4}), tid)
        cid = max(int(r["id"]) for r in orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}"))
        wait_for(lambda: orch.sql(f"SELECT * FROM robot_command WHERE id = {cid}")[0]["status"] == "done",
                 30, "run_eval done")
        ep_eval = rec.stop(success=None)
        assert ep_eval.meta["status"] == "recorded", ep_eval.meta["problems"]
        counts = ep_eval.meta["control_source_counts"]
        assert counts.get("policy", 0) >= 100, counts  # ~4 s of streamed policy ticks at 30 Hz
        assert check_local(ep_eval).passed, check_local(ep_eval).reason()

        # 2) a takeover recording, uploaded by the same queue while the media API is briefly unreachable
        rec.uploader.base = "http://127.0.0.1:9"  # network blip: connection refused
        ep_tk = record_sim_takeover(sim_rig, tmp_root, uploader=rec.uploader, ticket_id=tid)
        wait_for(lambda: rec.uploader.status(ep_tk.key).get("state") == "retrying", 20, "a failed attempt")
        rec.uploader.base = media_api["url"]
        assert rec.uploader.wait_idle(90)
        for ep in (ep_eval, ep_tk):
            st = rec.uploader.status(ep.key)
            assert st["state"] == "done", st

        rows = {r["storage_uri"]: r for r in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")}
        assert len(rows) == 2
        tk_row = rows[rec.uploader.status(ep_tk.key)["storage_uri"]]
        ev_row = rows[rec.uploader.status(ep_eval.key)["storage_uri"]]
        assert tk_row["kind"] == "recovery_takeover" and tk_row["review_status"] == "pending"
        assert ev_row["kind"] == "eval" and ev_row["review_status"] == "not_reviewed"
        assert tk_row["frame_count"] == ep_tk.meta["frame_count"] and tk_row["duration_ms"] == ep_tk.meta["duration_ms"]
        assert tk_row["takeover_ts"] == ep_tk.meta["takeover_ts"] and tk_row["handback_ts"] == ep_tk.meta["handback_ts"]
        assert tk_row["success"] is True and ev_row["checkpoint_uri"] == "sim://none"
        cams = json.loads(tk_row["cameras_json"])
        assert set(cams) == {"top", "wrist"}

        # the reviewer's path: signed GETs through the media API, then the deterministic checks on the DB row
        def fetch(uri: str) -> bytes:
            s = httpx.get(f"{media_api['url']}/media/sign", params={"uri": uri}, timeout=10).json()
            r = httpx.get(s["url"], timeout=30)
            assert r.status_code == 200
            return r.content

        act = read_actions(fetch(tk_row["actions_uri"]))
        mp4 = tmp_root / "fetched-top.mp4"
        mp4.write_bytes(fetch(cams["top"]))
        assert count_video_frames(mp4) == act.n == tk_row["frame_count"]
        report = run_checks(tk_row, act, ReviewThresholds.from_yaml(
            REPO / "services/orchestrator/config/review.yaml").replace(allow_fixtures=True))
        assert report.passed, report.reason()
    finally:
        if rec.recording:
            rec.abort("test cleanup")
        if rec.uploader:
            rec.uploader.stop()
        client.stop()
