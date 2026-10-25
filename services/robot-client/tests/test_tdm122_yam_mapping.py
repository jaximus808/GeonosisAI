"""TDM-122: the 7-dim YAM state/action mapping between the robot client and the openpi pi0.5 policy server,
checked offline against REAL data (no GPU, no network, no arm):

  * ``fixtures/tdm122_allenai_yam01_excerpt.json``: hf://allenai/24112025-yam-01 (the real-YAM dataset the
    TDM-40 smoke fine-tune trained on) - its own feature schema, the converted v2.1 schema, 4 s of left-arm
    state/command rows - plus the REAL smoke checkpoint's ``rdf_policy.json`` and norm stats
    (modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199).
    Rebuild: spikes/tdm-122/make_fixture.py.
  * the policy server's pure-Python half (``services/training/src/rdf_training/policy_server.py``).
  * the dry-run harness (``rdf_robot_client.tdm122_dryrun``).
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

from rdf_robot_client.adapters.base import ArmState, Pose
from rdf_robot_client.adapters.yam import ACTION_SPACE
from rdf_robot_client.adapters.yam_kinematics import SDK_CLIP_BUFFER, YAM_JOINT_NAMES, YAM_V1_LIMITS
from rdf_robot_client.cameras import DEFAULT_KEYS, POLICY_SIZE, load_config
from rdf_robot_client.policy_client import IMAGE_KEYS, Observation, build_request
from rdf_robot_client.rollout import ExecConfig, map_actions
from rdf_robot_client.tdm122_dryrun import (
    YAM_READY,
    ChunkChecker,
    Sample,
    ShadowYamArm,
    mapping_preflight,
    parse_state,
    run,
)

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "services" / "training" / "src"))
from rdf_training import policy_server as ps  # noqa: E402 - pure Python; openpi/jax/PIL load lazily

FX = json.loads((Path(__file__).parent / "fixtures" / "tdm122_allenai_yam01_excerpt.json").read_text())
NS = FX["checkpoint"]["norm_stats"]["norm_stats"]
SIDECAR = FX["checkpoint"]["rdf_policy"]
ROWS = FX["episode_rows"]
JP = np.asarray(ROWS["joint_pos"], float)
GP = np.asarray(ROWS["gripper_pos"], float)
JC = np.asarray(ROWS["joint_cmd"], float)
GC = np.asarray(ROWS["gripper_cmd"], float)


def _cv2_decode_rgb(b64: str) -> np.ndarray:
    """What the server's PIL decoder returns (RGB uint8), without PIL in this venv."""
    bgr = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def _ckpt_dir(tmp_path: Path, sidecar: bool = True) -> Path:
    d = tmp_path / "199"
    (d / "params").mkdir(parents=True)
    a = d / "assets" / SIDECAR["asset_id"]
    a.mkdir(parents=True)
    (a / "norm_stats.json").write_text(json.dumps(FX["checkpoint"]["norm_stats"]))
    (d / "params" / "_METADATA").write_text('{"lora_a": 1}')
    if sidecar:
        (d / "rdf_policy.json").write_text(json.dumps(SIDECAR))
    return d


# ----------------------------------------------------------------------------- joint order / dims / units


def test_joint_order_matches_between_dataset_and_robot_client():
    src = FX["source_info"]["features"]
    left = [n for n in src["observation.state"]["names"] if n.startswith("left_")]
    assert left == [*(f"left_joint_{i}.pos" for i in range(6)), "left_gripper.pos"]
    assert src["action"]["names"][:7] == left  # action uses the same channel order as state
    # SDK index i (LeRobot "joint_i") is our joint{i+1}; the import took channels 0..5 + the gripper, in order
    assert FX["import_meta"]["source"]["joint_channels"] == [0, 1, 2, 3, 4, 5]
    assert FX["import_meta"]["source"]["gripper_channel"] == "left_gripper.pos"
    conv = FX["converted_info"]["features"]
    assert conv["observation.state"]["shape"] == [7] and conv["action"]["shape"] == [7]
    assert conv["observation.state"]["names"][-1] == "gripper"  # gripper LAST
    # the robot client records/sends the same layout: joint1..joint6 (SDK order) then the gripper
    assert ACTION_SPACE["state"] == ACTION_SPACE["action"] == [*YAM_JOINT_NAMES, "gripper"]
    assert YAM_JOINT_NAMES == [f"joint{i + 1}" for i in range(6)]
    st = ArmState(t=0.0, joints=tuple(range(6)), gripper=0.7, ee_pose=Pose((0, 0, 0), (1, 0, 0, 0)))
    assert st.state_vector() == [0, 1, 2, 3, 4, 5, 0.7]


def test_units_are_radians_inside_the_yam_joint_ranges():
    lo = np.asarray(YAM_V1_LIMITS.lower) - SDK_CLIP_BUFFER
    hi = np.asarray(YAM_V1_LIMITS.upper) + SDK_CLIP_BUFFER
    for arr in (JP, JC, np.asarray(NS["state"]["q01"])[None, :6], np.asarray(NS["state"]["q99"])[None, :6]):
        assert np.all(arr >= lo - 1e-6) and np.all(arr <= hi + 1e-6), "not radians in YAM SDK joint order?"
    # degrees would blow past +/- pi on the big joints; the real data spans ~2 rad on joint 2
    assert np.abs(JP).max() < np.pi and np.ptp(np.r_[NS["state"]["q01"][1], NS["state"]["q99"][1]]) > 1.0
    # signs/offsets agree with our model: joints 2/3 positive, joint 4 negative around our ready pose
    mean = np.asarray(NS["state"]["mean"][:6])
    assert mean[1] > 0.3 and mean[2] > 0.3 and mean[3] < 0
    assert all(np.sign(mean[i]) == np.sign(YAM_READY[i]) for i in (1, 2, 3))


def test_gripper_is_normalised_0_closed_to_1_open_and_absolute():
    for k in ("q01", "q99"):
        assert 0.0 <= NS["state"][k][6] <= 1.0 and 0.0 <= NS["actions"][k][6] <= 1.0
    assert GP.min() >= -0.05 and GP.max() <= 1.05
    assert "gripper 0=closed..1=open" in ACTION_SPACE["action_units"]
    # the gripper action is ABSOLUTE (same distribution as the gripper state), the joints are DELTAS
    assert abs(NS["actions"]["mean"][6] - NS["state"]["mean"][6]) < 0.01
    assert max(abs(v) for v in NS["actions"]["mean"][:6]) < 0.02 < min(abs(v) for v in NS["state"]["mean"][1:4])
    # the rollout passes a joint_pos_abs gripper through as is (no 1 - g inversion, unlike DROID)
    acts = np.c_[np.zeros((3, 6)), [0.0, 0.4, 1.0]]
    out = map_actions(acts, "joint_pos_abs", np.zeros(7), ExecConfig(), np.ones(6))
    assert out[:, 6].tolist() == [0.0, 0.4, 1.0]


# ----------------------------------------------------------------------------- the checkpoint the server will load


def test_server_reads_the_real_smoke_checkpoint_as_the_7dim_yam_spec(tmp_path):
    for sidecar in (True, False):
        spec = ps.infer_spec(_ckpt_dir(tmp_path / str(sidecar), sidecar=sidecar))
        assert (spec.layout, spec.state_dim, spec.action_dim, spec.joint_dims) == ("single_arm", 7, 7, 6)
        assert spec.asset_id == "rdf-imports/tdm40-yam-allenai-left" and spec.mode == "lora"
        assert spec.action_horizon == 50 and spec.action_space == "joint_pos_abs"
    assert (ps.YAM_SPEC.state_dim, ps.YAM_SPEC.action_dim, ps.YAM_SPEC.joint_dims) == (7, 7, 6)
    # LoRA rank: the server builds openpi's default-rank LoRA. A run trained with another rank won't load.
    assert SIDECAR["lora_rank"] == 16


def test_delta_training_and_absolute_output_round_trip_on_real_rows():
    """openpi DeltaActions(mask 6 x True + gripper False) at train time, AbsoluteActions at inference."""
    mask = np.array([True] * 6 + [False])
    state = np.r_[JP[0], GP[0]]
    chunk = np.c_[JC[:50], GC[:50]]  # what the dataset's ``action`` holds: absolute joint targets + gripper
    deltas = chunk - np.where(mask, state, 0.0)
    back = deltas + np.where(mask, state, 0.0)
    assert np.allclose(back, chunk)
    assert np.allclose(deltas[:, 6], chunk[:, 6])  # gripper untouched
    # the normalised deltas sit inside the checkpoint's action quantiles (same mapping it was trained on)
    q01, q99 = np.asarray(NS["actions"]["q01"]), np.asarray(NS["actions"]["q99"])
    inside = (deltas >= q01 - 0.25) & (deltas <= q99 + 0.25)
    assert inside.mean() > 0.95


# ----------------------------------------------------------------------------- the wire: client -> server obs


def test_wire_round_trip_keeps_state_order_camera_order_and_rgb():
    top = np.zeros((POLICY_SIZE, POLICY_SIZE, 3), np.uint8)
    top[..., 0] = 220  # RGB red
    wrist = np.zeros_like(top)
    wrist[..., 2] = 220  # RGB blue
    state = [*JP[5], GP[5]]
    req = json.loads(json.dumps(build_request(Observation(images=[top, wrist], state=state, prompt="Fold the cloth",
                                                          t_obs=0.0, checkpoint_uri=FX["checkpoint"]["uri"]))))
    assert req["checkpoint_uri"] == FX["checkpoint"]["uri"] and len(req["images"]) == 2
    obs = ps.build_obs(req, ps.PolicySpec(
        layout="single_arm", state_dim=7, action_dim=7, joint_dims=6, asset_id=SIDECAR["asset_id"]),
        decode=_cv2_decode_rgb)
    assert set(obs) == {"observation/image", "observation/wrist_image", "observation/state", "prompt"}
    assert obs["observation/image"].shape == (224, 224, 3) == obs["observation/wrist_image"].shape
    r, b = obs["observation/image"].mean((0, 1)), obs["observation/wrist_image"].mean((0, 1))
    assert r[0] > 150 > r[2] and b[2] > 150 > b[0], "camera order or RGB/BGR swapped on the wire"
    assert np.allclose(obs["observation/state"], state, atol=1e-5) and obs["observation/state"].dtype == np.float32
    assert obs["prompt"] == "Fold the cloth"


@pytest.mark.parametrize("n", [6, 8])
def test_server_refuses_a_state_that_is_not_7_long(n):
    spec = ps.PolicySpec(layout="single_arm", state_dim=7, action_dim=7, joint_dims=6, asset_id="x")
    with pytest.raises(ps.PolicyError) as e:
        ps.build_obs({"state": [0.0] * n}, spec, decode=_cv2_decode_rgb)
    assert e.value.code == "bad_state"


def test_camera_keys_and_sizes_line_up():
    # robot rig -> policy keys -> openpi slots: top = base camera, wrist = left wrist slot
    assert IMAGE_KEYS == ("observation/image", "observation/wrist_image")
    assert DEFAULT_KEYS["top"] == ("observation.images.top", "observation/image")
    assert DEFAULT_KEYS["wrist"] == ("observation.images.wrist", "observation/wrist_image")
    cfg = load_config(REPO / "services" / "robot-client" / "config" / "cameras.yaml")
    assert {c.name: c.policy_key for c in cfg.cameras} == {"top": "observation/image", "wrist": "observation/wrist_image"}
    assert cfg.policy_size == ps.IMAGE_SIZE == 224
    conv = FX["converted_info"]["features"]
    assert {"observation.images.top", "observation.images.wrist"} <= set(conv)
    # the real dataset: exo <- its top camera, wrist <- the LEFT arm's wrist camera (the arm we kept)
    cams = FX["import_meta"]["cameras"]
    assert cams["exo"]["source_key"] == "observation.images.top" and cams["wrist"]["source_key"] == "observation.images.left"
    # aspect: the dataset is 640x360 (16:9); our cameras.yaml records 640x480 (4:3). Both are letterboxed
    # to 224, so a checkpoint trained on our own recordings sees the same geometry live.
    assert conv["observation.images.top"]["shape"] == [360, 640, 3]


def test_chunk_horizon_matches_the_rollout_schedule():
    c = ExecConfig()
    assert SIDECAR["action_horizon"] == 50 and SIDECAR["fps"] == 30 and round(1 / c.period_s) == 30
    assert c.requery_every < c.hold_at < SIDECAR["action_horizon"]
    acts = np.c_[JC[:50], GC[:50]]
    out = map_actions(acts, "joint_pos_abs", np.r_[JP[0], GP[0]], c, np.ones(6))
    assert out.shape == (50, 7) and np.allclose(out[:, :6], JC[:50])


# ----------------------------------------------------------------------------- dry-run harness


@pytest.fixture(scope="module")
def checker():
    return ChunkChecker()


def test_dryrun_real_chunk_is_clamped_not_rejected(checker):
    """Real teleop speed (allenai) vs our unverified YAM caps: the layer slows it down, never refuses it."""
    res = checker.check(np.c_[JC[:50], GC[:50]], "joint_pos_abs", [*JP[0], GP[0]])
    assert res["shape"] == [50, 7] and res["verdict"] == "clamp", res
    assert not any(k.startswith("rejected") for k in res["replay"])
    assert res["replay"]["sent"] == 50
    assert res["static"]["first_step_jump_rad"] < checker.env.max_jump_rad
    assert checker.env.speed_scale <= 0.3 and not checker.env.verified  # unverified profile: speed capped


def test_dryrun_flags_a_jump_nan_and_droid_chunks(checker):
    st = [*YAM_READY, 1.0]
    jump = np.tile(np.r_[YAM_READY, 1.0], (50, 1))
    jump[:, 0] += 0.6
    r = checker.check(jump, "joint_pos_abs", st)
    assert r["verdict"] == "reject" and "REJECT" in " ".join(r["problems"])
    nan = jump.copy()
    nan[3, 2] = np.nan
    assert checker.check(nan, "joint_pos_abs", st)["verdict"] == "reject"
    droid = np.zeros((50, 8))
    r = checker.check(droid, "droid_base", st)
    assert r["verdict"] == "unmapped" and "MEANINGLESS" in " ".join(r["problems"])
    hold = np.tile(np.r_[YAM_READY, 0.5], (50, 1))
    assert checker.check(hold, "joint_pos_abs", st)["verdict"] == "ok"


def test_dryrun_never_touches_hardware_and_reports_latency():
    from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule
    from rdf_robot_client.policy_client import PolicyClient, PolicyConfig

    img = [np.full((224, 224, 3), 90, np.uint8), np.full((224, 224, 3), 160, np.uint8)]
    samples = (Sample(state=[*JP[i], GP[i]], images=img, label=f"row {i}", meta={"task": "Fold the cloth"})
               for i in range(0, 60, 10))
    fake = FakePolicyServer(schedule=jitter_schedule(20, 5)).start()
    try:
        chk = ChunkChecker()
        with PolicyClient(PolicyConfig(backend="spike", url=fake.url)) as pc:
            rep = run(samples, pc, chk, prompt=None, checkpoint_uri=None, calls=5, warmup=1)
        assert rep["calls"] == 5 and rep["warmup_calls"] == 1
        assert rep["latency"]["ok"] == 5 and rep["latency"]["rtt_p50_ms"] > 0
        assert sum(rep["verdicts"].values()) == 5 and "reject" not in rep["verdicts"]
        assert all(r["prompt"] == "Fold the cloth" for r in rep["rows"])
        assert all(len(c.state) == 7 for c in fake.calls)
        assert isinstance(chk.arm, ShadowYamArm) and chk.layer.inner is chk.arm
    finally:
        fake.stop()


def test_dryrun_preflight_and_state_parsing():
    base = {"active_checkpoint_uri": ps.BASE_URI, "active": {"spec": {"action_space": "droid_base", "state_dim": 8}}}
    assert not mapping_preflight(base, None)["yam_ok"]
    yam = {"active_checkpoint_uri": FX["checkpoint"]["uri"],
           "active": {"spec": {"action_space": "joint_pos_abs", "state_dim": 7, "action_dim": 7, "joint_dims": 6}}}
    assert mapping_preflight(yam, FX["checkpoint"]["uri"])["yam_ok"]
    assert mapping_preflight(yam, FX["checkpoint"]["uri"] + "/params")["yam_ok"]
    p = mapping_preflight(yam, "modal-vol://rdf-checkpoints/runs/ticket-9/x/1999")
    assert not p["yam_ok"] and "checkpoint_mismatch" in p["problems"][0]
    so100 = {"active": {"spec": {"action_space": "joint_pos_abs", "state_dim": 6, "action_dim": 6, "joint_dims": 5}}}
    assert not mapping_preflight(so100, None)["yam_ok"]
    assert parse_state("fixed:ready") == [*YAM_READY, 1.0]
    assert parse_state("0,0,0,0,0,0,0.5") == [0, 0, 0, 0, 0, 0, 0.5]
    with pytest.raises(SystemExit):
        parse_state("fixed:1,2,3")


def test_dryrun_stream_source_reads_state_and_snapshots_read_only():
    """The live path on the Jetson: /ws/state + /snapshot/<cam>.jpg of a running stream server (view token)."""
    from rdf_common.stream_token import mint
    from rdf_robot_client.adapters.sim import SimArm
    from rdf_robot_client.cameras import CameraRig
    from rdf_robot_client.rollout import StandaloneRobot
    from rdf_robot_client.safety import SafetyLayer
    from rdf_robot_client.stream import CameraStreamServer, StreamConfig, install_state_feed
    from rdf_robot_client.tdm122_dryrun import StreamSource

    secret = "tdm122-test-secret-0123456789abcdef"
    rig = CameraRig.from_config(synthetic=True).start()
    arm = SafetyLayer(SimArm())
    arm.connect()
    robot = StandaloneRobot(arm, services={"cameras": rig})
    robot.current_job = None
    server = None
    try:
        assert rig.wait_ready(10)
        server = CameraStreamServer(rig, StreamConfig(port=0, robot_id=5, secret=secret)).start()
        install_state_feed(server, robot, hz=30, sim=True)
        tok = mint(secret, robot_id=5, sub="tdm122-dryrun", ttl_s=60, scope="view")[0]
        src = StreamSource(f"http://127.0.0.1:{server.port}", tok)
        try:
            s1, s2 = src.sample(), src.sample()
        finally:
            src.close()
        for s in (s1, s2):
            assert len(s.state) == 7 and 0.0 <= s.state[6] <= 1.0
            assert np.allclose(s.state[:6], arm.read_state().joints, atol=1e-3)
            assert [im.shape for im in s.images] == [(224, 224, 3)] * 2
        assert s1.meta["sim"] is True
    finally:
        if server is not None:
            server.stop()
        arm.disconnect()
        rig.stop()


# ----------------------------------------------------------------------------- jumps: first policy action + home


class _OneChunkPolicy:
    """In-process policy: answers every request on the next poll with the same absolute chunk."""

    def __init__(self, chunk: np.ndarray) -> None:
        self.chunk, self.pending, self.n = chunk, None, 0

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def submit(self, obs):
        if self.pending is not None:
            return None
        self.n += 1
        from rdf_robot_client.policy_client import PolicyReply

        self.pending = PolicyReply(self.n, obs.step, obs.t_obs, obs.t_obs, obs.t_obs, actions=self.chunk,
                                   action_space="joint_pos_abs")
        return self.n

    def poll(self):
        out, self.pending = ([self.pending] if self.pending else []), None
        return out


def _shadow_layer(clock):
    from rdf_robot_client.safety import SafetyLayer, load_safety_config

    arm = ShadowYamArm(joints=YAM_READY, gripper=1.0)
    return arm, SafetyLayer(arm, load_safety_config("yam"), clock=lambda: clock[0], start_watchdog=False)


def _drive(ex, layer, clock, ticks, start_tick=0):
    from rdf_robot_client.rollout import ChunkExecutor  # noqa: F401 - same executor the rollout uses

    traj = []
    for i in range(start_tick, start_tick + ticks):
        clock[0] += 1 / 30
        st = layer.read_state()
        tgt = ex.tick(i, clock[0], st)
        layer.command_joints(tuple(tgt[:6]), float(tgt[6]))
        traj.append(layer.read_state().joints)
    return np.asarray(traj)


def test_first_policy_action_1rad_away_ramps_from_the_measured_pose_without_a_rejected_jump():
    from rdf_robot_client.rollout import ChunkExecutor

    clock = [0.0]
    arm, layer = _shadow_layer(clock)
    goal = np.r_[YAM_READY, 0.5]
    goal[0] += 1.0  # the policy's first chunk asks for joint 1 one radian away from where the arm is
    cfg = ExecConfig()
    ex = ChunkExecutor(_OneChunkPolicy(np.tile(goal, (50, 1))),
                       lambda s, n, st: Observation([], list(st.state_vector()), "p", n, s), cfg, layer.limits.max_velocity)
    traj = _drive(ex, layer, clock, 30 * 30)
    stats = layer.stats()
    assert stats["rejected_jump"] == 0 and stats["consecutive_rejects"] == 0
    # the executor's targets ramp at <= speed_fraction x the live cap (0.1 rad/s on joint 1 at the unverified
    # 0.25 scale); the arm (behind the layer's acceleration limiter) never exceeds the cap itself (0.2 rad/s)
    assert ex.stats.max_jump_rad > 0.2 and ex.stats.rate_limited_ticks > 250  # raw chunk was far; it was rate-limited
    steps = np.abs(np.diff(np.vstack([YAM_READY, traj]), axis=0))
    assert np.all(steps <= np.asarray(layer.limits.max_velocity) * cfg.period_s + 1e-9)
    assert steps[:, 0].max() < layer.config.max_jump_rad / 40
    assert np.all(np.diff(traj[:, 0]) >= -1e-12)  # monotonic approach, no overshoot
    assert abs(traj[-1, 0] - goal[0]) < 0.01  # it got there (~10 s for 1 rad)


def test_after_a_pause_the_executor_reanchors_on_the_measured_pose():
    """Takeover moved the arm 1 rad during the pause: resuming must not command the pre-pause target."""
    from rdf_robot_client.rollout import ChunkExecutor

    clock = [0.0]
    arm, layer = _shadow_layer(clock)
    hold_here = np.tile(np.r_[YAM_READY, 0.5], (50, 1))
    cfg = ExecConfig()
    ex = ChunkExecutor(_OneChunkPolicy(hold_here), lambda s, n, st: Observation([], list(st.state_vector()), "p", n, s),
                       cfg, layer.limits.max_velocity)
    _drive(ex, layer, clock, 20)
    moved = list(YAM_READY)
    moved[0] += 1.0
    arm.reset(moved, 0.5)  # the human drove it here
    layer._reset_ref()  # noqa: SLF001 - what the takeover's hold() does
    clock[0] += 2.0
    ex.client.chunk = np.tile(np.r_[moved, 0.5], (50, 1))  # the next observation sees the new pose
    traj = _drive(ex, layer, clock, 60, start_tick=20)
    assert ex.stats.gap_resets == 1
    assert layer.stats()["rejected_jump"] == 0
    assert np.max(np.abs(traj[:, 0] - moved[0])) < 0.01  # held where the arm is, not yanked back


def test_home_starts_from_the_measured_pose_and_is_slow(monkeypatch):
    import types

    import rdf_robot_client.adapters.base as base_mod

    clock = [0.0]

    def sleep(dt):
        clock[0] += dt

    monkeypatch.setattr(base_mod, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep,
                                                                 monotonic_ns=lambda: int(clock[0] * 1e9)))
    arm, layer = _shadow_layer(clock)
    start = list(YAM_READY)
    start[0] += 1.0
    arm.reset(start, 1.0)
    seen = []
    orig = arm.command_joints

    def rec(q, g=None):
        seen.append(tuple(q))
        orig(q, g)

    arm.command_joints = rec
    assert layer.home()
    s = layer.stats()
    assert s["rejected_jump"] == 0 and s["clamped_speed"] == 0
    q = np.asarray(seen)
    assert np.allclose(q[0], start, atol=1e-6)  # first setpoint = the measured pose
    assert np.allclose(q[-1], YAM_READY, atol=1e-6)
    peak = np.abs(np.diff(q[:, 0])).max() * 30
    assert peak <= 0.25 * layer.limits.max_velocity[0] + 1e-6  # speed_fraction 0.25 of the live cap: 0.05 rad/s
    assert clock[0] > 25  # a 1 rad homing move takes ~35 s at the unverified scale


def test_dryrun_flags_targets_beyond_max_jump_from_the_observed_pose(checker):
    st = [*YAM_READY, 1.0]
    walk = np.tile(np.r_[YAM_READY, 1.0], (50, 1))
    walk[:, 0] += np.linspace(0.01, 0.5, 50)  # small steps, but it ends 0.5 rad away
    r = checker.check(walk, "joint_pos_abs", st)
    assert r["static"]["targets_beyond_max_jump_from_state"] > 0 and r["static"]["first_step_jump_rad"] < 0.3
    assert r["verdict"] in ("clamp", "reject") and "max_jump_rad" in " ".join(r["problems"])


def test_safety_yaml_sits_inside_larp_joint_limits(checker):
    from rdf_robot_client.tdm122_dryrun import LARP_LOWER, LARP_UPPER

    env = checker.env
    assert env.to_dict()["inside_larp_limits"]
    assert all(lo >= ll for lo, ll in zip(env.lower, LARP_LOWER)) and all(hi <= lu for hi, lu in zip(env.upper, LARP_UPPER))
    assert env.lower[3] == pytest.approx(-1.5708 + 0.10)  # J4: LARP's -1.5708 (not the URDF's -1.693) + margin
    # the real allenai data reaches J4 = -1.61 / commands -1.674: past LARP's limit -> flagged, and clamped
    over = np.tile(np.r_[YAM_READY, 0.5], (50, 1))
    over[:, 3] = -1.60
    r = checker.check(over, "joint_pos_abs", [*over[0, :6], 0.5])
    assert r["static"]["targets_outside_larp_limits"] == 50 and "LARP" in " ".join(r["problems"])
    assert r["verdict"] in ("clamp", "reject")


def test_policy_moves_are_capped_at_0p3_rad_s_and_soft_start_after_every_hold():
    """docs/real-arm-safety.md rule 6: ramp from the measured pose, never a step, even at full safety scale."""
    from rdf_robot_client.rollout import ChunkExecutor

    cfg = ExecConfig()
    goal = np.r_[np.full(6, 2.0), 0.5]
    pol = _OneChunkPolicy(np.tile(goal, (50, 1)))
    vmax_full = (2.0,) * 6  # a verified profile at scale 1.0 would allow far more than 0.3 rad/s
    ex = ChunkExecutor(pol, lambda s, n, st: Observation([], list(st.state_vector()), "p", n, s), cfg, vmax_full)
    q = np.r_[np.zeros(6), 1.0]
    out = []
    for i in range(120):
        st = ArmState(t=i / 30, joints=tuple(q[:6]), gripper=q[6], ee_pose=Pose((0, 0, 0), (1, 0, 0, 0)))
        q = ex.tick(i, i / 30, st).copy()
        out.append(q[:6].copy())
    v = np.abs(np.diff(np.vstack([np.zeros(6), out]), axis=0))[:, 0] * 30
    assert v.max() <= cfg.max_speed_rad_s + 1e-9
    moving = np.flatnonzero(v > 0)
    first = moving[0]
    ramp = v[first:first + cfg.soft_start_ticks]
    assert np.all(np.diff(ramp) > 0) and ramp[0] <= cfg.max_speed_rad_s / cfg.soft_start_ticks + 1e-9
    assert v[first + cfg.soft_start_ticks + 1] == pytest.approx(cfg.max_speed_rad_s)
