"""TDM-95 proof: planner decision -> dataset offer (after approve_spend) -> [FAKE] seller delivery ->
QA -> seller payout reservation -> training set -> TDM-39 convert, plus the timeout fallback.

Everything runs on a THROWAWAY local SpacetimeDB booted with the TDM-28 harness (``LocalStack``,
127.0.0.1:3095, db ``tdm95offer``, data under $TMPDIR/tdm-95, never maincloud):

    cd services/orchestrator && uv run python scripts/tdm95_prove_plan_offer.py [--real-llm-qa] [--keep]

What is real and what is not:
  * REAL Claude calls: the TDM-24 planner, once per ticket (2 calls). It decides data type, amount and
    whether to suggest a dataset offer, from FIXTURE baseline trials / clusters ("[FIXTURE]").
  * STUB: TDM-93's LLM QA judge ("[STUB LLM QA]", no Claude call) unless ``--real-llm-qa``; the
    TDM-25 reviewer is not involved (it skips purchased episodes). TDM-92's deterministic QA is real.
  * STUB: Modal fine-tune (``RDF_MODAL_STUB=1`` + the harness StubTrainer, ``fake://`` checkpoint).
    The TDM-39 LeRobot conversion of the resulting training set runs FOR REAL, locally.
  * [FAKE] seller: its own SpacetimeDB identity and the real offers HTTP API, delivering episodes of the
    PUBLIC dataset hf://allenai/24112025-yam-01 (real two-arm YAM data, left arm). Labelled in the
    event log as public data, not something actually bought. Stripe is not called: the seller has no
    connected account, so TDM-63 parks the payout as awaiting_onboarding.
  * Media store: a local directory with the storage.py signed-URL contract (no rdf-media Volume).
  * The 2 "already accepted" demos of ticket A are [FIXTURE] stand-ins (public episodes 4 and 5).

Scenario A (the brief): target_episodes 5, 2 accepted -> the planner suggests an offer -> nothing is
created until approve_spend -> offer for 3 (5 - 2) -> no paid tasks are dispatched for those 3 slots ->
the seller delivers 4 -> 3 ingested, the 4th refused at ingest -> TDM-92 + LLM QA -> finalize_offer ->
reserve_dataset_payment 3 x price -> the flywheel trains on 2 + 3 -> convert.
Scenario B (fallback): a second ticket's offer is never accepted -> RDF_OFFER_TIMEOUT_S passes -> the
offer is cancelled -> the dispatcher posts paid clean_teleop tasks for the freed slots.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack  # noqa: E402

PORT, DB, API_PORT = 3095, "tdm95offer", 8795
OUT = REPO / "spikes" / "tdm-95-plan-offer" / "tmp"
HF = "hf://allenai/24112025-yam-01"
HF_KW = {"arm": "left", "camera_map": {"right": ""}}
TASK = "[FIXTURE] Fold the cloth"
NOTE = ("[FAKE] seller for the TDM-95 proof: episodes of the PUBLIC dataset hf://allenai/24112025-yam-01 "
        "(left arm); public data, not something actually bought")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((bool(ok), what))
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    return bool(ok)


def say(msg: str) -> None:
    print(f"..... {msg}", flush=True)


def wait(pred, timeout: float, what: str, every: float = 0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:  # noqa: BLE001
            pass
        time.sleep(every)
    say(f"timed out after {timeout:.0f}s waiting for {what}")
    return None


# --------------------------------------------------------------------------- local media store (stand-in)

class LocalStore:
    """storage.py's backend contract over a local directory: HMAC-signed PUT/GET URLs served by this
    script, list(prefix), put(). URIs are modal-vol://tdm95-proof/<key> so the TDM-39 converter can read
    them with mounts={"tdm95-proof": <dir>}; storage.backend_for_uri resolves them to this store."""

    scheme, bucket = "modal-vol", "tdm95-proof"

    def __init__(self, root: Path, base_url: str) -> None:
        self.root, self.base_url, self._key = root, base_url, os.urandom(16).hex()
        root.mkdir(parents=True, exist_ok=True)

    def _sig(self, method: str, key: str, exp: int) -> str:
        return hmac.new(self._key.encode(), f"{method}\n{key}\n{exp}".encode(), hashlib.sha256).hexdigest()

    def _key_of(self, uri_or_key: str) -> str:
        from rdf_orchestrator.storage import parse_uri, validate_key

        return validate_key(parse_uri(uri_or_key)[2] if "://" in uri_or_key else uri_or_key)

    def _signed(self, method: str, key: str, ttl_s: int, ctype: str | None):
        from rdf_orchestrator.storage import SignedUrl, make_uri

        exp = int(time.time()) + ttl_s
        url = f"{self.base_url}/o/{quote(key, safe='/')}?exp={exp}&sig={self._sig(method, key, exp)}"
        return SignedUrl(url, method, make_uri(self.scheme, self.bucket, key), key, exp,
                         headers={"Content-Type": ctype} if ctype else {}, content_type=ctype)

    def signed_put(self, key: str, content_type: str, *, ttl_s: int = 900):
        return self._signed("PUT", self._key_of(key), ttl_s, content_type)

    def signed_get(self, uri_or_key: str, *, ttl_s: int = 900):
        return self._signed("GET", self._key_of(uri_or_key), ttl_s, None)

    def verify(self, method: str, key: str, exp: int, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(sig, self._sig(method, key, exp))

    def put(self, key: str, data, content_type: str | None = None) -> str:
        from rdf_orchestrator.storage import make_uri

        key = self._key_of(key)
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.read_bytes() if isinstance(data, Path) else data)
        return make_uri(self.scheme, self.bucket, key)

    def list(self, prefix: str = "", *, limit: int = 1000):
        from rdf_orchestrator.storage import ObjectInfo, make_uri

        out = []
        for p in sorted(self.root.rglob("*")):
            k = p.relative_to(self.root).as_posix()
            if p.is_file() and k.startswith(prefix):
                out.append(ObjectInfo(k, make_uri(self.scheme, self.bucket, k), p.stat().st_size, int(p.stat().st_mtime)))
        return out[:limit]


class StubJudge:
    """[STUB LLM QA]: TDM-93's judge interface without a Claude call (harness stub)."""

    model = "stub:tdm95-proof"
    last_usage = None

    def judge(self, messages):
        from rdf_orchestrator.llm_qa import LlmJudgement

        return LlmJudgement(observations="[STUB LLM QA] frames not judged by Claude in this proof",
                            scene_matches=True, on_task=True, task_succeeded="yes", quality=4,
                            reason="[STUB LLM QA] accepted without a Claude call (harness stub)")


# --------------------------------------------------------------------------- seeding

def goto(c, tid: int, status: str) -> None:
    """set_ticket_status unless the (live) flywheel already moved the ticket there."""
    if c.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] != status:
        c.call_reducer("set_ticket_status", tid, status, "[FIXTURE] TDM-95 proof")


def seed_ticket(c, robot_id: int, *, target: int, pre: list, store: LocalStore, label: str,
                clusters: bool = True) -> int:
    """A ticket with a [FIXTURE] baseline (0/10, 'never reaches the cloth'); with ``clusters`` also its
    failure clusters and status plan_data, else it is left in diagnose (see ``add_clusters``)."""
    from rdf_orchestrator.stats import wilson_interval

    desc = f"{TASK} ({label} {int(time.time())})"
    c.call_reducer("create_ticket", robot_id, desc, 0.8, 10_000, json.dumps({"target_episodes": target}))
    tid = int(next(t for t in c.sql("SELECT * FROM ticket") if t["task_description"] == desc)["id"])
    for i, ie in enumerate(pre):  # already-accepted demos ([FIXTURE] stand-ins)
        base = f"tickets/{tid}/episodes/pre-{i}"
        cams = {r: store.put(f"{base}/{r}.mp4", p.read_bytes()) for r, p in ie.cameras.items()}
        act = store.put(f"{base}/actions.parquet", ie.actions_path.read_bytes())
        c.call_reducer("create_episode", tid, 0, 0, "clean_teleop", f"{store.scheme}://{store.bucket}/{base}",
                       act, json.dumps(cams), ie.frame_count, ie.duration_ms, None, None, True, None)
    for e in c.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}"):
        c.call_reducer("submit_review", int(e["id"]), True,
                       "[FIXTURE] stand-in for a demo already collected (public HF episode)", "{}")
    goto(c, tid, "baseline")
    c.call_reducer("start_eval_run", tid, "pi05_base", 10)
    run = max(int(r["id"]) for r in c.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}"))
    notes = ["[FIXTURE] arm hovers above the table, never reaches the cloth"] * 6 + \
            ["[FIXTURE] no motion from home pose until timeout"] * 4
    for i, n in enumerate(notes):
        c.call_reducer("record_eval_trial", run, i, False, 60_000, 0, n)
    lo, hi = wilson_interval(0, 10)
    c.call_reducer("finalize_eval_run", run, 0, 10, lo, hi, "[FIXTURE] TDM-95 proof baseline")
    goto(c, tid, "diagnose")
    if clusters:
        add_clusters(c, tid, run)
    return tid


def add_clusters(c, tid: int, run: int) -> None:
    clusters = [("never reaches the cloth", 6, "approach", "The arm hovers above the table and never gets near the cloth."),
                ("freezes at start", 4, "start", "Near-zero actions; the arm stays at its home pose.")]
    for lbl, n, _, _ in clusters:
        c.call_reducer("record_failure_cluster", tid, lbl, n, "[]")
    ids = {r["label"]: int(r["id"]) for r in c.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {tid}")}
    c.call_reducer("append_event", "test.tdm95", "diagnose.clusters", json.dumps({
        "ticket_id": tid, "iteration": 0, "eval_run_id": run, "fixture": "TDM-95 proof FIXTURE clusters",
        "reasoning": "[FIXTURE] grouped by the failure notes",
        "clusters": [{"failure_cluster_id": ids[lbl], "label": lbl, "count": n, "phase": ph,
                      "recoverable_mid_episode": False, "description": d,
                      "recoverability_reason": "No near-success state to recover from."} for lbl, n, ph, d in clusters]}))
    goto(c, tid, "plan_data")


def events(c, type_: str, **match) -> list[dict]:
    out = []
    for r in c.sql(f"SELECT * FROM event_log WHERE type = '{type_}'"):
        p = json.loads(r["payload_json"] or "{}")
        if all(str(p.get(k)) == str(v) for k, v in match.items()):
            out.append(p)
    return out


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-llm-qa", action="store_true", help="TDM-93's real Claude judge instead of the stub")
    ap.add_argument("--keep", action="store_true", help="leave the local SpacetimeDB running")
    args = ap.parse_args()

    from rdf_orchestrator.dataset_import import import_lerobot

    pre_dir = OUT / "pre"
    say(f"importing 2 [FIXTURE] pre-existing demos from {HF} episodes 4, 5 (left arm)")
    from rdf_orchestrator.dataset_import import ImportedEpisode

    marker = pre_dir / "imported.json"
    if marker.exists():  # cached from an earlier run of this proof
        pre = [ImportedEpisode(e["i"], Path(e["dir"]), Path(e["actions"]), {k: Path(v) for k, v in e["cams"].items()},
                               e["n"], e["ms"], e["ok"], e["task"]) for e in json.loads(marker.read_text())]
    else:
        pre = import_lerobot(HF, pre_dir, episodes=[4, 5], assume_success=True, **HF_KW)
        marker.write_text(json.dumps([{"i": e.source_index, "dir": str(e.dir), "actions": str(e.actions_path),
                                       "cams": {k: str(v) for k, v in e.cameras.items()}, "n": e.frame_count,
                                       "ms": e.duration_ms, "ok": e.success, "task": e.task} for e in pre]))

    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-95")
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        os.environ.update(st.env("orchestrator"))
        os.environ.update({"RDF_MODAL_STUB": "1", "RDF_OFFER_TICK_S": "1", "RDF_OFFER_TIMEOUT_S": "1800",
                           "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": "0"})
        from rdf_common.settings import OrchestratorSettings, load_settings

        s = load_settings(OrchestratorSettings, "orchestrator")
        assert s.spacetime_host.startswith("http://127.0.0.1") and s.spacetime_db_name == DB, "not local!"
        return run(st, pre, args)
    finally:
        if not args.keep:
            st.stop()


def run(st: LocalStack, pre: list, args) -> int:  # noqa: C901 - a linear proof script
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request, Response
    from rdf_common.episode_format import read_actions
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient, create_identity

    import rdf_orchestrator.handlers  # noqa: F401 - registers every handler
    from rdf_orchestrator import offers as of
    from rdf_orchestrator import plan_offers as po
    from rdf_orchestrator import storage
    from rdf_orchestrator.bus import Bus, Registry, registry
    from rdf_orchestrator.flywheel import NotImplementedStep
    from rdf_orchestrator.handlers import llm_qa as h93
    from rdf_orchestrator.handlers.flywheel import FLYWHEEL
    from rdf_orchestrator.offers_api import offers_router
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient
    from stub_modal import StubTrainer

    c = st.client("orchestrator")
    run_dir = OUT / f"run-{int(time.time())}"
    store = LocalStore(run_dir / "media", f"http://127.0.0.1:{API_PORT}/tdm95-store")
    storage.get_backend = lambda: store  # every storage user (QA, offers, reviewer fetch) -> the local store
    if not args.real_llm_qa:
        h93.make_judge = lambda cfg: StubJudge()
    for stage in ("baseline", "eval"):
        FLYWHEEL.register_step(stage, NotImplementedStep(stage, "TDM-67", "not run in the TDM-95 proof (no robot)"))

    # ---- the [FAKE] seller: its own identity, registered as a dataset seller
    _, seller_tok = create_identity(st.host)
    seller = SpacetimeClient(st.host, st.db, seller_tok)
    seller.call_reducer("register_contributor", "[FAKE] tdm95 seller (public HF data, nothing bought)", "", [],
                        "dataset_seller", 0)
    seller_id = int(max(x["id"] for x in c.sql("SELECT * FROM contributor") if x["kind"] == "dataset_seller"))
    c.call_reducer("register_robot", "test.tdm95-yam", "i2rt_yam", "v0", "{}")
    robot_id = int(next(r for r in c.sql("SELECT * FROM robot") if r["name"] == "test.tdm95-yam")["id"])
    ta = seed_ticket(c, robot_id, target=5, pre=pre, store=store, label="A")
    tb = seed_ticket(c, robot_id, target=3, pre=[], store=store, label="B", clusters=False)  # parked in diagnose
    check(c.sql(f"SELECT * FROM dataset WHERE ticket_id = {ta}")[0]["accepted_count"] == 2,
          f"setup: ticket A {ta} at plan_data, target_episodes 5, 2 accepted demos ([FIXTURE] baseline 0/10)")

    # ---- orchestrator: one bus with the loop's handlers (no robot, no diagnose, no Factory)
    keep = ("tdm24_", "tdm95_", "tdm91_", "tdm92_", "tdm93_", "dispatch_", "payments.", "flywheel.", "tdm21_",
            "event_log_tap")
    reg = Registry()
    reg.handlers = [x for x in registry.handlers if x.name.startswith(keep)]
    for q in registry.queries():
        reg.watch(q.removeprefix("SELECT * FROM "))
    bus_box: dict = {}

    def bus_thread():
        async def go():
            bus = Bus(reg, BusClient(host=st.host, db=st.db, token=st.token("orchestrator"), max_backoff=5.0), workers=8)
            bus_box["bus"] = bus
            await bus.start()
            while not bus_box.get("stop"):
                await asyncio.sleep(0.2)
            await bus.stop()
        asyncio.run(go())

    threading.Thread(target=bus_thread, name="tdm95-bus", daemon=True).start()
    stub = StubTrainer(SpacetimeClient(st.host, st.db, st.token("orchestrator")), mode="stub-call").start()

    # ---- offers API (what the seller talks to) + the local store's signed URLs
    app = FastAPI()
    app.include_router(offers_router)
    app.state.offers_service = of.OffersService(AsyncSpacetimeClient(st.host, st.db, st.token("orchestrator")),
                                                store=store, fetch=lambda uri: (store.root / of._uri_key(uri)).read_bytes())

    @app.put("/tdm95-store/o/{key:path}")
    async def store_put(key: str, exp: int, sig: str, request: Request) -> dict:
        if not store.verify("PUT", key, exp, sig):
            raise HTTPException(403, "bad signature")
        store.put(key, await request.body())
        return {"ok": True}

    @app.get("/tdm95-store/o/{key:path}")
    async def store_get(key: str, exp: int, sig: str) -> Response:
        if not store.verify("GET", key, exp, sig) or not (store.root / key).is_file():
            raise HTTPException(403, "bad signature or missing")
        return Response((store.root / key).read_bytes())

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    import httpx

    api = httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=1800)
    wait(lambda: api.get("/docs").status_code == 200, 20, "offers API")

    try:
        # ================= scenario A
        say("planner (REAL Claude call) deciding ticket A ...")
        plan = wait(lambda: next(iter(c.sql(f"SELECT * FROM data_plan WHERE ticket_id = {ta}")), None), 300,
                    "the planner's data_plan")
        if not check(plan is not None, "planner recorded a data_plan for ticket A"):
            return finish()
        pe = events(c, "planner.data_plan", data_plan_id=plan["id"])[0]
        hint = pe["evidence"]["dataset_offer"]
        say(f"planner: {plan['data_type']} x {plan['amount']} (est {plan['est_cost_cents']}c), served by "
            f"{pe['evidence']['model'].get('served')}; dataset_offer {hint}")
        say("reasoning: " + plan["reasoning"][:600].replace("\n", " "))
        if not check(hint.get("consider") is True and plan["data_type"] == "clean_teleop",
                     f"planner suggests a dataset offer: max {hint.get('max_price_per_episode_cents')}c/episode "
                     f"for up to {hint.get('max_episodes')} episodes"):
            return finish()
        time.sleep(3)
        check(not c.sql(f"SELECT * FROM dataset_offer WHERE ticket_id = {ta}")
              and not events(c, po.EVENT_CREATED, ticket_id=ta) and plan["status"] == "proposed",
              "spend gate: no offer while the plan is only proposed (approve_spend not called)")

        c.call_reducer("approve_spend", ta, plan["id"], 0, "human:tdm95-proof")
        say(f"approve_spend(ticket {ta}, plan {plan['id']}, est) as human:tdm95-proof")
        dec = wait(lambda: (events(c, po.EVENT_CREATED, data_plan_id=plan["id"])
                            or events(c, po.EVENT_SKIPPED, data_plan_id=plan["id"])), 60, "the offer decision")
        if not check(bool(dec) and "offer_id" in dec[0] and dec[0].get("needed_count") == 3,
                     f"plan_offer.created after approval: {json.dumps(dec[0] if dec else None)[:400]}"):
            return finish()
        d = dec[0]
        oid, price = int(d["offer_id"]), int(d["price_per_episode_cents"])
        check(d["spend_check"]["ok"] and d["max_cost_cents"] <= d["spend_check"]["approved_headroom_cents"],
              f"offer {oid}: 3 x {price}c = {d['max_cost_cents']}c within approved {d['spend_check']['approved_cents']}c "
              f"and remaining budget {d['spend_check']['ticket_remaining_cents']}c")
        time.sleep(4)
        tasks = [t for t in c.sql(f"SELECT * FROM task WHERE ticket_id = {ta}") if t["type"] != "reset"]
        cov = events(c, "dispatch.covered_by_offer", data_plan_id=plan["id"])
        check(not tasks and bool(cov), f"dispatcher posted 0 paid tasks: offer {oid} holds all "
                                       f"{plan['amount']} slots (dispatch.covered_by_offer)")
        bus = bus_box["bus"]
        desc = FLYWHEEL.describe(bus, ta)
        check(desc["status"] == "dispatch" and "dataset offer" in desc["decision"]["reason"],
              f"flywheel waits in dispatch: {desc['decision']['reason']}")

        # ---- the [FAKE] seller accepts as itself and delivers 4 public episodes
        seller.call_reducer("accept_dataset_offer", oid)
        say("seller accepted; delivering 4 episodes (hf://allenai/24112025-yam-01 ep 0-3, left arm) ...")
        t0 = time.time()
        r = api.post(f"/offers/{oid}/episodes", json={"format": "lerobot", "source": HF, "episodes": [0, 1, 2, 3],
                                                      "assume_success": True, "note": NOTE, **HF_KW})
        ing = r.json()
        say(f"ingest took {time.time() - t0:.0f}s: {json.dumps({k: ing.get(k) for k in ('ingested', 'refused')})[:600]}")
        check(r.status_code == 200 and [x["ref"] for x in ing["ingested"]] == ["lerobot-000000", "lerobot-000001",
                                                                               "lerobot-000002"],
              f"3 of 4 ingested: {[x['ref'] for x in ing.get('ingested', [])]}")
        check([(x["ref"], x["code"]) for x in ing.get("refused", [])] == [("lerobot-000003", "offer_full")],
              f"the 4th is refused at ingest: {ing.get('refused')}")
        eps = [e for e in c.sql(f"SELECT * FROM episode WHERE ticket_id = {ta}") if e["kind"] == "purchased"]
        a0 = read_actions((store.root / of._uri_key(eps[0]["actions_uri"])).read_bytes())
        check(all(e["actions_uri"].endswith("/actions.parquet") and "/lerobot/data/" not in e["actions_uri"]
                  and set(json.loads(e["cameras_json"])) == {"exo", "wrist"} for e in eps) and a0.n > 0,
              f"imported at ingest into OUR episode format (read_actions ok, {a0.n} ticks; cameras exo+wrist): "
              f"{eps[0]['actions_uri']}")
        dlv = events(c, of.EVENT_DELIVERY, offer_id=oid)
        check(bool(dlv) and dlv[0]["import"]["public_dataset"] and "public data" in (dlv[0]["note"] or ""),
              "event_log dataset_offer.delivery: ingested + refused + import record, labelled public data")

        # ---- QA: TDM-92 deterministic (real) + TDM-93 LLM (stub unless --real-llm-qa)
        def reviewed():
            rows = [e for e in c.sql(f"SELECT * FROM episode WHERE ticket_id = {ta}") if e["kind"] == "purchased"]
            return rows if rows and all(e["review_status"] != "pending" for e in rows) else None

        rows = wait(reviewed, 900, "QA verdicts on the purchased episodes", every=2) or []
        for e in rows:
            rv = c.sql(f"SELECT * FROM review WHERE episode_id = {e['id']}")
            qa = c.sql(f"SELECT * FROM qa_result WHERE episode_id = {e['id']}")
            say(f"episode {e['id']} {e['storage_uri'].rsplit('/', 1)[-1]}: {e['review_status']}; "
                f"{len(qa)} qa rows, failed {[q['check_name'] for q in qa if not q['passed']]}; "
                f"review: {(rv[0]['reason'] if rv else '')[:160]}")
        accepted = [e for e in rows if e["review_status"] == "accepted"]
        for nxt in (6, 7, 8):  # a rejected slot stays open: the seller sends a replacement (4, 5 are the demos)
            if len(accepted) >= 3:
                break
            say(f"{3 - len(accepted)} slot(s) reopened by rejections; seller sends replacement episode {nxt}")
            api.post(f"/offers/{oid}/episodes", json={"format": "lerobot", "source": HF, "episodes": [nxt],
                                                      "assume_success": True, "note": NOTE, **HF_KW})
            rows = wait(reviewed, 900, "QA on the replacement", every=2) or []
            accepted = [e for e in rows if e["review_status"] == "accepted"]
        check(len(accepted) == 3, f"QA accepted 3 purchased episodes ({len(rows) - len(accepted)} rejected)")
        api.post(f"/offers/{oid}/delivered")

        def settled():
            o = c.sql(f"SELECT * FROM dataset_offer WHERE id = {oid}")[0]
            pays = [p for p in c.sql(f"SELECT * FROM payment WHERE ticket_id = {ta}") if p["dataset_offer_id"] == oid]
            return (o, pays) if o["status"] == "done" and pays and pays[0]["status"] != "pending" else None

        got = wait(settled, 120, "finalize_offer + the seller payout")
        o, pays = got if got else (c.sql(f"SELECT * FROM dataset_offer WHERE id = {oid}")[0], [])
        check(o["status"] == "done" and o["accepted_count"] == 3, f"finalize_offer: accepted_count {o['accepted_count']}")
        check(len(pays) == 1 and pays[0]["amount_cents"] == 3 * price and pays[0]["contributor_id"] == seller_id
              and pays[0]["status"] in ("awaiting_onboarding", "pending", "paid"),
              f"seller payout reserved only after QA: {[(p['amount_cents'], p['status'], p['batch_ref']) for p in pays]}"
              f" (3 x {price}c; TDM-63: seller has no Stripe account -> awaiting_onboarding)")

        # ---- the flywheel trains on 2 + 3
        tr = wait(lambda: next((x for x in c.sql(f"SELECT * FROM training_run WHERE ticket_id = {ta}")
                                if x["status"] == "done"), None), 120, "the (stub) training run")
        acc_ids = sorted(int(e["id"]) for e in c.sql(f"SELECT * FROM episode WHERE ticket_id = {ta}")
                         if e["review_status"] == "accepted")
        check(tr is not None and sorted(int(i) for i in tr["episode_ids"]) == acc_ids and len(acc_ids) == 5,
              f"training run {tr and tr['id']} on {acc_ids} (2 demos + 3 purchased); checkpoint {tr and tr['checkpoint_uri']} [STUB]")
        tk = c.sql(f"SELECT * FROM ticket WHERE id = {ta}")[0]
        check(tk["status"] in ("train", "eval"), f"ticket A moved dispatch -> review -> train -> {tk['status']}")

        if tr is not None:
            from rdf_orchestrator.modal_client import build_manifest

            eps_by = {int(e["id"]): e for e in c.sql(f"SELECT * FROM episode WHERE ticket_id = {ta}")}
            man = build_manifest(tr, tk, eps_by)
            mpath = run_dir / "convert_manifest.json"
            mpath.write_text(json.dumps(man, indent=1))
            say(f"TDM-39 convert (REAL, local CPU) on {len(man['episodes'])} episodes ...")
            code = ("import json,sys; from rdf_training.convert import convert; "
                    f"r=convert(json.load(open({str(mpath)!r})), lerobot_root={str(run_dir / 'datasets/lerobot')!r}, "
                    f"assets_root={str(run_dir / 'datasets/assets')!r}, mounts={{{store.bucket!r}: {str(store.root)!r}}}); "
                    "print(json.dumps({k: r.get(k) for k in ('repo_id','episodes','frames','total_frames','seconds')}, default=str))")
            p = subprocess.run(["uv", "run", "--quiet", "python", "-c", code], cwd=REPO / "services/training",
                               capture_output=True, text=True, timeout=3600)
            say("convert: " + (p.stdout.strip()[-400:] or p.stderr.strip()[-800:]))
            info_p = run_dir / "datasets/lerobot" / man["repo_id"] / "meta/info.json"
            info = json.loads(info_p.read_text()) if info_p.exists() else {}
            check(p.returncode == 0 and info.get("total_episodes") == 5,
                  f"TDM-39 converted the training set to LeRobot v2.1: {info.get('total_episodes')} episodes, "
                  f"{info.get('total_frames')} frames ({info_p.parent.parent})")

        # ---- report (TDM-71) + evidence trail (TDM-68)
        from rdf_orchestrator import decision, report

        async def docs():
            async with AsyncSpacetimeClient(st.host, st.db, st.token("orchestrator")) as a:
                rep = await report.build(a.sql, ta)
                ev = decision.assemble(await decision.load_tables(a.sql, ta), ta)
                return rep, ev

        rep, ev = asyncio.run(docs())
        lines = [x for it in rep["iterations"] for x in it["dataset_offers"]]
        md = report.render_markdown(rep)
        offer_md = [ln for ln in md.splitlines() if "Dataset offer" in ln]
        say("report: " + (offer_md[0] if offer_md else "(no offer line)"))
        check(bool(lines) and lines[0]["line"].startswith("bought 3 of 3 needed (target 5)")
              and "1 refused at ingest (offer_full)" in lines[0]["line"],
              f"report lists the purchase separately: '{lines[0]['line'] if lines else None}'")
        pv = next((x for x in ev.get("decisions", []) if x.get("plan")), {}).get("plan") or {}
        od = pv.get("dataset_offer_decision") or {}
        check(od.get("outcome") == "created" and od.get("offer_id") == oid,
              f"evidence trail: data_plan {pv.get('data_plan_id')} -> dataset_offer_decision {od.get('outcome')} "
              f"offer {od.get('offer_id')} (event {(od.get('event') or {}).get('event_id')})")

        # ================= scenario B: nobody delivers -> timeout -> paid demos
        os.environ["RDF_OFFER_TIMEOUT_S"] = "10"
        run_b = max(int(r["id"]) for r in c.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tb}"))
        add_clusters(c, tb, run_b)
        say(f"ticket B {tb}: target 3, nothing collected; planner (REAL Claude call) ...")
        pb = wait(lambda: next(iter(c.sql(f"SELECT * FROM data_plan WHERE ticket_id = {tb}")), None), 300, "plan B")
        if not check(pb is not None, "planner recorded a data_plan for ticket B"):
            return finish()
        hb = events(c, "planner.data_plan", data_plan_id=pb["id"])[0]["evidence"]["dataset_offer"]
        say(f"planner B: {pb['data_type']} x {pb['amount']}; dataset_offer {hb}")
        c.call_reducer("approve_spend", tb, pb["id"], 0, "human:tdm95-proof")
        db_ = wait(lambda: events(c, po.EVENT_CREATED, data_plan_id=pb["id"])
                   or events(c, po.EVENT_SKIPPED, data_plan_id=pb["id"]), 60, "offer decision B")
        if not check(bool(db_) and "offer_id" in db_[0],
                     f"ticket B: offer created after approval ({json.dumps(db_[0] if db_ else None)[:200]})"):
            return finish()
        ob = int(db_[0]["offer_id"])
        check(not [t for t in c.sql(f"SELECT * FROM task WHERE ticket_id = {tb}") if t["type"] != "reset"],
              f"ticket B: no paid tasks while offer {ob} is open")
        say("the seller never accepts offer B; waiting for RDF_OFFER_TIMEOUT_S=10 ...")
        to = wait(lambda: events(c, po.EVENT_TIMEOUT, offer_id=ob), 60, "plan_offer.timeout")
        check(bool(to) and to[0]["action"] == "cancel"
              and c.sql(f"SELECT * FROM dataset_offer WHERE id = {ob}")[0]["status"] == "cancelled",
              f"timeout: offer {ob} cancelled after {to[0]['waited_s'] if to else '?'}s ({to[0]['reason'] if to else ''})")
        fb = wait(lambda: events(c, po.EVENT_FALLBACK, offer_id=ob), 30, "plan_offer.fallback")
        tasks = [t for t in c.sql(f"SELECT * FROM task WHERE ticket_id = {tb}") if t["type"] == pb["data_type"]]
        check(bool(fb) and len(tasks) == int(pb["amount"]),
              f"fallback: {len(tasks)} paid {pb['data_type']} tasks posted for the freed slots "
              f"(plan_offer.fallback {fb[0]['created_task_ids'] if fb else None})")
        types = {}
        for r in c.sql("SELECT * FROM event_log"):
            if r["type"].startswith(("plan_offer.", "dataset_offer.", "dispatch.covered", "payment.", "qa.",
                                     "review.", "spend.")):
                types[r["type"]] = types.get(r["type"], 0) + 1
        say("event_log: " + json.dumps(types, sort_keys=True))
        return finish()
    finally:
        bus_box["stop"] = True
        server.should_exit = True
        if stub:
            stub.stop()
        time.sleep(0.5)


def finish() -> int:
    failed = [w for ok, w in results if not ok]
    print(f"---- {len(results) - len(failed)} passed, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
