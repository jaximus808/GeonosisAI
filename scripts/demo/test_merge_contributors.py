"""merge_contributors.py: candidates by shared number / name, the plan, dry run by default, --yes merges the text
store and calls merge_contributor when the module has it. No network.

    cd services/orchestrator && uv run --quiet --with 'pytest>=8' pytest -q ../../scripts/demo/test_merge_contributors.py
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_contributors as M  # noqa: E402
from rdf_orchestrator.text_offers import CONSENT_TEXT, TextStore  # noqa: E402

FULL = "+14155557008"


def at(h: int) -> dt.datetime:
    return dt.datetime(2026, 10, 24, h, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, reducers: tuple[str, ...] = ("merge_contributor",)) -> None:
        self.reducers = set(reducers)
        self.tables = {
            "contributor": [
                {"id": 1, "name": "Jaxon Poentis", "kind": "contributor", "identity": "0x" + "a" * 64, "total_paid_cents": 0},
                {"id": 2, "name": "balls", "kind": "contributor", "identity": "0x" + "b" * 64, "total_paid_cents": 0},
                {"id": 3, "name": "[Sample] Demo contributor", "kind": "contributor", "identity": None, "total_paid_cents": 8400},
                {"id": 4, "name": "Jaxon Poentis", "kind": "contributor", "identity": "0x" + "d" * 64, "total_paid_cents": 0},
            ],
            "task": [{"id": 50, "assignee_id": 2, "status": "completed"}],
            "episode": [{"id": 70, "contributor_id": 3}],
            "payment": [],
            "contributor_presence": [{"contributor_id": 1, "last_seen": at(1)}, {"contributor_id": 4, "last_seen": at(9)}],
        }
        self.calls: list[tuple] = []

    def sql(self, q: str) -> list[dict]:
        return [dict(r) for r in self.tables[q.split("FROM ")[1].split()[0]]]

    def schema(self):
        return SimpleNamespace(reducers={r: [] for r in self.reducers})

    def call_reducer(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        f, into, take = args
        rows = {r["id"]: r for r in self.tables["contributor"]}
        rows[f].update(kind="merged", name=f"[merged into #{into}] {rows[f]['name']}")
        if take:
            rows[into]["identity"], rows[f]["identity"] = rows[f]["identity"], None
        for t in self.tables["task"]:
            if t["assignee_id"] == f:
                t["assignee_id"] = into

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass


def store(tmp_path: Path) -> Path:
    p = tmp_path / "texts.sqlite3"
    s = TextStore(p)
    s.upsert_optin(FULL, 4, False, CONSENT_TEXT, time.time())
    s.add_offer([39], FULL, 2, "recovery_takeover", 7, time.time())
    s.remember_device("d" * 64, 4, "tok-d", time.time())
    return p


def run(tmp_path, *extra, client=None):
    c = client or FakeClient()
    rc = M.main(["--target", "local", "--text-store", str(tmp_path / "texts.sqlite3"), *extra], client=c)
    return rc, c


def test_listing_shows_candidates_masked_and_writes_nothing(tmp_path, capsys):
    store(tmp_path)
    rc, c = run(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0 and c.calls == []
    assert "same job-text number ***7008: #2, #4" in out and "same name 'jaxon poentis': #1, #4" in out
    assert FULL not in out and "5557008" not in out
    assert "[Sample]" not in out.split("CANDIDATE GROUPS")[1]  # sample rows are never candidates


def test_dry_run_plan_for_1_and_4(tmp_path, capsys):
    p = store(tmp_path)
    rc, c = run(tmp_path, "--merge", "1,4")
    out = capsys.readouterr().out
    assert rc == 0 and c.calls == []
    assert "survivor #1" in out  # tie on history: the lowest id
    assert "merge_contributor(4, 1, take_identity=true)" in out  # 4's browser was seen more recently
    assert "0 tasks" in out and "opt-in ***7008 moves to #1" in out
    assert TextStore(p).optin(FULL).contributor_id == 4  # nothing written
    assert TextStore(p).canonical(4) == 4


def test_yes_merges_store_and_calls_the_reducer(tmp_path, capsys):
    p = store(tmp_path)
    rc, c = run(tmp_path, "--merge", "1,4", "--keep", "1", "--yes")
    assert rc == 0
    assert c.calls == [("merge_contributor", 4, 1, True)]
    s = TextStore(p)
    assert s.optin(FULL).contributor_id == 1 and s.canonical(4) == 1
    assert s.devices_for(1) == [("d" * 64, "tok-d")]  # the identity moved, so its token now is #1's
    # contributor 2 shares the number in the offer history but was not named: untouched
    assert s.canonical(2) == 2 and all(r[0] == 2 for r in s._q("SELECT contributor_id FROM offer"))
    rc, c2 = run(tmp_path, "--merge", "1,4", "--keep", "1", "--yes", client=c)  # rerun: idempotent
    assert rc == 0 and c.calls == [("merge_contributor", 4, 1, True)]


def test_identity_keep_forgets_the_merged_devices(tmp_path):
    p = store(tmp_path)
    rc, c = run(tmp_path, "--merge", "1,4", "--identity", "keep", "--yes")
    assert c.calls == [("merge_contributor", 4, 1, False)]
    assert TextStore(p).devices_for(1) == [] and TextStore(p).devices_for(4) == []


def test_unpublished_reducer_merges_the_store_only(tmp_path, capsys):
    p = store(tmp_path)
    rc, c = run(tmp_path, "--merge", "1,4", "--yes", client=FakeClient(reducers=()))
    out = capsys.readouterr().out
    assert rc == 0 and c.calls == []
    assert "spacetime publish -s maincloud robo-factory-a706j --delete-data=never --yes=remote,migrate" in out
    assert TextStore(p).canonical(4) == 1


def test_more_history_wins_and_guards(tmp_path):
    store(tmp_path)
    c = FakeClient()
    c.tables["task"] += [{"id": 51, "assignee_id": 4, "status": "claimed"}]
    hist = M.history(M.snapshot(c), M.read_store(tmp_path / "texts.sqlite3"))
    assert M.pick_survivor([1, 4], hist, None)[0] == 4
    assert run(tmp_path, "--merge", "4")[0] == 2
    assert run(tmp_path, "--merge", "1,4", "--keep", "2")[0] == 2
    assert run(tmp_path, "--merge", "1,9")[0] == 2
