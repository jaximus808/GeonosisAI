"""TDM-29 / S-04 deliverable: start a Factory Droid session, park it, resume it with a message.

This is the shape the orchestrator's resume bridge (TDM-27) should copy.

Protocol A (chosen, native resume): a droid "pauses" by ending its turn after reporting
`WAITING_FOR <predicate>`; the session goes `idle` and costs nothing while idle (its
Droid Computer auto-pauses). When the predicate becomes true the bridge POSTs a new
message to the SAME session id. History, cwd and the computer's working tree are kept.

Protocol B (fallback, end-and-restart): if the session or its computer is gone
(404 / computer error), start a NEW session seeded with the `resume_payload` the droid
last reported, and record the new id in agent_session.factory_session_ids[].

Backends:
  --backend cloud  Factory Sessions API over HTTP (api.factory.ai/api/v0). Sessions run on a
                   Droid Computer created from a Factory Environment. No local process.
  --backend local  droid-sdk (pip install droid-sdk) driving a local `droid` subprocess;
                   resume = droid_sdk.Session.resume(session_id).

Usage (from spikes/tdm-29-factory, venv has droid-sdk + httpx + python-dotenv):
  .venv/bin/python spike_factory.py start  --backend cloud --computer <computerId>
  .venv/bin/python spike_factory.py resume --backend cloud --session <sessionId> \
      --message "predicate true: 5 accepted episodes" [--payload '{"next":"train"}' --computer <id>]
  .venv/bin/python spike_factory.py start  --backend local
  .venv/bin/python spike_factory.py resume --backend local --session <sessionId> --message "..."

FACTORY_API_KEY comes from the repo .env and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parents[1] / ".env")

API = "https://api.factory.ai/api/v0"
MODEL = os.environ.get("FACTORY_SPIKE_MODEL", "gpt-5.4-mini")
CLOUD_CWD = "/home/factory-user/tdm29"

DROID_CONTRACT = (
    "You are a long-running factory droid. When you need something from the physical world, "
    "write your state to resume_payload.json in the working directory, then end your turn with a "
    "final line exactly like: WAITING_FOR <table>:<predicate>. The orchestrator will message you "
    "when the predicate is true; continue from resume_payload.json."
)
FIRST_TASK = (
    f"{DROID_CONTRACT}\n\nTask: in {{cwd}} create notes.txt with the line 'step=1 adapter drafted'. "
    "Then wait for 5 accepted episodes for ticket T-1."
)
WAITING_RE = re.compile(r"WAITING_FOR\s+(\S+)")


@dataclass
class Outcome:
    backend: str
    protocol: str  # "start" | "A-native-resume" | "B-restart"
    session_id: str
    waiting_for: str | None
    text: str
    credits: float | None
    seconds: float
    extra: dict = field(default_factory=dict)


def record(o: Outcome) -> None:
    (HERE / "out").mkdir(exist_ok=True)
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **asdict(o)})
    print(line)
    with open(HERE / "out" / "spike_factory.jsonl", "a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------------- cloud backend
class Cloud:
    def __init__(self) -> None:
        self.c = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {os.environ['FACTORY_API_KEY']}"},
            timeout=120,
        )

    def create_session(self, computer_id: str) -> str:
        r = self.c.post("/sessions", json={"computerId": computer_id, "sessionSettings": {
            "model": MODEL, "reasoningEffort": "low", "interactionMode": "auto",
            "autonomyLevel": "medium", "tags": [{"name": "tdm-29-spike"}]}})
        r.raise_for_status()
        return r.json()["sessionId"]

    def post(self, sid: str, text: str) -> httpx.Response:
        # A just-created session can 404 for a moment under concurrency: retry briefly.
        for attempt in range(5):
            r = self.c.post(f"/sessions/{sid}/messages", json={"text": text})
            if r.status_code != 404 or attempt == 4:
                return r
            time.sleep(1 + attempt)
        return r

    def wait_turn(self, sid: str, limit_s: float = 600) -> dict:
        t0, seen_busy = time.time(), False
        while time.time() - t0 < limit_s:
            s = self.c.get(f"/sessions/{sid}").json()
            seen_busy |= s.get("status") in ("pending", "running")
            if s.get("status") == "idle" and (seen_busy or time.time() - t0 > 30):
                return s
            time.sleep(3)
        raise TimeoutError(sid)

    def last_text(self, sid: str) -> str:
        msgs = self.c.get(f"/sessions/{sid}/messages", params={"limit": "20", "role": "assistant"}).json()
        for m in msgs.get("messages", []):  # newest first
            for p in m.get("content") or []:
                if p.get("type") == "text" and p.get("text", "").strip():
                    return p["text"]
        return ""

    def turn(self, sid: str, text: str, protocol: str, **extra) -> Outcome:
        t0 = time.time()
        r = self.post(sid, text)
        r.raise_for_status()
        s = self.wait_turn(sid)
        out = self.last_text(sid)
        m = WAITING_RE.search(out)
        return Outcome("cloud", protocol, sid, m.group(1) if m else None, out[-600:],
                       s.get("factoryCredits"), round(time.time() - t0, 1),
                       {"post": r.json(), **extra})


def cloud_start(computer: str) -> None:
    cl = Cloud()
    sid = cl.create_session(computer)
    record(cl.turn(sid, FIRST_TASK.format(cwd=CLOUD_CWD), "start", computer=computer))


def cloud_resume(sid: str, message: str, payload: str | None, computer: str | None) -> None:
    cl = Cloud()
    msg = f"[resume-bridge] {message}"
    r = cl.c.get(f"/sessions/{sid}")
    if r.status_code == 200:
        record(cl.turn(sid, msg, "A-native-resume", status_before=r.json().get("status")))
        return
    # Protocol B: session is gone -> new session seeded with the last resume_payload.
    if not computer:
        raise SystemExit(f"session {sid} not resumable ({r.status_code}) and no --computer for fallback")
    new_sid = cl.create_session(computer)
    seeded = (f"{DROID_CONTRACT}\n\nYou are continuing agent work from a previous session ({sid}). "
              f"resume_payload: {payload or '{}'}\n\n{msg}")
    record(cl.turn(new_sid, seeded, "B-restart", previous_session=sid))


# ----------------------------------------------------------------------------- local backend
async def local_turn(session, prompt: str, protocol: str) -> Outcome:
    t0 = time.time()
    async with session.stream(prompt, timeout=300) as stream:
        async for _ in stream:
            pass
    res = stream.result
    text = getattr(res, "text", "") or ""
    m = WAITING_RE.search(text)
    usage = getattr(res, "usage", None)
    return Outcome("local", protocol, session.id, m.group(1) if m else None, text[-600:],
                   getattr(usage, "factory_credits", None), round(time.time() - t0, 1))


def _local_handlers():
    from droid_sdk import InteractionHandlers, ToolConfirmationOutcome
    from droid_sdk.permissions import CreateFile, EditAction

    def approve(req):
        ok = req.actions and all(isinstance(a, (CreateFile, EditAction)) for a in req.actions)
        return req.respond(ToolConfirmationOutcome.PROCEED_ONCE if ok else ToolConfirmationOutcome.CANCEL)

    return InteractionHandlers(on_permission=approve)


async def local_start() -> None:
    from droid_sdk import Autonomy, ReasoningEffort, Session, SessionConfig

    work = HERE / "work"
    work.mkdir(exist_ok=True)
    cfg = SessionConfig(autonomy=Autonomy.LOW, disabled_tools={"Execute"})
    async with Session(cwd=work, model=MODEL, reasoning_effort=ReasoningEffort.LOW,
                       config=cfg, interactions=_local_handlers()) as s:
        record(await local_turn(s, FIRST_TASK.format(cwd="the current directory"), "start"))
    # leaving the context closes the droid subprocess: nothing runs while we wait


async def local_resume(sid: str, message: str) -> None:
    from droid_sdk import Session

    async with Session.resume(sid, interactions=_local_handlers(), disabled_tools={"Execute"}) as s:
        record(await local_turn(s, f"[resume-bridge] {message}", "A-native-resume"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["start", "resume"])
    ap.add_argument("--backend", choices=["cloud", "local"], default="cloud")
    ap.add_argument("--computer")
    ap.add_argument("--session")
    ap.add_argument("--message", default="predicate true: episode_accepted_count=5 for ticket T-1. "
                    "Append 'step=2 resumed' to notes.txt and reply with its contents.")
    ap.add_argument("--payload")
    a = ap.parse_args()
    if a.backend == "cloud":
        if a.cmd == "start":
            cloud_start(a.computer or ap.error("--computer required"))
        else:
            cloud_resume(a.session or ap.error("--session required"), a.message, a.payload, a.computer)
    else:
        if a.cmd == "start":
            asyncio.run(local_start())
        else:
            asyncio.run(local_resume(a.session or ap.error("--session required"), a.message))


if __name__ == "__main__":
    main()
