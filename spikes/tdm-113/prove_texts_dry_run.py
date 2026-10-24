"""TDM-113 dry-run proof: a fake contributor opts in to job texts, a paid task opens, they reply YES by a signed
Inkbox webhook, and the outbox shows the welcome, the offer and the claim link. Nothing is texted (RDF_TEXT_MODE
is forced to dry_run), nothing touches SpacetimeDB or Inkbox (fake claim reducer, fake identity check), and the
number is a fictional 555-01xx one.

Real HTTP on 127.0.0.1:8113 (the orchestrator's /texts/* router alone, in this process). Run:

    cd services/orchestrator && uv run --no-sync python ../../spikes/tdm-113/prove_texts_dry_run.py
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import job_drops as jd
from rdf_orchestrator import text_offers as tx
from rdf_orchestrator.stream_api import get_identity_verifier, get_stream_stdb
from rdf_orchestrator.text_offers_api import get_text_offers, text_offers_router

PORT = 8113
TMP = Path(__file__).resolve().parent / "tmp" / "proof"
SECRET = "whsec_tdm113_proof_only"
ME, TOKEN = "c3" + "ab" * 31, "browser-token-tdm113-proof"
PHONE = "(415) 555-0113"  # fictional


class FakeStdb:
    async def sql(self, q: str) -> list[dict]:
        return [{"id": 7, "identity": "0x" + ME, "kind": "contributor"}]


async def verifier(identity: str, token: str) -> bool:
    return identity == ME and token == TOKEN


TASKS = {42: {"id": 42, "ticket_id": 4, "robot_id": 1, "type": "recovery_takeover", "price_cents": 350,
              "status": "open", "assignee_id": 0, "instructions": "Finish the drop."}}
ROWS = {"task": lambda: list(TASKS.values()),
        "robot": lambda: [{"id": 1, "name": "sim-yam-mujoco", "camera_config_json": json.dumps({"sim": True})}],
        "ticket": lambda: [{"id": 4, "task_description": "Pick up the cube and place it on the plate"}]}
claims: list[tuple[int, int]] = []
events: list[tuple[str, dict]] = []


def rows(table: str) -> list[dict]:
    return [dict(r) for r in ROWS.get(table, lambda: [])()]


def fake_claim(task_id: int, cid: int) -> None:
    t = TASKS[task_id]
    if t["status"] != "open":
        raise RuntimeError("already claimed")
    t.update(status="claimed", assignee_id=cid)
    claims.append((task_id, cid))


def _no_remote_writer():
    raise RuntimeError("TDM-113 proof: no SpacetimeDB writes allowed")


def main() -> int:
    dsp.make_writer = _no_remote_writer  # any accidental reducer call fails loudly instead of reaching maincloud
    jd.set_dropper(None)
    TMP.mkdir(parents=True, exist_ok=True)
    for f in TMP.glob("*"):
        f.unlink()
    s = tx.TextSettings(_env_file=None, rdf_text_mode="dry_run", rdf_text_store=str(TMP / "texts.sqlite3"),
                        rdf_text_outbox=str(TMP / "outbox.jsonl"), rdf_text_webhook_secret=SECRET)
    ds = jd.DropSettings(_env_file=None, rdf_web_public_url="https://geonosis.jaxonp.com",
                         rdf_drop_invite_secret="tdm113-proof-invite-secret")  # not the real secret: links are dead
    offers = tx.TextOffers(s, link=tx.InkboxLink(s, client=object(), state_path=TMP / "inkbox.json"),
                           append=lambda t, p: events.append((t, p)), claim=fake_claim, heartbeat=lambda t: None,
                           rows=rows, drop_settings=ds, inline=True,
                           record_claim=lambda t, c, src: events.append(("job_drop.claimed", {"task_id": t, "src": src})))
    assert offers.transport() is tx.dry_run_transport
    app = FastAPI()
    app.include_router(text_offers_router)
    app.dependency_overrides[get_stream_stdb] = lambda: FakeStdb()
    app.dependency_overrides[get_identity_verifier] = lambda: verifier
    app.dependency_overrides[get_text_offers] = lambda: offers
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{PORT}"
    me = {"identity": ME, "spacetime_token": TOKEN}
    out: dict = {}
    try:
        with httpx.Client(base_url=base, timeout=10) as c:
            # 1. /join opt-in (what TextOptIn posts through /api/tdm113/optin)
            r = c.post("/texts/optin", json={**me, "phone": PHONE, "in_person": False, "consent": True,
                                              "consent_text": tx.CONSENT_TEXT})
            out["1_optin"] = [r.status_code, r.json()]
            # 2. a paid task opens: the dispatcher's page listeners run (TDM-65 push, TDM-108 drops, TDM-113 texts)
            out["2_page"] = offers.on_page(dsp.PageRequest(4, [42], "recovery_takeover", 350, []), rows)
            # 3. the phone replies YES: Inkbox's signed imessage.received webhook
            body = json.dumps({"id": "evt_proof_yes", "event_type": "imessage.received", "timestamp": "",
                               "data": {"message": {"id": "m1", "conversation_id": "c1", "direction": "inbound",
                                                    "remote_number": "+14155550113", "content": "Yes", "is_group": False},
                                        "reaction": None, "contacts": [], "agent_identities": []}}).encode()
            now = int(time.time())
            r = c.post("/texts/webhook", content=body, headers=tx.sign_webhook(SECRET, body, request_id="r-yes", ts=now))
            out["3_webhook_signed_yes"] = [r.status_code, r.json()]
            r = c.post("/texts/webhook", content=body, headers=tx.sign_webhook(SECRET, body, request_id="r-yes", ts=now))
            out["3b_replay_same_event"] = [r.status_code, r.json()]
            forged = tx.sign_webhook("whsec_attacker", body, request_id="r-x", ts=now)
            r = c.post("/texts/webhook", content=body.replace(b"evt_proof_yes", b"evt_forged"), headers=forged)
            out["4_webhook_forged"] = [r.status_code, r.json()]
            stale = tx.sign_webhook(SECRET, body, request_id="r-old", ts=now - 600)
            r = c.post("/texts/webhook", content=body, headers=stale)
            out["5_webhook_stale"] = [r.status_code, r.json()]
            r = c.post("/texts/state", json=me)
            out["6_state"] = [r.status_code, r.json()]
    finally:
        server.should_exit = True
        th.join(timeout=5)
    out["claims"] = claims
    out["events"] = [{"type": t, "payload": p} for t, p in events]
    out["outbox"] = offers.outbox.tail(10)
    print(json.dumps(out, indent=1, default=str))
    ok = (out["1_optin"][0] == 200 and out["2_page"]["count"] == 1 and out["3_webhook_signed_yes"][0] == 200
          and out["4_webhook_forged"][0] == 401 and out["5_webhook_stale"][0] == 401 and claims == [(42, 7)]
          and [m["kind"] for m in out["outbox"]] == ["welcome", "offer", "claimed"]
          and all(m["state"] == "dry_run" for m in out["outbox"]) and "555" not in json.dumps(out["events"]))
    print("PROOF", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
