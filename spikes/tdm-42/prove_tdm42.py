"""TDM-42 proof on the real TDM-40 smoke checkpoint (read-only on Modal; nothing is written to
maincloud or the Volume).

1. Registry: scan rdf-checkpoints, resolve ``latest`` for ticket ``tdm40-smoke``.
2. Cost: feed that run's real finetune result (Volume metrics: wall_s, nvidia-smi GPU name) through
   the TDM-21 ``TrainingRunner`` done path with an in-memory writer -> the ``gpu_cost_cents`` and
   ``metrics_json.cost`` the orchestrator would write, then the TDM-71 report built from that row.
3. Billing: reconcile the estimate with Modal's billing report for the ephemeral App that ran it.

    cd services/orchestrator && uv run python ../../spikes/tdm-42/prove_tdm42.py
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime

from rdf_orchestrator import report
from rdf_orchestrator.modal_client import PollResult, TrainingRunner
from rdf_training import cost, registry

TICKET = "tdm40-smoke"


def main() -> None:
    vol = registry.ModalVolume()
    st = registry.State(policy_state=registry.read_policy_state())
    recs = registry.build(vol, st)
    rec = registry.resolve(recs, TICKET, "latest")
    print(f"[registry] {len([r for r in recs if r['kind'] == 'finetuned'])} fine-tuned checkpoints; latest for {TICKET}:")
    print(f"  {rec['uri']}  step {rec['step']}  {rec['size_bytes'] / 1e9:.2f} GB  complete={rec['complete']}  "
          f"dataset={rec['dataset']}  loss={rec['metrics'].get('final_loss')}  wall_s={rec['metrics']['wall_s']}")
    full = vol.read_json(f"metrics/ticket-{TICKET}/{rec['run_id']}.json")
    result = {"checkpoint_uri": rec["uri"], "gpu_seconds": full["wall_s"],
              "metrics": {k: full[k] for k in ("final_loss", "steps", "wall_s", "gpu", "smoke") if k in full}}

    # ---- the TDM-21 runner's done path on the real result
    rows: dict[int, dict] = {1: {"id": 1, "ticket_id": 1, "iteration": 0, "status": "running", "modal_call_id": "fc-proof",
                                 "episode_ids": [1], "base_checkpoint_uri": registry.BASE_URI, "hyperparams_json": "{}",
                                 "metrics_json": json.dumps({"phase": "finetune", "finetune_call_id": "fc-proof",
                                                             "finetune_spawned_at": time.time() - 500}),
                                 "gpu_cost_cents": 0, "created_at": 1, "started_at": 1_000_000, "finished_at": None}}

    class Writer:
        def call_reducer(self, name, *a):
            rid, status, call_id, ckpt, metrics, c = a
            rows[rid].update(status=status, checkpoint_uri=ckpt or rows[rid].get("checkpoint_uri"),
                             metrics_json=metrics or rows[rid]["metrics_json"],
                             gpu_cost_cents=c if c is not None else rows[rid]["gpu_cost_cents"],
                             finished_at=int((1 + full["wall_s"]) * 1e6))

    class Modal:
        def poll(self, cid):
            return PollResult("done", value=result)

    class Bus:
        def get(self, t, rid):
            return rows.get(rid)

        def rows(self, t):
            return list(rows.values()) if t == "training_run" else []

    r = TrainingRunner(writer=Writer, modal=Modal)
    r.bind(Bus())
    r.track(rows[1])
    r.stop()
    assert r.poll_once(1) == "done"
    row = rows[1]
    c = json.loads(row["metrics_json"])["cost"]
    print(f"[cost] training_run.gpu_cost_cents = {row['gpu_cost_cents']}  ({c['source']}, {c['method']}, "
          f"runtime {c['runtime_from']})")
    print(f"  {c['formula']}")
    print(f"  GPU part: {c['seconds']} s x {c['gpu_rate_cents_per_hour']}c/h ({c['gpu']}) = {c['gpu_cents']:.2f}c; "
          f"CPU {c['cpu_cents']:.2f}c + memory {c['mem_cents']:.2f}c; rates {c['rates']}")
    assert c["gpu_cents"] == round(full["wall_s"] / 3600 * c["gpu_rate_cents_per_hour"], 4)

    # ---- the TDM-71 report built from that row
    tables = {"ticket": [{"id": 1, "robot_id": 1, "task_description": "TDM-42 proof (smoke checkpoint)", "status": "train",
                          "budget_cents": 5000, "spent_cents": 0, "target_success": 0.8, "created_at": 1}],
              "robot": [{"id": 1, "name": "test.tdm42"}], "event_log": [], "eval_trial": [], "training_run": [row]}
    for n in report.TICKET_TABLES:
        tables.setdefault(n, [])
    rep = report.assemble(tables, 1)
    md = report.render_markdown(rep)
    print(f"[report] total_cost.gpu = {rep['total_cost']['gpu']}")
    for line in md.splitlines():
        if "Training run" in line or line.startswith("- GPU") or "GPU" in line and "Cost" in line:
            print("  md> " + line)

    # ---- registry join: the record now carries its run, cost and source
    joined = registry.resolve(registry.join(recs, registry.State(training_runs=[row])), TICKET, "latest")
    print(f"[registry+row] training_run={joined['training_run']['id']} gpu_cost_cents={joined['training_run']['gpu_cost_cents']} "
          f"cost.source={joined['training_run']['cost']['source']}")

    # ---- Modal's own bill for the ephemeral App that ran this smoke run
    apps = json.loads(subprocess.run(["modal", "app", "list", "--json"], capture_output=True, text=True, check=True).stdout)
    start = registry._created_from_run_id(rec["run_id"])
    ts = lambda s: datetime.fromisoformat(s).timestamp() if s else None  # noqa: E731
    app = next((a for a in apps if a.get("description") == "rdf-training" and a.get("state") != "deployed"
                and ts(a["created_at"]) <= start + 5 and ts(a.get("stopped_at")) and ts(a["stopped_at"]) >= start + full["wall_s"] - 5), None)
    if app is None:
        print("[billing] no ephemeral App found for the run window")
        return
    bill = cost.billed(app["app_id"], ts(app["created_at"]), ts(app["stopped_at"]))
    rc = cost.reconcile(c, bill, exclusive=True)
    b = rc.get("billing") or rc.get("billing_check")
    print(f"[billing] app {app['app_id']} ({app['created_at']} -> {app['stopped_at']}): Modal billed "
          f"{b['total_cents']}c {b['by_resource_cents']}; complete={b['complete']}; delta vs estimate "
          f"{b['delta_vs_estimate_cents']:+d}c -> would record {rc['total_cents']}c ({rc['source']})")


if __name__ == "__main__":
    main()
