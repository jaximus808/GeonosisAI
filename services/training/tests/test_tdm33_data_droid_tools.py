"""TDM-33: the Data Droid's tools on labelled fixture episodes (no SpacetimeDB, no droid).

Run: cd services/training && uv run pytest tests/test_tdm33_data_droid_tools.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdf_training import convert_fixtures as fx
from rdf_training import data_droid_tools as dt


class FakeStdb:
    def __init__(self, ticket, dataset, settings, episodes):
        self.ticket, self.dataset, self.settings, self.episodes = ticket, dataset, settings, episodes
        self.calls = []

    def sql(self, q):
        if q.startswith("SELECT * FROM ticket"):
            return [self.ticket]
        if q.startswith("SELECT * FROM dataset"):
            return [self.dataset]
        if q.startswith("SELECT * FROM run_settings"):
            return [self.settings]
        if q.startswith("SELECT * FROM episode"):
            return list(self.episodes)
        raise AssertionError(q)

    def call_reducer(self, name, *args):
        self.calls.append((name, args))


def _row(eid, entry, status="accepted"):
    return {"id": eid, "ticket_id": 7, "kind": entry["kind"], "review_status": status,
            "actions_uri": "file://" + entry["actions_uri"], "cameras_json": json.dumps(entry["cameras"]),
            "takeover_ts": entry.get("takeover_ts"), "handback_ts": entry.get("handback_ts"), "frame_count": 0}


@pytest.fixture(scope="module")
def stdb(tmp_path_factory):
    d = tmp_path_factory.mktemp("eps")
    w = fx.write_fixture_episode
    rows = [
        _row(11, w(d, episode_id="e11", kind="clean_teleop", n_frames=40)),
        _row(12, w(d, episode_id="e12", kind="clean_teleop", n_frames=45)),
        _row(13, w(d, episode_id="e13", kind="recovery_takeover", n_frames=60, takeover_frame=20, handback_frame=50)),
        _row(14, w(d, episode_id="e14", kind="clean_teleop", n_frames=35)),
        _row(16, w(d, episode_id="e16", kind="clean_teleop", n_frames=30), status="rejected"),
    ]
    noto = _row(15, w(d, episode_id="e15", kind="recovery_takeover", n_frames=50, takeover_frame=10,
                      handback_frame=50, with_joint_cmd=False))
    noto["takeover_ts"] = None  # converter falls back to the first human tick
    rows.insert(4, noto)
    return FakeStdb({"id": 7, "status": "review", "iteration": 0, "task_description": fx.FIXTURE_TASK, "robot_id": 1},
                    {"id": 3, "ticket_id": 7, "accepted_count": 5, "total_count": 6, "lerobot_uri": None},
                    {"ticket_id": 7, "target_episodes": 5}, rows)


def test_inspect_validate_assemble_verify_publish(stdb, tmp_path):
    doc = dt.inspect_ticket(7, client=stdb)
    assert doc["candidate_ids"] == [11, 12, 13, 14, 15] and doc["ready"]
    assert [e["episode_id"] for e in doc["excluded"]] == [16]

    v = dt.validate(doc)
    assert v["ok_ids"] == [11, 12, 13, 14, 15] and not v["flagged_ids"]
    assert any("first human tick" in w for w in v["warnings_by_id"]["15"])
    assert all("fixture" in " ".join(v["warnings_by_id"][str(i)]) for i in (11, 12, 13, 14, 15))
    by = {r["episode_id"]: r for r in v["episodes"]}
    assert by[13]["stats"]["cut_before_takeover"] == 20 and by[13]["stats"]["kept_frames"] == 30

    with pytest.raises(Exception, match="fixture"):
        dt.assemble(doc, out_root=tmp_path / "ds0")
    with pytest.raises(dt.ToolError, match="not accepted"):
        dt.assemble(doc, out_root=tmp_path / "ds0", exclude={"16": "x"}, allow_fixtures=True)

    out = dt.assemble(doc, out_root=tmp_path / "ds", allow_fixtures=True)
    assert out["episode_ids"] == [11, 12, 13, 14, 15]
    man = json.loads(Path(out["manifest_path"]).read_text())
    assert man["fixture"] and man["norm_stats"]["state"]["mean"] and man["excluded"][0]["episode_id"] == 16

    rep = dt.verify(out["dataset_dir"], ep_doc=doc, loader=False)
    assert rep["ok"], rep["errors"]
    assert {c["episode_id"]: c["no_frames_before_takeover"] for c in rep["takeover_cuts"]} == {"13": True, "15": True}
    assert rep["total_frames"] == 40 + 45 + 30 + 35 + 40

    res = dt.publish(7, out["manifest_path"], client=stdb)
    name, args = stdb.calls[-1]
    assert name == "set_dataset_lerobot_uri" and args[0] == 7 and json.loads(args[2]) == [11, 12, 13, 14, 15]
    assert res["lerobot_uri"].startswith("file://")


def test_validate_flags_short_video_and_exclusion(stdb, tmp_path):
    doc = dt.inspect_ticket(7, client=stdb)
    bad = dict(doc["episodes"][0])
    cams = json.loads(bad["cameras_json"])
    short = fx.write_fixture_episode(tmp_path, episode_id="short", kind="clean_teleop", n_frames=10)
    cams["wrist"] = short["cameras"]["wrist"]  # 10-frame wrist video for a 40-tick episode
    bad["cameras_json"] = json.dumps(cams)
    r = dt.validate_episode(bad)
    assert not r["ok"] and any("wrist video has 10 frames" in e for e in r["errors"])

    doc2 = {**doc, "episodes": [bad, *doc["episodes"][1:]]}
    out = dt.assemble(doc2, out_root=tmp_path / "ds", allow_fixtures=True,
                      exclude=dt._parse_excludes(["11:wrist video shorter than actions"]))
    assert out["episode_ids"] == [12, 13, 14, 15]
    assert {"episode_id": 11, "reason": "wrist video shorter than actions"} in out["excluded"]


def test_publish_refuses_fixture_on_maincloud(stdb, tmp_path, monkeypatch):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"ticket_id": 7, "fixture": True, "lerobot_uri": "file:///x", "episode_ids": [1],
                             "repo_id": "r", "total_frames": 1, "fps": 30, "episodes": []}))
    monkeypatch.setenv("SPACETIME_HOST", "https://maincloud.spacetimedb.com")
    with pytest.raises(dt.ToolError, match="maincloud"):
        dt.publish(7, p, client=stdb)
