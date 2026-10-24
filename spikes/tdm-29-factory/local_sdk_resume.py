"""TDM-29 spike, part 1: local droid-sdk session -> close -> Session.resume(id) with a new message.

Proves (or disproves) that a droid session can be "parked" (process exits, nothing
running, nothing billed) and continued later by id with a new message, keeping its
conversation history and working tree.

Usage:
  .venv/bin/python local_sdk_resume.py start            # turn 1, prints session id, exits
  .venv/bin/python local_sdk_resume.py resume <id>      # turn 2 on the same session id

FACTORY_API_KEY is read from the repo .env (never printed).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / ".env")

from droid_sdk import (  # noqa: E402
    Autonomy,
    InteractionHandlers,
    PermissionRequest,
    PermissionResponse,
    ReasoningEffort,
    Session,
    SessionConfig,
    ToolConfirmationOutcome,
)
from droid_sdk.permissions import CreateFile, EditAction  # noqa: E402

HERE = Path(__file__).resolve().parent
WORK = HERE / "work"
OUT = HERE / "out"
MODEL = "gpt-5.4-mini"  # cheap model; keep credit use modest


def approve_file_edits(request: PermissionRequest) -> PermissionResponse:
    """Allow file create/edit only (the trivial task needs nothing else)."""
    if request.actions and all(isinstance(a, (CreateFile, EditAction)) for a in request.actions):
        return request.respond(ToolConfirmationOutcome.PROCEED_ONCE)
    return request.respond(ToolConfirmationOutcome.CANCEL)


HANDLERS = InteractionHandlers(on_permission=approve_file_edits)


async def run_turn(session: Session, prompt: str) -> dict:
    t0 = time.time()
    async with session.stream(prompt, timeout=180) as stream:
        async for _ in stream:
            pass
    r = stream.result
    usage = getattr(r, "usage", None)
    return {
        "session_id": session.id,
        "subtype": r.subtype,
        "success": r.success,
        "text": getattr(r, "text", None),
        "seconds": round(time.time() - t0, 1),
        "factory_credits": getattr(usage, "factory_credits", None) if usage else None,
        "input_tokens": getattr(usage, "input_tokens", None) if usage else None,
        "output_tokens": getattr(usage, "output_tokens", None) if usage else None,
    }


async def start() -> None:
    WORK.mkdir(exist_ok=True)
    cfg = SessionConfig(autonomy=Autonomy.LOW, disabled_tools={"Execute"})
    async with Session(
        cwd=WORK, model=MODEL, reasoning_effort=ReasoningEffort.LOW, config=cfg,
        interactions=HANDLERS,
    ) as s:
        res = await run_turn(
            s,
            "Create a file named state.txt in the current directory containing exactly "
            "the line 'step=1 codeword=PELICAN'. Then reply with just: WAITING_FOR episodes>=5",
        )
    res["phase"] = "start"
    res["closed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _log(res)


async def resume(session_id: str) -> None:
    async with Session.resume(session_id, interactions=HANDLERS, disabled_tools={"Execute"}) as s:
        res = await run_turn(
            s,
            "[resume-bridge] predicate satisfied: episode_accepted_count=5 for ticket T-1. "
            "Without reading any file first, tell me the codeword you wrote earlier. Then read "
            "state.txt, append the line 'step=2 resumed', and reply with the full file contents.",
        )
        res["cwd_after_resume"] = str(s.cwd)
    res["phase"] = "resume"
    res["state_txt"] = (WORK / "state.txt").read_text() if (WORK / "state.txt").exists() else None
    _log(res)


def _log(res: dict) -> None:
    OUT.mkdir(exist_ok=True)
    line = json.dumps(res)
    print(line)
    with open(OUT / "local_sdk_resume.jsonl", "a") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    if sys.argv[1:2] == ["start"]:
        asyncio.run(start())
    elif sys.argv[1:2] == ["resume"] and len(sys.argv) == 3:
        asyncio.run(resume(sys.argv[2]))
    else:
        sys.exit(__doc__)
