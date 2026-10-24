"""TDM-93 proof: LLM QA on labelled FIXTURE purchased episodes, end to end through SpacetimeDB.

Boots a throwaway local SpacetimeDB (127.0.0.1:3093, db tdm93llmqa, data under $TMPDIR/tdm-93; never
maincloud) with the TDM-28 ``LocalStack`` harness, publishes the module, then:

  dataset_seller signs up (own identity) -> robot + cup ticket (target_episodes 5) -> create + accept a
  dataset offer (needed_count 5) -> ingest 5 purchased FIXTURE episodes -> a Bus running ONLY the
  ``tdm93_llm_qa`` handler subscribes -> a TDM-92 STAND-IN writes the deterministic qa_result rows,
  with ``deterministic_qa`` last (labelled ``simulated: TDM-92 stand-in`` in detail_json, because TDM-92
  is still in flight) -> the handler gates on that row and calls REAL Claude (unless --scripted).

  cup_demo        gate pass -> Claude -> ACCEPT
  block_stacking  gate pass -> Claude -> REJECT llm_relevance, and the reason names the mismatch
  cup_missed      gate pass -> Claude -> REJECT llm_success
  gate_failed     deterministic_qa=false + the stand-in's reject -> the handler never calls Claude
  outage          gate pass -> scripted API outage -> qa.error event, NO review, still pending;
                  then the retry (run_after_deterministic) -> ACCEPT

Then ``finalize_offer`` settles the offer: accepted_count 2, payout 2 x $5.

    cd services/orchestrator && uv run python scripts/tdm93_prove_llm_qa.py [--scripted] [--keep]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(HERE.parents[1] / "tests"))

import tdm93_fixtures as f93  # noqa: E402
from harness import LocalStack  # noqa: E402
from rdf_common.spacetime_client import SpacetimeClient  # noqa: E402

from rdf_orchestrator import llm_qa as lq  # noqa: E402

PORT, DB = 3093, "tdm93llmqa"
PRICE = 500
STANDIN = {"simulated": "TDM-92 stand-in (tdm93 proof); TDM-92's real checks were not run"}


class ProofJudge:
    """Real Claude (or the scripted judge), plus one scripted outage for the `outage` episode."""

    def __init__(self, inner, outage_ids: set[int]):
        self.inner, self.outage_ids, self.model = inner, outage_ids, inner.model
        self.calls = 0

    @property
    def last_usage(self):
        return getattr(self.inner, "last_usage", None)

    def judge(self, messages):
        import re

        eid = int(re.search(r"Purchased episode (\d+)", messages[0]["content"][0]["text"]).group(1))
        if eid in self.outage_ids:
            self.outage_ids.discard(eid)
            raise lq.LlmQaError("Claude API error: APIConnectionError: scripted outage (tdm93 proof)")
        self.calls += 1
        return self.inner.judge(messages)


def start_bus(st: LocalStack, cfg: lq.LlmQaConfig, judge) -> None:
    from rdf_orchestrator.bus import Bus, Registry
    from rdf_orchestrator.bus import registry as default_registry
    from rdf_orchestrator.handlers import llm_qa as h
    from rdf_orchestrator.stdb import SpacetimeClient as BusClient

    reg = Registry()
    reg.handlers = [x for x in default_registry.handlers if x.name == "tdm93_llm_qa"]
    for t in ("episode", "ticket", "qa_result"):
        reg.watch(t)
    h.make_config = lambda: cfg
    h.make_judge = lambda c: judge
    h.make_stdb = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))

    def run():
        async def go():
            bus = Bus(reg, BusClient(host=st.host, db=st.db, token=st.token("orchestrator")), workers=4)
            await bus.start()
            while True:
                await asyncio.sleep(3600)
        asyncio.run(go())

    threading.Thread(target=run, name="tdm93-bus", daemon=True).start()
    time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scripted", action="store_true", help="scripted judge instead of real Claude")
    ap.add_argument("--keep", action="store_true", help="leave the local SpacetimeDB running")
    args = ap.parse_args()

    out = REPO / "spikes" / "tdm-93-llm-qa" / "tmp" / "proof"
    fixtures = f93.build_fixture_set(out)
    extra = {}
    for name, seed in (("gate_failed", 21), ("outage", 22)):
        ep, files = f93.make_episode(out, name, f93.CUP_PLAN, "cup", seed=seed)
        extra[name] = f93.Fixture(name, ep, name == "outage", None, ("",), files)
    allfx = fixtures + list(extra.values())

    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-93")
    st.start_server()
    try:
        st.publish()
        st.make_identity("orchestrator")
        orch = st.client("orchestrator")

        req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            seller_token = json.loads(r.read())["token"]
        with SpacetimeClient(st.host, st.db, seller_token) as c:
            c.call_reducer("register_contributor", "test.tdm93 seller", "", [], "dataset_seller", 0)
        seller_id = int(st.newest("contributor")["id"])
        orch.call_reducer("register_robot", "test.tdm93-yam", "i2rt_yam", "v0", "{}")
        robot_id = int(st.newest("robot")["id"])
        orch.call_reducer("create_ticket", robot_id, f"test.tdm93 {f93.TASK}", 0.8, 10_000, "")
        ticket = st.newest("ticket")
        ticket_id = int(ticket["id"])
        orch.call_reducer("create_dataset_offer", ticket_id, seller_id, PRICE, {"some": "fixture://tdm93-seller"})
        offer = st.newest("dataset_offer")
        offer_id = int(offer["id"])
        orch.call_reducer("accept_dataset_offer", offer_id)
        print(f"ticket {ticket_id}, offer {offer_id}: needed_count {offer['needed_count']}")

        cfg = lq.load_config()
        if args.scripted:
            from test_tdm93_llm_qa import ScriptedJudge

            inner = ScriptedJudge()
        else:
            inner = lq.ClaudeLlmQaJudge(cfg)
        judge = ProofJudge(inner, set())
        start_bus(st, cfg, judge)

        by_episode: dict[int, f93.Fixture] = {}
        for f in allfx:
            e = f.episode
            orch.call_reducer("ingest_purchased_episode", offer_id, e["storage_uri"], e["actions_uri"],
                              e["cameras_json"], e["frame_count"], e["duration_ms"], {"some": True})
            eid = int(st.newest("episode")["id"])
            by_episode[eid] = f
            if args.scripted:
                inner.verdicts[eid] = {"block_stacking": "off_task", "cup_missed": "failed"}.get(f.name, "good")
            if f.name == "outage":
                judge.outage_ids.add(eid)

        # TDM-92 stand-in: per-check rows, then the summary row LAST (the gate contract).
        for eid, f in by_episode.items():
            ok = f.name != "gate_failed"
            for check in ("embodiment", "camera_set", "duration", "jerk", "static"):
                orch.call_reducer("record_qa_result", eid, check, True, json.dumps(STANDIN))
            orch.call_reducer("record_qa_result", eid, "duplicate", ok,
                              json.dumps({**STANDIN, **({} if ok else {"reason": "same frames as an episode already bought"})}))
            orch.call_reducer("record_qa_result", eid, "deterministic_qa", ok, json.dumps(STANDIN))
            if not ok:
                orch.call_reducer("submit_review", eid, False, "Rejected by deterministic QA: duplicate",
                                  json.dumps({"reviewer": "tdm92-standin", **STANDIN}))

        def settled() -> bool:
            reviews = {int(r["episode_id"]) for r in st.sql("SELECT * FROM review")}
            errs = [e for e in st.events() if e["type"] == "qa.error"]
            return len(reviews) >= 4 and len(errs) >= 1

        deadline = time.time() + 600
        while time.time() < deadline and not settled():
            time.sleep(1)

        outage_id = next(e for e, f in by_episode.items() if f.name == "outage")
        gate_id = next(e for e, f in by_episode.items() if f.name == "gate_failed")
        ok = True

        def qa_of(eid):
            return {r["check_name"]: r["passed"] for r in st.sql(f"SELECT * FROM qa_result WHERE episode_id = {eid}")}

        errs = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "qa.error"]
        pending = st.sql(f"SELECT * FROM episode WHERE id = {outage_id}")[0]["review_status"]
        print(f"\noutage: qa.error events {len(errs)} -> {errs[:1]}; review_status={pending}; "
              f"llm rows={[k for k in qa_of(outage_id) if k.startswith('llm_')]}")
        ok &= len(errs) == 1 and errs[0]["episode_id"] == outage_id and pending == "pending"
        ok &= not any(k.startswith("llm_") for k in qa_of(outage_id))
        ok &= not st.sql(f"SELECT * FROM review WHERE episode_id = {outage_id}")
        print(f"gate_failed: llm rows={[k for k in qa_of(gate_id) if k.startswith('llm_')]} (Claude never called)")
        ok &= not any(k.startswith("llm_") for k in qa_of(gate_id))

        # retry after the outage (what the next boot does)
        d = lq.run_after_deterministic(orch, st.sql(f"SELECT * FROM episode WHERE id = {outage_id}")[0], ticket,
                                       cfg=cfg, judge=judge)
        print(f"outage retry: accepted={d and d.accepted}")

        print(f"\n{'fixture':16s} {'episode':>7s}  verdict  llm_relevance llm_success  reason")
        reviews = {int(r["episode_id"]): r for r in st.sql("SELECT * FROM review")}
        for eid, f in by_episode.items():
            r = reviews.get(eid)
            q = qa_of(eid)
            verdict = "ACCEPT" if r and r["accepted"] else ("reject" if r else "MISSING")
            print(f"{f.name:16s} {eid:7d}  {verdict:7s}  {str(q.get('llm_relevance', '-')):13s} "
                  f"{str(q.get('llm_success', '-')):11s}  {(r or {}).get('reason', '')}")
            good = r is not None and bool(r["accepted"]) == f.expect_accept
            if f.expect_failed_check:
                good &= q.get(f.expect_failed_check) is False
                good &= any(w in r["reason"].lower() for w in f.expect_reason)
            if f.expect_accept:
                good &= q.get("llm_relevance") is True and q.get("llm_success") is True
            if not good:
                print(f"   ^ UNEXPECTED for {f.name}")
            ok &= good

        rates = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "qa.acceptance_rate"]
        print(f"\nqa.acceptance_rate (last): {rates[-1] if rates else None}")
        ok &= bool(rates) and rates[-1]["reviewed"] == 5 and rates[-1]["accepted"] == 2 and not rates[-1]["suspect"]
        print(f"Claude calls: {judge.calls} (+1 scripted outage); cap per offer {lq.call_cap(offer, cfg)}")
        ok &= judge.calls == 4

        orch.call_reducer("finalize_offer", offer_id)
        o = st.sql(f"SELECT * FROM dataset_offer WHERE id = {offer_id}")[0]
        settle = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "dataset_offer.settled"]
        print(f"finalize_offer: status={o['status']} accepted_count={o['accepted_count']} "
              f"ingested_count={o['ingested_count']} payout_cents={settle[-1].get('payout_cents') if settle else None}")
        ok &= o["status"] == "done" and int(o["accepted_count"]) == 2
        print("\nOK" if ok else "\nFAILED")
        return 0 if ok else 1
    finally:
        if not args.keep:
            st.stop()


if __name__ == "__main__":
    sys.exit(main())
