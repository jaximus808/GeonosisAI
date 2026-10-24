"""TDM-92 proof: deterministic dataset QA on purchased episodes, end to end through SpacetimeDB.

Boots a throwaway local SpacetimeDB (127.0.0.1:3092, db tdm92qa, data under $TMPDIR/tdm-92, never
maincloud) with the TDM-28 ``LocalStack`` harness, publishes the module, then:

  robot test.tdm92-yam (i2rt_yam) -> ticket (target_episodes 4) -> seller signs up (own identity,
  kind dataset_seller) -> offer A (FIXTURES) create + accept -> the 4-episode synthetic LeRobot v2.1
  fixture dataset is IMPORTED (dataset_import: LeRobot -> our episode format) -> ingest_purchased_episode
  x4 -> a Bus running ONLY the ``tdm92_dataset_qa`` handler writes qa_result rows (summary
  ``deterministic_qa`` last) and rejects failures via submit_review.

``--real`` adds a second ticket (target 3) and offer B with REAL public HF data: 2 episodes of allenai/24112025-yam-01 (bimanual YAM,
left arm imported) and 1 of lerobot/svla_so100_pickplace (SO-100, 5 joints + gripper, degrees).

The LLM stage (TDM-93) is NOT run by default: passing episodes get a SCRIPTED accept, clearly labelled
"[SCRIPTED LLM STAND-IN]" in the review reason, so finalize_offer can settle and show the count.
``--llm`` instead runs TDM-93's real handler (real Claude calls) when it is importable.

    cd services/orchestrator && uv run python scripts/tdm92_prove_dataset_qa.py [--real] [--llm] [--keep]

Checks: fixtures -> exactly ``good`` passes deterministic QA; ``duplicate`` / ``wrong_camera_count`` /
``static`` are rejected with check names duplicate / camera_set / static in the review reason; offer A
settles with accepted_count 1. Real -> both YAM episodes pass; the SO-100 one fails ``embodiment``.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(HERE.parents[1] / "tests"))

import tdm92_fixtures as fx  # noqa: E402
from harness import LocalStack  # noqa: E402
from rdf_common.episode_format import read_actions  # noqa: E402
from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402

from rdf_orchestrator import dataset_qa as dq  # noqa: E402
from rdf_orchestrator.dataset_import import ImportedEpisode, import_lerobot  # noqa: E402

PORT, DB = 3092, "tdm92qa"
PRICE = 500
OUT = REPO / "spikes" / "tdm-92-dataset-qa" / "tmp" / "proof"
REAL = [  # (label, spec, episodes, arm, camera_map, expect_pass, expect_check)
    ("yam_left_ep0", "hf://allenai/24112025-yam-01", [0], "left", {"right": ""}, True, None),
    ("yam_left_ep1", "hf://allenai/24112025-yam-01", [1], "left", {"right": ""}, True, None),
    ("so100_ep1", "hf://lerobot/svla_so100_pickplace", [1], None, {}, False, "embodiment"),
]


def scripted_llm(stdb, episode, ticket) -> None:
    stdb.call_reducer("submit_review", int(episode["id"]), True,
                      "[SCRIPTED LLM STAND-IN, not TDM-93] deterministic QA passed; accepted for the proof only",
                      json.dumps({"scripted": True, "stage": "llm_qa stand-in"}))


def start_bus(st: LocalStack, cfg: dq.QAConfig, llm: bool):
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.bus import registry as default_registry
    from rdf_orchestrator.handlers import dataset_qa as h
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    names = {"tdm92_dataset_qa"} | ({"tdm93_llm_qa"} if llm else set())
    reg = Registry()
    reg.handlers = [x for x in default_registry.handlers if x.name in names]
    missing = names - {x.name for x in reg.handlers}
    if missing:
        raise SystemExit(f"handler(s) not registered: {missing}")
    for t in ("episode", "ticket", "robot", "qa_result", "review", "dataset_offer"):
        reg.watch(t)
    h.make_config = lambda: cfg
    h.make_stdb = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))
    h.after_pass = None if llm else scripted_llm
    if llm:
        from rdf_orchestrator.handlers import llm_qa as l93

        l93.make_stdb = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))

    def run():
        import asyncio

        async def go():
            bus = Bus(reg, BusClient(host=st.host, db=st.db, token=st.token("orchestrator")), workers=4)
            await bus.start()
            while True:
                await asyncio.sleep(3600)
        asyncio.run(go())

    threading.Thread(target=run, name="tdm92-bus", daemon=True).start()
    time.sleep(2)


def reuse_or_import(label: str, spec: str, episodes, arm, camera_map) -> list[ImportedEpisode]:
    d = OUT / "real" / label
    marker = d / "imported.json"
    if marker.exists():
        out = []
        for e in json.loads(marker.read_text()):
            out.append(ImportedEpisode(e["source_index"], Path(e["dir"]), Path(e["actions"]),
                                       {k: Path(v) for k, v in e["cameras"].items()}, e["frames"], e["duration_ms"],
                                       e["success"], e["task"], e["notes"]))
        return out
    got = import_lerobot(spec, d, episodes=episodes, arm=arm, camera_map=camera_map, assume_success=True)
    marker.write_text(json.dumps([{"source_index": e.source_index, "dir": str(e.dir), "actions": str(e.actions_path),
                                   "cameras": {k: str(v) for k, v in e.cameras.items()}, "frames": e.frame_count,
                                   "duration_ms": e.duration_ms, "success": e.success, "task": e.task,
                                   "notes": e.notes} for e in got]))
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="also import + QA real public HF LeRobot episodes")
    ap.add_argument("--llm", action="store_true", help="run TDM-93's real LLM QA handler instead of the stand-in")
    ap.add_argument("--keep", action="store_true", help="leave the local SpacetimeDB running")
    args = ap.parse_args()

    root = fx.build_lerobot_fixture(OUT / "lerobot_fixture")
    fixtures = import_lerobot(str(root), OUT / "imported_fixture", assume_success=True)
    print(f"imported {len(fixtures)} fixture episodes from {root} (LeRobot v2.1 -> rdf episode format)")
    real = []
    if args.real:
        for label, spec, eps, arm, cmap, exp, chk in REAL:
            for e in reuse_or_import(label, spec, eps, arm, cmap):
                real.append((label, e, exp, chk))
                print(f"imported {label} from {spec} ep {e.source_index}: {e.frame_count} frames, cameras {sorted(e.cameras)}")

    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-92")
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        orch = st.client("orchestrator")
        req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            seller_token = json.loads(r.read())["token"]
        with SpacetimeClient(st.host, st.db, seller_token) as c:
            c.call_reducer("register_contributor", "test.tdm92 dataset seller", "", [], "dataset_seller", 0)
        seller_id = int(st.newest("contributor")["id"])
        orch.call_reducer("register_robot", "test.tdm92-yam", "i2rt_yam", "v0", "{}")
        robot_id = int(st.newest("robot")["id"])

        def make_ticket(target: int, task: str) -> int:
            orch.call_reducer("create_ticket", robot_id, f"test.tdm92 {task}", 0.8, 100_000,
                              json.dumps({"target_episodes": target}))
            return int(st.newest("ticket")["id"])

        cfg = dq.load_config(allow_fixtures=True)  # fixture data is labelled fixture:true; production rejects it
        start_bus(st, cfg, args.llm)

        def make_offer(ticket_id: int):
            orch.call_reducer("create_dataset_offer", ticket_id, seller_id, PRICE, {"some": "test.tdm92"})
            oid = int(st.newest("dataset_offer")["id"])
            orch.call_reducer("accept_dataset_offer", oid)
            return oid

        offer_a = make_offer(make_ticket(len(fixtures), fx.TASK))
        expected: dict[int, tuple[str, bool, str | None, str]] = {}
        for f, e in zip(fx.EPISODES, fixtures):
            orch.call_reducer("ingest_purchased_episode", *e.ingest_args(offer_a))
            eid = int(st.newest("episode")["id"])
            expected[eid] = (f"fixture:{f.name}", f.expect_pass, f.expect_check, f.expect_reason)
        offer_b = None
        if real:
            offer_b = make_offer(make_ticket(len(real), 'real HF episodes (any task)'))
            for label, e, exp, chk in real:
                orch.call_reducer("ingest_purchased_episode", *e.ingest_args(offer_b))
                expected[int(st.newest("episode")["id"])] = (f"real:{label}", exp, chk, "")

        deadline = time.time() + (900 if args.llm else 300)
        while time.time() < deadline:
            done = {int(r["episode_id"]) for r in st.sql("SELECT * FROM review")}
            if set(expected) <= done:
                break
            time.sleep(1)

        ok = True
        print(f"\n{'episode':>7s}  {'source':26s} {'deterministic_qa':16s} review    failed checks / reason")
        for eid, (label, exp_pass, exp_check, exp_reason) in expected.items():
            qa = st.sql(f"SELECT * FROM qa_result WHERE episode_id = {eid}")
            latest = {}
            for r in sorted(qa, key=lambda r: int(r["id"])):
                latest[r["check_name"]] = r
            order = [r["check_name"] for r in sorted(qa, key=lambda r: int(r["id"]))]
            summ = latest.get(dq.SUMMARY_CHECK)
            rev = (st.sql(f"SELECT * FROM review WHERE episode_id = {eid}") or [None])[0]
            failed = [k for k, r in latest.items() if not r["passed"] and k != dq.SUMMARY_CHECK]
            verdict = "-" if rev is None else ("ACCEPT" if rev["accepted"] else "reject")
            print(f"{eid:7d}  {label:26s} {('pass' if summ and summ['passed'] else 'FAIL' if summ else 'missing'):16s} "
                  f"{verdict:8s}  {failed} {'' if rev is None else rev['reason'][:400]}")
            good = summ is not None and bool(summ["passed"]) == exp_pass and order[-1] == dq.SUMMARY_CHECK
            good &= set(dq.EPISODE_CHECKS + dq.DATASET_CHECKS) <= set(latest)
            if exp_pass:
                good &= not failed and rev is not None and (rev["accepted"] or args.llm)
            else:
                good &= exp_check in failed and rev is not None and not rev["accepted"]
                good &= exp_check in rev["reason"] and exp_reason.lower() in rev["reason"].lower()
            if not good:
                print(f"         ^^ UNEXPECTED (expected {'pass' if exp_pass else 'reject: ' + str(exp_check)}; "
                      f"qa order ends with {order[-1:]})")
            ok &= good

        for oid in [o for o in (offer_a, offer_b) if o]:
            try:
                orch.call_reducer("finalize_offer", oid)
            except Exception as e:  # noqa: BLE001 - TDM-91's handler may have settled it already (not running here)
                print(f"finalize_offer({oid}): {e}")
            o = st.sql(f"SELECT * FROM dataset_offer WHERE id = {oid}")[0]
            print(f"offer {oid}: status={o['status']} needed={o['needed_count']} ingested={o['ingested_count']} "
                  f"accepted={o['accepted_count']} -> payout {int(o['accepted_count']) * PRICE} cents (test mode, not reserved here)")
            if oid == offer_a and not args.llm:
                ok &= o["status"] == "done" and int(o["accepted_count"]) == 1
        evs = st.sql("SELECT * FROM event_log WHERE type = 'qa.dataset_checked'")
        print(f"qa.dataset_checked events: {len(evs)}; qa_result rows: {len(st.sql('SELECT * FROM qa_result'))}")
        ok &= len(evs) == len(expected)
        print("\nOK" if ok else "\nFAILED")
        return 0 if ok else 1
    finally:
        if not args.keep:
            st.stop()


if __name__ == "__main__":
    sys.exit(main())
