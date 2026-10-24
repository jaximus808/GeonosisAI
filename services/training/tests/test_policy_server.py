"""TDM-41: policy server logic without openpi/jax/Modal (fake loader)."""

import json

import numpy as np
import pytest

from rdf_training import policy_server as ps

MOUNTS = {"rdf-checkpoints": "/checkpoints", "rdf-s05a-checkpoints": "/s05a"}
SMOKE = "modal-vol://rdf-s05a-checkpoints/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199"


def test_resolve_base_aliases():
    for u in ["", "base", "pi05_base", ps.BASE_URI, ps.BASE_URI + "/params"]:
        r = ps.resolve_uri(u, MOUNTS)
        assert (r.key, r.kind) == (ps.BASE_URI, "base")


def test_resolve_volume_forms():
    r = ps.resolve_uri(SMOKE, MOUNTS)
    assert r.kind == "finetuned"
    assert r.path == "/s05a/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199"
    assert ps.resolve_uri(SMOKE + "/params", MOUNTS).key == SMOKE
    alt = ps.resolve_uri("rdf-s05a-checkpoints:/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199", MOUNTS)
    assert alt.key == SMOKE
    assert ps.resolve_uri("modal-vol://rdf-checkpoints/runs/t-1/r/1999", MOUNTS).path == "/checkpoints/runs/t-1/r/1999"


@pytest.mark.parametrize(
    "uri,code",
    [
        ("stub://rdf-checkpoints/runs/x/1999", "stub_checkpoint"),
        ("modal-vol://rdf-media/x", "unknown_volume"),
        ("modal-vol://rdf-checkpoints/../etc", "bad_uri"),
        ("https://example.com/ckpt", "bad_uri"),
    ],
)
def test_resolve_rejects(uri, code):
    with pytest.raises(ps.PolicyError) as e:
        ps.resolve_uri(uri, MOUNTS)
    assert e.value.code == code


def test_bearer():
    ps.check_bearer("Bearer s3cret", None, "s3cret")
    ps.check_bearer(None, "s3cret", "s3cret")  # ?token= for websocket clients without headers
    for h, q in [(None, None), ("Bearer nope", None), ("s3cret", None), (None, "nope")]:
        with pytest.raises(ps.PolicyError) as e:
            ps.check_bearer(h, q, "s3cret")
        assert e.value.status == 401
    with pytest.raises(ps.PolicyError) as e:  # fails closed without a configured token
        ps.check_bearer("Bearer x", None, "")
    assert e.value.status == 503


def _fake_ckpt(tmp_path, dims=6, lora=True, sidecar=None):
    d = tmp_path / "199"
    (d / "params").mkdir(parents=True)
    (d / "params" / "_METADATA").write_text('{"x": "lora_a"}' if lora else "{}")
    a = d / "assets" / "lerobot" / "svla_so100_pickplace"
    a.mkdir(parents=True)
    stats = {k: {"mean": [0.0] * dims, "std": [1.0] * dims} for k in ("state", "actions")}
    (a / "norm_stats.json").write_text(json.dumps({"norm_stats": stats}))
    if sidecar:
        (d / "rdf_policy.json").write_text(json.dumps(sidecar))
    return d


def test_infer_spec_from_norm_stats(tmp_path):
    s = ps.infer_spec(_fake_ckpt(tmp_path))
    assert (s.layout, s.state_dim, s.action_dim, s.joint_dims, s.mode) == ("single_arm", 6, 6, 5, "lora")
    assert s.asset_id == "lerobot/svla_so100_pickplace"
    assert s.action_space == "joint_pos_abs"


def test_infer_spec_sidecar_yam(tmp_path):
    side = {"asset_id": "lerobot/svla_so100_pickplace", "state_dim": 7, "action_dim": 7, "joint_dims": 6, "mode": "full"}
    s = ps.infer_spec(_fake_ckpt(tmp_path, dims=7, lora=True, sidecar=side))
    assert (s.state_dim, s.action_dim, s.joint_dims, s.mode) == (7, 7, 6, "full")


def test_build_obs_yam_checks_state_length():
    obs = ps.build_obs({"state": [0.1] * 7, "prompt": "p"}, ps.YAM_SPEC)
    assert obs["observation/state"].shape == (7,) and obs["observation/image"].shape == (224, 224, 3)
    with pytest.raises(ps.PolicyError) as e:
        ps.build_obs({"state": [0.1] * 8}, ps.YAM_SPEC)
    assert e.value.code == "bad_state"


def test_build_obs_base_droid_layout():
    obs = ps.build_obs({"state": [1, 2, 3, 4, 5, 6, 0.5]}, ps.BASE_SPEC)
    assert list(obs["observation/joint_position"]) == [1, 2, 3, 4, 5, 6, 0]
    assert list(obs["observation/gripper_position"]) == [0.5]


class _FakePolicy:
    def __init__(self, name, dim):
        self.name, self.dim, self.calls = name, dim, 0
        self._sample_actions = None

    def infer(self, obs):
        self.calls += 1
        return {"actions": np.full((50, self.dim), 1.0 if self.name == "base" else 2.0)}


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(ps, "_instrument", lambda entry: None)
    loads = []

    def loader(r):
        loads.append(r.key)
        if r.kind == "base":
            return _FakePolicy("base", 8), ps.BASE_SPEC
        return _FakePolicy(r.key, 6), ps.PolicySpec("single_arm", 6, 6, 5, "x")

    persisted = []
    s = ps.PolicyServer(MOUNTS, loader, capacity=2, persist=persisted.append)
    s._loads, s._persisted = loads, persisted
    return s


def test_hot_swap_cache(server):
    a = server.activate("base")
    assert a["swapped"] and not a["cache_hit"]
    assert server.infer({"state": [0] * 7})["action_dim"] == 8
    b = server.activate(SMOKE)
    assert b["swapped"] and not b["cache_hit"] and server.health()["active_checkpoint_uri"] == SMOKE
    out = server.infer({"state": [0] * 6})
    assert out["action_dim"] == 6 and out["checkpoint_uri"] == SMOKE and out["action_space"] == "joint_pos_abs"
    # flip back and forth: both cached, no new loads
    for uri in ["base", SMOKE, ps.BASE_URI + "/params"]:
        r = server.activate(uri)
        assert r["cache_hit"] and r["swapped"]
    assert server._loads == [ps.BASE_URI, SMOKE]
    assert server._persisted[-1]["active"] == ps.BASE_URI + "/params" and server._persisted[-1]["previous"] == SMOKE


def test_lru_evicts_non_active(server):
    server.activate("base")
    server.activate(SMOKE)
    third = "modal-vol://rdf-checkpoints/runs/t/r/1999"
    server.activate(third)  # evicts base (LRU, not active)
    assert [e["checkpoint_uri"] for e in server.health()["cached"]] == [SMOKE, third]
    server.activate(SMOKE, preload_only=True)  # already cached; active stays `third`
    assert server.health()["active_checkpoint_uri"] == third


def test_checkpoint_mismatch_and_not_ready(server):
    with pytest.raises(ps.PolicyError) as e:
        server.infer({"state": [0] * 7})
    assert e.value.code == "not_ready"
    server.activate("base")
    with pytest.raises(ps.PolicyError) as e:
        server.infer({"state": [0] * 7, "checkpoint_uri": SMOKE})
    assert e.value.code == "checkpoint_mismatch" and e.value.status == 409
    assert server.infer({"state": [0] * 7, "checkpoint_uri": "base"})["checkpoint_uri"] == ps.BASE_URI


def test_policy_error_pickles():
    import pickle

    e = pickle.loads(pickle.dumps(ps.PolicyError("bad_state", "msg", 400)))
    assert (e.code, str(e), e.status) == ("bad_state", "msg", 400)
