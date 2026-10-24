"""TDM-91 proof: the offer flow end to end on a THROWAWAY local SpacetimeDB (never maincloud).

    spacetime start --listen-addr 127.0.0.1:3091 --data-dir $TMPDIR/tdm91/stdb
    (publish a clean HEAD worktree's module:  spacetime publish --no-config -s http://127.0.0.1:3091 \
        -p ./spacetimedb tdm91offers --yes)
    cd services/orchestrator && uv run python ../../spikes/tdm-91-offers/prove_offers.py

What runs for real: the TDM-14 / TDM-11 reducers, the orchestrator's offers router (``offers_api``) and
its bus handlers (``handlers/offers.py``, the ``tdm91_*`` handlers only, on a real WebSocket
subscription), signed upload URLs + HTTP PUTs + storage listing. Two stand-ins, labelled:
  * media store: a local-directory backend with the same signed-URL contract as ``storage.py``
    (HMAC-signed PUT URLs served by this script), so no test bytes go to the real rdf-media Volume;
  * QA verdicts: written with the exact row contract of TDM-92 (deterministic checks + the
    ``deterministic_qa`` summary, reject via submit_review) and TDM-93 (``llm_relevance``,
    ``llm_success`` + submit_review). [SIMULATED QA] - no Claude call, no real media judged.

Scenario (the brief): target_episodes 5, 2 already accepted -> cap 3. Sign 4 (1 refused), ingest 4
(the reducer refuses the 4th), QA-pass 2 + fail 1, seller marks delivery complete, the handler
finalizes and reserves a payout of 2 x price.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient, SpacetimeError, create_identity

HOST = os.environ.get("TDM91_HOST", "http://127.0.0.1:3091")
DB = os.environ.get("TDM91_DB", "tdm91offers")
PORT = int(os.environ.get("TDM91_PORT", "8791"))
ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "tdm91"
PRICE = 400
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)


# --------------------------------------------------------------------------- local media store (stand-in)

class LocalStore:
    """Same contract as storage.ModalVolumeBackend: HMAC-signed PUT URLs, list(prefix), put()."""

    scheme, bucket = "local", "tdm91"

    def __init__(self, root: Path, base_url: str) -> None:
        self.root, self.base_url, self._key = root, base_url, os.urandom(16).hex()

    def _sig(self, method: str, key: str, exp: int) -> str:
        return hmac.new(self._key.encode(), f"{method}\n{key}\n{exp}".encode(), hashlib.sha256).hexdigest()

    def signed_put(self, key: str, content_type: str, *, ttl_s: int = 900):
        from rdf_orchestrator.storage import SignedUrl, make_uri, validate_key

        key = validate_key(key)
        exp = int(time.time()) + ttl_s
        url = f"{self.base_url}/o/{quote(key, safe='/')}?exp={exp}&sig={self._sig('PUT', key, exp)}"
        return SignedUrl(url, "PUT", make_uri(self.scheme, self.bucket, key), key, exp,
                         headers={"Content-Type": content_type}, content_type=content_type)

    def verify(self, key: str, exp: int, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(sig, self._sig("PUT", key, exp))

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"{self.scheme}://{self.bucket}/{key}"

    def list(self, prefix: str = "", *, limit: int = 1000):
        from rdf_orchestrator.storage import ObjectInfo

        out = []
        for p in sorted(self.root.rglob("*")):
            k = p.relative_to(self.root).as_posix()
            if p.is_file() and k.startswith(prefix):
                out.append(ObjectInfo(k, f"{self.scheme}://{self.bucket}/{k}", p.stat().st_size, int(p.stat().st_mtime)))
        return out[:limit]


# --------------------------------------------------------------------------- setup

def owner_call(*args: str) -> None:
    r = subprocess.run(["spacetime", "call", "-s", HOST, DB, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])


def main() -> int:
    import rdf_orchestrator.handlers  # noqa: F401 - registers every handler on the default registry
    from rdf_orchestrator import offers as of
    from rdf_orchestrator.bus import Bus, Registry, registry
    from rdf_orchestrator.handlers import offers as offers_handler
    from rdf_orchestrator.offers_api import offers_router
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    run = ROOT / f"run-{int(time.time())}"
    store = LocalStore(run / "media", f"http://127.0.0.1:{PORT}/tdm91-store")

    orch_id, orch_tok = create_identity(HOST)
    owner_call("register_service_identity", json.dumps({"__identity__": "0x" + orch_id}), '"orchestrator"',
               '"test.tdm91 orchestrator"')
    seller_id_hex, seller_tok = create_identity(HOST)
    orch = SpacetimeClient(HOST, DB, orch_tok)
    seller = SpacetimeClient(HOST, DB, seller_tok)

    seller.call_reducer("register_contributor", "test.tdm91 seller", "", [], "dataset_seller", 0)
    sellers = [c for c in orch.sql("SELECT * FROM contributor") if c["kind"] == "dataset_seller"]
    seller_cid = int(sellers[-1]["id"])
    orch.call_reducer("register_robot", "test.tdm91-yam", "i2rt_yam", "v0", "{}")
    robot_id = int([r for r in orch.sql("SELECT * FROM robot") if r["name"] == "test.tdm91-yam"][0]["id"])
    desc = f"test.tdm91 put the cup in the bin {run.name}"
    orch.call_reducer("create_ticket", robot_id, desc, 0.8, 10_000, json.dumps({"target_episodes": 5}))
    ticket_id = int([t for t in orch.sql("SELECT * FROM ticket") if t["task_description"] == desc][0]["id"])
    for i in range(2):  # two episodes already uploaded and accepted for the ticket
        orch.call_reducer("create_episode", ticket_id, 0, 0, "clean_teleop", f"local://tdm91/existing/{i}",
                          "local://tdm91/existing/actions.parquet", "{}", 300, 10_000, None, None, True, None)
    for e in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {ticket_id}"):
        orch.call_reducer("submit_review", int(e["id"]), True, "existing demo", "{}")
    ds = orch.sql(f"SELECT * FROM dataset WHERE ticket_id = {ticket_id}")[0]
    check(ds["accepted_count"] == 2, f"setup: ticket {ticket_id} target_episodes=5 with 2 accepted episodes")

    # ---- orchestrator app: offers router + local store endpoint + tdm91 handlers on a real subscription
    offers_handler.make_stdb = lambda: SpacetimeClient(HOST, DB, orch_tok)
    reg = Registry()
    for h in registry.handlers:
        if h.name.startswith("tdm91_"):
            reg.handlers.append(h)
            reg.watch(h.table)
    for t in ("dataset_offer", "episode", "qa_result", "review", "payment", "event_log"):
        reg.watch(t)
    bus = Bus(reg, BusClient(host=HOST, db=DB, token=orch_tok, max_backoff=5.0), workers=4)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        await bus.start()
        yield
        await bus.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(offers_router)
    app.state.offers_service = of.OffersService(
        AsyncSpacetimeClient(HOST, DB, orch_tok), store=store,
        fetch=lambda uri: (store.root / of._uri_key(uri)).read_bytes())  # LeRobot meta reads from the local store

    @app.put("/tdm91-store/o/{key:path}")
    async def store_put(key: str, exp: int, sig: str, request: Request) -> dict:
        if not store.verify(key, exp, sig):
            raise HTTPException(403, "bad signature")
        store.put(key, await request.body())
        return {"ok": True}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    api = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30)
    for _ in range(100):
        try:
            api.get("/docs")
            break
        except httpx.HTTPError:
            time.sleep(0.1)

    try:
        # 1. create the offer: the reducer computes needed_count = 5 - 2 = 3
        r = api.post(f"/tickets/{ticket_id}/offers", json={"price_per_episode_cents": PRICE, "seller_id": seller_cid,
                                                          "source_uri": "hf://test.tdm91/cups"})
        body = r.json()
        offer_id = int(body["offer"]["id"])
        check(r.status_code == 201 and body["needed_count"] == 3,
              f"POST /tickets/{ticket_id}/offers -> offer {offer_id}, needed_count {body.get('needed_count')} (5 - 2)")
        check(body["offer"]["status"] == "proposed", "offer starts proposed")
        r = api.post(f"/tickets/{ticket_id}/offers", json={"price_per_episode_cents": PRICE, "seller_id": seller_cid})
        check(r.status_code == 409 and "no episodes needed" in r.text,
              "a 2nd offer is refused: 2 existing + 3 reserved = 5 (" + r.json()["detail"]["message"][:80] + ")")
        r = api.post(f"/offers/{offer_id}/episodes", json={"episodes": [
            {"ref": "x", "cameras": {"exo": "exo.mp4"}, "frame_count": 1, "duration_ms": 1}]})
        check(r.status_code == 409 and r.json()["detail"]["code"] == "offer_not_open", "ingest before accept -> 409")

        # 2. seller accepts as ITSELF (its own identity), not via a service
        seller.call_reducer("accept_dataset_offer", offer_id)
        check(api.get(f"/offers/{offer_id}").json()["offer"]["status"] == "accepted", "seller accepted the offer")

        # 3. signed uploads: 4 episodes asked, only 3 slots -> 3 signed, 1 refused
        files = [{"filename": "exo.mp4"}, {"filename": "wrist.mp4"}, {"filename": "actions.parquet"}]
        refs = ["ep_a", "ep_b", "ep_c", "ep_d"]
        r = api.post(f"/offers/{offer_id}/uploads/sign", json={"episodes": [{"ref": x, "files": files} for x in refs]})
        sign = r.json()
        check([u["ref"] for u in sign["uploads"]] == refs[:3] and [x["ref"] for x in sign["refused"]] == ["ep_d"],
              f"sign: {len(sign['uploads'])} signed, refused {[(x['ref'], x['reason'][:60]) for x in sign['refused']]}")
        check(all(f["key"].startswith(f"tickets/{ticket_id}/purchased/{offer_id}/") for u in sign["uploads"]
                  for f in u["files"]), f"keys under tickets/{ticket_id}/purchased/{offer_id}/<ref>/")
        for u in sign["uploads"]:
            for f in u["files"]:
                pr = httpx.put(f["upload_url"], content=f"[FAKE] {u['ref']} {f['filename']}".encode(), headers=f["headers"])
                assert pr.status_code == 200, pr.text
        bad = httpx.put(sign["uploads"][0]["files"][0]["upload_url"].replace("sig=", "sig=0"), content=b"x")
        check(bad.status_code == 403, "a tampered signed URL is refused (403)")
        # The seller's 4th episode exists in storage anyway (out-of-band copy), so only the reducer can stop it.
        for f in files:
            store.put(f"tickets/{ticket_id}/purchased/{offer_id}/ep_d/{f['filename']}", b"[FAKE] ep_d")

        # 4. ingest 4: the reducer refuses the 4th
        manifest = {"format": "raw", "episodes": [
            {"ref": x, "cameras": {"exo": "exo.mp4", "wrist": "wrist.mp4"}, "actions": "actions.parquet",
             "frame_count": 300, "duration_ms": 10_000, "success": True} for x in refs]}
        r = api.post(f"/offers/{offer_id}/episodes", json=manifest)
        ing = r.json()
        check([x["ref"] for x in ing["ingested"]] == refs[:3], f"ingested {[x['ref'] for x in ing['ingested']]}")
        ref4 = ing["refused"][0] if ing["refused"] else {}
        check(ref4.get("ref") == "ep_d" and ref4.get("code") == "offer_full" and "is full: 3/3" in ref4.get("reason", ""),
              f"4th refused by the reducer: {ref4.get('reason', '')[:150]}")
        again = api.post(f"/offers/{offer_id}/episodes", json={**manifest, "episodes": manifest["episodes"][:1]}).json()
        check(len(again["already_ingested"]) == 1 and not again["ingested"], "re-posting ep_a is idempotent")
        eps = [e for e in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {ticket_id}") if e["kind"] == "purchased"]
        check(len(eps) == 3 and all(e["review_status"] == "pending" and e["dataset_offer_id"] == offer_id for e in eps),
              "3 episode{kind=purchased, review_status=pending} rows")
        o = api.get(f"/offers/{offer_id}").json()
        check(o["offer"]["status"] == "qa" and o["offer"]["ingested_count"] == 3,
              f"offer status {o['offer']['status']} (accepted -> ingesting -> qa), ingested_count {o['offer']['ingested_count']}")
        check(orch.sql(f"SELECT * FROM dataset WHERE ticket_id = {ticket_id}")[0]["total_count"] == 5, "dataset.total_count 5")
        cams = json.loads(eps[0]["cameras_json"])
        check(set(cams) == {"exo", "wrist"} and cams["exo"].endswith(f"/purchased/{offer_id}/ep_a/exo.mp4"),
              "cameras_json holds one uri per camera")

        # 5. QA [SIMULATED QA], with TDM-92 / TDM-93's row contract. ep_a, ep_b pass; ep_c fails jerk.
        by_ref = {e["storage_uri"].rsplit("/", 1)[-1]: int(e["id"]) for e in eps}
        for ref in ("ep_a", "ep_b"):
            eid = by_ref[ref]
            for c in ("actions", "duration", "frame_drops", "jerk", "embodiment", "fps", "camera_set",
                      "action_range", "static", "duplicate"):
                orch.call_reducer("record_qa_result", eid, c, True, json.dumps({"simulated": True}))
            orch.call_reducer("record_qa_result", eid, "deterministic_qa", True, json.dumps({"simulated": True}))
            time.sleep(0.3)
            mid = api.get(f"/offers/{offer_id}").json()
            if ref == "ep_a":
                check(mid["offer"]["status"] == "qa", "deterministic pass alone does not settle (waits for the LLM review)")
            orch.call_reducer("record_qa_result", eid, "llm_relevance", True, json.dumps({"simulated": True}))
            orch.call_reducer("record_qa_result", eid, "llm_success", True, json.dumps({"simulated": True}))
            orch.call_reducer("submit_review", eid, True, "[SIMULATED QA] on task, succeeded", "{}")
        eid = by_ref["ep_c"]
        orch.call_reducer("record_qa_result", eid, "jerk", False, json.dumps({"max_jerk": 9.1, "simulated": True}))
        orch.call_reducer("record_qa_result", eid, "deterministic_qa", False, json.dumps({"failed": ["jerk"]}))
        orch.call_reducer("submit_review", eid, False, "[SIMULATED QA] jerk: max jerk 9.1 above limit", "{}")
        time.sleep(1.5)
        o = api.get(f"/offers/{offer_id}").json()
        check(o["offer"]["status"] == "qa" and o["billable"] == 2 and o["rejected"] == 1 and o["open_slots"] == 1,
              f"after QA: billable {o['billable']}, rejected {o['rejected']}, open_slots {o['open_slots']}; "
              f"not settled yet ({o['finalize_note']})")

        # 6. seller is done -> the handler finalizes and reserves 2 x price
        api.post(f"/offers/{offer_id}/delivered")
        pay = []
        for _ in range(60):
            offer = orch.sql(f"SELECT * FROM dataset_offer WHERE id = {offer_id}")[0]
            pay = [p for p in orch.sql(f"SELECT * FROM payment WHERE ticket_id = {ticket_id}")
                   if p["dataset_offer_id"] == offer_id]
            if offer["status"] == "done" and pay:
                break
            time.sleep(0.25)
        check(offer["status"] == "done" and offer["accepted_count"] == 2,
              f"handler finalized: status {offer['status']}, accepted_count {offer['accepted_count']}")
        check(len(pay) == 1 and pay[0]["amount_cents"] == 2 * PRICE and pay[0]["status"] == "pending"
              and pay[0]["contributor_id"] == seller_cid and pay[0]["batch_ref"] == "settle",
              f"payout reserved: {[(p['amount_cents'], p['status'], p['batch_ref']) for p in pay]} (2 x {PRICE})")
        settled = [json.loads(e["payload_json"]) for e in orch.sql(
            "SELECT * FROM event_log WHERE type = 'dataset_offer.settled'") if json.loads(e["payload_json"]).get("offer_id") == str(offer_id)
            or json.loads(e["payload_json"]).get("offer_id") == offer_id]
        check(bool(settled) and str(settled[-1]["payout_cents"]) == str(2 * PRICE),
              f"event_log dataset_offer.settled payout_cents {settled[-1]['payout_cents'] if settled else None}")
        fin = api.post(f"/offers/{offer_id}/finalize").json()
        check(fin["payment"]["amount_cents"] == 2 * PRICE and len(fin["payments"]) == 1,
              "POST /offers/{id}/finalize afterwards is idempotent (same single payment)")
        t = orch.sql(f"SELECT * FROM ticket WHERE id = {ticket_id}")[0]
        check(t["spent_cents"] == 2 * PRICE, f"ticket.spent_cents {t['spent_cents']}")
        try:
            orch.call_reducer("create_dataset_offer", ticket_id, seller_cid, PRICE, None)
            need = [x for x in orch.sql(f"SELECT * FROM dataset_offer WHERE ticket_id = {ticket_id}")
                    if x["status"] == "proposed"][0]["needed_count"]
            check(need == 1, f"after settling, a new offer needs {need} (5 - 2 existing - 2 bought)")
        except SpacetimeError as e:
            check(False, f"follow-up offer: {e}")
        # 7. LeRobot v2.1 folder, unpacked per episode: ticket 2, target 3, nothing existing, 4 in the folder
        desc2 = desc + " lerobot"
        orch.call_reducer("create_ticket", robot_id, desc2, 0.8, 10_000, json.dumps({"target_episodes": 3}))
        t2 = int(next(t for t in orch.sql("SELECT * FROM ticket") if t["task_description"] == desc2)["id"])
        o2 = int(api.post(f"/tickets/{t2}/offers", json={"price_per_episode_cents": PRICE, "seller_id": seller_cid,
                                                         "accept": True}).json()["offer"]["id"])
        info = {"codebase_version": "v2.1", "fps": 30, "chunks_size": 1000, "features": {
            "observation.images.exo": {"dtype": "video"}, "observation.images.wrist": {"dtype": "video"},
            "action": {"dtype": "float32"}}}
        lfiles = ["meta/info.json", "meta/episodes.jsonl"] + [
            f for i in range(4) for f in (f"data/chunk-000/episode_{i:06d}.parquet",
                                          f"videos/chunk-000/observation.images.exo/episode_{i:06d}.mp4",
                                          f"videos/chunk-000/observation.images.wrist/episode_{i:06d}.mp4")]
        sg = api.post(f"/offers/{o2}/uploads/sign", json={"lerobot_files": lfiles}).json()
        check(len(sg["uploads"]) == 2 + 9 and len(sg["refused"]) == 3,
              f"lerobot sign: {len(sg['uploads'])} files signed (meta + 3 episodes), {len(sg['refused'])} refused (episode 3)")
        content = {"meta/info.json": json.dumps(info).encode(),
                   "meta/episodes.jsonl": b"".join(json.dumps({"episode_index": i, "tasks": ["cups"], "length": 240}).encode()
                                                   + b"\n" for i in range(4))}
        for u in sg["uploads"]:
            httpx.put(u["upload_url"], content=content.get(u["file"], b"[FAKE] " + u["file"].encode()),
                      headers=u["headers"]).raise_for_status()
        li = api.post(f"/offers/{o2}/episodes", json={"format": "lerobot", "final": True}).json()
        check([x["ref"] for x in li["ingested"]] == ["lerobot-000000", "lerobot-000001", "lerobot-000002"]
              and [(x["ref"], x["code"]) for x in li["refused"]] == [("lerobot-000003", "offer_full")],
              f"lerobot ingest: {[x['ref'] for x in li['ingested']]}, refused {[x['ref'] for x in li['refused']]}")
        le = [e for e in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {t2}") if e["kind"] == "purchased"]
        check(len(le) == 3 and all(e["frame_count"] == 240 and e["duration_ms"] == 8000 for e in le)
              and le[0]["actions_uri"].endswith("lerobot/data/chunk-000/episode_000000.parquet"),
              "lerobot episodes: frame_count/duration from meta, actions_uri = the episode parquet")
    finally:
        server.should_exit = True
        time.sleep(0.5)

    failed = [w for ok, w in results if not ok]
    print(f"---- {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
