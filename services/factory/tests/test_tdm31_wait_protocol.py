"""TDM-31 droid side of the wait-for protocol: timeout, ticket scoping, the line helper, the contract text."""
import json

import pytest

from rdf_factory import parse_waiting_for, waiting_for_line
from rdf_factory.client import CONTRACT
from rdf_factory.events import parse_timeout, scope_to_ticket


@pytest.mark.parametrize("raw,secs", [("x:y>=1 timeout=30m", 1800), ("x:y>=1 timeout 2h", 7200),
                                      ("x:y>=1 timeout=90", 90), ("x:y>=1 TIMEOUT=1.5d", 129600), ("x:y>=1", None)])
def test_parse_timeout(raw, secs):
    body, t = parse_timeout(raw)
    assert body == "x:y>=1" and t == secs


def test_text_form_with_timeout_and_scope():
    p = parse_waiting_for("Parked.WAITING_FOR dataset:accepted_count>=5 timeout=30m", ticket_id=4)
    assert p == {"table": "dataset", "field": "accepted_count", "op": ">=", "value": 5, "timeout_s": 1800.0,
                 "ticket_id": 4, "raw": "dataset:accepted_count>=5 timeout=30m"}


def test_ticket_table_is_keyed_by_id_and_unscoped_tables_are_left_alone():
    assert parse_waiting_for("WAITING_FOR ticket:status==train", ticket_id=4)["id"] == 4
    assert "ticket_id" not in parse_waiting_for("WAITING_FOR ticket:status==train", ticket_id=4)
    for t in ("qa_result", "eval_trial", "robot", "event_log"):
        p = parse_waiting_for(f"WAITING_FOR {t}:x==1", ticket_id=4)
        assert "ticket_id" not in p and "id" not in p


def test_json_groups_are_scoped_per_leaf_and_explicit_filters_win():
    line = ('WAITING_FOR {"all":[{"table":"dataset","field":"accepted_count","op":">=","value":1},'
            '{"any":[{"table":"ticket","field":"status","value":"train"},'
            '{"table":"training_run","where":{"ticket_id":9},"field":"status","value":"done"}]}],"timeout_s":60}')
    p = parse_waiting_for(line, ticket_id=4)
    assert p["timeout_s"] == 60 and p["raw"].startswith('{"all"')
    ds, grp = p["all"]
    assert ds["ticket_id"] == 4 and grp["any"][0]["id"] == 4
    assert "ticket_id" not in grp["any"][1] and grp["any"][1]["where"] == {"ticket_id": 9}
    assert scope_to_ticket({"table": "dataset"}, 0) == {"table": "dataset"}


def test_waiting_for_line_round_trips():
    leaf = {"table": "dataset", "field": "accepted_count", "op": ">=", "value": 1}
    assert waiting_for_line(leaf) == "WAITING_FOR dataset:accepted_count>=1"
    assert waiting_for_line(leaf, timeout_s=600) == "WAITING_FOR dataset:accepted_count>=1 timeout=600s"
    assert parse_waiting_for(waiting_for_line(leaf, timeout_s=600), ticket_id=2) == {
        **leaf, "timeout_s": 600.0, "ticket_id": 2, "raw": "dataset:accepted_count>=1 timeout=600s"}
    agg = {"table": "episode", "agg": "count", "where": {"review_status": "accepted"}, "op": ">=", "value": 3}
    line = waiting_for_line(agg, timeout_s=3600)
    assert line.startswith('WAITING_FOR {"table":"episode","agg":"count"')
    back = parse_waiting_for("ok\n" + line, ticket_id=2)
    assert back["timeout_s"] == 3600 and back["ticket_id"] == 2 and back["agg"] == "count"
    assert json.loads(line.removeprefix("WAITING_FOR "))["where"] == {"review_status": "accepted"}
    assert waiting_for_line("dataset:accepted_count>=2") == "WAITING_FOR dataset:accepted_count>=2"


def test_contract_documents_json_and_timeout():
    text = CONTRACT.format(role="data_droid", agent_session_id=1, ticket="", workdir="/w", mkdir="", refused="")
    assert '"all":[...]' in text and '"agg":"count"' in text and "timeout" in text and "TIMED OUT" in text
    example = next(l.strip() for l in text.splitlines() if l.strip().startswith('WAITING_FOR {'))
    assert parse_waiting_for(example)["timeout_s"] == 3600  # the example in the contract parses
