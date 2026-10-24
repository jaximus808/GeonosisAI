"""TDM-71 proof: a full TDM-28 harness loop (local SpacetimeDB + fake robot + real orchestrator,
stub Modal, fake contributor), then the report routes on that orchestrator.

    cd services/orchestrator && TDM28_PORT=3071 uv run python ../../spikes/tdm-71/harness_report_proof.py [--driver orchestrator|scripted]

Never touches maincloud. The orchestrator driver makes real Anthropic calls (diagnose, planner) on
fake data. Writes the report JSON/markdown to spikes/tdm-71/tmp/ (gitignored).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from conftest import FAKE_ROBOT_ARGS, new_ticket  # noqa: E402
from harness import LocalStack, wait_for  # noqa: E402
from scripted_flywheel import ScriptedFlywheel  # noqa: E402
from stub_modal import StubTrainer  # noqa: E402
from tdm26_fake_contributor import FakeContributor  # noqa: E402

OUT = REPO / "spikes" / "tdm-71" / "tmp"
ORCH_PORT = 8071


def _get(url: str, method: str = "GET") -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", default="orchestrator", choices=["orchestrator", "scripted"])
    ap.add_argument("--port", type=int, default=3071)
    ap.add_argument("--save", action="store_true", help="also POST /report/save (writes to the media store)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    st = LocalStack(port=a.port)
    contributor = trainer = None
    try:
        st.boot(fake_robot_args=FAKE_ROBOT_ARGS)
        robot_id = int(st.robot()["id"])
        if a.driver == "orchestrator":
            trainer = StubTrainer(st.client(), mode="stub-call").start()
        else:
            trainer = StubTrainer(st.client(), mode="queued").start()
        tid = new_ticket(st, robot_id, "put the cup in the bin (TDM-71 report proof)",
                         '{"eval_trials": 10, "target_episodes": 3, "max_iterations": 3}')
        t0 = time.monotonic()
        if a.driver == "orchestrator":
            contributor = FakeContributor(st.host, st.db, ticket_id=tid).start()
            # Auto-approve stands in for the human approve_spend click (approved_by "auto:flywheel").
            st.spawn_orchestrator(port=ORCH_PORT, extra_env={"RDF_MODAL_STUB": "1",
                                                             "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": "5000"})
            wait_for(lambda: st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] in ("shipped", "failed"),
                     420, f"ticket {tid} shipped (log {st.logs / 'orchestrator.log'})", every=1)
        else:
            ScriptedFlywheel(st, robot_id).run(tid)
            st.spawn_orchestrator(port=ORCH_PORT, extra_env={"RDF_MODAL_STUB": "1"})
        status = st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"]
        print(f"[tdm71] driver={a.driver} ticket {tid} -> {status} in {time.monotonic() - t0:.0f}s")
        time.sleep(2)  # let the shipped step append flywheel.shipped

        base = f"http://127.0.0.1:{ORCH_PORT}/tickets/{tid}"
        code, body = _get(base + "/report")
        rep = json.loads(body)
        (OUT / f"report-{a.driver}.json").write_text(json.dumps(rep, indent=2))
        code_md, md = _get(base + "/report.md")
        (OUT / f"report-{a.driver}.md").write_bytes(md)
        print(f"[tdm71] GET /report {code}, /report.md {code_md} ({len(md)} bytes) -> {OUT}")
        if a.save:
            code_s, saved = _get(base + "/report/save", method="POST")
            print(f"[tdm71] POST /report/save {code_s}: {saved.decode()[:600]}")
        b, f = rep["baseline"], rep["final"]
        print("[tdm71] fake.any:", rep["fake"]["any"])
        print("[tdm71] banner:", rep["fake"]["banner"])
        print("[tdm71] mocked:", [m["detail"] for m in rep["mocked"]])
        print("[tdm71] before:", b and b["ci_text"], " after:", f and f["ci_text"])
        print("[tdm71] outcome:", rep["outcome"].get("verdict"))
        for it in rep["iterations"]:
            p = it["data_plan"] or {}
            print(f"[tdm71] iter {it['iteration']}: plan {p.get('data_type')} x{p.get('amount')} "
                  f"bought {it['episodes_bought']} accepted {it['episodes_accepted']} rejected {len(it['episodes_rejected'])} "
                  f"cost {it['cost_cents']} train {(it['training_run'] or {}).get('training_run_id')}")
            print(f"[tdm71]   reasoning: {(p.get('reasoning') or '')[:300]}")
        print("[tdm71] total_cost:", json.dumps(rep["total_cost"]))
        print("[tdm71] deployed:", rep["deployed_checkpoint"])
        print("[tdm71] event_log:", rep["event_log"]["algorithm"], rep["event_log"]["count"], rep["event_log_hash"],
              "span", {k: v for k, v in rep["event_log"]["ticket_span"].items() if k != "by_type"})
        print("[tdm71] missing:", rep["missing"])
        ok = code == 200 and code_md == 200 and rep["fake"]["any"] and md.decode().splitlines()[2].startswith("> **WARNING: FAKE")
        print("[tdm71] PROOF", "OK" if ok else "FAILED")
        return 0 if ok else 1
    finally:
        if trainer:
            trainer.stop()
        if contributor:
            contributor.stop()
        st.stop()


if __name__ == "__main__":
    raise SystemExit(main())
