"""release_robot_hold.py: who holds a robot (numbers masked), dry run by default, --yes releases. No network.

    cd services/orchestrator && uv run --quiet --with 'pytest>=8' pytest -q ../../scripts/demo/test_release_robot_hold.py
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_robot_hold as R  # noqa: E402

FULL = "+14155550177"


class FakeClient:
    def __init__(self) -> None:
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=45)
        self.tables = {
            "robot": [{"id": 3, "name": "sim-yam-mujoco", "status": "online"},
                      {"id": 4, "name": "yam-01", "status": "online"}],
            "task": [{"id": 12, "robot_id": 4, "ticket_id": 7, "type": "recovery_takeover", "status": "claimed",
                      "assignee_id": 4, "lease_expires_at": soon},
                     {"id": 13, "robot_id": 4, "ticket_id": 7, "type": "clean_teleop", "status": "open", "assignee_id": 0},
                     {"id": 14, "robot_id": 3, "ticket_id": 8, "type": "clean_teleop", "status": "claimed",
                      "assignee_id": 2, "lease_expires_at": soon}],
            "ticket": [{"id": 7, "status": "collecting", "task_description": "Pick up the cube"}],
            "contributor": [{"id": 4, "name": "Jaxon Poentis"}, {"id": 2, "name": "balls"}],
            "robot_command": [{"id": 90, "robot_id": 4, "type": "start_teleop", "status": "acked", "ticket_id": 7}],
        }
        self.calls: list[tuple] = []

    def sql(self, q: str) -> list[dict]:
        return [dict(r) for r in self.tables[q.split("FROM ")[1].split()[0]]]

    def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name == "release_task":
            for t in self.tables["task"]:
                if t["id"] == args[0]:
                    t.update(status="open", assignee_id=0, lease_expires_at=None)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass


def store(tmp_path: Path) -> Path:
    p = tmp_path / "texts.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE optin (phone TEXT PRIMARY KEY, contributor_id INTEGER NOT NULL)")
    db.execute("INSERT INTO optin VALUES (?, 4)", (FULL,))
    db.commit()
    db.close()
    return p


def test_dry_run_lists_the_holder_masked_and_writes_nothing(tmp_path, capsys):
    c = FakeClient()
    rc = R.main(["--target", "local", "--robot", "yam-01", "--text-store", str(store(tmp_path))], client=c)
    out = capsys.readouterr().out
    assert rc == 0 and c.calls == []
    assert "Jaxon Poentis" in out and "***0177" in out and FULL not in out and "5550177" not in out
    claimed = out.split("CLAIMED TASKS")[1].split("OPEN ROBOT JOBS")[0]
    assert "would call release_task(12)" in out and "balls" not in claimed  # robot 3's claim is not listed
    assert "start_teleop" in out  # open robot jobs are listed


def test_yes_releases_only_that_robots_claims(tmp_path, capsys):
    c = FakeClient()
    rc = R.main(["--target", "local", "--robot", "4", "--yes", "--text-store", str(store(tmp_path))], client=c)
    assert rc == 0
    assert c.calls == [("release_task", 12)]  # robot 3's task 14 is untouched; robot jobs are not changed
    assert "nobody" not in capsys.readouterr().out


def test_cancel_and_task_filter(tmp_path):
    c = FakeClient()
    R.main(["--target", "local", "--robot", "yam-01", "--yes", "--cancel", "--task", "12",
            "--text-store", str(tmp_path / "missing.sqlite3")], client=c)
    assert c.calls == [("cancel_task", 12, R.NOTE)]


def test_unknown_robot(capsys):
    assert R.main(["--target", "local", "--robot", "nope"], client=FakeClient()) == 2


def test_holds_helper_marks_lapsed_leases():
    snap = {"tasks": [{"id": 1, "robot_id": 4, "status": "claimed", "assignee_id": 9, "lease_expires_at": None}],
            "tickets": [], "contributors": []}
    assert R.holds(snap, 4, {}, 0)[0]["lease"] == "lapsed"
