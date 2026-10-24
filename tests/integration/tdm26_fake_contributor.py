"""TDM-26 fake contributor for the TDM-28 harness: claims the dispatcher's paid tasks and does them.

LOCAL ONLY (refuses maincloud and non-local hosts). Everything it writes says it is fake:
contributor name ``[FAKE] contributor ...``, devices ``["fake"]``, every result_json / command
payload carries ``"fake": true``.

    from tdm26_fake_contributor import FakeContributor
    fc = FakeContributor(st.host, st.db).start()      # background thread, polls open tasks
    ...
    fc.stop(); fc.done / fc.errors

    # or standalone against `make fake-stack`:
    cd services/orchestrator && uv run python ../../tests/integration/tdm26_fake_contributor.py \
        --host http://127.0.0.1:3028 --db tdm28loop

Per open task (oldest first, one at a time, like one person at the arm):
  * clean_teleop / recovery_takeover: ``claim_task`` as itself -> ``start_teleop`` on the task's
    robot as the claimant ({task_id, kind, contributor_id}; the TDM-28 fake robot records one fake
    episode linked to the task) -> waits for the command to finish, heartbeating the 60 s lease ->
    ``complete_task``. The episode then waits for the reviewer like a real one; this helper never
    reviews anything.
  * reset: ``claim_task`` -> ``reset_delay_s`` -> ``complete_task`` (the dispatcher then pays it and
    sends ``resume_policy``).
A robot busy with another job (``start_teleop`` refused) is retried until ``job_timeout_s``.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from urllib.parse import urlparse

FAKE_TAG = "[FAKE]"
ARM_TYPES = ("clean_teleop", "recovery_takeover")


def _is_local(host: str) -> bool:
    h = (urlparse(host).hostname or "").lower()
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _log(msg: str, **fields) -> None:
    print(json.dumps({"ts": time.strftime("%H:%M:%S"), "svc": "fake-contributor", "msg": msg, **fields},
                     default=str), file=sys.stderr, flush=True)


class FakeContributor:
    def __init__(self, host: str, db: str, *, name: str = f"{FAKE_TAG} contributor tdm26",
                 types: tuple[str, ...] = ("clean_teleop", "recovery_takeover", "reset"),
                 ticket_id: int | None = None, poll_s: float = 0.3, reset_delay_s: float = 0.2,
                 job_timeout_s: float = 60.0) -> None:
        if "maincloud" in host.lower() or not _is_local(host):
            raise ValueError(f"fake contributor is local only; refusing {host}")
        from rdf_common.spacetime_client import SpacetimeClient, create_identity

        self.host, self.db, self.name, self.types, self.ticket_id = host, db, name, types, ticket_id
        self.poll_s, self.reset_delay_s, self.job_timeout_s = poll_s, reset_delay_s, job_timeout_s
        _identity, token = create_identity(host)
        self.c = SpacetimeClient(host, db, token)  # acts as the contributor itself, never as a service
        self.c.call_reducer("register_contributor", name, "", ["fake"], "contributor", ticket_id or 0)
        rows = [r for r in self.c.sql("SELECT * FROM contributor") if r["name"] == name]
        self.contributor_id = int(max(rows, key=lambda r: int(r["id"]))["id"])
        self.done: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        _log("registered", contributor_id=self.contributor_id, name=name)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeContributor":
        self._t = threading.Thread(target=self._loop, name="fake-contributor", daemon=True)
        self._t.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._t:
            self._t.join(timeout)
        self.c.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._next_task()
                if task is None:
                    self._stop.wait(self.poll_s)
                    continue
                self.work(task)
            except Exception as e:  # noqa: BLE001 - keep serving; the test reads .errors
                self.errors.append(f"{type(e).__name__}: {e}")
                _log("error", error=str(e)[:300])
                self._stop.wait(self.poll_s)

    def _next_task(self) -> dict | None:
        rows = [t for t in self.c.sql("SELECT * FROM task WHERE status = 'open'") if t["type"] in self.types
                and (self.ticket_id is None or int(t["ticket_id"]) == self.ticket_id)]
        return min(rows, key=lambda t: int(t["id"])) if rows else None

    # ------------------------------------------------------------------ one task
    def work(self, task: dict) -> dict | None:
        tid = int(task["id"])
        try:
            self.c.call_reducer("claim_task", tid, self.contributor_id)
        except Exception as e:  # noqa: BLE001 - someone else won the claim / it was cancelled
            _log("claim_lost", task_id=tid, error=str(e)[:200])
            return None
        _log("claimed", task_id=tid, type=task["type"])
        result: dict = {"fake": True, "tag": FAKE_TAG, "contributor_id": self.contributor_id}
        if task["type"] in ARM_TYPES:
            result.update(self._teleop(task))
        else:
            time.sleep(self.reset_delay_s)
            result["reset"] = "fake: scene declared reset"
        self.c.call_reducer("complete_task", tid, json.dumps(result))
        out = {"task_id": tid, "type": task["type"], **result}
        self.done.append(out)
        _log("completed", **out)
        return out

    def _teleop(self, task: dict) -> dict:
        tid, rid = int(task["id"]), int(task["robot_id"])
        payload = json.dumps({"task_id": tid, "kind": task["type"], "contributor_id": self.contributor_id,
                              "fake": True})
        deadline = time.monotonic() + self.job_timeout_s
        last_hb = time.monotonic()
        while True:  # the robot may still be busy with another job (eval, reset_request, ...)
            try:
                self.c.call_reducer("issue_robot_command", rid, "start_teleop", payload, int(task["ticket_id"]))
                break
            except Exception as e:  # noqa: BLE001
                if "busy" not in str(e) or time.monotonic() > deadline:
                    raise
                if time.monotonic() - last_hb > 20:
                    self.c.call_reducer("heartbeat_claim", tid)
                    last_hb = time.monotonic()
                time.sleep(self.poll_s)
        cmd = None
        while time.monotonic() < deadline:
            cmds = [c for c in self.c.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
                    if c["type"] == "start_teleop" and json.loads(c["payload_json"] or "{}").get("task_id") == tid]
            cmd = max(cmds, key=lambda c: int(c["id"])) if cmds else None
            if cmd and cmd["status"] in ("done", "failed", "preempted"):
                break
            if time.monotonic() - last_hb > 20:
                self.c.call_reducer("heartbeat_claim", tid)
                last_hb = time.monotonic()
            time.sleep(self.poll_s)
        if not cmd or cmd["status"] != "done":
            raise RuntimeError(f"start_teleop for task {tid} did not finish: {cmd and cmd['status']}")
        res = json.loads(cmd.get("result_json") or "{}")
        return {"robot_command_id": int(cmd["id"]), "episode_id": res.get("episode_id")}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="http://127.0.0.1:3028")
    p.add_argument("--db", default="tdm28loop")
    p.add_argument("--ticket", type=int, default=None, help="only this ticket's tasks")
    a = p.parse_args()
    fc = FakeContributor(a.host, a.db, ticket_id=a.ticket).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fc.stop()


if __name__ == "__main__":
    main()
