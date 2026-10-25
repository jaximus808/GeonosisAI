"""TDM-114: the sim robot unlatches its soft e-stop on an orchestrator's robot.estop_clear_requested row."""
from __future__ import annotations

import json

from rdf_robot_client.tdm114_estop_watch import Tdm114EstopWatch


class _Tdm114Client:
    robot_id = 3

    def __init__(self, locked=True):
        self.locked, self.services, self.calls = locked, {}, []

    def clear_estop(self, by=""):
        self.calls.append(by)
        was, self.locked = self.locked, False
        return was


def _row(robot_id=3, actor="orchestrator:arbitration", id_=9):
    return {"id": id_, "type": "robot.estop_clear_requested", "actor": actor,
            "payload_json": json.dumps({"robot_id": robot_id, "by": "op"})}


def test_clears_own_robot_from_orchestrator():
    c = _Tdm114Client()
    w = Tdm114EstopWatch(c)
    assert w.handle(_row()) is True and c.calls and "web operator op" in c.calls[0] and w.cleared == [9]


def test_ignores_other_robot_and_other_actor():
    c = _Tdm114Client()
    w = Tdm114EstopWatch(c)
    assert w.handle(_row(robot_id=4)) is False
    assert w.handle(_row(actor="web")) is False
    assert c.calls == []
