"""TDM-55: camera stream server (auth, ack pacing, expiry) against synthetic cameras."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

from rdf_common.stream_token import StreamTokenError, mint, verify
from rdf_robot_client.cameras import CameraRig
from rdf_robot_client.stream import StreamConfig, CameraStreamServer, unpack_frame

SECRET = "tdm55-test-secret-0123456789abcdef"


def test_token_roundtrip_and_tamper():
    tok, _ = mint(SECRET, robot_id=1, task_id=7, sub="contributor:3", ttl_s=30)
    assert verify(SECRET, tok).task_id == 7
    body, sig = tok.split(".")
    with pytest.raises(StreamTokenError) as e:
        verify(SECRET, body[:-2] + "AA." + sig)
    assert e.value.code == "bad_signature"
    with pytest.raises(StreamTokenError) as e:
        verify("another-secret-0123456789abcdef", tok)
    assert e.value.code == "bad_signature"
    old, _ = mint(SECRET, robot_id=1, sub="x", ttl_s=5, now=time.time() - 60)
    with pytest.raises(StreamTokenError) as e:
        verify(SECRET, old)
    assert e.value.code == "expired"


@pytest.fixture(scope="module")
def server():
    rig = CameraRig.from_config(synthetic=True).start()
    assert rig.wait_ready(5)
    srv = CameraStreamServer(rig, StreamConfig(port=0, robot_id=1, secret=SECRET, fps=30)).start()
    yield srv
    srv.stop()
    rig.stop()


def url(srv) -> str:
    return f"ws://127.0.0.1:{srv.port}/ws/cameras"


async def _connect(srv, token: str):
    ws = await websockets.connect(url(srv), max_size=None)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws


def test_no_frames_without_valid_token(server):
    async def go():
        for tok in ["garbage", mint(SECRET, robot_id=2, sub="x")[0]]:
            ws = await _connect(server, tok)
            msg = json.loads(await ws.recv())
            assert msg["type"] == "error"
            with pytest.raises(websockets.ConnectionClosed) as e:
                await ws.recv()
            assert e.value.rcvd.code == 4003
    asyncio.run(go())


def test_frames_and_ack_pacing(server):
    async def go():
        ws = await _connect(server, mint(SECRET, robot_id=1, sub="t")[0])
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and set(hello["cams"]) == {"top", "wrist"}
        # without acks the server stops at max_in_flight frames per camera
        got: dict[str, int] = {}
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            try:
                m = await asyncio.wait_for(ws.recv(), 0.3)
            except asyncio.TimeoutError:
                continue
            if isinstance(m, bytes):
                cam, cap, sent, seq, jpg = unpack_frame(m)
                assert jpg[:2] == b"\xff\xd8" and sent >= cap
                got[cam] = got.get(cam, 0) + 1
        assert got == {"top": 2, "wrist": 2}
        for cam in ("top", "wrist"):
            for _ in range(2):
                await ws.send(json.dumps({"type": "ack", "cam": cam}))
        # with acks it flows at ~fps
        n = 0
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            m = await ws.recv()
            if isinstance(m, bytes):
                cam, _, _, seq, _ = unpack_frame(m)
                n += 1
                await ws.send(json.dumps({"type": "ack", "cam": cam, "seq": seq}))
        assert n > 30
        await ws.close()
    asyncio.run(go())


def test_expiry_closes_and_refresh_keeps_open(server):
    async def go():
        ws = await _connect(server, mint(SECRET, robot_id=1, sub="t", ttl_s=1)[0])
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "auth", "token": mint(SECRET, robot_id=1, sub="t", ttl_s=30)[0]}))
        t_end = time.monotonic() + 4.5  # past the first token's exp + 2 s grace
        saw_ok = False
        while time.monotonic() < t_end:
            m = await asyncio.wait_for(ws.recv(), 2)
            if isinstance(m, str) and json.loads(m)["type"] == "auth_ok":
                saw_ok = True
        assert saw_ok
        await ws.close()

        ws = await _connect(server, mint(SECRET, robot_id=1, sub="t", ttl_s=1)[0])
        with pytest.raises(websockets.ConnectionClosed) as e:
            while True:
                await asyncio.wait_for(ws.recv(), 6)
        assert e.value.rcvd.code == 4001
    asyncio.run(go())
