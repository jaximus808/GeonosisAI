"""TDM-118: read-only YAM adapter + client. A fake CAN bus answers like DM motors; nothing touches hardware."""
from __future__ import annotations

import math
import queue
import time

import pytest

from rdf_robot_client.adapters import make_adapter
from rdf_robot_client.adapters.base import ArmError, Pose
from rdf_robot_client.adapters.tdm118_yam_readonly import (
    TDM118_REFRESH_ID,
    ReadOnlyArmError,
    Tdm118ReadOnlyYamArm,
    Tdm118TxGuard,
    tdm118_parse_feedback,
    tdm118_refresh_frame,
)

RANGES = {1: (12.5, 10, 28), 2: (12.5, 10, 28), 3: (12.5, 10, 28), 4: (12.5, 30, 10), 5: (12.5, 30, 10),
          6: (12.5, 30, 10), 7: (12.5, 30, 10)}


def _f2u(x: float, lim: float, bits: int) -> int:
    return round((x + lim) * ((1 << bits) - 1) / (2 * lim))


def tdm118_feedback_frame(mid: int, pos: float, state: int = 0) -> tuple[int, bytes]:
    p_lim, v_lim, t_lim = RANGES[mid]
    p, v, t = _f2u(pos, p_lim, 16), _f2u(0.0, v_lim, 12), _f2u(0.0, t_lim, 12)
    data = bytes([(state << 4) | mid, p >> 8, p & 0xFF, v >> 4, ((v & 0xF) << 4) | (t >> 8), t & 0xFF, 31, 30])
    return 0x10 + mid, data


class Tdm118FakeDmBus:
    """Answers each refresh query with a feedback frame; records every transmitted frame."""

    def __init__(self, positions: dict[int, float], chatter: bool = False, silent: bool = False):
        self.positions = positions
        self.sent: list[tuple[int, bytes]] = []
        self.rx: queue.Queue[tuple[int, bytes]] = queue.Queue()
        self.silent = silent
        self.closed = False
        if chatter:  # another controller is already streaming on this bus
            self.rx.put((0x01, bytes(8)))

    def send(self, arb_id: int, data: bytes) -> None:
        self.sent.append((arb_id, bytes(data)))
        if arb_id == TDM118_REFRESH_ID and data[2] == 0xCC and not self.silent:
            mid = data[0]
            if mid in self.positions:
                self.rx.put(tdm118_feedback_frame(mid, self.positions[mid]))

    def recv(self, timeout: float):
        try:
            return self.rx.get(timeout=max(timeout, 1e-4))
        except queue.Empty:
            return None

    def close(self) -> None:
        self.closed = True


REST = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.5}


@pytest.fixture(autouse=True)
def tdm118_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("YAM_LOCK_DIR", str(tmp_path))
    return tmp_path


def _arm(bus: Tdm118FakeDmBus, **kw) -> Tdm118ReadOnlyYamArm:
    kw.setdefault("vrteleop_check", lambda: False)
    return Tdm118ReadOnlyYamArm(channel="can_right", bus_factory=lambda ch: bus, read_hz=100, kinematics="poe", **kw)


def test_refresh_frame_is_the_dm_status_query():
    assert tdm118_refresh_frame(3) == bytes([3, 0, 0xCC, 0, 0, 0, 0, 0])
    with pytest.raises(ValueError):
        tdm118_refresh_frame(0)


@pytest.mark.parametrize("arb,data", [
    (0x01, bytes([0xFF] * 7 + [0xFC])),  # enable
    (0x01, bytes([0xFF] * 7 + [0xFD])),  # disable
    (0x01, bytes([0xFF] * 7 + [0xFE])),  # save zero
    (0x01, bytes([0xFF] * 7 + [0xFB])),  # clear error
    (0x02, bytes(8)),                    # an MIT control frame
    (0x7FF, bytes([1, 0, 0x55, 0x0A, 0, 0, 0, 0])),  # register write
    (0x7FF, bytes([1, 0, 0xAA, 0, 0, 0, 0, 0])),     # save params to flash
    (0x7FF, bytes([9, 0, 0xCC, 0, 0, 0, 0, 0])),     # refresh, but not a YAM motor id
])
def test_tx_guard_refuses_everything_but_refresh(arb, data):
    bus = Tdm118FakeDmBus(REST)
    g = Tdm118TxGuard(bus, [1, 2, 3, 4, 5, 6, 7])
    with pytest.raises(ReadOnlyArmError):
        g.send(arb, data)
    assert bus.sent == []
    g.send(TDM118_REFRESH_ID, tdm118_refresh_frame(1))
    assert bus.sent == [(TDM118_REFRESH_ID, tdm118_refresh_frame(1))]


def test_parse_feedback_roundtrip():
    arb, data = tdm118_feedback_frame(4, 1.234, state=1)
    fb = tdm118_parse_feedback(arb, data, {4: "DM4310"}, 0.0)
    assert fb is not None and fb.motor_id == 4 and fb.state_text == "enabled"
    assert fb.position == pytest.approx(1.234, abs=1e-3)
    assert tdm118_parse_feedback(0x99, data, {4: "DM4310"}, 0.0) is None


def test_reads_joints_and_never_sends_anything_but_refresh():
    pos = {1: 0.1, 2: 0.5, 3: 0.7, 4: -0.2, 5: 0.05, 6: 2 * math.pi - 0.1, 7: 0.3}  # j6 needs the -2pi wrap
    bus = Tdm118FakeDmBus(pos)
    arm = _arm(bus)
    arm.connect()
    try:
        st = arm.read_state()
        assert st.joints[:5] == pytest.approx((0.1, 0.5, 0.7, -0.2, 0.05), abs=1e-3)
        assert st.joints[5] == pytest.approx(-0.1, abs=1e-3)
        assert st.gripper == 0.0  # uncalibrated
        s = arm.status()
        assert s.connected and not s.enabled and s.fault is None
        assert s.detail["read_only"] and s.detail["gripper_calibrated"] is False
        assert s.detail["gripper_raw"] == pytest.approx(0.3, abs=1e-3)
        for fn in (lambda: arm.command_joints(st.joints), lambda: arm.command_gripper(0.5), lambda: arm.home(),
                   lambda: arm.move_joints(st.joints), lambda: arm.command_ee(Pose((0.3, 0.0, 0.2)))):
            with pytest.raises(ReadOnlyArmError):
                fn()
        arm.hold()  # no-op, never raises
        arm.stop()
        assert arm.status().estopped
    finally:
        arm.disconnect()
    assert bus.sent and all(a == TDM118_REFRESH_ID and d[2] == 0xCC and d[3:] == bytes(5) for a, d in bus.sent)
    assert bus.closed


def test_gripper_normalised_with_limits():
    bus = Tdm118FakeDmBus({**REST, 7: 1.0})
    arm = _arm(bus, gripper_limits=(0.0, 4.0))
    arm.connect()
    try:
        assert arm.read_state().gripper == pytest.approx(0.25, abs=1e-3)
    finally:
        arm.disconnect()


def test_refuses_busy_bus_without_sending():
    bus = Tdm118FakeDmBus(REST, chatter=True)
    with pytest.raises(ArmError, match="busy"):
        _arm(bus).connect()
    assert bus.sent == [] and bus.closed


def test_refuses_while_vrteleop_runs():
    bus = Tdm118FakeDmBus(REST)
    with pytest.raises(ArmError, match="vrteleop"):
        _arm(bus, vrteleop_check=lambda: True).connect()
    assert bus.sent == []


def test_unpowered_arm_reports_fault_not_joints():
    bus = Tdm118FakeDmBus(REST, silent=True)
    arm = _arm(bus)
    arm.connect()
    try:
        time.sleep(1.1)
        with pytest.raises(ArmError, match="no feedback"):
            arm.read_state()
        assert "no feedback" in (arm.status().fault or "")
    finally:
        arm.disconnect()


def test_registered_and_describes_read_only():
    arm = make_adapter("yam_readonly", channel="can_right", kinematics="poe", vrteleop_check=None)
    d = arm.describe()
    assert d["read_only"] is True and d["adapter"] == "yam_readonly" and d["embodiment"] == "i2rt_yam"
    assert d["can_channel"] == "can_right"


def test_client_refuses_every_motion_job():
    from rdf_robot_client.tdm118_readonly import tdm118_handlers, tdm118_refuse

    h = tdm118_handlers()
    for job in ("home", "run_baseline", "run_eval", "start_teleop", "reset_request"):
        assert h[job] is tdm118_refuse
    assert "load_checkpoint" not in h


def test_check_mode_opens_nothing(capsys, monkeypatch):
    from rdf_robot_client import tdm118_readonly

    monkeypatch.setenv("YAM_CAN_CHANNEL", "can_right")
    assert tdm118_readonly.run(["--check", "--no-vrteleop-check"]) == 0
    assert '"read_only": true' in capsys.readouterr().out


def test_tdm120_northstar_camera_config_check(capsys, monkeypatch):
    import json
    from pathlib import Path

    from rdf_robot_client import tdm118_readonly

    cfg = Path(__file__).resolve().parents[1] / "config" / "cameras.yaml"
    monkeypatch.setenv("RDF_CAMERAS_CONFIG", str(cfg))
    assert tdm118_readonly.run(["--check", "--no-vrteleop-check", "--cameras", "config"]) == 0
    out = json.loads(capsys.readouterr().out)
    cams = {c["name"]: c for c in out["camera_rig"]["cameras"]}
    assert set(cams) == {"top", "wrist"} and all(c["fourcc"] == "YUYV" and c["size"] == [640, 360] for c in cams.values())
    assert 15 <= out["camera_rig"]["stream"]["fps"] <= 20


def test_takes_larps_bus_lock(tdm118_lock_dir):
    import fcntl
    import os

    from rdf_robot_client.adapters.yam_larp import larp_lock_path

    fd = os.open(larp_lock_path("can_right"), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    bus = Tdm118FakeDmBus(REST)
    try:
        with pytest.raises(ArmError, match="in use"):
            _arm(bus).connect()
        assert bus.sent == []
    finally:
        os.close(fd)
    arm = _arm(Tdm118FakeDmBus(REST))
    arm.connect()
    arm.disconnect()


def test_disable_query_mode_is_larps_passive_read():
    bus = Tdm118FakeDmBus(REST)
    g = Tdm118TxGuard(bus, [1, 2, 3, 4, 5, 6, 7], disable_query=True)
    g.send(0x03, bytes([0xFF] * 7 + [0xFD]))
    with pytest.raises(ReadOnlyArmError):
        g.send(0x03, bytes([0xFF] * 7 + [0xFC]))  # enable is never allowed


def test_passive_read_prints_joints_and_releases(capsys, monkeypatch):
    import json

    from rdf_robot_client import tdm118_readonly

    bus = Tdm118FakeDmBus({**REST, 2: 0.25})
    arm = _arm(bus)
    assert tdm118_readonly.tdm118_passive_read(arm, 0.0, lambda: False) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["joints"][1] == pytest.approx(0.25, abs=1e-3) and line["fault"] is None
    assert bus.closed and all(a == TDM118_REFRESH_ID for a, _ in bus.sent)
    assert tdm118_readonly.tdm118_passive_read(_arm(Tdm118FakeDmBus(REST)), 0.0, lambda: True) == 3
