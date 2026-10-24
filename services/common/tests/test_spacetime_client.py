"""Offline tests for rdf_common.spacetime_client (TDM-16).

The fixture schema and the wire messages below were captured from a real SpacetimeDB 2.13
server (a throwaway `probe` module with option / timestamp / identity / array columns), so the
codec is tested against both row shapes the server really sends.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rdf_common.spacetime_client import Schema, SpacetimeConfig, SpacetimeError, Subscription
from rdf_common.spacetime_codegen import render

SCHEMA = Schema(json.loads((Path(__file__).parent / "fixtures" / "spacetime_probe_schema.json").read_text()))
ID_A = "0xc2007095442e47104a80f3efaacb77b7d62cbee6050a6e5d201c076509a800e0"
ID_B = "0xc200ccd8aee6bc9eb1c047cff490f9bce599b1e153edf5d9d2090bae37e89734"

# InitialSubscription rows are JSON objects; transaction rows are positional arrays.
OBJ_ROW = ('{"id":1,"name":"a2","opt_s":[1,{}],"opt_ts":[0,{"__timestamp_micros_since_unix_epoch__":1792824944544416}],'
           '"ident":[1,{}],"who":{"__identity__":"%s"},"arr":[1,2],"f":1.5,"flag":true,'
           '"ts":{"__timestamp_micros_since_unix_epoch__":1792825154101654}}' % ID_B)
POS_ROW = '[1,"a",[0,"x"],[0,[1792824944544416]],[1,[]],["%s"],[1,2],1.5,true,[1792825154101654]]' % ID_B
POS_ROW_RENAMED = '[1,"a2",[1,[]],[0,[1792824944544416]],[0,["%s"]],["%s"],[1,2],1.5,true,[1792825154101654]]' % (ID_A, ID_B)


def test_decode_both_row_shapes():
    obj = SCHEMA.decode_row("probe", OBJ_ROW)
    pos = SCHEMA.decode_row("probe", POS_ROW)
    assert obj == {"id": 1, "name": "a2", "opt_s": None, "opt_ts": 1792824944544416, "ident": None, "who": ID_B,
                   "arr": [1, 2], "f": 1.5, "flag": True, "ts": 1792825154101654}
    assert pos["opt_s"] == "x" and pos["opt_ts"] == 1792824944544416 and pos["ident"] is None
    assert pos["who"] == ID_B and pos["ts"] == 1792825154101654
    assert SCHEMA.decode_row("probe", POS_ROW_RENAMED)["ident"] == ID_A


def test_encode_reducer_args():
    args = SCHEMA.encode_args("probe_insert", ("c", None, 1792824944544416, ID_A, [3], 2.5), {})
    assert args == ["c", {"none": []}, {"some": {"__timestamp_micros_since_unix_epoch__": 1792824944544416}},
                    {"some": {"__identity__": ID_A}}, [3], 2.5]
    # by name; options may be omitted; wire-form values pass through untouched
    kw = SCHEMA.encode_args("probe_insert", (), {"name": "c", "arr": [], "f": 1.0, "opt_s": {"some": "y"}})
    assert kw[0] == "c" and kw[1] == {"some": "y"} and kw[2] == {"none": []}
    with pytest.raises(TypeError):
        SCHEMA.encode_args("probe_rename", (1,), {})
    with pytest.raises(TypeError):
        SCHEMA.encode_args("probe_rename", (), {"id": 1, "nme": "x"})
    with pytest.raises(SpacetimeError):
        SCHEMA.encode_args("nope", (), {})


def _sub(**cb) -> tuple[Subscription, list]:
    events: list = []
    sub = Subscription(
        ["SELECT * FROM probe"], config=SpacetimeConfig("http://x", "db", None),
        on_insert=lambda t, r, m: events.append(("ins", r["id"], r["name"])),
        on_delete=lambda t, r, m: events.append(("del", r["id"], r["name"])),
        on_applied=lambda snap: events.append(("applied", len(snap.get("probe", [])))),
        on_error=lambda e: events.append(("err", str(e))),
        **cb,
    )
    sub.schema = SCHEMA

    class _Ev:  # stand-in for the asyncio.Event run() creates
        def set(self): pass
        def clear(self): pass

    sub._connected = _Ev()
    return sub, events


def _tx(deletes, inserts, light=True):
    upd = {"tables": [{"table_name": "probe", "updates": [{"deletes": deletes, "inserts": inserts}]}]}
    if light:
        return {"TransactionUpdateLight": {"request_id": 0, "update": upd}}
    return {"TransactionUpdate": {"status": {"Committed": upd}, "timestamp": {"__timestamp_micros_since_unix_epoch__": 5},
                                  "caller_identity": {"__identity__": ID_A},
                                  "reducer_call": {"reducer_name": "probe_rename", "request_id": 7}}}


def test_subscription_update_pairing_and_cache():
    updates: list = []
    sub, events = _sub(on_update=lambda t, o, n, m: updates.append((o["name"], n["name"], m)))
    sub._dispatch({"InitialSubscription": {"database_update": {"tables": [
        {"table_name": "probe", "updates": [{"deletes": [], "inserts": [OBJ_ROW]}]}]}}})
    assert events == [("applied", 1)] and sub.ready and sub.get("probe", 1)["name"] == "a2"
    sub._dispatch(_tx([OBJ_ROW], [POS_ROW], light=False))
    assert updates[0][:2] == ("a2", "a")
    assert updates[0][2] == {"reducer": "probe_rename", "request_id": 7, "caller": ID_A, "timestamp": 5}
    assert sub.get("probe", 1)["name"] == "a"
    sub._dispatch(_tx([POS_ROW], []))
    assert events[-1] == ("del", 1, "a") and sub.rows("probe") == []


def test_subscription_without_on_update_emits_delete_then_insert():
    sub, events = _sub()
    sub._dispatch(_tx([], [POS_ROW]))
    sub._dispatch(_tx([POS_ROW], [POS_ROW_RENAMED]))
    assert events == [("ins", 1, "a"), ("del", 1, "a"), ("ins", 1, "a2")]


def test_failed_reducer_goes_to_on_error():
    sub, events = _sub()
    sub._dispatch({"TransactionUpdate": {"status": {"Failed": "boom"}, "reducer_call": {"reducer_name": "probe_rename"}}})
    assert events and events[0][0] == "err" and "probe_rename" in events[0][1]


def test_typed_rows_from_generated_module():
    ns: dict = {}
    exec(compile(render(SCHEMA, "fixture"), "spacetime_tables_probe.py", "exec"), ns)
    Probe = ns["ROW_TYPES"]["probe"]
    row = Probe.from_row(SCHEMA.decode_row("probe", POS_ROW))
    assert row.name == "a" and row.opt_s == "x" and row.PRIMARY_KEY == ("id",)
    assert Probe.from_row(row.to_row()) == row

    calls = []

    class Fake:
        def call_reducer(self, name, *args):
            calls.append((name, args))

    ns["Reducers"](Fake()).probe_rename(id=3, name="z")
    assert calls == [("probe_rename", (3, "z"))]


def test_config_anonymous_and_ws_url(monkeypatch):
    monkeypatch.setenv("SPACETIME_TOKEN", "secret")
    monkeypatch.setenv("SPACETIME_DB_NAME", "db1")
    cfg = SpacetimeConfig.resolve(host="https://h.example/", token="")
    assert cfg.token is None and cfg.headers() == {}
    assert cfg.ws_url == "wss://h.example/v1/database/db1/subscribe"
    assert SpacetimeConfig.resolve().headers() == {"Authorization": "Bearer secret"}
