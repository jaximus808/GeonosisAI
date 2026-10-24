"""TDM-29 spike, part 2: Factory cloud (Droid Computer from an Environment) + Sessions API.

Tests whether a *cloud* session can sit idle, let its computer auto-pause, and then be
resumed by a plain HTTP call (POST /sessions/{id}/messages) with a new message, keeping
conversation history and the computer's working tree.

Usage:
  .venv/bin/python cloud_api_resume.py computer            # create Droid Computer from our Environment
  .venv/bin/python cloud_api_resume.py start <computerId>  # create session + turn 1
  .venv/bin/python cloud_api_resume.py resume <sessionId>  # turn 2 (the resume-bridge call)
  .venv/bin/python cloud_api_resume.py status <sessionId> [computerId]
  .venv/bin/python cloud_api_resume.py delete-computer <computerId>

Every request/response summary is appended to out/cloud_api.jsonl. The API key comes from
the repo .env and is never printed.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE / "out"
load_dotenv(REPO / ".env")

API = "https://api.factory.ai/api/v0"
ENVIRONMENT_ID = os.environ.get("FACTORY_ENVIRONMENT_ID", "173ec531-f4f8-46e0-b246-6594c9590ea5")
MODEL = "gpt-5.4-mini"
CWD = "/home/factory-user/tdm29"


def client() -> httpx.Client:
    key = os.environ["FACTORY_API_KEY"]
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=120)


def log(event: str, **data) -> None:
    OUT.mkdir(exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **data}
    print(json.dumps(rec)[:4000])
    with open(OUT / "cloud_api.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def call(c: httpx.Client, method: str, path: str, **kw) -> dict:
    r = c.request(method, path, **kw)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:2000]}
    log("http", method=method, path=path, status=r.status_code, body=body)
    return body if r.is_success else {"_error": r.status_code, **(body if isinstance(body, dict) else {})}


def wait_idle(c: httpx.Client, sid: str, limit_s: int = 300) -> dict:
    t0 = time.time()
    seen_running = False
    while time.time() - t0 < limit_s:
        s = c.get(f"/sessions/{sid}").json()
        st = s.get("status")
        seen_running |= st in ("running", "pending")
        if st == "idle" and (seen_running or time.time() - t0 > 20):
            log("idle", session=sid, seconds=round(time.time() - t0, 1), messageCount=s.get("messageCount"))
            return s
        time.sleep(3)
    log("wait_timeout", session=sid)
    return {}


def last_assistant(c: httpx.Client, sid: str) -> list[str]:
    m = c.get(f"/sessions/{sid}/messages", params={"limit": "100"}).json()
    texts = []
    for msg in m.get("messages", m.get("items", [])):
        if msg.get("role") != "assistant":
            continue
        for part in msg.get("content", []) if isinstance(msg.get("content"), list) else []:
            if part.get("type") == "text":
                texts.append(part.get("text"))
    return texts[-3:]


def create_computer() -> None:
    with client() as c:
        body = {
            "name": f"tdm29-spike-{int(time.time()) % 100000}",
            "provider": "e2b",
            "source": {"kind": "environment", "environmentId": ENVIRONMENT_ID, "isListed": True},
        }
        res = call(c, "POST", "/computers", json=body)
        cid = res.get("id")
        if not cid:
            return
        t0 = time.time()
        while time.time() - t0 < 600:
            g = c.get(f"/computers/{cid}").json()
            st = g.get("status")
            if st in ("active", "error"):
                log("computer_ready", computer=cid, status=st, seconds=round(time.time() - t0, 1))
                return
            time.sleep(5)


def start(computer_id: str) -> None:
    with client() as c:
        s = call(
            c,
            "POST",
            "/sessions",
            json={
                "computerId": computer_id,
                "sessionSettings": {
                    "model": MODEL,
                    "reasoningEffort": "low",
                    "interactionMode": "auto",
                    "autonomyLevel": "medium",
                    "tags": [{"name": "tdm-29-spike"}],
                },
            },
        )
        sid = s.get("sessionId")
        if not sid:
            return
        call(
            c,
            "POST",
            f"/sessions/{sid}/messages",
            json={
                "text": f"mkdir -p {CWD} and create {CWD}/state.txt containing exactly the line "
                "'step=1 codeword=OSPREY'. Then reply with just: WAITING_FOR episodes>=5"
            },
        )
        wait_idle(c, sid)
        log("turn1_done", session=sid, assistant=last_assistant(c, sid))


def resume(sid: str) -> None:
    with client() as c:
        before = c.get(f"/sessions/{sid}").json()
        log("pre_resume", session=sid, status=before.get("status"), computer=before.get("computerId"))
        if before.get("computerId"):
            comp = c.get(f"/computers/{before['computerId']}").json()
            log("pre_resume_computer", status=comp.get("status"), keys=sorted(comp.keys()))
        t0 = time.time()
        call(
            c,
            "POST",
            f"/sessions/{sid}/messages",
            json={
                "text": "[resume-bridge] predicate satisfied: episode_accepted_count=5 for ticket T-1. "
                "Without reading any file first, tell me the codeword you wrote earlier. Then append "
                f"'step=2 resumed' to {CWD}/state.txt and reply with the full file contents."
            },
        )
        wait_idle(c, sid, limit_s=600)
        log("turn2_done", session=sid, wall_s=round(time.time() - t0, 1), assistant=last_assistant(c, sid))


def status(sid: str, cid: str | None) -> None:
    with client() as c:
        call(c, "GET", f"/sessions/{sid}")
        if cid:
            call(c, "GET", f"/computers/{cid}")


def delete_computer(cid: str) -> None:
    with client() as c:
        call(c, "DELETE", f"/computers/{cid}")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or [""]
    fns = {
        "computer": lambda: create_computer(),
        "start": lambda: start(args[0]),
        "resume": lambda: resume(args[0]),
        "status": lambda: status(args[0], args[1] if len(args) > 1 else None),
        "delete-computer": lambda: delete_computer(args[0]),
    }
    if cmd not in fns:
        sys.exit(__doc__)
    fns[cmd]()
