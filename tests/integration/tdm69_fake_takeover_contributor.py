"""TDM-69 fake takeover contributor for the TDM-28 harness (LOCAL ONLY, everything [FAKE]).

Plays the person on the phone for DAgger interventions. It watches ``intervention.started`` events
(the orchestrator's routing decision), and for each one it takes: ``claim_task`` on the routed
recovery_takeover task as itself (the TDM-10 atomic claim, 60 s arm-control lease), "drives" for
``drive_s`` (heartbeating the lease; a real claimant streams teleop through TDM-52/56/57), sends
``handback`` on the robot as the claimant, waits for it to be done, then ``complete_task``.

``skip_first`` interventions are ignored on purpose, to prove the claim-timeout fallback
(the orchestrator ends the trial, resets, and goes on). Built on the TDM-26 FakeContributor
(identity + contributor row, name ``[FAKE] contributor ...``).
"""
from __future__ import annotations

import json
import time

from tdm26_fake_contributor import FakeContributor, _log


class TakeoverContributor(FakeContributor):
    def __init__(self, host: str, db: str, *, skip_first: int = 0, drive_s: float = 2.0, **kw) -> None:
        kw.setdefault("name", "[FAKE] contributor tdm69 takeover")
        kw.setdefault("types", ("recovery_takeover",))
        super().__init__(host, db, **kw)
        self.skip_first, self.drive_s = skip_first, drive_s
        self.seen: dict[str, str] = {}  # ref -> taken | skipped
        self.after_event = 0

    def _next_task(self) -> dict | None:
        rows = self.c.sql(f"SELECT * FROM event_log WHERE id > {self.after_event}")
        for e in sorted(rows, key=lambda r: int(r["id"])):
            self.after_event = max(self.after_event, int(e["id"]))
            if e["type"] != "intervention.started":
                continue
            p = json.loads(e["payload_json"] or "{}")
            ref = p.get("ref")
            if not ref or ref in self.seen:
                continue
            if sum(1 for v in self.seen.values() if v == "skipped") < self.skip_first:
                self.seen[ref] = "skipped"
                _log("ignoring this takeover on purpose (claim-timeout proof)", ref=ref, task_id=p.get("task_id"))
                continue
            self.seen[ref] = "taken"
            task = self.c.sql(f"SELECT * FROM task WHERE id = {int(p['task_id'])}")
            if task and task[0]["status"] == "open":
                return {**task[0], "_ref": ref}
        return None

    def work(self, task: dict) -> dict | None:
        try:
            return super().work(task)
        except Exception as e:  # noqa: BLE001 - the orchestrator may complete it for us after handback
            row = self.c.sql(f"SELECT * FROM task WHERE id = {int(task['id'])}")
            if row and row[0]["status"] == "completed":
                _log("task already completed by the orchestrator after handback", task_id=task["id"])
                self.done.append({"task_id": int(task["id"]), "completed_by": "orchestrator"})
                return None
            raise e

    def _teleop(self, task: dict) -> dict:
        tid, rid = int(task["id"]), int(task["robot_id"])
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.drive_s:  # "driving": a real phone streams clutch deltas here
            time.sleep(min(0.5, self.drive_s))
            self.c.call_reducer("heartbeat_claim", tid)
        before = max((int(c["id"]) for c in self.c.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")), default=0)
        self.c.call_reducer("issue_robot_command", rid, "handback",
                            json.dumps({"task_id": tid, "ref": task.get("_ref"), "fake": True}), int(task["ticket_id"]))
        deadline = time.monotonic() + self.job_timeout_s
        while time.monotonic() < deadline:
            cmds = [c for c in self.c.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
                    if c["type"] == "handback" and int(c["id"]) > before]
            if cmds and cmds[0]["status"] in ("done", "failed", "preempted"):
                return {"handback_command_id": int(cmds[0]["id"]), "handback_status": cmds[0]["status"],
                        "drive_s": self.drive_s, "ref": task.get("_ref")}
            time.sleep(self.poll_s)
        raise RuntimeError(f"handback for task {tid} did not finish")
