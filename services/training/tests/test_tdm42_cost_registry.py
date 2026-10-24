"""TDM-42: rdf_training.cost (runtime x Modal rates, billing reconcile) and rdf_training.registry
(scan / join / resolve / cleanup) against an in-memory Volume. No Modal calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from rdf_training import cost, registry
from rdf_training.registry import Entry, State


@pytest.fixture(autouse=True)
def _rates(monkeypatch, tmp_path):
    p = tmp_path / "rates.json"
    p.write_text(json.dumps({"as_of": "test", "gpu_usd_per_hour": {"H100": 3.95, "A100-80GB": 2.5, "L40S": 1.95},
                             "cpu_core_usd_per_hour": 0.0473, "mem_gib_usd_per_hour": 0.008}))
    monkeypatch.setenv("RDF_MODAL_RATES_FILE", str(p))
    monkeypatch.delenv("RDF_GPU_RATES_JSON", raising=False)


# --------------------------------------------------------------------------- cost


@pytest.mark.parametrize("raw,want", [
    ("H100", ("H100", 1)), ("h100:2", ("H100", 2)), ("NVIDIA H100 80GB HBM3, 81559 MiB", ("H100", 1)),
    ("NVIDIA A100-SXM4-80GB", ("A100-80GB", 1)), ("NVIDIA A100-SXM4-40GB", ("A100-40GB", 1)),
    ("a100_40gb", ("A100-40GB", 1)), ("NVIDIA L40S", ("L40S", 1)), ("NVIDIA L4", ("L4", 1)),
    ("A10G", ("A10G", 1)), (None, (None, 0)), ("", (None, 0)),
])
def test_normalize_gpu(raw, want):
    assert cost.normalize_gpu(raw) == want


def test_estimate_gpu_part_is_seconds_times_rate_and_total_adds_reserved_cpu_mem():
    c = cost.estimate(482.8, "NVIDIA H100 80GB HBM3, 81559 MiB")
    assert c["source"] == "estimate" and c["method"] == "runtime_x_modal_rate" and c["gpu"] == "H100"
    assert c["gpu_cents"] == pytest.approx(482.8 / 3600 * 395, abs=1e-3)  # 52.97c
    assert c["gpu_cost_cents"] == 53
    want_total = 482.8 / 3600 * (395 + 16 * 4.73 + 96 * 0.8)
    assert c["total_cents"] == round(want_total) == 73
    assert c["rates"]["as_of"] == "test"


def test_estimate_cpu_only_stub_costs_nothing_and_unknown_gpu_never_under_reports():
    assert cost.estimate(20, None)["total_cents"] == 0
    c = cost.estimate(3600, "MI300X")
    assert c["gpu_rate_cents_per_hour"] == 395 and "warning" in c


def test_gpu_rate_override_env(monkeypatch):
    monkeypatch.setenv("RDF_GPU_RATES_JSON", json.dumps({"H100": 500}))
    c = cost.estimate(3600, "H100", cpu=0, memory_gib=0)
    assert c["total_cents"] == 500 and "RDF_GPU_RATES_JSON" in c["rates"]["source"]


def test_rates_from_modal_cli_shape():
    cfg = cost.rates_from_modal_cli({"gpu_hour_cost_h100": "3.95000", "gpu_hour_cost_a100_40gb": "2.1",
                                     "cpu_hour_cost": "0.0473", "mem_gib_hour_cost": "0.008",
                                     "volume_storage_gib_month_cost": "0.09", "egress_gib_cost": "0.04"},
                                    fetched_at="t")
    assert cfg["gpu_usd_per_hour"] == {"A100-40GB": 2.1, "H100": 3.95}
    assert cfg["cpu_core_usd_per_hour"] == 0.0473 and cfg["mem_gib_usd_per_hour"] == 0.008


def test_run_seconds_prefers_reported():
    assert cost.run_seconds({"wall_s": 10}, {"gpu_seconds": 12}) == 12
    assert cost.run_seconds({"wall_s": 10}) == 10
    assert cost.run_seconds({}) is None


def _report(rows):
    return lambda start, end: [r for r in rows if start <= r["interval_start"] < end]


def _h(hh):
    return datetime(2026, 10, 24, hh, tzinfo=timezone.utc)


def test_billed_sums_the_apps_hours_and_reconcile_uses_only_exclusive_complete_bills():
    rows = [
        {"object_id": "ap-A", "interval_start": _h(7), "cost": "0.50", "cost_by_resource": {"H100": "0.40", "CPU": "0.1"}},
        {"object_id": "ap-A", "interval_start": _h(8), "cost": "0.26", "cost_by_resource": {"H100": "0.15", "Memory": "0.11"}},
        {"object_id": "ap-B", "interval_start": _h(7), "cost": "9.99", "cost_by_resource": {"H100": "9.99"}},
    ]
    start, end = _h(7).timestamp() + 2500, _h(8).timestamp() + 300
    b = cost.billed("ap-A", start, end, report=_report(rows), now=_h(9).timestamp() + 1)
    assert b["total_cents"] == 76 and b["gpu_cents"] == pytest.approx(55) and b["complete"]
    assert set(b["hours"]) == {_h(7).isoformat(), _h(8).isoformat()}
    est = cost.estimate(482.8, "H100")
    r = cost.reconcile(est, b, exclusive=True)
    assert r["source"] == "modal_billing" and r["total_cents"] == 76 and r["estimate"]["total_cents"] == 73
    assert r["billing"]["delta_vs_estimate_cents"] == 3
    shared = cost.reconcile(est, b, exclusive=False)
    assert shared["source"] == "estimate" and shared["billing_check"]["attribution"] == "shared_app_upper_bound"
    open_hour = cost.billed("ap-A", start, end, report=_report(rows), now=_h(8).timestamp() + 600)
    assert not open_hour["complete"]
    assert cost.reconcile(est, open_hour, exclusive=True)["source"] == "estimate"


def test_ticket_spend_by_source():
    runs = [{"gpu_cost_cents": 73, "metrics_json": json.dumps({"cost": {"source": "estimate"}})},
            {"gpu_cost_cents": 76, "metrics_json": json.dumps({"cost": {"source": "modal_billing"}})},
            {"gpu_cost_cents": 40, "metrics_json": "{}"}, {"gpu_cost_cents": 0, "metrics_json": "{}"}]
    assert cost.ticket_spend(runs) == {"gpu_cost_cents": 189,
                                       "by_source_cents": {"estimate": 73, "modal_billing": 76, "unlabelled": 40, "none": 0}}


# --------------------------------------------------------------------------- registry


T0 = 1792828000.0


class FakeVolume:
    def __init__(self) -> None:
        self.files: dict[str, int] = {}
        self.json: dict[str, dict] = {}
        self.removed: list[str] = []

    def add_run(self, ticket: str, run: str, *, steps=(199,), created=T0, complete=True, metrics=None):
        base = f"runs/ticket-{ticket}/{run}"
        for s in steps:
            self.files[f"{base}/{s}/params/p.ocdbt"] = 6_000_000_000
            self.files[f"{base}/{s}/train_state/t"] = 3_500_000_000
            self.files[f"{base}/{s}/assets/x/norm_stats.json"] = 1000
            self.files[f"{base}/{s}/rdf_policy.json"] = 266
            if complete:
                self.files[f"{base}/{s}/_CHECKPOINT_METADATA"] = 426
        self.json[f"{base}/rdf_run.json"] = {"ticket_id": ticket, "run_id": run, "created_at": created,
                                             "dataset_uri": f"modal-vol://rdf-datasets/lerobot/rdf/{ticket}",
                                             "settings": {"lr": 2.5e-5, "steps": 200}}
        self.files[f"{base}/rdf_run.json"] = 1500
        self.json[f"metrics/ticket-{ticket}/{run}.json"] = {"final_loss": 0.01, "wall_s": 480.0,
                                                            "gpu": "NVIDIA H100 80GB HBM3", **(metrics or {})}
        return f"modal-vol://rdf-checkpoints/{base}/{steps[-1]}"

    def listdir(self, path, recursive=False):
        path = path.strip("/")
        out, dirs = [], set()
        for f, size in self.files.items():
            if not f.startswith(path + "/"):
                continue
            rest = f[len(path) + 1:].split("/")
            for i in range(1, len(rest)):
                dirs.add("/".join([path, *rest[:i]]))
            out.append(Entry(f, False, size, T0))
        return out + [Entry(d, True, 60, T0) for d in sorted(dirs)]

    def read_json(self, path):
        return self.json.get(path)

    def remove(self, path):
        self.removed.append(path)
        for f in [f for f in self.files if f.startswith(path + "/")]:
            del self.files[f]


def _vol():
    v = FakeVolume()
    old = v.add_run("42", "20261024-070000-aaaaaa", created=T0)
    mid = v.add_run("42", "20261024-080000-bbbbbb", created=T0 + 3600)
    new = v.add_run("42", "20261024-090000-cccccc", created=T0 + 7200)
    other = v.add_run("7", "20261024-060000-dddddd", created=T0 - 3600)
    return v, old, mid, new, other


def test_scan_records_sizes_metadata_and_base():
    v, old, *_ = _vol()
    v.files[registry.BASE_CACHE_PATH + "/params/x"] = 12_000_000_000
    recs = registry.scan(v)
    r = next(x for x in recs if x["uri"] == old)
    assert r["kind"] == "finetuned" and r["ticket"] == "42" and r["step"] == 199 and r["complete"]
    assert r["size_bytes"] == 6_000_000_000 + 3_500_000_000 + 1000 + 266 + 426
    assert r["dataset_uri"].endswith("/42") and r["hyperparams"]["steps"] == 200 and r["metrics"]["final_loss"] == 0.01
    base = next(x for x in recs if x["kind"] == "base")
    assert base["uri"] == registry.BASE_URI and base["size_bytes"] == 12_000_000_000


def test_join_resolve_latest_deployed_base():
    v, old, mid, new, _ = _vol()
    st = State(
        training_runs=[{"id": 3, "ticket_id": 42, "iteration": 0, "status": "done", "checkpoint_uri": mid,
                        "episode_ids": [1, 2], "base_checkpoint_uri": registry.BASE_URI, "hyperparams_json": "{}",
                        "gpu_cost_cents": 73, "metrics_json": json.dumps({"cost": {"source": "estimate"}})}],
        eval_runs=[{"id": 9, "checkpoint_uri": mid, "kind": "candidate", "status": "done", "trials": 10,
                    "completed": 10, "successes": 8, "ci_low": 0.49, "ci_high": 0.94}],
        tickets=[{"id": 42, "deployed_checkpoint_uri": {"some": mid}}],
        policy_state={"active": mid + "/params", "previous": registry.BASE_URI})
    recs = registry.build(v, st)
    assert registry.resolve(recs, 42, "latest")["uri"] == new
    d = registry.resolve(recs, "42", "deployed")
    assert d["uri"] == mid and d["deployed"] and d["policy_server"] == "active"
    assert d["training_run"]["id"] == 3 and d["training_run"]["gpu_cost_cents"] == 73
    assert d["evals"][0]["successes"] == 8
    assert registry.resolve(recs, 42, "base")["policy_server"] == "previous"
    assert registry.resolve(recs, 7, "deployed") is None
    with pytest.raises(ValueError):
        registry.resolve(recs, 42, "best")


def test_cleanup_keeps_base_deployed_latest_n_and_inflight_and_is_a_dry_run():
    v, old, mid, new, other = _vol()
    young = "20261024-100000-eeeeee"
    v.json[f"runs/ticket-42/{young}/rdf_run.json"] = {"created_at": T0 + 9000}
    v.files[f"runs/ticket-42/{young}/rdf_run.json"] = 10  # a fine-tune still writing: no step yet
    st = State(tickets=[{"id": 42, "deployed_checkpoint_uri": old}],
               eval_runs=[{"id": 1, "checkpoint_uri": other, "status": "running"}])
    plan = registry.plan_cleanup(registry.build(v, st), st, keep_latest=1, now=T0 + 9100)
    keep = {g["run_id"]: g["reasons"] for g in plan["keep"]}
    assert plan["dry_run"] and not v.removed
    assert set(keep) == {"20261024-070000-aaaaaa", "20261024-090000-cccccc", "20261024-060000-dddddd", young}
    assert any("deployed" in x for x in keep["20261024-070000-aaaaaa"])
    assert any("running eval_run" in x for x in keep["20261024-060000-dddddd"])
    assert [g["run_id"] for g in plan["delete"]] == ["20261024-080000-bbbbbb"]
    assert all(not g["run_path"].startswith("openpi-cache") for g in plan["delete"])
    assert registry.BASE_CACHE_PATH in plan["never_touched"]

    removed = registry.apply_cleanup(v, plan, records=registry.build(v, st), state=st, log=lambda _m: None)
    assert removed == ["runs/ticket-42/20261024-080000-bbbbbb"] and not any("080000" in f for f in v.files)


def test_cleanup_fails_closed_without_state_and_apply_rechecks_guards():
    v, old, mid, new, _ = _vol()
    bad = State(ok=False, errors=["SpacetimeDB: down"])
    plan = registry.plan_cleanup(registry.build(v, bad), bad, keep_latest=1)
    assert plan["refused"] and plan["delete"] == []
    with pytest.raises(RuntimeError):
        registry.apply_cleanup(v, plan, records=[], state=bad)

    st = State()
    plan = registry.plan_cleanup(registry.build(v, st), st, keep_latest=1)
    assert {g["run_id"] for g in plan["delete"]} == {"20261024-070000-aaaaaa", "20261024-080000-bbbbbb"}
    # mid gets deployed between the dry run and apply: apply re-checks and skips it
    st2 = State(tickets=[{"id": 42, "deployed_checkpoint_uri": mid}])
    removed = registry.apply_cleanup(v, plan, records=registry.build(v, st2), state=st2, log=lambda _m: None)
    assert removed == ["runs/ticket-42/20261024-070000-aaaaaa"]


def test_download_paths_skip_train_state():
    v, old, *_ = _vol()
    rec = next(r for r in registry.scan(v) if r["uri"] == old)
    paths = registry.download_paths(rec)
    assert not any("train_state" in p for p in paths) and paths[-1].endswith("rdf_run.json")
    assert any(p.endswith("train_state") for p in registry.download_paths(rec, with_train_state=True))
