"""TDM-47 camera rig tests (synthetic cameras; no hardware). Run:
    cd services/robot-client && uv run --with pytest pytest tests/test_cameras.py -q
"""
from __future__ import annotations

import threading
import time
import urllib.request

import numpy as np
import pytest

from rdf_robot_client.camera_stream import MjpegFeed, serve_mjpeg
from rdf_robot_client.cameras import (
    CameraConfig, CameraRig, Frame, LatestQueue, RigConfig, config_from_dict, load_config,
    nearest_frame, resize_with_pad, synthetic_config, run_capture_check,
)


def _rig(**over) -> CameraRig:
    top = CameraConfig("top", source="synthetic", synthetic_phase_ms=0.0, **over.pop("top", {}))
    wrist = CameraConfig("wrist", source="synthetic", synthetic_phase_ms=over.pop("phase_ms", 10.0),
                         **over.pop("wrist", {}))
    return CameraRig(RigConfig(cameras=[top, wrist], **over))


def _frame(t_ns: int, seq: int = 0) -> Frame:
    return Frame("x", seq, t_ns, 0.0, np.zeros((2, 2, 3), np.uint8))


def test_nearest_frame_rule():
    fs = [_frame(t, i) for i, t in enumerate([0, 33, 66, 100])]
    assert nearest_frame(fs, 40).t_ns == 33
    assert nearest_frame(fs, 50).t_ns == 66
    assert nearest_frame(fs, 49).t_ns == 33
    assert nearest_frame(fs, 83).t_ns == 66   # tie goes to the earlier frame
    assert nearest_frame(fs, -5).t_ns == 0
    assert nearest_frame(fs, 500).t_ns == 100
    assert nearest_frame([], 1) is None


def test_resize_with_pad_policy_shape():
    img = np.full((480, 640, 3), 255, np.uint8)
    out = resize_with_pad(img, 224)
    assert out.shape == (224, 224, 3) and out.dtype == np.uint8
    assert out[0].max() == 0 and out[-1].max() == 0       # letterbox bars top/bottom
    assert out[112].min() == 255                          # content row untouched


def test_default_config_loads_and_synthetic_swap():
    cfg = load_config()
    assert {c.name for c in cfg.cameras} == {"top", "wrist"} and cfg.reference == "top"
    assert cfg.camera("top").policy_key == "observation/image"
    assert cfg.camera("wrist").lerobot_key == "observation.images.wrist"
    syn = synthetic_config(cfg)
    assert all(c.source == "synthetic" for c in syn.cameras)


def test_config_validation():
    with pytest.raises(ValueError):
        config_from_dict({"reference": "nope", "cameras": {"top": {"source": "synthetic"}}})
    with pytest.raises(ValueError):
        CameraConfig("top", rotate=45)


def test_synced_capture_pairs_within_tolerance():
    rig = _rig(phase_ms=10.0)
    with rig:
        report = run_capture_check(rig, seconds=2.0, warmup=0.3)
        s = rig.latest_synced()
    assert report["pass"], report
    for cam in report["per_camera"].values():
        assert cam["dropped"] == 0 and 25 <= cam["fps"] <= 31
    assert 8.0 <= report["sync"]["skew_ms_p50"] <= 12.0     # the 10 ms phase offset is what we measure
    assert s.in_sync and set(s.frames) == {"top", "wrist"}
    imgs = s.policy_images()
    assert set(imgs) == {"observation/image", "observation/wrist_image"}
    assert all(v.shape == (224, 224, 3) for v in imgs.values())
    assert not s.frames["top"].image.flags.writeable       # shared frames are read-only


def test_pairing_picks_nearest_not_latest():
    # wrist leads by 20 ms of a 33 ms period, so the nearest wrist frame is the NEXT one (13 ms away)
    rig = _rig(phase_ms=20.0)
    with rig:
        assert rig.wait_ready(3)
        samples = [rig.wait_synced(1.0) for _ in range(20)]
    skews = [s.skew_ms for s in samples if s is not None]
    assert skews and max(skews) < 16.0, skews


def test_drops_are_counted_and_unsynced_flagged():
    rig = _rig(wrist={"synthetic_drop_prob": 0.2}, pair_tolerance_ms=5.0, phase_ms=10.0)
    with rig:
        time.sleep(2.0)
        st = rig.stats()
    w = st["cameras"]["wrist"]
    assert w["dropped"] > 3 and 5.0 < w["drop_pct"] < 40.0
    assert st["cameras"]["top"]["dropped"] == 0
    assert st["sync"]["out_of_sync"] > 0                   # 10 ms skew > 5 ms tolerance


def test_orientation_rotate_applied():
    rig = _rig(wrist={"rotate": 90})
    with rig:
        assert rig.wait_ready(3)
        assert rig.latest("wrist").image.shape == (640, 480, 3)
        assert rig.latest("top").image.shape == (480, 640, 3)


def test_callbacks_queues_and_unsubscribe():
    rig = _rig()
    seen_frames, seen_synced = [], []
    off_f = rig.add_frame_callback(seen_frames.append)
    off_s = rig.add_synced_callback(seen_synced.append)
    q = rig.subscribe_synced(maxsize=5)
    fq = rig.subscribe_frames("wrist")
    with rig:
        time.sleep(0.6)
        off_f(), off_s()
        n_f, n_s = len(seen_frames), len(seen_synced)
        time.sleep(0.3)
        assert len(seen_frames) == n_f and len(seen_synced) == n_s
        assert fq.get(timeout=1).camera == "wrist"
        assert q.qsize() == 5 and q.dropped > 0          # drop-oldest, never blocks capture
        q.close(), fq.close()
    assert n_s >= 10 and {f.camera for f in seen_frames} == {"top", "wrist"}
    seqs = [s.seq for s in seen_synced]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    ts = [s.t_ns for s in seen_synced]
    assert ts == sorted(ts)                                # monotonic synced timeline


def test_failing_source_reopens(monkeypatch):
    class Flaky:
        opens = 0

        def __init__(self, cfg):
            self.cfg = cfg

        def open(self):
            Flaky.opens += 1
            if Flaky.opens == 1:
                raise RuntimeError("device busy")
            return {"device": "flaky"}

        def read(self):
            time.sleep(1 / 30)
            return np.zeros((48, 64, 3), np.uint8), None

        def close(self):
            pass

    cfg = RigConfig(cameras=[CameraConfig("top", source="synthetic")])
    rig = CameraRig(cfg, source_factory=Flaky)
    with rig:
        assert rig.wait_ready(4)
        st = rig.stats()["cameras"]["top"]
    assert st["reopens"] >= 1 and "device busy" in st["last_error"] and st["ts_source"] == "host"


def test_latest_queue_drop_oldest():
    q = LatestQueue(2)
    for i in range(5):
        q.put(i)
    assert [q.get_nowait(), q.get_nowait()] == [3, 4] and q.dropped == 3


def test_mjpeg_server_snapshot_and_stream():
    rig = _rig().start()
    feed = MjpegFeed(rig, fps=30)
    srv = serve_mjpeg(feed, port=0)
    port = srv.server_address[1]
    try:
        assert rig.wait_ready(3)
        jpg = urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot/top.jpg", timeout=3).read()
        assert jpg[:2] == b"\xff\xd8"
        stats = urllib.request.urlopen(f"http://127.0.0.1:{port}/stats", timeout=3).read()
        assert b'"healthy": true' in stats
        for stream in ("wrist", "both"):
            r = urllib.request.urlopen(f"http://127.0.0.1:{port}/stream/{stream}.mjpg", timeout=3)
            assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            chunk = r.read(4096)
            assert chunk.startswith(b"--frame\r\n") and b"X-Timestamp-Ns:" in chunk and b"\xff\xd8" in chunk
            r.close()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/stream/nope.mjpg", timeout=3)
    finally:
        srv.shutdown()
        feed.close()
        rig.stop()


def test_rig_stop_joins_threads():
    rig = _rig().start()
    assert rig.wait_ready(3)
    rig.stop()
    names = {t.name for t in threading.enumerate()}
    assert not {"cam-top", "cam-wrist", "cam-sync"} & names
    assert not rig.healthy()
