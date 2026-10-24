"""Unit test for the media-store FastAPI app, no Modal needed (TDM-4)."""

import time

import pytest
from fastapi.testclient import TestClient

import rdf_media_store as m

KEY = "tickets/t1/episodes/e1/cam0.mp4"
SECRET = "unit-test-signing-key"


class FakeVolume:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def reload(self):
        pass


@pytest.fixture
def client(tmp_path):
    vol = FakeVolume()
    api = m.build_api(SECRET, str(tmp_path), vol)
    c = TestClient(api)
    c.vol = vol  # type: ignore[attr-defined]
    return c


def sig(method, key, exp):
    return m._signature(SECRET, method, key, exp)


def test_put_get_range_and_list(client):
    exp = int(time.time()) + 600
    body = bytes(range(256)) * 100
    r = client.put(f"/o/{KEY}", params={"exp": exp, "sig": sig("PUT", KEY, exp)}, content=body)
    assert r.status_code == 200 and r.json()["size"] == len(body)
    assert client.vol.commits == 1

    r = client.get(f"/o/{KEY}", params={"exp": exp, "sig": sig("GET", KEY, exp)})
    assert r.status_code == 200 and r.content == body
    assert r.headers["content-type"] == "video/mp4" and r.headers["accept-ranges"] == "bytes"

    r = client.get(f"/o/{KEY}", params={"exp": exp, "sig": sig("GET", KEY, exp)}, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == body[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(body)}"

    r = client.head(f"/o/{KEY}", params={"exp": exp, "sig": sig("GET", KEY, exp)})
    assert r.status_code == 200 and r.headers["content-length"] == str(len(body))

    r = client.get("/list", params={"prefix": "tickets/t1"}, headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200 and [o["key"] for o in r.json()["objects"]] == [KEY]


def test_rejects_bad_sig_expired_traversal_and_unauth_list(client):
    exp = int(time.time()) + 600
    assert client.get(f"/o/{KEY}", params={"exp": exp, "sig": "nope"}).status_code == 403
    old = exp - 1200
    assert client.get(f"/o/{KEY}", params={"exp": old, "sig": sig("GET", KEY, old)}).status_code == 403
    # a PUT signature must not work for GET
    assert client.get(f"/o/{KEY}", params={"exp": exp, "sig": sig("PUT", KEY, exp)}).status_code == 403
    bad = "tickets/../../etc/passwd"
    assert client.put(f"/o/{bad}", params={"exp": exp, "sig": sig("PUT", bad, exp)}, content=b"x").status_code in (400, 404)
    assert client.get("/list", params={"prefix": "tickets"}).status_code == 401
