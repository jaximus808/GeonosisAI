"""TDM-69 fake robot with DAgger interventions, for the TDM-28 harness (LOCAL ONLY, everything [FAKE]).

A subclass of the TDM-28 fake robot (``rdf_robot_client.fake.FakeRobot``) that implements the
robot side of ``rdf_robot_client.interventions`` for rollouts whose payload carries
``interventions.enabled``; anything else behaves exactly like the stock fake.

  * advertises ``camera_config_json.capabilities = ["interventions"]`` (so the orchestrator starts
    collection rollouts on it), name ``fake-yam-iv`` by default
  * each rollout runs the REAL ``FailureDetector`` over a SYNTHETIC progress trace, at
    --time-scale. What happens is scripted per rollout with --script (cycled):
        success          progress rises to 1.0: the policy succeeds, no intervention
        no_progress      progress stalls at 0.4 -> detector no_progress fires
        timeout          progress creeps up but never finishes -> detector timeout fires
        success_check    the grasp completes but the success check says the gripper is empty
        slow             a long (wall clock) successful rollout, for an operator "Failing" flag
  * on a detection: appends ``intervention.failure_detected`` (fake: true), holds, waits
    ``verdict_wait_s`` for the orchestrator: ``pause_for_takeover`` -> heartbeat ``paused``, wait for
    the routed task to be claimed (takeover) and for ``handback`` (or ``resume_policy`` end_trial),
    then "the policy finishes" and a SYNTHETIC recovery_takeover episode (the TDM-28 ``takeover``
    clip, with task_id + the claimant as contributor) is written. ``resume_policy`` end_trial or no
    verdict: the trial ends as a failure.
  * an unsolicited ``pause_for_takeover`` during a rollout (operator flag) pauses it the same way.
  * every event / result says ``fake: true``; eval trials it records start with ``[FAKE]`` and an
    intervened eval trial is a FAILURE (``[FAKE] [intervened] ...``).

    uv run --project services/robot-client python tests/integration/tdm69_fake_intervention_robot.py \
        --host http://127.0.0.1:3069 --db tdm69iv --token-file <robot_client.token> --media-dir <dir> \
        --script no_progress,timeout,success,slow,success_check
"""
from __future__ import annotations

import json
import queue
import signal
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services" / "robot-client" / "src"))

from rdf_robot_client import fake as base  # noqa: E402
from rdf_robot_client.interventions import CAPABILITY, FAILURE_EVENT, Failure, FailureDetector  # noqa: E402

TAG = base.FAKE_TAG
SCRIPT_STEPS = ("success", "no_progress", "timeout", "success_check", "slow")


class InterventionFakeRobot(base.FakeRobot):
    def __init__(self, cfg: base.FakeConfig, script: list[str], slow_wall_s: float = 8.0) -> None:
        super().__init__(cfg)
        self.script = script or ["success"]
        self.rollouts = 0
        self.slow_wall_s = slow_wall_s
        self.verdicts: queue.Queue = queue.Queue()

    # ---- boot: same as the stock fake, plus the capability
    def boot(self) -> None:
        schema = self.http.schema()
        self.has_create_episode = "create_episode" in schema.reducers
        if self.cfg.episodes:
            base._fixtures()
        cams = {"fake": True, "tag": TAG, "cameras": {c: f"fake://{c}" for c in base.FAKE_CAMERAS},
                "capabilities": [CAPABILITY]}
        self.call("register_robot", self.cfg.name, "fake_yam", "fake-tdm28+tdm69", json.dumps(cams))
        rows = self.http.sql(f"SELECT * FROM robot WHERE name = '{self.cfg.name}'")
        self.robot_id = int(rows[0]["id"])
        self.heartbeat("online")
        base._log("registered (TDM-69 interventions)", robot_id=self.robot_id, script=self.script)
        self.event("robot.fake_client_started", name=self.cfg.name, capabilities=[CAPABILITY], script=self.script)

    # ---- control commands land here (control thread) and are completed right after
    def _verdict(self, row: dict, payload: dict) -> dict:
        self.verdicts.put((row["type"], payload, int(row["id"]), time.time()))
        return {"queued_for_job": True}

    _do_pause_for_takeover = _verdict
    _do_handback = _verdict
    _do_resume_policy = _verdict

    def _take(self, timeout: float, accept) -> tuple | None:
        """Next verdict for which accept(type, payload) is true (others are dropped), or None."""
        end = time.monotonic() + timeout
        while True:
            if self.abort.is_set() or self.stopping.is_set():
                raise base.Aborted("preempted or stopped")
            left = end - time.monotonic()
            if left <= 0:
                return None
            try:
                v = self.verdicts.get(timeout=min(left, 0.05))
            except queue.Empty:
                continue
            if accept(v[0], v[1]):
                return v
            base._log("verdict ignored", type=v[0], payload=v[1])

    # ---- rollouts
    def _do_run_eval(self, row: dict, payload: dict) -> dict:
        ivcfg = payload.get("interventions") or {}
        if not ivcfg.get("enabled"):
            return super()._do_run_eval(row, payload)
        run = self._find_eval_run(row, payload) if int(payload.get("eval_run_id") or 0) else None
        trials = int(payload.get("trials") or (run or {}).get("trials") or 1)
        out = []
        for i in range(trials):
            res = self._iv_trial(row, payload, ivcfg, i)
            out.append(res)
            if run is not None:
                ok = res["outcome"] == "success"
                if not res.get("episode_id"):  # eval rollouts are recorded like the stock fake's (not_reviewed)
                    kind = "baseline" if run.get("kind") == "baseline" else "eval"
                    res["episode_id"], _ = self._make_episode(row, kind, success=ok,
                                                              checkpoint_uri=payload.get("checkpoint_uri"))
                note = f"{TAG} success" if ok else f"{TAG} {'[intervened] ' if res.get('intervened') else ''}{res['note']}"
                self.call("record_eval_trial", int(run["id"]), i, ok, int(res["sim_ms"]), int(res.get("episode_id") or 0), note)
                if i < trials - 1:
                    self._request_reset(row, eval_run_id=run["id"], after_trial=i)
        return {"purpose": payload.get("purpose") or row["type"], "rollouts": out, "fake": True,
                "episode_ids": [r["episode_id"] for r in out if r.get("episode_id")]}

    _do_run_baseline = _do_run_eval

    def _iv_trial(self, row: dict, payload: dict, ivcfg: dict, i: int) -> dict:
        step = self.script[self.rollouts % len(self.script)]
        self.rollouts += 1
        det = FailureDetector.from_payload(ivcfg)
        cid, rid, tid = int(row["id"]), self.robot_id, int(row.get("ticket_id") or 0)
        ref_prefix = f"iv:{rid}:{cid}:"
        dt = 0.1  # simulated seconds per tick
        t, prog, failure = 0.0, 0.0, None
        scale = 1.0 if step == "slow" else max(self._scale, 1e-6)  # slow: simulated time = wall time
        finish_at = {"success": 12.0, "slow": self.slow_wall_s, "success_check": 9.0}.get(step)
        det.start(0.0)
        base._log("rollout", command_id=cid, trial=i, script_step=step)
        while True:
            if self.abort.is_set() or self.stopping.is_set():
                raise base.Aborted("preempted or stopped")
            # unsolicited pause (operator "Failing" flag) while the policy runs
            try:
                v = self.verdicts.get_nowait()
                if v[0] == "pause_for_takeover" and str(v[1].get("ref", "")).startswith(ref_prefix):
                    return self._paused(row, payload, i, v, t, Failure("operator", v[1].get("reason") or "flagged", t))
                base._log("verdict ignored", type=v[0], payload=v[1])
            except queue.Empty:
                pass
            t += dt
            if step == "success" or step == "slow":
                prog = min(1.0, t / finish_at)
            elif step == "no_progress":
                prog = min(0.4, t / 10.0)
            elif step == "timeout":
                prog = min(0.95, 0.008 * t)  # always improving, never done -> the timeout fires
            elif step == "success_check":
                prog = min(1.0, t / finish_at)
            failure = det.tick(t, prog)
            if failure is None and step == "success_check" and t >= finish_at:
                failure = det.success_check(False, f"{TAG} gripper empty after the grasp (synthetic)", t)
            if failure is not None:
                break
            if step in ("success", "slow") and prog >= 1.0:
                return {"trial": i, "outcome": "success", "note": "policy succeeded", "sim_ms": int(t * 1000),
                        "intervened": False}
            time.sleep(dt / scale)
        # ---- failure detected: report, hold, wait for the verdict
        p = failure.payload(rid, tid, cid, i, int(payload.get("eval_run_id") or 0), fake=True, script_step=step)
        self.call("append_event", f"fake-robot:{rid}", FAILURE_EVENT, json.dumps(p))
        base._log("failure detected; holding for a verdict", **p)
        v = self._take(det.verdict_wait_s, lambda ty, pl: str(pl.get("ref", "")).startswith(ref_prefix)
                       and (ty == "pause_for_takeover" or (ty == "resume_policy" and pl.get("end_trial"))))
        if v is None:
            return self._fail(i, t, failure, "no verdict within verdict_wait_s")
        if v[0] == "resume_policy":
            return self._fail(i, t, failure, f"orchestrator: {v[1].get('reason')}")
        return self._paused(row, payload, i, v, t, failure)

    def _fail(self, i: int, t: float, failure: Failure, why: str) -> dict:
        base._log("trial ended as failure", trial=i, detector=failure.detector, why=why)
        self.heartbeat("busy")
        return {"trial": i, "outcome": "failure", "note": f"{failure.detector}: {failure.reason} ({why})",
                "sim_ms": int(t * 1000), "intervened": False, "detector": failure.detector}

    def _paused(self, row: dict, payload: dict, i: int, pause: tuple, t: float, failure: Failure) -> dict:
        pp = pause[1]
        ref, task_id = pp.get("ref"), int(pp.get("task_id") or 0)
        self.heartbeat("paused")
        paused_at = time.time()
        self.event("robot.fake_paused_for_takeover", ref=ref, task_id=task_id, detector=failure.detector,
                   ticket_id=row.get("ticket_id"), command_id=row["id"])
        # takeover starts when the routed task is claimed (the claimant drives; recorder.mark_takeover)
        takeover_at, contributor = None, 0
        while True:
            v = self._take(0.2, lambda ty, pl: (ty == "handback") or
                           (ty == "resume_policy" and pl.get("end_trial") and pl.get("ref") == ref))
            if takeover_at is None:
                rows = self.http.sql(f"SELECT * FROM task WHERE id = {task_id}") if task_id else []
                if rows and rows[0]["status"] in ("claimed", "completed"):
                    takeover_at, contributor = time.time(), int(rows[0].get("assignee_id") or 0)
                    self.event("robot.fake_takeover_started", ref=ref, task_id=task_id, contributor_id=contributor)
            if v is None:
                continue
            if v[0] == "resume_policy":
                self.heartbeat("busy")
                return self._fail(i, t, failure, f"unclaimed: {v[1].get('reason')}")
            break
        handback_at = time.time()
        self.heartbeat("busy")
        if takeover_at is None:
            rows = self.http.sql(f"SELECT * FROM task WHERE id = {task_id}") if task_id else []
            contributor = int((rows[0] if rows else {}).get("assignee_id") or 0)
            takeover_at = handback_at
        end = time.monotonic() + 2.0  # the policy finishes the recovered trial (wall clock, so the claimant can tap done)
        while time.monotonic() < end:
            if self.abort.is_set() or self.stopping.is_set():
                raise base.Aborted("preempted or stopped")
            time.sleep(0.05)
        ep, ms = self._make_episode(row, "recovery_takeover", success=True, checkpoint_uri=payload.get("checkpoint_uri"),
                                    task_id=task_id, contributor_id=contributor)
        self.event("robot.fake_recovery_recorded", ref=ref, task_id=task_id, episode_id=ep, contributor_id=contributor,
                   wall={"paused_at": paused_at, "takeover_at": takeover_at, "handback_at": handback_at},
                   note=f"{TAG} episode media/actions are the synthetic TDM-28 'takeover' clip; the wall times "
                        "above are the real orchestration moments")
        return {"trial": i, "outcome": "recovered", "note": f"{failure.detector}: {failure.reason}",
                "sim_ms": int(t * 1000) + ms, "episode_id": ep, "intervened": True, "ref": ref, "task_id": task_id,
                "detector": failure.detector}


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    script, slow = ["success"], 8.0
    for flag in ("--script", "--slow-wall-s"):
        if flag in argv:
            k = argv.index(flag)
            val = argv[k + 1]
            del argv[k:k + 2]
            if flag == "--script":
                script = [s.strip() for s in val.split(",") if s.strip()]
                bad = [s for s in script if s not in SCRIPT_STEPS]
                if bad:
                    raise SystemExit(f"unknown --script steps {bad}; use {SCRIPT_STEPS}")
            else:
                slow = float(val)
    if "--name" not in argv:
        argv += ["--name", "fake-yam-iv"]
    cfg = base.parse_args(argv)
    robot = InterventionFakeRobot(cfg, script, slow)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    robot.start()
    done.wait()
    robot.stop()


if __name__ == "__main__":
    main()
