"""Seed 3 finished, clearly labelled SAMPLE tickets so the dashboard shows what real usage looks like.

    cd services/orchestrator
    uv run python ../../scripts/demo/seed_samples.py --target maincloud                          # dry run
    uv run python ../../scripts/demo/seed_samples.py --target maincloud --orchestrator-stopped --yes
    uv run python ../../scripts/demo/seed_samples.py --target local --host http://127.0.0.1:3191 --db samplesdb --yes

These are FABRICATED demo records, never real runs. Every one is labelled:
  * task_description starts with "[Sample]" (the web shows a SAMPLE badge on the cockpit header, the
    dashboard list, /factory and the report, and the landing page's live numbers leave them out);
  * the robot is `sample-sim-yam` (camera_config_json {"sim": true, "sample": true}); it never
    heartbeats, so it stays offline and robot auto-selection never picks it;
  * run_settings hyperparams.prompt, every planner reasoning / review reason / failure note starts
    "[Sample]"; event_log payloads, training metrics_json, review metrics_json and plan evidence carry
    "sample": true (run_settings hyperparams only accept known keys, so `sample` can't go there);
  * training runs are STUB (modal_call_id `stub-sample-...`, checkpoint `sample://stub-no-gpu/...`):
    no GPU ran; payouts use provider `stripe_test` with ref `sample-no-transfer-<n>` (no Stripe call);
  * the diagnose / planner evidence names model "none (sample)": no LLM made these decisions.
Media: baseline-trial episodes reuse REAL sim recordings from this DB (sim robots, kind baseline,
modal-vol:// media) when there are any. Takeover / teleop episodes have no video
(storage_uri `sample://no-video`, cameras_json {}). No URL is made up.
Timestamps: every reducer stamps ctx.timestamp, so the rows carry the time you run this (only the
episodes' takeover/handback marks are set in the past). Faking row times would need a module change.

The tickets end shipped / shipped / failed, so nothing is left for the orchestrator to act on. But the
walk goes through every stage (created -> baseline -> ... -> eval -> shipped, one reducer per step)
and a running orchestrator reacts to each one (baseline/eval robot jobs, Claude diagnose/planner calls,
dispatch, Modal training). So on maincloud, STOP every orchestrator subscribed to it first (~20 s of
writes), then restart it: at boot it skips terminal tickets. --orchestrator-stopped confirms that.
Re-running is safe: a ticket whose "[Sample] ..." description already exists is skipped.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _target as T  # noqa: E402
from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402

TAG = "samples"
ACTOR = "sample:seed"
ROBOT_NAME = "sample-sim-yam"
ROBOT_CAMS = {"sim": True, "sample": True, "tag": "[Sample]", "cameras": {},
              "note": "Sample robot for seeded example tickets. Never online; not a real or running sim robot."}
CONTRIBUTOR_NAME = "[Sample] Demo contributor"
BASE_CKPT = "gs://openpi-assets/checkpoints/pi05_base/params"
NO_VIDEO = "sample://no-video"
MIN = 60_000_000  # micros


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, min(c - h, p)), min(1.0, max(c + h, p))


def j(o) -> str:
    return json.dumps(o, separators=(",", ":"))


# ---------------------------------------------------------------- the three stories
# trials: list of (success, failure_note); clusters: (label, description, phase, trial indices)

def trials(n_ok: int, fails: list[str], n: int = 10) -> list[tuple[bool, str]]:
    out = [(False, f) for f in fails]
    out += [(True, "")] * n_ok
    # interleave so successes are not all at the end
    order = [(i * 7) % n for i in range(n)]
    return [out[i] for i in order]


SAMPLES = [
    {
        "key": "cube",
        "desc": "[Sample] Pick up the red cube and place it on the plate",
        "prompt": "[Sample] pick up the red cube and place it on the plate",
        "target": 0.8, "budget": 6000,
        "iterations": [
            {
                "baseline": trials(3, ["[Sample] closed the gripper above the cube, missed the grasp"] * 4
                                   + ["[Sample] cube slipped out during transport"] * 3),
                "clusters": [
                    ("grasp closes early", "[Sample] The gripper closes 1-2 cm above the cube and lifts nothing.",
                     "grasp", True),
                    ("drops during transport", "[Sample] The cube is held but slips out on the way to the plate.",
                     "transport", True),
                ],
                "plan": ("recovery_takeover", 7, 500,
                         "[Sample] 7 of 10 baseline failures are recoverable mid-episode (both clusters leave the cube "
                         "in reach), so recovery takeovers at the failure point teach the exact correction for less "
                         "than clean demos would. 7 x $5.00 = $35.00 fits the $60.00 budget."),
                "episodes": [True] * 6 + [False],
                "reject": "[Sample] Takeover ended before the cube reached the plate; no complete recovery.",
                "final": trials(9, ["[Sample] cube placed on the plate rim and slid off"]),
            },
        ],
        "end": "shipped",
    },
    {
        "key": "cup",
        "desc": "[Sample] Put the paper cup in the bin",
        "prompt": "[Sample] put the paper cup in the bin",
        "target": 0.8, "budget": 9000,
        "iterations": [
            {
                "baseline": trials(2, ["[Sample] crushed the cup rim while grasping"] * 5
                                   + ["[Sample] released the cup beside the bin"] * 3),
                "clusters": [
                    ("crushes cup on grasp", "[Sample] Grasp force deforms the cup and it pops out.", "grasp", False),
                    ("releases off target", "[Sample] The cup is carried but dropped beside the bin.", "place", True),
                ],
                "plan": ("clean_teleop", 5, 800,
                         "[Sample] Most failures (5 of 8) are grasp failures a takeover cannot fix mid-episode, so "
                         "clean teleop demos of a soft side grasp come first. 5 x $8.00 = $40.00 of the $90.00 budget."),
                "episodes": [True] * 4 + [False],
                "reject": "[Sample] Demo is shaky: the operator re-grasped twice; not a clean demonstration.",
                "final": trials(6, ["[Sample] released the cup beside the bin"] * 4),
            },
            {
                "clusters": [
                    ("releases off target", "[Sample] After training the grasp holds, but the cup still lands beside "
                     "the bin.", "place", True),
                ],
                "plan": ("recovery_takeover", 4, 500,
                         "[Sample] Iteration 2: the grasp is fixed (6/10). The 4 remaining failures are all late "
                         "releases, recoverable at the bin, so 4 recovery takeovers. 4 x $5.00 = $20.00; spend so far "
                         "$32.00 + $20.00 stays inside $90.00."),
                "episodes": [True] * 4,
                "final": trials(9, ["[Sample] cup bounced off the bin edge"]),
            },
        ],
        "end": "shipped",
    },
    {
        "key": "stack",
        "desc": "[Sample] Stack the blue block on the green block",
        "prompt": "[Sample] stack the blue block on the green block",
        "target": 0.8, "budget": 3000,
        "iterations": [
            {
                "baseline": trials(1, ["[Sample] blue block placed off-center and toppled"] * 6
                                   + ["[Sample] knocked the green block over on approach"] * 3),
                "clusters": [
                    ("off-center placement", "[Sample] The top block lands off-center and falls.", "place", True),
                    ("knocks base block", "[Sample] The arm hits the base block on the way in.", "approach", False),
                ],
                "plan": ("recovery_takeover", 6, 500,
                         "[Sample] 6 of 9 failures are placement errors a takeover can correct before release. "
                         "6 x $5.00 = $30.00, the whole $30.00 budget."),
                "episodes": [True] * 5 + [False],
                "reject": "[Sample] Takeover knocked the base block over; the episode does not show a stack.",
                "final": trials(4, ["[Sample] blue block placed off-center and toppled"] * 5
                                + ["[Sample] knocked the green block over on approach"]),
            },
        ],
        "end": "failed",
        "fail_note": "[Sample] Budget hit before the target: 4/10 after iteration 1 (target 80%), $25.00 of $30.00 "
                     "spent, and the next plan (4 more takeovers, $20.00) does not fit the $5.00 left.",
    },
]


# ---------------------------------------------------------------- writer

class Seeder:
    def __init__(self, c: SpacetimeClient, host: str, db: str, write: bool) -> None:
        self.c, self.host, self.db, self.write = c, host, db, write
        self.n_calls = 0
        self.pay_ref = 0

    def call(self, reducer: str, *args) -> None:
        self.n_calls += 1
        if not self.write:
            return
        self.c.call_reducer(reducer, *args)

    def newest(self, table: str, where: str) -> int:
        if not self.write:
            return 0
        rows = self.c.sql(f"SELECT id FROM {table} WHERE {where}")
        return max(int(r["id"]) for r in rows) if rows else 0

    def event(self, type_: str, payload: dict) -> None:
        self.call("append_event", ACTOR, type_, j({"sample": True, **payload}))

    def status(self, tid: int, to: str, note: str) -> None:
        self.call("set_ticket_status", tid, to, f"[Sample] {note}")

    # -- shared rows
    def robot(self) -> int:
        rows = self.c.sql(f"SELECT * FROM robot WHERE name = '{ROBOT_NAME}'")
        if rows:
            log(f"robot {ROBOT_NAME} exists (id {rows[0]['id']})")
            return int(rows[0]["id"])
        log(f"robot {ROBOT_NAME}: register (sim, sample, never online)")
        self.call("register_robot", ROBOT_NAME, "yam_6dof", "sample-0", j(ROBOT_CAMS))
        return self.newest("robot", f"name = '{ROBOT_NAME}'")

    def contributor(self) -> int:
        rows = self.c.sql(f"SELECT * FROM contributor WHERE name = '{CONTRIBUTOR_NAME}'")
        if rows:
            log(f"contributor {CONTRIBUTOR_NAME!r} exists (id {rows[0]['id']})")
            return int(rows[0]["id"])
        log(f"contributor {CONTRIBUTOR_NAME!r}: register (fresh identity, left offline; token not kept)")
        if not self.write:
            return 0
        req = urllib.request.Request(f"{self.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            tok = json.loads(resp.read())["token"]
        with SpacetimeClient(self.host, self.db, tok) as cc:
            cc.call_reducer("register_contributor", CONTRIBUTOR_NAME, "", ["laptop"], "contributor", 0)
            cc.call_reducer("contributor_offline")
        return self.newest("contributor", f"name = '{CONTRIBUTOR_NAME}'")

    def real_sim_media(self) -> list[dict]:
        """Real sim baseline recordings already in this DB (sim robot, modal-vol:// media), with the
        outcome their eval trial recorded, so a sample trial reuses a video of the same outcome."""
        sim = {int(r["id"]) for r in self.c.sql("SELECT * FROM robot")
               if (json.loads(r.get("camera_config_json") or "{}") or {}).get("sim") is True
               and not str(r.get("name", "")).startswith("sample-")}
        if not sim:
            return []
        outcome = {int(t["episode_id"]): bool(t["success"]) for t in self.c.sql("SELECT * FROM eval_trial")
                   if int(t.get("episode_id") or 0)}
        out = []
        for e in self.c.sql("SELECT * FROM episode WHERE kind = 'baseline'"):
            if int(e.get("robot_id") or 0) in sim and str(e.get("storage_uri", "")).startswith("modal-vol://") \
                    and int(e["id"]) in outcome:
                out.append({**e, "outcome": outcome[int(e["id"])]})
        return out

    # -- one ticket
    def ticket(self, s: dict, robot_id: int, contributor_id: int, media: list[dict]) -> None:
        desc = s["desc"].replace("'", "''")
        if self.c.sql(f"SELECT id FROM ticket WHERE task_description = '{desc}'"):
            log(f"{s['desc']!r} exists: skipped")
            return
        log(f"ticket {s['desc']!r}: budget ${s['budget'] / 100:.2f}, target {s['target']:.0%}, ends {s['end']}")
        overrides = {"eval_trials": 10, "max_iterations": 3, "hyperparams": {"prompt": s["prompt"]}}
        self.call("create_ticket", robot_id, s["desc"], s["target"], s["budget"], j(overrides))
        tid = self.newest("ticket", f"task_description = '{desc}'")
        self.event("sample.seeded", {"ticket_id": tid, "note": "Fabricated example ticket for the demo dashboard. "
                                     "Not a real run: no robot, no LLM, no GPU, no Stripe transfer.",
                                     "script": "scripts/demo/seed_samples.py"})
        self.status(tid, "baseline", "robot already onboarded")
        used_media: set[int] = set()
        accepted_all: list[int] = []
        prev_final = None
        spent = 0
        now = int(time.time() * 1_000_000)
        for it, step in enumerate(s["iterations"]):
            if it == 0:
                base = self.eval_run(tid, BASE_CKPT, step["baseline"], media, used_media, "baseline")
            else:
                base = prev_final
            if it == 0:
                self.status(tid, "diagnose", "baseline finished")
            clusters = []
            fails = [i for i, (ok, _) in enumerate(base["trials"]) if not ok]
            for ci, (label, d, phase, recoverable) in enumerate(step["clusters"]):
                idx = fails[ci::len(step["clusters"])]
                ex = [base["episodes"][i] for i in idx if base["episodes"].get(i)][:3]
                self.call("record_failure_cluster", tid, f"[Sample] {label}", len(idx), j(ex))
                cid = self.newest("failure_cluster", f"ticket_id = {tid}")
                clusters.append({"failure_cluster_id": cid, "label": f"[Sample] {label}", "description": d,
                                 "phase": phase, "recoverable_mid_episode": recoverable, "count": len(idx),
                                 "trial_indices": idx, "example_episode_ids": ex})
            self.event("diagnose.clusters", {
                "ticket_id": tid, "iteration": it, "eval_run_id": base["id"], "eval_kind": base["kind"],
                "checkpoint_uri": base["ckpt"], "trials": 10, "successes": base["k"], "failures": 10 - base["k"],
                "model": "none (sample)", "clusters": clusters,
                "recoverable_failures": sum(c["count"] for c in clusters if c["recoverable_mid_episode"]),
                "unrecoverable_failures": sum(c["count"] for c in clusters if not c["recoverable_mid_episode"]),
                "reasoning": "[Sample] Written by the sample seeder, not by a model."})
            self.status(tid, "plan_data", "failures clustered")
            dtype, amount, unit, why = step["plan"]
            evidence = {"sample": True, "model": "none (sample)", "failure_cluster_ids": [c["failure_cluster_id"] for c in clusters if c["failure_cluster_id"]],
                        "unit_price_cents": unit, "baseline": {"eval_run_id": base["id"], "successes": base["k"], "trials": 10},
                        "alternatives": [{"data_type": "clean_teleop" if dtype == "recovery_takeover" else "recovery_takeover",
                                          "rejected_because": "[Sample] fewer failures addressed per dollar"}]}
            if self.write:
                evidence["failure_cluster_ids"] = [x for x in evidence["failure_cluster_ids"] if x]
            else:
                evidence.pop("failure_cluster_ids")
            self.call("record_data_plan", tid, dtype, amount, why, j(evidence))
            pid = self.newest("data_plan", f"ticket_id = {tid}")
            self.call("approve_spend", tid, pid, amount * unit, "operator:sample")
            self.status(tid, "dispatch", "spend approved")
            eps = self.collect(tid, pid, dtype, unit, step, contributor_id, now - (len(s["iterations"]) - it) * 40 * MIN)
            spent += unit * sum(1 for ok in step["episodes"] if ok)
            self.status(tid, "review", "episodes collected")
            for (eid, ok) in eps:
                if ok:
                    accepted_all.append(eid)
            self.status(tid, "train", "enough accepted episodes")
            run_id, ckpt = self.train(tid, it, accepted_all)
            self.status(tid, "eval", "training finished ([STUB] no GPU)")
            final = self.eval_run(tid, ckpt, step["final"], media, used_media, "candidate")
            prev_final = final
            last = it == len(s["iterations"]) - 1
            if not last:
                self.status(tid, "diagnose", f"{final['k']}/10 is below the {s['target']:.0%} target: plan more data")
                continue
            if s["end"] == "shipped":
                self.status(tid, "shipped", f"{final['k']}/10 meets the {s['target']:.0%} target")
                self.call("mark_checkpoint_deployed", tid, run_id, final["id"], "[Sample] stub checkpoint, not deployed to any robot")
                self.event("flywheel.shipped", {
                    "ticket_id": tid, "iteration": it, "checkpoint_uri": ckpt, "training_run_id": run_id,
                    "eval_run_id": final["id"],
                    "baseline": {"eval_run_id": self._first_base, "successes": self._first_k, "trials": 10},
                    "final": {"eval_run_id": final["id"], "successes": final["k"], "trials": 10,
                              "ci_low": final["ci"][0], "ci_high": final["ci"][1]},
                    "accepted_episodes": len(accepted_all), "spent_cents": spent})
            else:
                # the next plan the budget can't cover, recorded as the reason (not approved)
                self.event("planner.blocked", {"ticket_id": tid, "iteration": it + 1, "reason": s["fail_note"],
                                               "budget_cents": s["budget"], "spent_cents": spent,
                                               "wanted": {"data_type": "recovery_takeover", "amount": 4, "est_cost_cents": 2000}})
                self.status(tid, "failed", s["fail_note"].removeprefix("[Sample] "))
        log(f"  ticket {tid}: done")

    def eval_run(self, tid: int, ckpt: str, ts: list[tuple[bool, str]], media: list[dict], used: set[int], kind: str) -> dict:
        self.call("start_eval_run", tid, ckpt, len(ts))
        rid = self.newest("eval_run", f"ticket_id = {tid}")
        episodes: dict[int, int] = {}
        for i, (ok, note) in enumerate(ts):
            eid = 0
            if kind == "baseline":  # reuse a real sim baseline recording with the same outcome, if any
                src = next((m for m in media if m["outcome"] == ok and int(m["id"]) not in used), None)
                if src is not None:
                    used.add(int(src["id"]))
                    self.call("create_episode", tid, 0, 0, "baseline", src["storage_uri"], src.get("actions_uri") or "",
                              src.get("cameras_json") or "{}", int(src.get("frame_count") or 0),
                              int(src.get("duration_ms") or 0), None, None, ok, ckpt)
                    eid = self.newest("episode", f"ticket_id = {tid}")
            episodes[i] = eid
            self.call("record_eval_trial", rid, i, ok, 14_000 + 900 * ((i * 5) % 7) + (0 if ok else 9_000), eid, note)
        k = sum(1 for ok, _ in ts if ok)
        ci = wilson(k, len(ts))
        self.call("finalize_eval_run", rid, k, len(ts), ci[0], ci[1], f"[Sample] {kind}: {k}/{len(ts)}")
        if kind == "baseline":
            self._first_base, self._first_k = rid, k
        log(f"  {kind} eval {k}/{len(ts)} (95% CI {ci[0]:.2f}-{ci[1]:.2f})"
            + (f", {sum(1 for v in episodes.values() if v)} trials with real sim video" if kind == "baseline" else ""))
        return {"id": rid, "k": k, "ci": ci, "kind": kind, "ckpt": ckpt, "trials": ts, "episodes": episodes}

    def collect(self, tid: int, pid: int, dtype: str, unit: int, step: dict, cid: int, t0: int) -> list[tuple[int, bool]]:
        out = []
        for n, ok in enumerate(step["episodes"]):
            instr = (f"[Sample] {'Take over the paused rollout at the failure and finish the task' if dtype == 'recovery_takeover' else 'Demonstrate the task start to finish'}"
                     f" (sample task {n + 1}/{len(step['episodes'])})")
            self.call("create_task", tid, dtype, unit, j({"instructions": instr, "data_plan_id": pid, "sample": True}))
            task_id = self.newest("task", f"ticket_id = {tid}")
            self.call("claim_task", task_id, cid)
            self.call("complete_task", task_id, j({"sample": True, "recorded": True}))
            start = t0 + n * 4 * MIN
            dur = 22_000 + 1_700 * n
            to = start + 6_000_000 if dtype == "recovery_takeover" else None
            hb = start + dur * 1000 - 2_000_000 if dtype == "recovery_takeover" else None
            self.call("create_episode", tid, task_id, cid, dtype, NO_VIDEO, "", "{}", dur * 30 // 1000, dur,
                      to, hb, ok, None)
            eid = self.newest("episode", f"ticket_id = {tid}")
            reason = "[Sample] Passes the deterministic checks: task completed, smooth, in workspace." if ok else step["reject"]
            self.call("submit_review", eid, ok, reason, j({"sample": True, "checks_passed": ok}))
            out.append((eid, ok))
        # payouts: accepted episodes reserve a pending payment; mark them paid (test mode, no transfer),
        # the last one waits on contributor onboarding
        if self.write:
            pays = sorted((p for p in self.c.sql(f"SELECT * FROM payment WHERE ticket_id = {tid}") if p["status"] == "pending"),
                          key=lambda p: int(p["id"]))
            for i, p in enumerate(pays):
                if i == len(pays) - 1 and len(pays) > 2:
                    self.call("mark_payment_awaiting_onboarding", int(p["id"]),
                              "[Sample] contributor has not finished Stripe test-mode onboarding")
                else:
                    self.pay_ref += 1
                    self.call("mark_payment_paid", int(p["id"]), "stripe_test", f"sample-no-transfer-{tid}-{self.pay_ref}")
        acc = sum(1 for _, ok in out if ok)
        log(f"  collected {len(out)} {dtype} episodes ({acc} accepted, {len(out) - acc} rejected), ${unit / 100:.2f} each")
        return out

    def train(self, tid: int, it: int, episode_ids: list[int]) -> tuple[int, str]:
        hp = {"sample": True, "stub": True, "steps": 2000, "batch_size": 32, "lr": 2.5e-5, "lora_rank": 16,
              "label": "[STUB] no GPU (sample)"}
        self.call("queue_training_run", tid, j(episode_ids or [1]), j(hp))
        rid = self.newest("training_run", f"ticket_id = {tid}")
        ckpt = f"sample://stub-no-gpu/ticket-{tid}/iter-{it}"
        self.call("set_training_status", rid, "running", f"stub-sample-{tid}-{it}", None, None, None)
        metrics = {"sample": True, "stub": True, "label": "[STUB] no GPU (sample)", "steps": 2000,
                   "note": "Sample training run: no GPU ran, no Modal job; the checkpoint is not a real model."}
        self.call("set_training_status", rid, "done", None, ckpt, j(metrics), 0)
        log(f"  training run [STUB] on {len(episode_ids)} accepted episodes -> {ckpt}")
        return rid, ckpt


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    T.add_target_args(ap)
    ap.add_argument("--orchestrator-stopped", action="store_true",
                    help="maincloud: confirm no orchestrator is subscribed to it while this runs")
    ap.add_argument("--only", help="seed only these sample keys (cube,cup,stack)")
    args = ap.parse_args()
    write = args.yes and not args.dry_run
    host, db, token = T.resolve(args, TAG)
    if args.target == "maincloud" and write and not args.orchestrator_stopped:
        T.die(TAG, "on maincloud, stop every orchestrator subscribed to it first (it reacts to each stage of the walk), "
                   "then pass --orchestrator-stopped. Restart it afterwards.")
    log(f"target {args.target}: {host} / {db}  ({'WRITE' if write else 'dry run: pass --yes to write'})")
    picked = [s for s in SAMPLES if not args.only or s["key"] in args.only.split(",")]
    with T.client(host, db, token) as c:
        sd = Seeder(c, host, db, write)
        media = sd.real_sim_media()
        log(f"{len(media)} real sim baseline recordings available for reuse "
            f"({sum(1 for m in media if m['outcome'])} successes, {sum(1 for m in media if not m['outcome'])} failures)")
        robot_id = sd.robot()
        cid = sd.contributor()
        for s in picked:
            sd.ticket(s, robot_id, cid, media)
        log(f"{'wrote' if write else 'would make'} {sd.n_calls} reducer calls")
        if write:
            rows = [r for r in c.sql("SELECT * FROM ticket") if str(r["task_description"]).startswith("[Sample]")]
            print(T.table([{"id": r["id"], "status": r["status"], "iter": r["iteration"], "spent": r["spent_cents"],
                            "budget": r["budget_cents"], "task": r["task_description"]} for r in rows],
                          ["id", "status", "iter", "spent", "budget", "task"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
