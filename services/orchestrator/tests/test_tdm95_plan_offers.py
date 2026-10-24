"""TDM-95: planner offer suggestion -> offer after approve_spend, dispatcher reservation, timeout
fallback, flywheel wait, report line. In-memory mirror + fake writer (no network)."""
from __future__ import annotations

import json
import time

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import plan_offers as po
from rdf_orchestrator.flywheel import TicketView, decide
from rdf_orchestrator.handlers import plan_offers as h

NOW = time.time()
US = int(NOW * 1e6)
TABLES = (*dsp.TABLES, "event_log", "dataset_offer", "dataset", "review")


class FakeBus:
    """Mirror (rows/get) + writer (call_reducer / call_reducer_threadsafe) over the same tables."""

    def __init__(self):
        self.t: dict[str, dict] = {n: {} for n in TABLES}
        self.calls: list[tuple] = []

    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def put(self, table, **row):
        row.setdefault("id", max(self.t[table], default=0) + 1)
        self.t[table][row["ticket_id"] if table == "run_settings" else row["id"]] = row
        return row

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name == "append_event":
            self.put("event_log", actor=args[0], type=args[1], payload_json=args[2], ts=US)
        elif name == "create_task":
            tid, typ, price, payload = args
            p = json.loads(payload)
            self.put("task", ticket_id=tid, robot_id=1, data_plan_id=p.get("data_plan_id") or 0, type=typ,
                     status="open", instructions="", price_cents=price, assignee_id=0, completed_at=None)
        elif name == "cancel_task":
            self.t["task"][args[0]]["status"] = "cancelled"
        elif name == "cancel_dataset_offer":
            self.t["dataset_offer"][args[0]]["status"] = "cancelled"

    def call_reducer_threadsafe(self, name, args, timeout=10.0):
        self.call_reducer(name, *args)
        return 0

    def events(self, type_):
        return [json.loads(r["payload_json"]) for r in self.rows("event_log") if r["type"] == type_]


def world(*, amount=3, approved=None, hint=True, price=600, plan_status="approved", target=5, accepted=2):
    b = FakeBus()
    dsp._instances.clear()
    dsp.make_writer = lambda: b
    b.put("robot", id=1, status="online", last_heartbeat=1)
    b.put("ticket", id=7, robot_id=1, task_description="put the cup in the bin", budget_cents=10_000, spent_cents=0,
          approved_cents=0, status="dispatch", iteration=0)
    b.put("run_settings", ticket_id=7, target_episodes=target, hyperparams_json="{}")
    b.put("dataset", id=1, ticket_id=7, accepted_count=accepted)
    for i in range(accepted):
        b.put("episode", ticket_id=7, task_id=0, kind="clean_teleop", review_status="accepted", dataset_offer_id=0,
              created_at=US - 10_000_000_000)
    b.put("data_plan", id=3, ticket_id=7, iteration=0, data_type="clean_teleop", amount=amount, unit_price_cents=1000,
          est_cost_cents=1000 * amount, status=plan_status, approved_cents=approved if approved is not None else 1000 * amount,
          approved_at=US - 1_000_000, created_at=US - 5_000_000, failure_cluster_ids=[])
    b.put("contributor", id=9, kind="dataset_seller", name="[FAKE] seller", online=False)
    b.put("event_log", type="planner.data_plan", actor="planner", ts=US, payload_json=json.dumps({
        "data_plan_id": 3, "ticket_id": 7, "evidence": {"dataset_offer": {
            "consider": hint, "max_price_per_episode_cents": price if hint else 0, "max_episodes": target - accepted}}}))
    return b


def add_offer(b, *, needed=3, status="accepted", price=600, created=None, updated=None, accepted_count=0):
    return b.put("dataset_offer", ticket_id=7, seller_id=9, price_per_episode_cents=price, needed_count=needed,
                 ingested_count=0, accepted_count=accepted_count, status=status,
                 created_at=created or US - 1_000_000, updated_at=updated or US - 1_000_000)


def decided(b, offer_id=None):
    b.call_reducer("append_event", po.ACTOR, po.EVENT_CREATED if offer_id else po.EVENT_SKIPPED,
                   json.dumps({"ticket_id": 7, "data_plan_id": 3, "offer_id": offer_id}))


# --------------------------------------------------------------------------- dispatcher reservation

def test_dispatcher_holds_while_the_offer_is_being_decided():
    b = world()
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.state == "awaiting_offer" and not b.rows("task"), st


def test_no_hint_dispatches_as_before():
    b = world(hint=False)
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.state == "collecting" and len(b.rows("task")) == 3 and st.offer_reserved == 0


def test_hold_ends_after_hold_s():
    b = world()
    b.t["data_plan"][3]["approved_at"] = US - int((po.HOLD_S + 5) * 1e6)
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.state == "collecting" and len(b.rows("task")) == 3


def test_open_offer_covering_the_plan_posts_no_paid_tasks():
    b = world()
    o = add_offer(b, needed=3)
    decided(b, o["id"])
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.state == "covered_by_offer" and st.offer_reserved == 3 and st.target == 0 and not b.rows("task")
    assert b.events("dispatch.covered_by_offer")


def test_partial_offer_tops_up_with_paid_tasks_inside_approved_minus_offer_cents():
    b = world(approved=3000)
    o = add_offer(b, needed=2, price=600)
    decided(b, o["id"])
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.offer_reserved == 2 and st.target == 1 and st.payable == (3000 - 1200) // 1000
    assert len(b.rows("task")) == 1


def test_offer_created_after_tasks_cancels_the_excess_open_ones():
    b = world(hint=False)
    d = dsp.get_dispatcher(b)
    d.dispatch_plan(3)
    assert len(b.rows("task")) == 3
    add_offer(b, needed=2)
    st = d.dispatch_plan(3)
    assert len([t for t in b.rows("task") if t["status"] == "open"]) == 1 and len(st.cancelled_task_ids) == 2
    assert b.events("dispatch.offer_reserved")


def test_cancelled_offer_falls_back_to_paid_demos():
    b = world()
    o = add_offer(b, needed=3)
    decided(b, o["id"])
    d = dsp.get_dispatcher(b)
    assert d.dispatch_plan(3).state == "covered_by_offer"
    b.t["dataset_offer"][o["id"]]["status"] = "cancelled"
    st = h.redispatch(b, 3, cause="test", offer=b.t["dataset_offer"][o["id"]])
    assert len(b.rows("task")) == 3 and st.offer_reserved == 0
    assert b.events(po.EVENT_FALLBACK)[0]["created_task_ids"] == st.created_task_ids


def test_settled_offer_holds_only_its_accepted_slots():
    b = world()
    o = add_offer(b, needed=3, status="done", accepted_count=2)
    decided(b, o["id"])
    st = dsp.get_dispatcher(b).dispatch_plan(3)
    assert st.offer_reserved == 2 and st.target == 1 and len(b.rows("task")) == 1


# --------------------------------------------------------------------------- spend gate + decision

def test_check_spend():
    b = world()
    plan = b.get("data_plan", 3)
    ok = po.check_spend(b, plan, 600, plan_task_committed_cents=0, ticket_remaining_cents=10_000)
    assert ok.ok and ok.predicted_needed == 3 and ok.max_cost_cents == 1800
    over = po.check_spend(b, plan, 600, plan_task_committed_cents=2000, ticket_remaining_cents=10_000)
    assert not over.ok and "approve_spend" in over.reason
    poor = po.check_spend(b, plan, 600, plan_task_committed_cents=0, ticket_remaining_cents=1000)
    assert not poor.ok and "budget" in poor.reason
    b.t["data_plan"][3].update(status="proposed", approved_cents=0)
    assert not po.check_spend(b, b.get("data_plan", 3), 600, plan_task_committed_cents=0,
                              ticket_remaining_cents=10_000).ok


class FakeAsync:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_decide_offer_creates_after_approval(monkeypatch):
    b = world()
    seen = {}

    class Svc:
        async def offer_from_plan(self, tid, consider, price):
            seen.update(tid=tid, consider=consider, price=price)
            o = add_offer(b, needed=3, status="proposed", price=price)
            return {"offer": o, "seller": {"id": 9}, "budget_warning": None}

    monkeypatch.setattr(h, "make_async_stdb", lambda bus: FakeAsync())
    monkeypatch.setattr(h, "make_offers_service", lambda stdb: Svc())
    out = h.decide_offer(b, 3)
    assert seen == {"tid": 7, "consider": True, "price": 600}
    ev = b.events(po.EVENT_CREATED)
    assert out and ev and ev[0]["needed_count"] == 3 and ev[0]["max_cost_cents"] == 1800
    assert ev[0]["spend_check"]["ok"] is True and ev[0]["approved_by"] is None
    assert h.decide_offer(b, 3) is None  # one decision per plan


def test_decide_offer_needs_approve_spend_and_a_seller(monkeypatch):
    b = world(plan_status="proposed")
    monkeypatch.setattr(h, "make_offers_service", lambda stdb: (_ for _ in ()).throw(AssertionError("no call")))
    assert h.decide_offer(b, 3) is None and not b.events(po.EVENT_SKIPPED)  # not approved: nothing yet
    b = world()
    del b.t["contributor"][9]
    out = h.decide_offer(b, 3)
    assert out["reason"] == "no dataset seller is registered" and b.events(po.EVENT_SKIPPED)
    b = world(approved=1000)  # approved 1000c < 3 x 600c
    out = h.decide_offer(b, 3)
    assert "approve_spend" in out["reason"]


# --------------------------------------------------------------------------- timeout

def test_timeout_actions():
    b = world()
    old = US - int(4000 * 1e6)
    o = add_offer(b, status="proposed", created=old, updated=old)
    assert po.timeout_action(b, o, timeout_s=3600).action == "cancel"
    assert po.timeout_action(b, o, timeout_s=5000) is None
    b.t["dataset_offer"][o["id"]]["status"] = "qa"
    b.put("episode", ticket_id=7, task_id=0, kind="purchased", review_status="pending", dataset_offer_id=o["id"],
          created_at=old)
    a = po.timeout_action(b, b.get("dataset_offer", o["id"]), timeout_s=3600)
    assert a.action == "delivery_complete" and a.delivered == 1


def test_handle_timeout_cancels_and_logs(monkeypatch):
    monkeypatch.setenv("RDF_OFFER_TIMEOUT_S", "60")
    h._timed_out.clear()
    b = world()
    old = US - int(120 * 1e6)
    b.t["data_plan"][3]["created_at"] = old - 1
    o = add_offer(b, status="accepted", created=old, updated=old)
    a = h.handle_timeout(b, o)
    assert a.action == "cancel" and b.get("dataset_offer", o["id"])["status"] == "cancelled"
    ev = b.events(po.EVENT_TIMEOUT)[0]
    assert ev["data_plan_id"] == 3 and ev["action"] == "cancel" and ev["timeout_s"] == 60


# --------------------------------------------------------------------------- flywheel + report

def test_flywheel_waits_on_the_open_offer():
    b = world()
    o = add_offer(b, needed=3)
    b.t["ticket"][7]["status"] = "dispatch"
    b.put("episode", ticket_id=7, task_id=0, kind="purchased", review_status="pending", dataset_offer_id=o["id"],
          created_at=US)
    d = decide(TicketView.from_bus(b, 7))
    assert d.to is None and f"dataset offer {o['id']}" in d.reason and "1/3 delivered" in d.reason
    for _ in range(2):
        b.put("episode", ticket_id=7, task_id=0, kind="purchased", review_status="pending", dataset_offer_id=o["id"],
              created_at=US)
    assert decide(TicketView.from_bus(b, 7)).to == "review"  # purchased episodes count toward the plan


def test_offer_line():
    o = {"id": 4, "status": "done", "needed_count": 3, "accepted_count": 3}
    eps = [{"id": i, "review_status": "accepted"} for i in (1, 2, 3)] + [{"id": 5, "review_status": "rejected"}]
    line = po.offer_line(o, eps, {5: {"reason": "off-task"}}, [{"ref": "x", "code": "offer_full"}], 5)
    assert line == "bought 3 of 3 needed (target 5); 1 rejected: off-task; 1 refused at ingest (offer_full)"


# --------------------------------------------------------------------------- ingest imports LeRobot (TDM-92 importer)

def test_lerobot_ingest_is_imported_into_our_format(tmp_path):
    """A LeRobot upload is imported (dataset_import) before ingest: the episode row points at OUR
    actions.parquet + <role>.mp4, so QA and the TDM-39 converter read the same files. Extras beyond the
    open slots are refused without importing them, and the delivery is logged."""
    import asyncio
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tdm92_fixtures as fx
    from rdf_common.episode_format import read_actions
    from rdf_orchestrator import offers as of
    from rdf_orchestrator.storage import ObjectInfo, make_uri, parse_uri

    src = fx.build_lerobot_fixture(tmp_path / "ds")

    class Store:
        scheme, bucket = "local", "t95"

        def __init__(self):
            self.objs: dict[str, bytes] = {}

        def put(self, key, data, content_type=None):
            self.objs[key] = data if isinstance(data, bytes) else Path(data).read_bytes()
            return make_uri(self.scheme, self.bucket, key)

        def list(self, prefix="", *, limit=1000):
            return [ObjectInfo(k, make_uri(self.scheme, self.bucket, k), len(v), 0)
                    for k, v in sorted(self.objs.items()) if k.startswith(prefix)][:limit]

    store = Store()
    for p in src.rglob("*"):  # the seller's upload under the offer's lerobot/ prefix
        if p.is_file():
            store.put(of.lerobot_key(7, 4, p.relative_to(src).as_posix()), p.read_bytes())

    class Stdb:
        def __init__(self):
            self.rows = {"dataset_offer": [{"id": 4, "ticket_id": 7, "seller_id": 9, "needed_count": 2, "status": "accepted",
                                            "price_per_episode_cents": 500, "accepted_count": 0, "ingested_count": 0}],
                         "episode": [], "qa_result": [], "review": [], "payment": [], "event_log": []}
            self.calls = []

        async def sql(self, q):
            t = q.split("FROM ")[1].split()[0]
            rows = self.rows.get(t, [])
            if " WHERE " in q and "type =" not in q:
                col, val = q.split(" WHERE ")[1].split(" = ")
                rows = [r for r in rows if str(r.get(col)) == val.strip()]
            return rows

        async def call_reducer(self, name, *args):
            self.calls.append((name, *args))
            if name == "ingest_purchased_episode":
                self.rows["episode"].append({"id": len(self.rows["episode"]) + 1, "ticket_id": 7, "dataset_offer_id": 4,
                                             "kind": "purchased", "review_status": "pending", "storage_uri": args[1],
                                             "actions_uri": args[2], "cameras_json": args[3]})

    stdb = Stdb()
    svc = of.OffersService(stdb, store=store, fetch=lambda uri: store.objs[parse_uri(uri)[2]])
    out = asyncio.run(svc.ingest(4, {"format": "lerobot", "episodes": [0, 3, 2], "note": "[FAKE] seller"}))
    assert [x["ref"] for x in out["ingested"]] == ["lerobot-000000", "lerobot-000003"]
    assert [(x["ref"], x["code"]) for x in out["refused"]] == [("lerobot-000002", "offer_full")]
    ep0 = stdb.rows["episode"][0]
    assert ep0["actions_uri"] == "local://t95/tickets/7/purchased/4/lerobot-000000/actions.parquet"
    assert set(json.loads(ep0["cameras_json"])) == {"exo", "wrist"}
    acts = read_actions(store.objs["tickets/7/purchased/4/lerobot-000000/actions.parquet"])
    assert acts.n > 0  # our format: read_actions works (a raw LeRobot parquet would not)
    ev = [json.loads(c[3]) for c in stdb.calls if c[0] == "append_event" and c[2] == of.EVENT_DELIVERY]
    assert ev and ev[0]["refused"][0]["code"] == "offer_full" and ev[0]["note"] == "[FAKE] seller"
    assert [i["source_episode_index"] for i in ev[0]["import"]["imported"]] == [0, 3]
