"""TDM-12 walk-through: training queued->running->done, a 10-trial eval finalized
through the orchestrator's own EvalRunFinalizer (stats.py), and the rejections
(finalize with 9/10 trial rows, second finalize, bad transitions, edits).

Stdlib only. Usage (run_local.sh does the setup against a throwaway server):

    python3 -I walk.py --host http://127.0.0.1:3012 --db tdm12test \
        --token-file <orchestrator token file> --ticket 1 --episodes 1,2,3 --pending-episode 4

Every reducer call is checked for the expected outcome; exits non-zero on any
mismatch. Rows it creates are tagged test.* (ticket created_by / note text).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
STATS_PY = REPO / "services/orchestrator/src/rdf_orchestrator/stats.py"

FAILS: list[str] = []


def load_stats():
    spec = importlib.util.spec_from_file_location("tdm12_stats", STATS_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["tdm12_stats"] = mod
    spec.loader.exec_module(mod)
    return mod


class Client:
    def __init__(self, host: str, db: str, token: str | None):
        self.base = f"{host.rstrip('/')}/v1/database/{db}"
        self.token = token

    def _post(self, path: str, body: bytes, ctype: str) -> tuple[int, str]:
        req = urllib.request.Request(f"{self.base}/{path}", data=body, method="POST")
        req.add_header("Content-Type", ctype)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def call(self, reducer: str, args) -> tuple[bool, str]:
        status, text = self._post(f"call/{reducer}", json.dumps(args).encode(), "application/json")
        return 200 <= status < 300, text

    def sql(self, query: str) -> list[dict]:
        status, text = self._post("sql", query.encode(), "text/plain")
        if status != 200:
            raise RuntimeError(f"sql failed {status}: {text}")
        res = json.loads(text)[0]
        cols = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
        return [dict(zip(cols, row)) for row in res["rows"]]


def expect(c: Client, label: str, reducer: str, args, ok: bool, contains: str = "") -> None:
    good, text = c.call(reducer, args)
    passed = good == ok and (ok or contains.lower() in text.lower())
    mark = "PASS" if passed else "FAIL"
    detail = "ok" if good else f"rejected: {text.strip()[:160]}"
    print(f"[{mark}] {label}: {detail}")
    if not passed:
        FAILS.append(label)


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}".rstrip())
    if not cond:
        FAILS.append(label)


def opt(v):
    """SpacetimeDB JSON encoding of option<T>."""
    return {"none": []} if v is None else {"some": v}


def unopt(v):
    """Option as returned by /sql: [0, value] = some, [1, []] = none."""
    if isinstance(v, list) and len(v) == 2 and v[0] in (0, 1):
        return v[1] if v[0] == 0 else None
    if isinstance(v, dict):
        if "some" in v:
            return v["some"]
        if "none" in v:
            return None
    return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--token-file", required=True, help="file holding a token whose identity has role orchestrator")
    ap.add_argument("--ticket", type=int, required=True)
    ap.add_argument("--episodes", required=True, help="comma-separated accepted episode ids of the ticket")
    ap.add_argument("--pending-episode", type=int, default=0, help="a not-yet-accepted episode id (optional)")
    a = ap.parse_args()

    token = pathlib.Path(a.token_file).read_text().strip()
    c = Client(a.host, a.db, token)
    anon = Client(a.host, a.db, None)
    stats = load_stats()
    T = a.ticket
    eps = [int(x) for x in a.episodes.split(",")]

    # ---------------------------------------------------------------- auth
    expect(anon, "anonymous queue_training_run rejected", "queue_training_run", [T, json.dumps(eps), ""], False, "unauthorized")

    # ---------------------------------------------------------------- training
    if a.pending_episode:
        expect(c, "training on a pending episode rejected", "queue_training_run",
               [T, json.dumps(eps + [a.pending_episode]), ""], False, "only accepted")
    expect(c, "empty episode list rejected", "queue_training_run", [T, "[]", ""], False, "at least one")
    expect(c, "queue_training_run", "queue_training_run", [T, json.dumps(eps), json.dumps({"lr": 2.5e-5, "steps": 300})], True)
    expect(c, "second active run for ticket rejected", "queue_training_run", [T, json.dumps(eps), ""], False, "already has training run")
    run = max(c.sql(f"SELECT * FROM training_run WHERE ticket_id = {T}"), key=lambda r: r["id"])
    rid = run["id"]
    check("training run is queued", run["status"] == "queued", f"(id {rid}, status {run['status']})")

    none4 = [opt(None)] * 4
    expect(c, "queued -> done rejected", "set_training_status", [rid, "done", *none4], False, "not allowed")
    expect(c, "queued -> running (modal_call_id)", "set_training_status",
           [rid, "running", opt("fc-test-tdm12"), opt(None), opt(None), opt(None)], True)
    expect(c, "running -> running (progress metrics)", "set_training_status",
           [rid, "running", opt(None), opt(None), opt(json.dumps({"step": 150, "loss": 0.21})), opt(40)], True)
    expect(c, "done without checkpoint rejected", "set_training_status", [rid, "done", *none4], False, "requires checkpoint_uri")
    ckpt = f"test://tdm12/checkpoints/run-{rid}"
    expect(c, "running -> done", "set_training_status",
           [rid, "done", opt(None), opt(ckpt), opt(json.dumps({"step": 300, "loss": 0.12})), opt(85)], True)
    expect(c, "done -> running rejected (terminal)", "set_training_status", [rid, "running", *none4], False, "terminal")
    run = c.sql(f"SELECT * FROM training_run WHERE id = {rid}")[0]
    check("training run walked queued->running->done",
          run["status"] == "done" and unopt(run["checkpoint_uri"]) == ckpt and run["gpu_cost_cents"] == 85
          and unopt(run["modal_call_id"]) == "fc-test-tdm12" and unopt(run["started_at"]) is not None
          and unopt(run["finished_at"]) is not None,
          f"(status {run['status']}, gpu {run['gpu_cost_cents']})")

    # ---------------------------------------------------------------- eval
    expect(c, "start_eval_run", "start_eval_run", [T, ckpt, 10], True)
    ev = max(c.sql(f"SELECT * FROM eval_run WHERE ticket_id = {T}"), key=lambda r: r["id"])
    eid = ev["id"]
    check("eval run is a running candidate linked to the training run",
          ev["status"] == "running" and ev["kind"] == "candidate" and ev["training_run_id"] == rid and ev["trials"] == 10,
          f"(id {eid}, kind {ev['kind']})")

    outcomes = [True, True, False, True, True, True, False, True, True, True]  # 8/10
    for i, ok in enumerate(outcomes[:9]):
        good, text = c.call("record_eval_trial",
                            [eid, i, ok, 30_000 + i, eps[0] if i == 0 else 0, "" if ok else "test.tdm12 dropped cup"])
        if not good:
            FAILS.append(f"record trial {i}")
            print(f"[FAIL] record trial {i}: {text}")
    print("[INFO] recorded 9 trials")
    expect(c, "duplicate trial index rejected", "record_eval_trial", [eid, 3, True, 1, 0, ""], False, "already has trial")
    expect(c, "trial index out of range rejected", "record_eval_trial", [eid, 10, True, 1, 0, ""], False, "out of range")
    lo9, hi9 = stats.wilson_interval(7, 9)
    expect(c, "finalize with 9/10 trial rows rejected", "finalize_eval_run",
           {"eval_run_id": eid, "successes": 7, "trials": 10, "ci_low": lo9, "ci_high": hi9, "summary": "7/9"},
           False, "9 of 10 trial rows")

    good, text = c.call("record_eval_trial", [eid, 9, outcomes[9], 30_009, 0, ""])
    check("record trial 9 (10th)", good, text)
    lo, hi = stats.wilson_interval(8, 10)
    expect(c, "finalize with successes that don't match the trial rows rejected", "finalize_eval_run",
           {"eval_run_id": eid, "successes": 10, "trials": 10, "ci_low": 0.72, "ci_high": 1.0, "summary": "faked"},
           False, "does not match")

    # Finalize through the orchestrator's own EvalRunFinalizer (kwargs -> HTTP object body).
    results: list[tuple[bool, str]] = []
    fin = stats.EvalRunFinalizer(lambda **kw: results.append(c.call("finalize_eval_run", kw)))
    run_view = {"id": eid, "trials": 10, "status": "running"}
    fired = False
    for trial in c.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {eid}"):
        fired = fin.on_eval_trial(trial, run_view) or fired
    check("EvalRunFinalizer fired once and the reducer accepted it",
          fired and len(results) == 1 and results[0][0], str(results))

    expect(c, "second finalize rejected", "finalize_eval_run",
           {"eval_run_id": eid, "successes": 8, "trials": 10, "ci_low": lo, "ci_high": hi, "summary": stats.format_ci(8, 10)},
           False, "already finalized")
    expect(c, "trial after finalize rejected", "record_eval_trial", [eid, 0, True, 1, 0, ""], False, "not running")
    ev = c.sql(f"SELECT * FROM eval_run WHERE id = {eid}")[0]
    check("eval run finalized 8/10 with the Wilson CI",
          ev["status"] == "done" and ev["successes"] == 8 and ev["completed"] == 10
          and abs(ev["ci_low"] - lo) < 1e-9 and abs(ev["ci_high"] - hi) < 1e-9,
          f"({ev['successes']}/{ev['trials']}, CI {ev['ci_low']:.3f}-{ev['ci_high']:.3f})")

    # ---------------------------------------------------------------- planning
    expect(c, "failure cluster with a foreign/unknown episode rejected", "record_failure_cluster",
           [T, "test.tdm12 drops mid-grasp", 2, json.dumps([999_999])], False, "unknown episode")
    expect(c, "record_failure_cluster", "record_failure_cluster",
           [T, "test.tdm12 drops mid-grasp", 2, json.dumps(eps[:2])], True)
    cl = max(c.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {T}"), key=lambda r: r["id"])
    reasoning = ("test.tdm12: 2 of 10 eval trials dropped the cup mid-grasp; recovery takeovers at the "
                 "failure point are cheaper ($5) than clean demos and target that mode directly.")
    expect(c, "data plan with data_type purchased rejected", "record_data_plan",
           [T, "purchased", 5, reasoning, "{}"], False, "data_type must be one of")
    expect(c, "data plan with empty reasoning rejected", "record_data_plan",
           [T, "recovery_takeover", 5, "   ", "{}"], False, "reasoning must not be empty")
    expect(c, "data plan citing an unknown cluster rejected", "record_data_plan",
           [T, "recovery_takeover", 5, reasoning, json.dumps({"failure_cluster_ids": [999_999]})], False, "not on ticket")
    expect(c, "record_data_plan", "record_data_plan",
           [T, "recovery_takeover", 5, reasoning,
            json.dumps({"failure_cluster_ids": [cl["id"]], "eval_run_id": eid, "success_rate": 0.8})], True)
    plan = max(c.sql(f"SELECT * FROM data_plan WHERE ticket_id = {T}"), key=lambda r: r["id"])
    check("data plan stored as proposed, unapproved, with reasoning and cluster ids",
          plan["status"] == "proposed" and plan["approved_cents"] == 0 and unopt(plan["approved_by"]) is None
          and plan["unit_price_cents"] == 500 and plan["est_cost_cents"] == 2500
          and plan["reasoning"] == reasoning and plan["failure_cluster_ids"] == [cl["id"]],
          f"(id {plan['id']}, est {plan['est_cost_cents']}c)")

    types = [r["type"] for r in c.sql("SELECT * FROM event_log")]
    for ty in ("training.queued", "training.status", "eval.started", "eval.trial", "eval.finalized",
               "planner.failure_cluster", "planner.data_plan"):
        check(f"event_log has {ty}", ty in types)

    print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
