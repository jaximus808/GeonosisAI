import json

from rdf_orchestrator.flywheel import TicketView


def _view(robot):
    v = TicketView.__new__(TicketView)
    v.robot = robot
    return v


def test_adapter_version_enough_when_droid_off(monkeypatch):
    monkeypatch.delenv("RDF_ONBOARDING_DROID", raising=False)
    assert _view({"adapter_version": "yam@x"}).onboarded


def test_droid_on_requires_passing_onboarding_block(monkeypatch):
    monkeypatch.setenv("RDF_ONBOARDING_DROID", "1")
    assert not _view({"adapter_version": "yam@x", "camera_config_json": "{}"}).onboarded
    ok = json.dumps({"onboarding": {"status": "passed_sim"}})
    assert _view({"adapter_version": "yam@x+onb.t1", "camera_config_json": ok}).onboarded
    hil = json.dumps({"onboarding": {"status": "passed_hil"}})
    assert _view({"adapter_version": "yam@x", "camera_config_json": hil}).onboarded
    bad = json.dumps({"onboarding": {"status": "failed"}})
    assert not _view({"adapter_version": "yam@x", "camera_config_json": bad}).onboarded


def test_no_robot_not_onboarded(monkeypatch):
    monkeypatch.setenv("RDF_ONBOARDING_DROID", "1")
    assert not _view(None).onboarded
