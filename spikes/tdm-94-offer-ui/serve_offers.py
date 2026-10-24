"""TDM-94 live harness: the offer card + seller page against a THROWAWAY local SpacetimeDB (never maincloud).

    spacetime start --listen-addr 127.0.0.1:3094 --data-dir $TMPDIR/tdm94/stdb
    (publish a clean HEAD worktree's module:  spacetime publish --no-config -s http://127.0.0.1:3094 \
        -p ./spacetimedb tdm94offers --yes)
    cd services/orchestrator && uv run python ../../spikes/tdm-94-offer-ui/serve_offers.py
    # web, pointed at it:
    cd apps/web && NEXT_PUBLIC_SPACETIME_HOST=ws://127.0.0.1:3094 NEXT_PUBLIC_SPACETIME_DB_NAME=tdm94offers \
        RDF_ORCHESTRATOR_URL=http://127.0.0.1:8794 NEXT_DIST_DIR=.next-tdm94 npx next dev --port 3194

What is real: the TDM-14 / TDM-11 reducers, the orchestrator's offers router (``offers_api``), the TDM-91
``tdm91_*`` bus handlers (finalize + reserve payout), signed upload URLs and the browser's direct PUTs.

Stand-ins, labelled:
  * media store: a local-directory backend with the same signed-URL contract as ``storage.py`` (HMAC
    signed PUT URLs, CORS for the browser), so no test bytes reach the real rdf-media Volume.
  * QA: [SIMULATED QA]. A loop here writes TDM-92 / TDM-93's exact row contract a few seconds after each
    purchased episode lands (no Claude call, no media judged). The verdict comes from the seller's
    folder name so every UI state can be shown:
        *reject*  -> deterministic ``jerk`` fails (TDM-92 rejects via submit_review)
        *offtask* -> ``llm_relevance`` fails (TDM-93 rejects)
        *error*   -> one ``qa.error`` event first (outage, episode stays pending), then passes
        *cap*     -> ``qa.llm_capped`` event, the episode stays pending
        otherwise -> passes both layers
    After each LLM verdict it calls TDM-93's real ``acceptance_guard`` (``qa.acceptance_rate``), with
    ``guard.min_reviews`` lowered to TDM94_GUARD_MIN_REVIEWS (default 2) so ``suspect`` can show on a
    3-episode offer.

On boot it creates a test robot and ticket (target_episodes 5, two accepted demos -> an offer buys 3)
and prints the ids. Make an offer for the browser's seller with:
    curl -XPOST localhost:8794/tickets/<t>/offers -H 'content-type: application/json' \
        -d '{"price_per_episode_cents": 400, "seller_id": <contributor id>}'
Fixture episodes (labelled fixture=true, synthetic test video): ``--make-fixtures`` writes them to
spikes/tdm-94-offer-ui/tmp/fixtures/.
"""

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

HOST = os.environ.get("TDM94_HOST", "http://127.0.0.1:3094")
DB = os.environ.get("TDM94_DB", "tdm94offers")
PORT = int(os.environ.get("TDM94_PORT", "8794"))
ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "tdm94"
HERE = Path(__file__).resolve().parent
QA_DELAY_S = float(os.environ.get("TDM94_QA_DELAY_S", "4"))
SIM = "[SIMULATED QA]"


# --------------------------------------------------------------------------- fixtures

def make_fixtures(out: Path, names: list[str]) -> None:
    """Synthetic episodes in our raw format: exo.mp4 + wrist.mp4 (ffmpeg test pattern, 3 s at 30 fps,
    title metadata 'FIXTURE ...') + actions.parquet (90 ticks, rdf.episode fixture=true)."""
    import numpy as np
    from rdf_common.episode_format import make_metadata, write_actions

    n, fps = 90, 30.0
    for name in names:
        d = out / name
        d.mkdir(parents=True, exist_ok=True)
        for cam, src in (("exo", "testsrc2"), ("wrist", "smptebars")):
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            f"{src}=size=320x240:rate=30:duration=3",
                            "-metadata", f"title=FIXTURE {name} {cam} (synthetic test pattern)",
                            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(d / f"{cam}.mp4")], check=True)
        t0 = 1_760_000_000_000_000
        ts = np.arange(n)
        write_actions(d / "actions.parquet", {
            "frame_index": ts.astype(np.int32), "timestamp_us": (t0 + ts * 33_333).astype(np.int64),
            "joint_pos": np.tile(np.linspace(0, 0.2, 6), (n, 1)), "gripper_pos": np.linspace(1, 0, n),
            "ik_target_pos": np.tile([0.3, 0.0, 0.2], (n, 1)), "ik_target_quat": np.tile([1, 0, 0, 0], (n, 1)),
            "gripper_cmd": np.linspace(1, 0, n), "control_source": ["human"] * n,
        }, make_metadata(fps=fps, joint_names=[f"j{i}" for i in range(1, 7)], fixture=True,
                         cameras={"exo": {"frames": n, "fps": fps}, "wrist": {"frames": n, "fps": fps}},
                         note="TDM-94 synthetic fixture, not robot data"))
        print("fixture", d)


# --------------------------------------------------------------------------- local media store (stand-in)

class LocalStore:
    """Same contract as storage.ModalVolumeBackend: HMAC-signed PUT URLs, list(prefix), put()."""

    scheme, bucket = "local", "tdm94"

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


def owner_call(*args: str) -> None:
    r = subprocess.run(["spacetime", "call", "-s", HOST, DB, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-500:])


# --------------------------------------------------------------------------- [SIMULATED QA]

def qa_loop(orch, stop: threading.Event) -> None:
    from rdf_orchestrator import llm_qa as lq

    cfg = lq.load_config(guard_min_reviews=int(os.environ.get("TDM94_GUARD_MIN_REVIEWS", "2")))
    seen: dict[int, float] = {}
    stage: dict[int, str] = {}
    while not stop.wait(1.0):
        try:
            eps = [e for e in orch.sql("SELECT * FROM episode WHERE kind = 'purchased'")
                   if e["review_status"] == "pending"]
        except Exception as e:  # noqa: BLE001
            print("qa_loop sql", e)
            continue
        now = time.time()
        for e in eps:
            eid = int(e["id"])
            ref = e["storage_uri"].rstrip("/").rsplit("/", 1)[-1].lower()
            seen.setdefault(eid, now)
            age, st = now - seen[eid], stage.get(eid, "new")
            base = {"simulated": True, "harness": "tdm-94"}
            try:
                if st == "new" and age >= QA_DELAY_S:
                    if "reject" in ref:
                        orch.call_reducer("record_qa_result", eid, "jerk", False, json.dumps(
                            {**base, "status": "fail", "reason": f"{SIM} max jerk 9.1 rad/s^3 is above the 4.0 limit",
                             "max_jerk": 9.1}))
                        orch.call_reducer("record_qa_result", eid, "deterministic_qa", False, json.dumps(
                            {**base, "status": "fail", "reason": f"{SIM} failed: jerk", "failed": ["jerk"]}))
                        orch.call_reducer("submit_review", eid, False, f"{SIM} jerk: max jerk 9.1 above the 4.0 limit", "{}")
                        stage[eid] = "done"
                        continue
                    for c in ("actions", "duration", "frame_drops", "jerk", "fps", "camera_set", "duplicate"):
                        orch.call_reducer("record_qa_result", eid, c, True, json.dumps({**base, "status": "pass", "reason": ""}))
                    orch.call_reducer("record_qa_result", eid, "deterministic_qa", True, json.dumps(
                        {**base, "status": "pass", "reason": ""}))
                    stage[eid] = "det"
                    if "error" in ref:
                        lq.log_error(orch, e, f"{SIM} APIConnectionError: Claude API unreachable (simulated outage)")
                        stage[eid] = "errored"
                    elif "cap" in ref:
                        lq.log_event(orch, lq.EVENT_CAPPED, {"episode_id": eid, "dataset_offer_id": e["dataset_offer_id"],
                                                             "calls": 6, "cap": 6, "simulated": True,
                                                             "effect": "episode stays pending; raise cost.max_calls_per_offer_* to continue"})
                        stage[eid] = "capped"
                elif st == "errored" and age >= QA_DELAY_S * 3:
                    stage[eid] = "det"
                elif st == "det" and age >= QA_DELAY_S * 2:
                    on_task = "offtask" not in ref
                    orch.call_reducer("record_qa_result", eid, "llm_relevance", on_task, json.dumps({
                        **base, "status": "pass" if on_task else "fail",
                        "reason": "" if on_task else f"{SIM} wrong task: the clip shows stacking blocks, the ticket asks to put the cup in the bin",
                        "on_task": on_task}))
                    orch.call_reducer("record_qa_result", eid, "llm_success", on_task, json.dumps({
                        **base, "status": "pass" if on_task else "fail", "quality": 4 if on_task else 2,
                        "reason": "" if on_task else f"{SIM} not the ticket's task, so success cannot count"}))
                    reason = (f"{SIM} on task; the cup ends in the bin, quality 4/5" if on_task else
                              f"{SIM} rejected: wrong task (stacking blocks, not cup into bin)")
                    orch.call_reducer("submit_review", eid, on_task, reason, json.dumps({"simulated": True}))
                    lq.acceptance_guard(orch, int(e["dataset_offer_id"]), e["ticket_id"], cfg)
                    stage[eid] = "done"
            except Exception as ex:  # noqa: BLE001
                print("qa_loop", eid, ex)
                stage[eid] = "done"


# --------------------------------------------------------------------------- main

def main() -> int:
    if "--make-fixtures" in sys.argv:
        make_fixtures(HERE / "tmp" / "fixtures", ["ep_good_1", "ep_good_2", "ep_reject_jerk", "ep_offtask",
                                                    "ep_error_retry", "ep_cap", "ep_extra"])
        return 0

    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient, create_identity

    import rdf_orchestrator.handlers  # noqa: F401 - registers every handler on the default registry
    from rdf_orchestrator import offers as of
    from rdf_orchestrator.bus import Bus, Registry, registry
    from rdf_orchestrator.handlers import offers as offers_handler
    from rdf_orchestrator.offers_api import offers_router
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    run = ROOT / f"run-{int(time.time())}"
    store = LocalStore(run / "media", f"http://127.0.0.1:{PORT}/tdm94-store")

    orch_id, orch_tok = create_identity(HOST)
    owner_call("register_service_identity", json.dumps({"__identity__": "0x" + orch_id}), '"orchestrator"',
               '"test.tdm94 orchestrator"')
    orch = SpacetimeClient(HOST, DB, orch_tok)
    orch.call_reducer("register_robot", "test.tdm94-yam", "i2rt_yam", "v0", "{}")
    robot_id = int([r for r in orch.sql("SELECT * FROM robot") if r["name"] == "test.tdm94-yam"][-1]["id"])
    desc = f"test.tdm94 put the cup in the bin ({run.name})"
    orch.call_reducer("create_ticket", robot_id, desc, 0.8, 10_000, json.dumps({"target_episodes": 5}))
    ticket_id = int([t for t in orch.sql("SELECT * FROM ticket") if t["task_description"] == desc][0]["id"])
    for i in range(2):
        orch.call_reducer("create_episode", ticket_id, 0, 0, "clean_teleop", f"local://tdm94/existing/{i}",
                          "local://tdm94/existing/actions.parquet", "{}", 300, 10_000, None, None, True, None)
    for e in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {ticket_id}"):
        orch.call_reducer("submit_review", int(e["id"]), True, "existing demo (harness)", "{}")
    print(f"TICKET {ticket_id} (target_episodes 5, 2 accepted) robot {robot_id}", flush=True)

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

    store_app = FastAPI()
    store_app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["PUT", "OPTIONS"], allow_headers=["*"])

    @store_app.put("/o/{key:path}")
    async def store_put(key: str, exp: int, sig: str, request: Request) -> dict:
        if not store.verify(key, exp, sig):
            raise HTTPException(403, "bad signature")
        store.put(key, await request.body())
        return {"ok": True}

    app = FastAPI(lifespan=lifespan)
    app.include_router(offers_router)
    app.mount("/tdm94-store", store_app)
    app.state.offers_service = of.OffersService(
        AsyncSpacetimeClient(HOST, DB, orch_tok), store=store,
        fetch=lambda uri: (store.root / of._uri_key(uri)).read_bytes())

    stop = threading.Event()
    threading.Thread(target=qa_loop, args=(SpacetimeClient(HOST, DB, orch_tok), stop), daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
    finally:
        stop.set()
    _ = httpx
    return 0


if __name__ == "__main__":
    sys.exit(main())
