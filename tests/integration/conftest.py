"""pytest fixtures for the TDM-28 integration harness (run with ``make test-loop``)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import LocalStack  # noqa: E402

FAKE_ROBOT_ARGS = ("--time-scale", "400", "--seed", "28", "--estop-clear-seconds", "1",
                   "--heartbeat-seconds", "2", "--success-prob", "0.3", "--candidate-success-prob", "0.95",
                   # TDM-70: a ticket's first fine-tune fails its eval, the second passes -> two iterations
                   "--candidate-success-schedule", "0,1")


@pytest.fixture(scope="session")
def stack():
    if not shutil.which("spacetime"):
        pytest.skip("spacetime CLI not installed")
    st = LocalStack()
    try:
        st.boot(fake_robot_args=FAKE_ROBOT_ARGS)
        yield st
    finally:
        st.stop()  # data dir + logs stay under $TMPDIR/tdm-28/run-<port> for post-mortem; wiped on next boot


@pytest.fixture(scope="session")
def robot_id(stack) -> int:
    return int(stack.robot()["id"])


class EventLogWatch:
    """Subscribes to event_log and records any update/delete (it is append-only)."""

    def __init__(self, st: LocalStack) -> None:
        from rdf_common.spacetime_client import Subscription

        self.inserts = 0
        self.violations: list[str] = []
        self.sub = Subscription(
            ["SELECT * FROM event_log"],
            on_insert=lambda t, r, m: self._ins(),
            on_update=lambda t, o, n, m: self.violations.append(f"update id={o.get('id')}"),
            on_delete=lambda t, r, m: self.violations.append(f"delete id={r.get('id')}"),
            host=st.host, db=st.db, token=st.token("orchestrator"),
        ).start_thread()
        assert self.sub.wait_ready(15), "event_log watch did not subscribe"

    def _ins(self) -> None:
        self.inserts += 1

    def stop(self) -> None:
        self.sub.stop()


@pytest.fixture(scope="session")
def event_watch(stack):
    w = EventLogWatch(stack)
    yield w
    w.stop()


def new_ticket(st: LocalStack, robot_id: int, desc: str, overrides: str = "") -> int:
    c = st.client()
    c.call_reducer("create_ticket", robot_id, f"[FAKE] {desc}", 0.8, 5000, overrides)
    return int(st.newest("ticket")["id"])


def assert_event_log_unbroken(st: LocalStack, watch: EventLogWatch) -> list[dict]:
    ev = st.events()
    ids = [e["id"] for e in ev]
    assert ids == list(range(ids[0], ids[0] + len(ids))), f"event_log ids have gaps: {ids}"
    ts = [e["ts"] for e in ev]
    assert ts == sorted(ts), "event_log ts not monotonic with id"
    assert not watch.violations, f"event_log rows changed: {watch.violations}"
    return ev
