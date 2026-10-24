"""TDM-33 proof: a REAL local Factory Droid session (the Data Droid) on a throwaway SpacetimeDB with
LABELLED FIXTURE episodes. Never touches maincloud.

    cd services/factory && uv run python ../../spikes/tdm-33-data-droid/prove_data_droid.py

1. Clean worktree of HEAD -> publish the module to a local server on 127.0.0.1:3033 (db tdm33local),
   with fresh orchestrator / robot_client / reviewer / factory identities (tests/integration/harness.py).
2. Robot + ticket + 7 fixture episodes (rdf_training.convert_fixtures, fixture=true in every parquet):
   3 clean + 2 recovery_takeover (one has no takeover_ts on its row) + 1 more clean that review rejects
   + 1 recovery left pending. The fixture "reviewer" (reviewer identity) accepts 3 of them.
3. Turn 1: ``launch_data_droid(target=5)``: the droid inspects, validates and parks with
   WAITING_FOR dataset:accepted_count>=5.
4. The reviewer accepts 2 more (accepted_count 5). Once the predicate holds, the proof calls
   ``FactoryClient.resume`` with the text the TDM-27 bridge sends (the bridge itself is not run here).
5. Turn 2: assemble / verify / card / publish; parks with WAITING_FOR data_plan:iteration>=1.
6. Collects agent_session, droid.* event_log rows, the dataset, and writes proof_result.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "tests" / "integration"))
import harness  # noqa: E402

PORT, DB = 3033, "tdm33local"
TMP = Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "tdm-33"
FIX = TMP / "fixture-episodes"
TASK = "put the cup in the bin"


def sh(*cmd: str, cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{r.stderr[-2000:]}")
    return r.stdout


def clean_module() -> Path:
    wt = TMP / "pub"
    if wt.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO, capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)
    sh("git", "worktree", "add", "--detach", str(wt), "HEAD", cwd=REPO)
    mod = wt / "spacetime" / "spacetimedb"
    (mod / "node_modules").symlink_to(REPO / "spacetime" / "spacetimedb" / "node_modules")
    return mod


FIXTURE_SCRIPT = r"""
import json, sys
from rdf_training import convert_fixtures as fx
out = sys.argv[1]
w = fx.write_fixture_episode
specs = [("c1", "clean_teleop", 75, None, None), ("c2", "clean_teleop", 90, None, None),
         ("r1", "recovery_takeover", 150, 60, 135), ("c3", "clean_teleop", 60, None, None),
         ("r2", "recovery_takeover", 120, 40, 120), ("c4", "clean_teleop", 45, None, None),
         ("r3", "recovery_takeover", 90, 30, 80)]
eps = {}
for name, kind, n, tk, hb in specs:
    e = w(out, episode_id=name, kind=kind, n_frames=n, takeover_frame=tk, handback_frame=hb,
          with_joint_cmd=(name != "r1"))
    e["n_frames"] = n
    eps[name] = e
print(json.dumps(eps))
"""


def write_fixtures() -> dict:
    if FIX.exists():
        shutil.rmtree(FIX)
    FIX.mkdir(parents=True)
    out = sh("uv", "run", "--frozen", "--project", str(REPO / "services/training"), "python", "-c", FIXTURE_SCRIPT,
             str(FIX), cwd=REPO)
    return json.loads(out.strip().splitlines()[-1])


def create_episode(c, ticket_id: int, e: dict, *, drop_takeover: bool = False) -> int:
    before = {r["id"] for r in c.sql(f"SELECT id FROM episode WHERE ticket_id = {ticket_id}")}
    cams = {k: "file://" + v for k, v in e["cameras"].items()}
    tk = None if drop_takeover else e.get("takeover_ts")
    c.call_reducer("create_episode", ticket_id, 0, 0, e["kind"], "file://" + str(Path(e["actions_uri"]).parent),
                   "file://" + e["actions_uri"], json.dumps(cams), e["n_frames"], int(e["n_frames"] / 30 * 1000),
                   tk, e.get("handback_ts"), True if e["kind"] == "clean_teleop" else None, None)
    new = {r["id"] for r in c.sql(f"SELECT id FROM episode WHERE ticket_id = {ticket_id}")} - before
    return int(new.pop())


def review(c, eid: int, accepted: bool, reason: str) -> None:
    c.call_reducer("submit_review", eid, accepted, reason, json.dumps({"fixture": True, "by": "tdm33-proof"}))


async def main() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    harness.MODULE_SRC = clean_module()
    stack = harness.LocalStack(port=PORT, db=DB, root=TMP)
    stack.start_server()
    try:
        stack.publish()
        for role in ("orchestrator", "robot_client", "reviewer", "factory"):
            stack.make_identity(role)
        orch, robot, rev, fac = (stack.client(r) for r in ("orchestrator", "robot_client", "reviewer", "factory"))

        orch.call_reducer("register_robot", "test.tdm33-yam", "yam_7dof", "fixture", json.dumps({"exo": {}, "wrist": {}}))
        robot_id = int(orch.sql("SELECT id FROM robot WHERE name = 'test.tdm33-yam'")[0]["id"])
        orch.call_reducer("create_ticket", robot_id, TASK, 0.8, 50_000, json.dumps({"target_episodes": 5}))
        ticket_id = int(orch.sql("SELECT id FROM ticket")[0]["id"])

        eps = write_fixtures()
        ids = {}
        for name in ("c1", "c2", "r1", "c4", "r3"):
            ids[name] = create_episode(robot, ticket_id, eps[name], drop_takeover=(name == "r1"))
        review(rev, ids["c1"], True, "")
        review(rev, ids["c2"], True, "")
        review(rev, ids["c4"], False, "[FIXTURE] gripper never closes")
        review(rev, ids["r1"], True, "")
        print("[tdm33] seeded:", ids, file=sys.stderr)

        os.environ.update(stack.env("factory"))  # the droid's tool commands inherit this (local DB, factory role)
        if os.environ.get("TDM33_DRY") == "1":  # no droid: run the tool chain by hand (pre-flight, no credits)
            for c in ("c3", "r2"):
                ids[c] = create_episode(robot, ticket_id, eps[c])
                review(rev, ids[c], True, "")
            w, out = TMP / "dry", TMP / "dry-datasets"
            tool = ["uv", "run", "--frozen", "--project", "services/training", "python", "-m", "rdf_training.data_droid_tools"]
            for args in (["inspect", "--ticket", str(ticket_id), "--target", "5", "--out", f"{w}/episodes.json"],
                         ["validate", "--episodes", f"{w}/episodes.json", "--out", f"{w}/validation.json"],
                         ["assemble", "--episodes", f"{w}/episodes.json", "--out-root", str(out), "--allow-fixtures"],
                         ["verify", "--dataset", f"{out}/lerobot/rdf/ticket-{ticket_id}", "--episodes", f"{w}/episodes.json"],
                         ["publish", "--ticket", str(ticket_id), "--manifest", f"{out}/lerobot/rdf/ticket-{ticket_id}/manifest.json"]):
                r = subprocess.run(tool + args, cwd=REPO, capture_output=True, text=True)
                print(f"--- {args[0]} rc={r.returncode}\n{r.stdout[-3000:]}{r.stderr[-1500:]}")
            print(orch.sql(f"SELECT * FROM dataset WHERE ticket_id = {ticket_id}"))
            return
        os.environ.setdefault("FACTORY_MODE", "local")
        from rdf_factory import FactoryClient
        from rdf_factory.droids.data import launch_data_droid, resume_data_droid

        t0 = time.time()
        async with FactoryClient() as fc:
            r1 = await launch_data_droid(fc, ticket_id, target=5, iteration=0, allow_fixtures=True,
                                         name=f"data-droid-t{ticket_id}-proof")
            print(f"[tdm33] turn 1: {r1.status} {r1.waiting_for} credits={r1.credits}", file=sys.stderr)
            turns = [r1.to_dict()]
            if r1.status == "waiting_for":
                ids["c3"] = create_episode(robot, ticket_id, eps["c3"])
                ids["r2"] = create_episode(robot, ticket_id, eps["r2"])
                review(rev, ids["c3"], True, "")
                review(rev, ids["r2"], True, "")
                ds = orch.sql(f"SELECT * FROM dataset WHERE ticket_id = {ticket_id}")[0]
                assert int(ds["accepted_count"]) >= 5, ds
                text = (f"{r1.waiting_for.get('raw')} is now true: dataset accepted_count={ds['accepted_count']} "
                        f"for ticket {ticket_id}")
                r2 = await resume_data_droid(fc, r1.agent_session_id, text, reason="tdm33 proof: predicate true")
                print(f"[tdm33] turn 2: {r2.status} {r2.waiting_for} credits={r2.credits}", file=sys.stderr)
                turns.append(r2.to_dict())
                if r2.status == "paused":  # missed the final line once: one nudge (bounded)
                    r3 = await fc.send(r1.agent_session_id, "Finish the remaining steps, then give the final status line.")
                    turns.append(r3.to_dict())
            sid = r1.agent_session_id

        row = dict(orch.sql(f"SELECT * FROM agent_session WHERE id = {sid}")[0])
        evs = orch.sql("SELECT * FROM event_log")
        droid_evs = [dict(e) for e in evs if str(e["type"]).startswith("droid.")]
        mine = [e for e in droid_evs if json.loads(e["payload_json"]).get("agent_session_id") == sid]
        assembled = [dict(e) for e in evs if e["type"] == "dataset.assembled"]
        ds_row = dict(orch.sql(f"SELECT * FROM dataset WHERE ticket_id = {ticket_id}")[0])
        from rdf_factory.droids.data import dataset_dir, datasets_root, ticket_dir

        cfg = fc.cfg
        dsd = dataset_dir(datasets_root(cfg.work_root), ticket_id)
        manifest = json.loads((dsd / "manifest.json").read_text()) if (dsd / "manifest.json").exists() else None
        card = (dsd / "README.md").read_text() if (dsd / "README.md").exists() else None
        tdir = ticket_dir(cfg.work_root, ticket_id)
        verify = json.loads((tdir / "verify.json").read_text()) if (tdir / "verify.json").exists() else None
        accepted = sorted(int(r["id"]) for r in orch.sql(f"SELECT id, review_status FROM episode WHERE ticket_id = {ticket_id}")
                          if r["review_status"] == "accepted")
        result = {
            "fixture": True, "note": "LABELLED FIXTURE episodes on a throwaway local SpacetimeDB (127.0.0.1:3033)",
            "ticket_id": ticket_id, "episode_ids": ids, "accepted_ids": accepted,
            "agent_session": {k: row.get(k) for k in ("id", "role", "status", "current_step", "factory_session_id",
                                                      "factory_session_ids_json", "waiting_for_json", "resume_payload_json")},
            "turns": [{k: t.get(k) for k in ("protocol", "status", "turn", "credits", "credits_total", "seconds",
                                             "waiting_for", "error")} | {"final_text": (t.get("text") or "")[-1500:]}
                      for t in turns],
            "credits_total": turns[-1].get("credits_total"), "wall_s": round(time.time() - t0, 1),
            "event_log_total": len(evs), "droid_events": len(mine),
            "droid_event_kinds": {k: sum(1 for e in mine if e["type"] == k) for k in sorted({e["type"] for e in mine})},
            "tool_calls": [json.loads(e["payload_json"])["text"] for e in mine if e["type"] == "droid.tool_call"],
            "permission_denied": [json.loads(e["payload_json"])["text"] for e in mine if e["type"] == "droid.permission_denied"],
            "dataset_row": ds_row, "dataset_assembled_events": [json.loads(e["payload_json"]) for e in assembled],
            "dataset_dir": str(dsd), "manifest_episode_ids": (manifest or {}).get("episode_ids"),
            "manifest_matches_accepted": (manifest or {}).get("episode_ids") == accepted,
            "verify_ok": (verify or {}).get("ok"), "takeover_cuts": (verify or {}).get("takeover_cuts"),
            "lerobot_loader": (verify or {}).get("lerobot_loader"), "card_chars": len(card or ""),
        }
        (HERE / "proof_result.json").write_text(json.dumps(result, indent=2, default=str))
        if card:
            (HERE / "dataset_card.md").write_text(card)
        if manifest:
            (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps({k: result[k] for k in ("ticket_id", "accepted_ids", "manifest_episode_ids", "verify_ok",
                                                 "credits_total", "droid_events", "card_chars")}, default=str))
    finally:
        stack.stop()
        subprocess.run(["git", "worktree", "remove", "--force", str(TMP / "pub")], cwd=REPO, capture_output=True)


if __name__ == "__main__":
    asyncio.run(main())
