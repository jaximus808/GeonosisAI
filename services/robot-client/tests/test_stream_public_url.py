"""make demo-public: RDF_STREAM_PUBLIC_URL makes the stream announce an https URL served by someone else
(the demo-public edge at https://<app>/_cam, or a named tunnel) and bind loopback only."""
from __future__ import annotations

import pytest

from rdf_robot_client.cameras import CameraRig
from rdf_robot_client.stream import start_stream

SECRET = "public-url-test-secret-0123456789abcdef"


@pytest.fixture()
def rig():
    r = CameraRig.from_config(synthetic=True).start()
    assert r.wait_ready(5)
    yield r
    r.stop()


def test_external_public_url_is_announced(rig, monkeypatch):
    monkeypatch.setenv("RDF_STREAM_PUBLIC_URL", "https://app.example.com/_cam/")
    h = start_stream(rig, robot_id=1, tunnel="none", port=0, secret=SECRET)
    try:
        info = h.info()["stream"]
        assert info["http"] == "https://app.example.com/_cam"
        assert info["url"] == "wss://app.example.com/_cam/ws/cameras"
        assert info["tunnel"] == "external"
        assert h.server.cfg.host == "127.0.0.1"  # nothing on the LAN
    finally:
        h.stop()


def test_without_it_lan_mode_is_unchanged(rig, monkeypatch):
    monkeypatch.delenv("RDF_STREAM_PUBLIC_URL", raising=False)
    h = start_stream(rig, robot_id=1, tunnel="none", port=0, secret=SECRET, lan_host="192.168.1.5")
    try:
        info = h.info()["stream"]
        assert info["url"].startswith("ws://192.168.1.5:")
        assert info["tunnel"] == "none"
    finally:
        h.stop()


def test_bad_public_url_is_refused(rig, monkeypatch):
    monkeypatch.setenv("RDF_STREAM_PUBLIC_URL", "app.example.com")
    with pytest.raises(ValueError):
        start_stream(rig, robot_id=1, tunnel="none", port=0, secret=SECRET)
