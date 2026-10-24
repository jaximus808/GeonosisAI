"""TDM-96: /ws/state live robot state + view-only token scoping (SimArm + SafetyLayer, no SpacetimeDB).

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm96_state_feed.py -v
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time

import pytest
import websockets
from rdf_common.stream_token import StreamTokenError, mint, verify

from rdf_robot_client.adapters.base import JointTarget
from rdf_robot_client.adapters.sim import SimArm
from rdf_robot_client.cameras import CameraRig
from rdf_robot_client.rollout import StandaloneRobot
from rdf_robot_client.safety import SafetyLayer
from rdf_robot_client.state import RobotState as S
from rdf_robot_client.stream import (CLAMP_KEYS, CameraStreamServer, LiveStateSampler, StreamConfig, StreamHandle,
                                     install_state_feed, scene_for_contract)
from rdf_robot_client.tdm57_lease import ControlLeaseGuard
from rdf_robot_client.teleop import CLOSE_FORBIDDEN, hub_for, install_control

SECRET = "tdm96-test-secret-0123456789abcdef"
TASK, CONTRIB, ROBOT = 7, 3, 1
GEN = int((time.time() - 5) * 1e6)
CONTRACT_KEYS = {"t_ms", "joints", "gripper", "ee", "control_source", "robot_state", "estop", "safety"}


@pytest.fixture(scope="module")
def rig():
    r = CameraRig.from_config(synthetic=True).start()
    assert r.wait_ready(10)
    yield r
    r.stop()


class Bench:
    def __init__(self, rig):
        self.arm = SafetyLayer(SimArm())
        self.arm.connect()
        self.robot = StandaloneRobot(self.arm, services={"cameras": rig})
        self.robot.current_job = None
        self.robot.checkpoint_uri = "modal-vol://rdf-checkpoints/runs/test/2000"
        tasks = {TASK: {"id": TASK, "robot_id": ROBOT, "status": "claimed", "assignee_id": CONTRIB,
                        "claimed_at": GEN, "lease_expires_at": int((time.time() + 600) * 1e6),
                        "type": "clean_teleop", "ticket_id": 1}}
        self.hub = hub_for(self.robot, guard_factory=lambda c: ControlLeaseGuard(
            c, lambda tid: dict(tasks[tid]) if tid in tasks else None, robot_id=ROBOT, recheck_s=0.2))
        self.server = CameraStreamServer(rig, StreamConfig(port=0, robot_id=ROBOT, secret=SECRET)).start()
        install_control(self.server, self.hub)
        self.feed = install_state_feed(self.server, self.robot, hz=30, sim=True)
        self.base = f"ws://127.0.0.1:{self.server.port}"

    def token(self, **kw):
        args = dict(robot_id=ROBOT, task_id=TASK, sub=f"contributor:{CONTRIB}", ttl_s=60, claimed_at_us=GEN)
        args.update(kw)
        return mint(SECRET, **args)[0]

    def close(self):
        self.server.stop()
        self.hub.close()
        self.arm.disconnect()


@pytest.fixture
def bench(rig):
    b = Bench(rig)
    yield b
    b.close()


async def _auth(url, token):
    ws = await websockets.connect(url, max_size=1 << 20)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    return ws, json.loads(await ws.recv())


async def _closed_code(ws):
    with pytest.raises(websockets.ConnectionClosed) as e:
        while True:
            await ws.recv()
    return e.value.rcvd.code


# ----------------------------------------------------------------------------- token scoping

def test_scope_roundtrip_and_legacy_default():
    tok, c = mint(SECRET, robot_id=1, sub="operator:view", scope="view")
    assert verify(SECRET, tok).scope == "view" and not verify(SECRET, tok).can_control
    assert verify(SECRET, mint(SECRET, robot_id=1, sub="x")[0]).scope == "control"
    with pytest.raises(ValueError):
        mint(SECRET, robot_id=1, sub="x", scope="admin")
    # a token minted before TDM-96 (no scope key) is a control token
    import base64
    import hashlib
    import hmac as _h
    body = base64.urlsafe_b64encode(json.dumps({"v": 1, "robot_id": 1, "sub": "x", "exp": time.time() + 30}).encode()
                                    ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(_h.new(SECRET.encode(), b"rdf-stream-v1." + body.encode(), hashlib.sha256).digest()
                                   ).rstrip(b"=").decode()
    assert verify(SECRET, f"{body}.{sig}").scope == "control"


def test_view_token_watches_state_and_cameras_but_cannot_drive(bench):
    view = bench.token(task_id=0, sub="operator:view", scope="view", claimed_at_us=0)

    async def go():
        ws, hello = await _auth(bench.base + "/ws/state", view)
        assert hello["t"] == "hello" and hello["protocol"] == "rdf-state/1" and hello["scope"] == "view"
        st = json.loads(await ws.recv())
        assert st["t"] == "state" and CONTRACT_KEYS <= st.keys()
        await ws.close()
        ws, hello = await _auth(bench.base + "/ws/cameras", view)
        assert hello["type"] == "hello"
        await ws.close()
        for path in ("/ws/control", "/control"):
            ws, m = await _auth(bench.base + path, view)
            assert m["code"] == "view_only"
            assert await _closed_code(ws) == CLOSE_FORBIDDEN
        # the claimant's control token still drives
        ws, hello = await _auth(bench.base + "/ws/control", bench.token())
        assert hello["t"] == "hello"
        # ... and cannot be swapped for a view token mid-session
        await ws.send(json.dumps({"t": "auth", "token": view}))
        for _ in range(50):
            m = json.loads(await ws.recv())
            if m.get("t") == "error":
                assert m["code"] == "view_only"
                break
        else:
            raise AssertionError("no view_only refusal on the control socket")
        await ws.close()

    asyncio.run(go())


def test_state_refuses_bad_tokens(bench):
    async def go():
        for tok in ("garbage", mint(SECRET, robot_id=2, sub="x", scope="view")[0]):
            ws, m = await _auth(bench.base + "/ws/state", tok)
            assert m["t"] == "error"
            assert await _closed_code(ws) == CLOSE_FORBIDDEN
        ws = await websockets.connect(bench.base + "/ws/state")
        await ws.send(json.dumps({"type": "ping", "c": 1}))  # no auth first
        m = json.loads(await ws.recv())
        assert m["code"] == "auth_required" and await _closed_code(ws) == CLOSE_FORBIDDEN

    asyncio.run(go())


def test_state_expiry_closes(bench):
    async def go():
        tok = mint(SECRET, robot_id=ROBOT, sub="operator:view", scope="view", ttl_s=1)[0]
        ws, hello = await _auth(bench.base + "/ws/state", tok)
        assert hello["t"] == "hello"
        t0 = time.monotonic()
        assert await _closed_code(ws) == 4001
        assert time.monotonic() - t0 < 6

    asyncio.run(go())


# ----------------------------------------------------------------------------- the feed

def test_state_rate_contents_and_motion(bench):
    """>= 20 Hz to two viewers at once; joints track a moving arm; contract fields and units."""
    stop = threading.Event()

    def wiggle():  # stream joint targets at the arm's control rate, like a policy would
        q0 = list(bench.arm.read_state().joints)
        bench.robot.sm.transition(S.ROLLOUT, "test")
        t0 = time.monotonic()
        while not stop.is_set():
            t = time.monotonic() - t0
            q = [v + 0.15 * math.sin(2 * math.pi * 0.5 * t) for v in q0]
            tgt = JointTarget(tuple(q), gripper=0.5 + 0.4 * math.sin(t))
            bench.arm.command(tgt)
            bench.robot.record_command(tgt, "policy")
            time.sleep(1 / bench.arm.control_rate_hz)

    th = threading.Thread(target=wiggle, daemon=True)
    th.start()
    view = bench.token(task_id=0, sub="operator:view", scope="view", claimed_at_us=0)

    async def collect(seconds):
        ws, _ = await _auth(bench.base + "/ws/state", view)
        out = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            m = json.loads(await asyncio.wait_for(ws.recv(), 2))
            if m.get("t") == "state":
                m["_rx_ms"] = time.time() * 1000
                out.append(m)
        await ws.close()
        return out

    async def go():
        return await asyncio.gather(collect(2.0), collect(2.0))

    try:
        a, b = asyncio.run(go())
    finally:
        stop.set()
        th.join(2)
    for states in (a, b):
        assert len(states) / 2.0 >= 20, f"only {len(states) / 2.0:.1f} Hz"
    s = a[-1]
    assert len(s["joints"]) == 6 and 0.0 <= s["gripper"] <= 1.0
    assert len(s["ee"]["pos"]) == 3 and abs(math.hypot(*s["ee"]["quat"]) - 1) < 1e-3
    assert s["control_source"] == "policy" and s["robot_state"] == "rollout" and s["estop"] is False
    assert s["sim"] is True and s["checkpoint_uri"].startswith("modal-vol://")
    assert set(s["safety"]["clamps_1s"]) == set(CLAMP_KEYS)
    assert s["workspace"]["max"][2] > s["workspace"]["table_z"] == s["workspace"]["min"][2]
    assert "scene" not in s  # SimArm has no scene_state(); only the MuJoCo sim publishes one
    j0 = [m["joints"][0] for m in a]
    assert max(j0) - min(j0) > 0.1  # it moved
    lat = sorted(m["_rx_ms"] - m["t_ms"] for m in a)
    assert lat[len(lat) // 2] < 50  # same machine: sample -> receive


def test_estop_and_idle_source(bench):
    async def one():
        ws, _ = await _auth(bench.base + "/ws/state", bench.token(scope="view"))
        m = json.loads(await ws.recv())
        await ws.close()
        return m

    m = asyncio.run(one())
    assert m["control_source"] == "idle" and m["estop"] is False
    bench.arm.stop()
    bench.robot.sm.lock("test")
    m = asyncio.run(one())
    assert m["estop"] is True and m["robot_state"] == "locked" and m["safety"]["clamps_1s"]["estops"] >= 0


def test_clamps_1s_window():
    class _A:
        name = "sim"

        def stats(self):
            return {}
    s = LiveStateSampler(type("C", (), {"arm": _A(), "sm": None})(), sim=True)
    assert s.clamps_1s({"clamped_speed": 5}, now=100.0)["clamped_speed"] == 0
    assert s.clamps_1s({"clamped_speed": 9}, now=100.5)["clamped_speed"] == 4
    assert s.clamps_1s({"clamped_speed": 9}, now=101.6)["clamped_speed"] == 0  # older than 1 s dropped


def test_scene_hook_and_stream_info(bench, rig):
    class SceneArm(SafetyLayer):
        def scene_state(self):
            return {"cube": {"pos": [0.3, 0.0, 0.02], "quat": [1, 0, 0, 0]}, "plate": {"pos": [0.35, 0.1, 0.0]}}

    arm = SceneArm(SimArm())
    arm.connect()
    try:
        m = LiveStateSampler(StandaloneRobot(arm), sim=True).sample()
        assert m["scene"]["cube"]["pos"] == [0.3, 0.0, 0.02]
    finally:
        arm.disconnect()
    try:  # live never carries scene poses, even if an adapter offers them
        arm2 = SceneArm(SimArm())
        arm2.connect()
        assert "scene" not in LiveStateSampler(StandaloneRobot(arm2), sim=False).sample()
    finally:
        arm2.disconnect()
    # the MuJoCo sim adapter's shape (cube.xyz, plate.xy + top_z) is mapped to the contract
    raw = {"sim": True, "cube": {"xyz": [0.4, 0.1, 0.03], "quat": [1, 0, 0, 0], "half": 0.02},
           "plate": {"xy": [0.35, -0.1], "radius": 0.08, "top_z": 0.012}, "joints": [0] * 6, "task": {}}
    assert scene_for_contract(raw) == {"cube": {"pos": [0.4, 0.1, 0.03], "quat": [1.0, 0.0, 0.0, 0.0]},
                                       "plate": {"pos": [0.35, -0.1, 0.012]}}
    assert scene_for_contract({"cube": {"xyz": [1, 2]}}) is None
    info = StreamHandle(bench.server, None, "10.0.0.5").info()["stream"]
    assert info["state_url"] == f"ws://10.0.0.5:{bench.server.port}/ws/state"
    assert bench.server.health()["state"]["hz"] == 30
