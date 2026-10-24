"""TDM-17: seed a LOCAL SpacetimeDB with demo rows. Run it as `make seed`.

    make seed                                         # docker dev-local container, 127.0.0.1:3300
    make seed SEED_HOST=http://127.0.0.1:3028 SEED_DB=tdm28loop   # e.g. a `make fake-stack` DB
    make seed ARGS="--dry-run"                        # print the plan, write nothing

What it creates (each step only if the row is missing, so a second run is a no-op):
  * robot        test.seed-yam-office-1  (embodiment yam_7dof; camera_config_json has wrist + exo
                 cams and {"fake": true, "seed": true, "tag": "[SEED]"})
  * contributor  test.seed-contributor   (kind contributor, left offline; registered as a freshly minted
                 identity because register_contributor refuses service identities)
  * ticket       on that robot: "test.seed [SEED] <task>" with the module's default run_settings
                 (create_ticket inserts run_settings + dataset with it)
  * event_log    one `test.seed.applied` row ({"fake": true, "seed": true, ...}) when anything new
                 was written

It never writes eval runs, trials, episodes, reviews, data plans or payments: nothing seeded can
be read as a real result. For a full fake loop (fake robot, trials tagged [FAKE]) use the TDM-28
harness instead: `make fake-stack` / `make test-loop`.

Labels, so the dashboard and /events mark every seeded row as test data:
  * names / task_description start with `test.` -> apps/web/src/lib/eventlog/classify.ts flags the
    robot.registered, contributor.registered and ticket.created rows, and useEventLog then marks
    every later row of that ticket "test ticket";
  * camera_config_json {"fake": true} -> isFakeRobot() puts the FAKE badge on the ticket header;
  * the robot name starts with `test.`, so tickets_api's robot auto-selection never picks it.

Safety: the target must be a local server (127.0.0.1 / localhost / ::1 / the compose service
`spacetimedb`). Maincloud is always refused unless --allow-maincloud AND --confirm-db <db> are
both given; any other remote host needs --allow-remote. SPACETIME_HOST / SPACETIME_TOKEN from
.env are ignored on purpose (they point at maincloud); the host comes from --host/SEED_HOST.
Writes go through `spacetime call` as the CLI login, which is the owner of a locally published
module. No token is printed or stored.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from typing import Any
from urllib.parse import urlparse

from rdf_common.spacetime_client import SpacetimeClient

DEFAULT_HOST = "http://127.0.0.1:3300"  # make dev-local / make spacetime-publish
DEFAULT_DB = "robo-factory-a706j"
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "spacetimedb", "host.docker.internal"}

SEED_TAG = "[SEED]"
ROBOT_NAME = "test.seed-yam-office-1"
ROBOT_EMBODIMENT = "yam_7dof"
ROBOT_ADAPTER = "seed-0"
ROBOT_CAMERAS = {
    "fake": True,
    "seed": True,
    "tag": SEED_TAG,
    "cameras": {"wrist": "seed://wrist", "exo": "seed://exo"},
}
CONTRIBUTOR_NAME = "test.seed-contributor"
CONTRIBUTOR_DEVICES = ["phone", "laptop"]
# The easy task is still open (docs/morning-questions.md #4, TDM-82); override with --task.
DEFAULT_TASK = "Pick up the cup and put it in the bin"
TARGET_SUCCESS = 0.8
BUDGET_CENTS = 5000
ACTOR = "test.seed"
REQUIRED_REDUCERS = ("register_robot", "create_ticket", "register_contributor", "append_event")


def log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[seed] REFUSED: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def q(s: str) -> str:
    """SQL string literal."""
    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------- guard


def check_target(host: str, db: str, *, allow_maincloud: bool, confirm_db: str | None, allow_remote: bool) -> None:
    u = urlparse(host)
    if u.scheme not in ("http", "https") or not u.hostname:
        die(f"--host must be an http(s) URL, got {host!r}")
    name = u.hostname.lower()
    if name == "maincloud" or name.endswith("spacetimedb.com"):
        if not allow_maincloud:
            die(f"{host} is maincloud. Its event_log is the evidence for the real run and is append-only, "
                "so seed rows there can never be removed. Seed a local server instead "
                "(make dev-local, or make fake-stack). Override: --allow-maincloud --confirm-db <db>.")
        if confirm_db != db:
            die(f"--allow-maincloud also needs --confirm-db {db}")
        log(f"WARNING: seeding maincloud {db}: these rows are permanent")
        return
    if name not in LOCAL_HOSTNAMES and not allow_remote:
        die(f"{host} is not a local server ({', '.join(sorted(LOCAL_HOSTNAMES))}). Pass --allow-remote if you mean it.")


def ping(host: str) -> None:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/v1/ping", timeout=3) as r:
            if r.status == 200:
                return
    except Exception as e:  # noqa: BLE001
        die(f"no SpacetimeDB at {host} ({e}). Start one: make dev-local, or make fake-stack.")
    die(f"no SpacetimeDB at {host}")


# ---------------------------------------------------------------------------- writes


class Seeder:
    def __init__(self, host: str, db: str, dry_run: bool) -> None:
        self.host, self.db, self.dry_run = host.rstrip("/"), db, dry_run
        self.read = SpacetimeClient(self.host, db, "")  # anonymous: every seeded table is public
        self.created: list[str] = []

    def close(self) -> None:
        self.read.close()

    def check_module(self) -> None:
        try:
            reducers = set(self.read.schema().reducers)
        except Exception as e:  # noqa: BLE001
            die(f"database {self.db} on {self.host} is not reachable or not published ({e}). "
                "Publish it first: make spacetime-publish")
        missing = [r for r in REQUIRED_REDUCERS if r not in reducers]
        if missing:
            die(f"database {self.db} lacks reducers {missing}; republish spacetime/ (make spacetime-publish)")

    def owner_call(self, reducer: str, *args: Any) -> None:
        """Call a reducer as the spacetime CLI login (the owner of a locally published module)."""
        if self.dry_run:
            log(f"  would call {reducer}")
            return
        r = subprocess.run(["spacetime", "call", "--no-config", "-s", self.host, self.db, reducer,
                            *[json.dumps(a) for a in args]], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            err = r.stderr.strip()[-600:]
            hint = (" (the CLI login is not a service identity on this DB: publish it with this CLI, "
                    "e.g. make spacetime-publish)") if "not a service identity" in err or "unauthorized" in err else ""
            raise SystemExit(f"[seed] {reducer} failed{hint}: {err}")
        log(f"  {reducer}")

    def one(self, table: str, where: str) -> dict | None:
        rows = self.read.sql(f"SELECT * FROM {table} WHERE {where}")
        return min(rows, key=lambda r: r["id"]) if rows else None

    def robot(self) -> int:
        row = self.one("robot", f"name = {q(ROBOT_NAME)}")
        if row:
            log(f"robot {ROBOT_NAME} exists (id {row['id']})")
            return int(row["id"])
        log(f"robot {ROBOT_NAME}: creating")
        self.owner_call("register_robot", ROBOT_NAME, ROBOT_EMBODIMENT, ROBOT_ADAPTER, json.dumps(ROBOT_CAMERAS))
        self.created.append("robot")
        row = self.one("robot", f"name = {q(ROBOT_NAME)}")
        return int(row["id"]) if row else 0

    def contributor(self) -> int:
        row = self.one("contributor", f"name = {q(CONTRIBUTOR_NAME)}")
        if row:
            log(f"contributor {CONTRIBUTOR_NAME} exists (id {row['id']})")
            return int(row["id"])
        log(f"contributor {CONTRIBUTOR_NAME}: creating (fresh identity)")
        if self.dry_run:
            log("  would mint an identity and call register_contributor")
            return 0
        req = urllib.request.Request(f"{self.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = json.loads(resp.read())["token"]
        with SpacetimeClient(self.host, self.db, token) as c:
            # name, email ("" = none), devices, kind, source_ticket_id (0 = none)
            c.call_reducer("register_contributor", CONTRIBUTOR_NAME, "", CONTRIBUTOR_DEVICES, "contributor", 0)
            # Nobody holds this identity (its token is not kept), so don't leave it looking online.
            c.call_reducer("contributor_offline")
        log("  register_contributor, contributor_offline")
        self.created.append("contributor")
        row = self.one("contributor", f"name = {q(CONTRIBUTOR_NAME)}")
        return int(row["id"]) if row else 0

    def ticket(self, robot_id: int, task: str) -> int:
        desc = f"test.seed {SEED_TAG} {task}"
        if robot_id:
            row = self.one("ticket", f"robot_id = {robot_id}")
            if row:
                log(f"ticket on {ROBOT_NAME} exists (id {row['id']}): {row['task_description']}")
                return int(row["id"])
        log(f"ticket: creating {desc!r}")
        # settings_overrides_json "" -> the module's DEFAULT_RUN_SETTINGS (success_threshold = target)
        self.owner_call("create_ticket", robot_id, desc, TARGET_SUCCESS, BUDGET_CENTS, "")
        self.created.append("ticket")
        row = self.one("ticket", f"robot_id = {robot_id}") if robot_id else None
        return int(row["id"]) if row else 0

    def mark(self, robot_id: int, contributor_id: int, ticket_id: int) -> None:
        if not self.created:
            log("nothing new: already seeded (no-op)")
            return
        self.owner_call("append_event", ACTOR, "test.seed.applied", json.dumps({
            "fake": True, "seed": True, "tag": SEED_TAG, "tool": "scripts/seed.py (TDM-17)",
            "note": "seed data for local dev; not a real run and not an eval result",
            "created": self.created, "robot_id": robot_id, "robot_name": ROBOT_NAME,
            "contributor_id": contributor_id, "contributor_name": CONTRIBUTOR_NAME, "ticket_id": ticket_id,
        }))


def main() -> None:
    p = argparse.ArgumentParser(description="Seed a LOCAL SpacetimeDB with a demo robot, contributor and ticket (TDM-17)")
    p.add_argument("--host", default=os.environ.get("SEED_HOST") or DEFAULT_HOST,
                   help=f"SpacetimeDB URL (default {DEFAULT_HOST}; env SEED_HOST). SPACETIME_HOST is ignored.")
    p.add_argument("--db", default=os.environ.get("SEED_DB") or DEFAULT_DB, help=f"database name (default {DEFAULT_DB}; env SEED_DB)")
    p.add_argument("--task", default=DEFAULT_TASK, help="ticket task description (prefixed 'test.seed [SEED] ')")
    p.add_argument("--dry-run", action="store_true", help="print what would be written, write nothing")
    p.add_argument("--allow-remote", action="store_true", help="allow a non-local host that is not maincloud")
    p.add_argument("--allow-maincloud", action="store_true", help="DON'T. Rows on maincloud are permanent.")
    p.add_argument("--confirm-db", help="with --allow-maincloud: repeat the database name")
    a = p.parse_args()

    check_target(a.host, a.db, allow_maincloud=a.allow_maincloud, confirm_db=a.confirm_db, allow_remote=a.allow_remote)
    ping(a.host)
    log(f"target {a.host} db {a.db}" + (" (dry run)" if a.dry_run else ""))
    s = Seeder(a.host, a.db, a.dry_run)
    try:
        s.check_module()
        robot_id = s.robot()
        contributor_id = s.contributor()
        ticket_id = s.ticket(robot_id, a.task)
        s.mark(robot_id, contributor_id, ticket_id)
    finally:
        s.close()
    log(f"done: robot {robot_id}, contributor {contributor_id}, ticket {ticket_id}")
    if ticket_id:
        log(f"dashboard: NEXT_PUBLIC_SPACETIME_HOST={a.host} NEXT_PUBLIC_SPACETIME_DB_NAME={a.db} "
            f"pnpm --filter @rdf/web dev, then /tickets/{ticket_id}")


if __name__ == "__main__":
    main()
