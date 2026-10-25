"""Orchestrator discovery for the robot client (orch_endpoint.py; TDM-110 hybrid deploy). No network.

    cd services/robot-client && uv run pytest -q tests/test_orch_endpoint.py
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from rdf_robot_client import orch_endpoint as oe
from rdf_robot_client.recorder import EpisodeUploader, PermanentUploadError

SECRET = "s3cret-edge-" + "x" * 20
URL_A = "https://alpha-one.trycloudflare.com"
URL_B = "https://bravo-two.trycloudflare.com"
# pinned: scripts/demo/test_tdm110_hybrid.py checks public_guard.edge_proof gives the same hex
VECTOR = ("k" * 32, "n" * 20, "https://api-xyz.trycloudflare.com",
          "2dbf5c54a1d3763d6cc15030d313eae86cd600da09865682b3d9e32f2a584deb")


def row(i: int, url: str | None, actor: str = "orchestrator", **extra) -> dict:
    p = {"url": url, "robot_url": None, "tunnel": "quick", **extra}
    if url is None:
        p["state"] = "down"
    return {"id": i, "ts": 0, "actor": actor, "type": oe.EVENT_TYPE, "payload_json": json.dumps(p)}


class FakeDB:
    """event_log over 'SQL': honours the id > cursor the resolver sends."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = list(rows or [])
        self.queries: list[str] = []

    def sql(self, q: str) -> list[dict]:
        self.queries.append(q)
        after = int(q.split("id > ")[1]) if "id > " in q else 0
        return [r for r in self.rows if r["type"] == oe.EVENT_TYPE and r["id"] > after]


class FakeEdges:
    """GET <url>/_rdf/proof for a set of live edges, each holding a secret (or a wrong one)."""

    def __init__(self, live: dict[str, str]):
        self.live = live
        self.calls: list[str] = []

    def get(self, url: str, timeout: float):
        self.calls.append(url)
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.live:
            raise ConnectionError("tunnel gone")
        nonce = parse_qs(parts.query)["nonce"][0]
        return 200, {"v": 1, "url": base, "proof": oe.edge_proof(self.live[base], nonce, base)}


def resolver(db: FakeDB, edges: FakeEdges, **kw) -> oe.OrchestratorEndpoint:
    return oe.OrchestratorEndpoint(secret=SECRET, sql=db.sql, http_get=edges.get, **kw)


def test_proof_matches_the_edge_vector():
    secret, nonce, url, want = VECTOR
    assert oe.edge_proof(secret, nonce, url) == want
    assert oe.edge_proof(secret, nonce, url + "/") == want, "a trailing slash is not part of the bound URL"


def test_pick_endpoint_newest_orchestrator_row_only():
    rows = [row(5, URL_A), row(9, URL_B, actor="robot_client:1"), row(7, "https://evil.example.com/path"),
            {"id": 11, "actor": "orchestrator", "type": "other.event", "payload_json": "{}"}]
    p = oe.pick_endpoint(rows)
    assert p.id == 7 and p.url is None, "a URL with a path is never accepted (newest row still wins)"
    assert oe.pick_endpoint([row(5, URL_A), row(3, URL_B)]).url == URL_A
    assert oe.pick_endpoint([row(4, "http://10.0.0.5:8000")]).url is None, "plain http only on loopback"
    assert oe.pick_endpoint([row(4, "http://127.0.0.1:3767")]).url == "http://127.0.0.1:3767"
    assert oe.pick_endpoint([row(4, None)]).url is None
    assert oe.pick_endpoint([]) is None


def test_fake_row_proof_pass(capsys):
    db, edges = FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET})
    ep = resolver(db, edges)
    assert ep.base() is None and ep.headers() == {}
    assert ep.refresh() == URL_A
    assert ep.base() == URL_A and ep.headers() == {oe.SECRET_HEADER: SECRET}
    assert ep.describe()["event_id"] == 3
    err = capsys.readouterr().err
    assert "orchestrator endpoint discovered" in err and URL_A in err
    assert SECRET not in err


def test_proof_fail_never_gets_the_secret(capsys):
    db = FakeDB([row(3, URL_A)])
    ep = resolver(db, FakeEdges({URL_A: "w" * 32}))  # the URL answers, but with another secret
    assert ep.refresh() is None
    assert ep.base() is None and ep.headers() == {}
    assert "wrong proof" in ep.describe()["error"]
    ep2 = resolver(FakeDB([row(3, URL_A)]), FakeEdges({}))  # nothing answers at the URL
    assert ep2.refresh() is None and "not reachable" in ep2.describe()["error"]
    err = capsys.readouterr().err
    assert "failed the edge proof" in err and SECRET not in err


def test_url_change_between_restarts():
    db, edges = FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET})
    ep = resolver(db, edges)
    assert ep.refresh() == URL_A
    # make demo-hybrid restarts: state=down, then a new quick tunnel URL; the old one dies
    db.rows += [row(4, None, previous_url=URL_A), row(6, URL_B)]
    del edges.live[URL_A]
    edges.live[URL_B] = SECRET
    assert ep.refresh() == URL_B
    assert "id > 3" in db.queries[-1], "after the first read only newer rows are fetched"
    assert ep.headers() == {oe.SECRET_HEADER: SECRET}
    # nothing new: no new proof, same URL
    n = len(edges.calls)
    assert ep.refresh() == URL_B and len(edges.calls) == n
    # a stop with no restart: uploads wait instead of hitting a dead URL
    db.rows.append(row(8, None))
    assert ep.refresh() is None and ep.headers() == {}
    assert "offline" in ep.describe()["error"]


def test_new_url_that_is_not_up_yet_is_retried_each_poll():
    db, edges = FakeDB([row(3, URL_B)]), FakeEdges({})
    ep = resolver(db, edges)
    assert ep.refresh() is None
    edges.live[URL_B] = SECRET  # the tunnel's DNS caught up
    assert ep.refresh() == URL_B


def test_reproved_after_the_window():
    t = [0.0]
    db, edges = FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET})
    ep = resolver(db, edges, clock=lambda: t[0], reprove_s=300)
    ep.refresh()
    n = len(edges.calls)
    t[0] = 100
    ep.refresh()
    assert len(edges.calls) == n
    t[0] = 400
    del edges.live[URL_A]  # the edge went away without a down row
    assert ep.refresh() is None


def test_sql_blip_keeps_the_last_url():
    db, edges = FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET})
    ep = resolver(db, edges)
    ep.refresh()
    db.sql = lambda q: (_ for _ in ()).throw(OSError("maincloud blip"))  # type: ignore[method-assign]
    ep._sql = db.sql
    assert ep.refresh() == URL_A


def test_explicit_orchestrator_url_overrides_discovery(monkeypatch):
    called = []
    monkeypatch.setattr("rdf_common.spacetime_client.SpacetimeClient", lambda *a, **k: called.append(1))
    ep = oe.OrchestratorEndpoint.from_env(None, {"ORCHESTRATOR_URL": "http://192.168.1.20:8028/",
                                                 "RDF_EDGE_SECRET": SECRET})
    assert not ep.discovering and called == []
    assert ep.base() == "http://192.168.1.20:8028"
    assert ep.headers() == {oe.SECRET_HEADER: SECRET}  # as Vercel does; the edge strips it
    assert ep.refresh() == "http://192.168.1.20:8028"
    legacy = oe.OrchestratorEndpoint.from_env(None, {})
    assert legacy.base() == oe.LEGACY_DEFAULT and legacy.headers() == {}
    short = oe.OrchestratorEndpoint(secret="short", sql=lambda q: [row(1, URL_A)])
    assert short.refresh() is None and "24" in short.describe()["error"]


def test_explicit_api_path_is_redacted_in_logs(capsys):
    oe.OrchestratorEndpoint.fixed("https://app.trycloudflare.com/_api/AbCdEfGhIjKlMnOpQrSt").start()
    err = capsys.readouterr().err
    assert "AbCdEfGhIjKlMnOpQrSt" not in err and "/_api/<secret>" in err


# ------------------------------------------------------------------------------- uploader integration
class FakeHTTP:
    def __init__(self, sign_status: int = 200):
        self.sign_status = sign_status
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, dict(headers or {})))
        if self.sign_status != 200:
            return SimpleNamespace(status_code=self.sign_status, text="edge_secret_required")
        ups = [{"upload_url": f"https://media.example.com/o/{f['filename']}?sig=1", "method": "PUT",
                "headers": {"Content-Type": f["content_type"]}, "storage_uri": f"modal-vol://rdf-media/t/{f['filename']}"}
               for f in json["files"]]
        return SimpleNamespace(status_code=200, text="", json=lambda: {"uploads": ups})

    def request(self, method, url, content=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(headers or {})))
        return SimpleNamespace(status_code=200, text="")


def _episode(tmp_path: Path):
    (tmp_path / "actions.parquet").write_bytes(b"x" * 10)
    return SimpleNamespace(dir=tmp_path, key="ep1", meta={"files": ["actions.parquet"], "ticket_id": 4})


def test_uploader_uses_the_discovered_url_and_keeps_the_secret_off_media(tmp_path):
    db, edges = FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET})
    ep = resolver(db, edges)
    up = EpisodeUploader(ep, stdb=None, root=tmp_path / "root")
    up._http = http = FakeHTTP()
    st: dict = {}
    with pytest.raises(RuntimeError, match="no orchestrator endpoint yet"):
        up._upload_files(_episode(tmp_path), st)  # not discovered yet: a retryable error, nothing sent
    assert http.calls == []
    ep.refresh()
    up._upload_files(_episode(tmp_path), st)
    (m1, u1, h1), (m2, u2, h2) = http.calls
    assert (m1, u1) == ("POST", f"{URL_A}/uploads/sign-batch") and h1[oe.SECRET_HEADER] == SECRET
    assert m2 == "PUT" and u2.startswith("https://media.example.com/") and oe.SECRET_HEADER not in h2
    assert st["uploaded"] == {"actions.parquet": "modal-vol://rdf-media/t/actions.parquet"}
    assert up.base == URL_A


def test_uploader_treats_an_edge_refusal_as_transient_when_discovering(tmp_path):
    ep = resolver(FakeDB([row(3, URL_A)]), FakeEdges({URL_A: SECRET}))
    ep.refresh()
    up = EpisodeUploader(ep, stdb=None, root=tmp_path / "root")
    up._http = FakeHTTP(sign_status=401)
    with pytest.raises(RuntimeError) as e:
        up._upload_files(_episode(tmp_path), {})
    assert not isinstance(e.value, PermanentUploadError), "a rotated tunnel must not fail the episode for good"
    fixed = EpisodeUploader("http://127.0.0.1:9", stdb=None, root=tmp_path / "root2")
    fixed._http = FakeHTTP(sign_status=404)
    with pytest.raises(PermanentUploadError):
        fixed._upload_files(_episode(tmp_path), {})
    assert fixed.base == "http://127.0.0.1:9" and fixed.endpoint.headers() == {}


def test_recorder_for_shares_one_endpoint(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)
    monkeypatch.delenv("RDF_EDGE_SECRET", raising=False)
    client = SimpleNamespace(services={}, stdb=None)
    a = oe.orchestrator_endpoint_for(client)
    assert a is oe.orchestrator_endpoint_for(client) and client.services["orch_endpoint"] is a
    assert a.base() == oe.LEGACY_DEFAULT
