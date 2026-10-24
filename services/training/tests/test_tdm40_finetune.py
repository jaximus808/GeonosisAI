"""TDM-40: fine-tune logic that runs without openpi (hyperparams, dataset spec, paths, progress, layout)."""
import json
import sys
import types

import pytest

from rdf_training import finetune as ft


def test_defaults_match_run_settings():
    s = ft.resolve_hyperparams({})
    assert (s.lr, s.steps, s.batch_size, s.lora_rank, s.mode, s.prompt) == (2.5e-5, 2000, 32, 16, "lora", "")
    assert s.base_params == ft.PI05_BASE_PARAMS and not s.smoke and s.action_horizon == 50


def test_hyperparams_override_and_validation():
    s = ft.resolve_hyperparams({"lr": "1e-4", "steps": 200, "batch_size": 16, "lora_rank": 0, "prompt": " pick ",
                                "foo": 1, "training_run_id": "7"})
    assert (s.lr, s.steps, s.batch_size, s.mode, s.prompt, s.smoke, s.training_run_id) == (
        1e-4, 200, 16, "full", "pick", True, 7)
    assert s.extra == {"foo": 1}
    assert ft.resolve_hyperparams({"steps": 200, "smoke": False}).smoke is False
    for bad in ({"steps": 0}, {"lr": "x"}, {"lr": float("nan")}, {"batch_size": 1000}, {"norm_stats": "maybe"},
                {"lora_rank": 16, "mode": "full"}):
        with pytest.raises(ft.FinetuneError):
            ft.resolve_hyperparams(bad)


def test_base_checkpoint_uri():
    assert ft.base_params_path("") == ft.PI05_BASE_PARAMS
    assert ft.base_params_path("gs://openpi-assets/checkpoints/pi05_base") == ft.PI05_BASE_PARAMS
    assert ft.base_params_path("modal-vol://rdf-checkpoints/runs/ticket-1/r/1999") == "/checkpoints/runs/ticket-1/r/1999/params"
    with pytest.raises(ft.FinetuneError):
        ft.base_params_path("s3://x")


def test_dataset_uri_and_spec():
    assert ft.repo_id_from_dataset_uri("modal-vol://rdf-datasets/lerobot/rdf/ticket-42") == "rdf/ticket-42"
    for bad in ("stub://rdf-datasets/lerobot/rdf/ticket-42", "modal-vol://rdf-media/x", "",
                "modal-vol://rdf-datasets/lerobot/../etc"):
        with pytest.raises(ft.FinetuneError):
            ft.repo_id_from_dataset_uri(bad)
    feats = {"observation.state": {"shape": [7]}, "action": {"shape": [7]},
             "observation.images.top": {}, "observation.images.wrist": {}}
    spec = ft.spec_from_info({"codebase_version": "v2.1", "fps": 30, "features": feats}, "rdf/t")
    assert spec == {"repo_id": "rdf/t", "base_image_key": "observation.images.top",
                    "wrist_image_key": "observation.images.wrist", "state_key": "observation.state",
                    "action_key": "action", "joint_dims": 6, "action_dim": 7, "fps": 30}
    with pytest.raises(ft.FinetuneError):
        ft.spec_from_info({"codebase_version": "v3.0", "features": feats}, "rdf/t")
    with pytest.raises(ft.FinetuneError):
        ft.spec_from_info({"codebase_version": "v2.1", "features": {**feats, "action": {"shape": [8]}}}, "rdf/t")


def test_paths_and_timeout():
    assert ft.run_rel_dir("42", "r1") == "runs/ticket-42/r1"
    assert ft.make_run_id(smoke=True, now=0).startswith("smoke-19700101-000000-")
    with pytest.raises(ft.FinetuneError):
        ft.ticket_dir_name("../x")
    ft.check_fits_timeout(2000, 2 * 3600)
    with pytest.raises(ft.FinetuneError):
        ft.check_fits_timeout(6000, 2 * 3600)


def test_loss_curve_downsamples_and_keeps_last():
    c = ft.LossCurve()
    for i in range(0, 2000, 10):
        c.add(i, {"loss": 1.0 / (i + 1), "grad_norm": 0.5, "camera": "x"}, t=i * 1.3)
    c.add(5, {"grad_norm": 1})  # no loss -> ignored
    pts = c.curve(max_points=50)
    assert len(pts) <= 51 and pts[0][0] == 0 and pts[-1][0] == 1990
    assert c.sec_per_step() == pytest.approx(1.3)


def test_progress_reporter_fans_out_and_swallows_errors():
    got, posts = {}, []

    def post(url, payload, token):
        posts.append((url, payload["status"], token))
        if "callback" in url:
            raise OSError("down")
        return 200

    r = ft.ProgressReporter(base={"ticket_id": "1"}, keys=["fc-1", "ticket-1/latest"],
                            dict_put=lambda k, v: got.__setitem__(k, v), orchestrator_url="http://o/", token="t",
                            post=post)
    r.report(status="running", step=50, loss=0.1)
    r.flush()
    r.report(status="done", wait=True, path="/modal/callback", to_dict=False)
    r.report(status="done", wait=True, path=None)
    assert set(got) == {"fc-1", "ticket-1/latest"} and got["fc-1"]["status"] == "done"
    assert posts == [("http://o/modal/progress", "running", "t"), ("http://o/modal/callback", "done", "t")]
    assert r.errors == {"/modal/callback": 1}


class _FakeDict:
    def __init__(self):
        self.d = {}

    def put(self, k, v):
        self.d[k] = v


def _dataset(root, repo="rdf/tdm40-test"):
    meta = root / "lerobot" / repo / "meta"
    meta.mkdir(parents=True)
    feats = {"observation.state": {"shape": [7]}, "action": {"shape": [7]},
             "observation.images.top": {}, "observation.images.wrist": {}}
    (meta / "info.json").write_text(json.dumps({"codebase_version": "v2.1", "fps": 30, "features": feats,
                                                "total_episodes": 2, "total_frames": 165}))
    return f"modal-vol://rdf-datasets/lerobot/{repo}"


def test_run_finetune_layout_with_fake_openpi(tmp_path, monkeypatch):
    calls = {}

    def fake_train(*, spec, settings, config_name, exp_name, checkpoint_base_dir, assets_dir, reporter):
        calls.update(spec=spec, config_name=config_name, base=checkpoint_base_dir, assets=assets_dir)
        step = tmp_path / "ck" / "runs" / config_name / exp_name / str(settings.steps - 1)
        (step / "params").mkdir(parents=True)
        (step / "assets").mkdir()
        return {"checkpoint_dir": str(step), "metrics": {"final_loss": 0.01, "first_loss": 0.04, "steps": settings.steps,
                                                        "loss_curve": [[0, 0.04], [190, 0.01]]}}

    monkeypatch.setitem(sys.modules, "rdf_training.finetune_openpi", types.SimpleNamespace(train=fake_train))
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    uri = _dataset(tmp_path / "ds")
    pd, commits = _FakeDict(), []
    res = ft.run_finetune("42", uri, {"steps": 200}, checkpoints_root=str(tmp_path / "ck"),
                          datasets_root=str(tmp_path / "ds"), commit=lambda: commits.append(1), progress_dict=pd,
                          call_id="fc-xyz")
    run_id = res["metrics"]["run_id"]
    assert run_id.startswith("smoke-")
    assert res["checkpoint_uri"] == f"modal-vol://rdf-checkpoints/runs/ticket-42/{run_id}/199"
    assert res["metrics"]["final_loss"] == 0.01 and res["metrics"]["smoke"] is True and res["gpu_seconds"] >= 0
    assert calls["config_name"] == "ticket-42" and calls["spec"]["action_dim"] == 7
    assert calls["base"] == f"{tmp_path}/ck/runs" and calls["assets"] == f"{tmp_path}/ds/assets"
    run_dir = tmp_path / "ck" / "runs" / "ticket-42" / run_id
    meta = json.loads((run_dir / "rdf_run.json").read_text())
    assert meta["checkpoint_step_dir"].endswith(f"runs/ticket-42/{run_id}/199")
    side = json.loads((run_dir / "199" / "rdf_policy.json").read_text())
    assert (side["layout"], side["state_dim"], side["action_dim"], side["joint_dims"], side["asset_id"], side["mode"]) == (
        "single_arm", 7, 7, 6, "rdf/tdm40-test", "lora")
    assert json.loads((tmp_path / "ck" / "metrics" / "ticket-42" / f"{run_id}.json").read_text())["final_loss"] == 0.01
    assert commits and pd.d["fc-xyz"]["status"] == "done" and pd.d["ticket-42/latest"]["checkpoint_uri"] == res["checkpoint_uri"]


def test_run_finetune_missing_dataset_reports_failure(tmp_path):
    pd = _FakeDict()
    with pytest.raises(ft.FinetuneError, match="not found"):
        ft.run_finetune("42", "modal-vol://rdf-datasets/lerobot/rdf/none", {"steps": 10},
                        checkpoints_root=str(tmp_path / "ck"), datasets_root=str(tmp_path / "ds"), progress_dict=pd,
                        call_id="fc-1")
    assert pd.d["fc-1"]["status"] == "failed" and "not found" in pd.d["fc-1"]["error"]
    assert list((tmp_path / "ck" / "metrics" / "ticket-42").glob("*.json"))
