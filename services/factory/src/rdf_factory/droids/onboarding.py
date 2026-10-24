"""TDM-32 Onboarding Droid: write or validate a robot's adapter config, check it, and write the
ticket's success check and reset routine.

When a ticket enters ``onboarding`` (its robot has no ``adapter_version`` yet), the droid:
  1. probes the ticket, the registered robot row and the repo's adapters / camera / safety configs,
  2. writes (or validates an existing) ``adapter_config.yaml``: which adapter, its kwargs, the
     safety profile plus optional TIGHTEN-ONLY overrides, the camera rig config, and why,
  3. runs the onboarding check (``rdf_robot_client.onboarding``): the adapter against the
     ``ArmAdapter`` contract (live through the TDM-45 SafetyLayer on a simulated arm), the camera
     rig config (+ a synthetic capture on sim), the safety-limit sanity (overrides may only
     tighten), and an FK -> IK -> FK round trip; it fixes what the errors name and re-checks,
  4. writes ``success_check.yaml`` for the ticket's task: a spec the OPERATOR applies
     (``decided_by: operator``, ``auto_decide: false``); nothing decides success automatically,
  5. writes ``reset_routine.yaml`` (home pose, gripper, what the person puts back) and
     ``onboarding_report.md``,
  6. publishes. Simulated robot: ``robot.adapter_version`` becomes ``<adapter>+onb.t<ticket>`` and
     ``robot.camera_config_json.onboarding`` carries the spec, routine and check summary; the
     flywheel's ``onboarded`` condition then moves the ticket to ``baseline``. Hardware robot: the
     check never connects or moves the arm; publish logs ``onboarding.hil_requested`` and the droid
     parks on ``WAITING_FOR {"table":"event_log","type":"onboarding.hil_confirmed.t<ticket>"}``
     until a person runs the motion checks (``rdf_robot_client.onboarding_hil``); the TDM-27 bridge
     wakes it and the second publish marks the robot onboarded.

Bounds of an onboarding-droid session (local mode): ``write_roots`` = its workdir + its ticket dir
``work/sessions/onboarding-t<ticket>/`` only; ``allow_commands`` = ``TOOL_PREFIX`` only;
``max_turns`` = ``MAX_TURNS``; droid-sdk autonomy ``off`` (so every edit and command goes through
that policy). The tool enforces the rules in code (DB target, tighten-only safety, sim never
onboards a hardware robot, operator-decided success, hardware never moved).

Flywheel wiring: ``RDF_ONBOARDING_DROID=1`` registers ``OnboardingDroidStep`` for the
``onboarding`` stage (``handlers/flywheel.py``) in place of the ``step.not_implemented`` stub.
By hand: ``uv run --project services/factory python -m rdf_factory.droids.onboarding <ticket_id>``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("rdf_factory.droids.onboarding")

ROLE = "onboarding_droid"
ACTOR = "droid:onboarding"
MAX_TURNS = 4  # start (sim: the whole job) + HIL wake + slack for a missed final line
TOOL_MODULE = "rdf_robot_client.onboarding_tool"
TOOL_PREFIX = f"uv run --frozen --project services/robot-client python -m {TOOL_MODULE}"  # from the repo root


def ticket_dir(work_root: Path, ticket_id: int) -> Path:
    return Path(work_root).resolve() / f"onboarding-t{int(ticket_id)}"


BRIEF = """You are the Onboarding Droid for ticket {ticket_id}. Before this ticket can run, its robot's adapter,
camera rig and safety limits must be checked against our robot client, and the ticket needs a success check
and a reset routine. You do that engineering. {note}

Your ticket dir (every file you write goes here): {tdir}
Your only shell tool (run from the repo root, exactly this prefix; no cd, pipes, redirects or other commands):
  {tool} <probe|check|publish> --ctx {ctx}
Each prints one JSON object.

Steps:
1. `probe`. It prints the ticket task, the robot row, the mode (sim or HARDWARE), a draft adapter config, the
   drafted adapter's facts (home_joints, limits), file templates and the exact paths to write.
2. Write {tdir}/adapter_config.yaml. Start from draft_adapter_config. Keep a value unless the robot row or the
   facts give you a reason; `rationale` = 2-5 lines saying why this adapter / kwargs / safety profile fit this
   robot. `safety_overrides` is optional and may only TIGHTEN the profile (lower caps or speed, larger margin,
   smaller workspace box); never loosen anything and never set verified. Leave it {{}} unless you have a reason.
3. `check`. Read every group's errors and warnings. If all_ok is false, fix only what the errors name in
   adapter_config.yaml and run `check` again (at most 2 retries). If it still fails, write the report (step 6)
   explaining the failure and end with DONE without publishing.
4. Write {tdir}/success_check.yaml from templates["success_check.yaml"] for THIS ticket's task: concrete
   conditions an operator can see from the chosen camera. decided_by: operator and auto_decide: false (success is
   always judged by a person).
5. Write {tdir}/reset_routine.yaml from templates["reset_routine.yaml"]: home_joints = the adapter's home_joints,
   gripper open, speed_fraction <= 0.25, the human steps to put the task's objects back for the next trial.
6. Write {tdir}/onboarding_report.md (20-40 lines): robot and mode; the adapter config and why; each check group
   with its numbers (contract live steps, e-stop ms, camera capture, safety profile + home tool point, IK success
   rate and errors); every warning and what it means; the success check and reset routine in one line each; and
   what a human must still do at the real arm (hil_required). Use only numbers from the tool outputs.
7. `publish`. If it says published, end with DONE. If it says parked (hardware), write resume_payload.json in your
   working directory, print RESUME_PAYLOAD <that json> and end with its waiting_line verbatim as the final line;
   when a [resume-bridge] message wakes you, run `publish` again and end with DONE.
Rules: you never move a real arm; create/edit files only with your file tools (Create/Edit), never through the
shell; any other command is refused. Keep replies short; do not explore the repo beyond these commands."""


def build_brief(*, ticket_id: int, tdir: Path, ctx_path: Path, note: str = "") -> str:
    return BRIEF.format(ticket_id=ticket_id, tdir=tdir, ctx=ctx_path, tool=TOOL_PREFIX, note=note).strip()


def write_context(tdir: Path, *, host: str, db: str, ticket_id: int, extra: dict | None = None) -> Path:
    """droid_context.json: which database the tool may talk to (no secrets: the token stays in env)."""
    tdir.mkdir(parents=True, exist_ok=True)
    p = tdir / "droid_context.json"
    p.write_text(json.dumps({"host": host.rstrip("/"), "db": db, "ticket_id": int(ticket_id),
                             "created_at": time.time(), **(extra or {})}, indent=2))
    return p


async def launch_onboarding_droid(fc: Any, ticket_id: int, *, host: str | None = None, db: str | None = None,
                                  note: str = "", max_turns: int = MAX_TURNS, name: str = "") -> Any:
    """Start an onboarding-droid session for ``ticket_id``. Returns the first TurnResult.

    ``fc``: a ``FactoryClient``. host/db default to SPACETIME_HOST / SPACETIME_DB_NAME (what ``fc``'s
    mirror and the droid's tool both use).
    """
    host = host or os.environ.get("SPACETIME_HOST", "")
    db = db or os.environ.get("SPACETIME_DB_NAME", "")
    if not host or not db:
        raise RuntimeError("SPACETIME_HOST / SPACETIME_DB_NAME must be set for the onboarding droid")
    tdir = ticket_dir(fc.cfg.work_root, ticket_id)
    ctx = write_context(tdir, host=host, db=db, ticket_id=ticket_id)
    brief = build_brief(ticket_id=ticket_id, tdir=tdir, ctx_path=ctx, note=note)
    return await fc.start_session(ROLE, brief, env={"ticket_id": ticket_id}, ticket_id=ticket_id,
                                  name=name or f"onboarding-droid-t{ticket_id}", write_roots=[str(tdir)],
                                  allow_commands=[TOOL_PREFIX], max_turns=max_turns, autonomy="off")


# =========================================================================== flywheel step

@dataclass
class OnboardingDroidStep:
    """Flywheel ``onboarding`` step (opt-in ``RDF_ONBOARDING_DROID=1``): start an Onboarding Droid.

    Idempotent and credit-safe: skips if the robot is already onboarded or ANY onboarding droid
    session exists for the ticket (a finished one that did not publish is left for a human to look
    at; restart it by hand). It never marks a robot onboarded itself: if the droid cannot start,
    the ticket keeps waiting in ``onboarding`` with an ``onboarding.droid_error`` event.
    """

    stage: str = "onboarding"
    owner: str = "TDM-32 onboarding droid"
    client_factory: Callable[[], Any] | None = None  # -> async-context FactoryClient (tests / proofs)

    async def run(self, ctx: Any) -> None:
        v = ctx.view
        if v.onboarded:
            ctx.log("step.skip", reason=f"robot already onboarded ({v.robot.get('adapter_version')})")
            return
        if not v.robot:
            ctx.log("step.skip", reason="ticket has no robot row")
            return
        mine = [dict(s) for s in ctx.bus.rows("agent_session")
                if s.get("role") == ROLE and int(s.get("ticket_id") or 0) == ctx.ticket_id]
        if mine:
            last = max(mine, key=lambda s: int(s["id"]))
            ctx.log("step.skip", reason=f"onboarding droid agent_session {last['id']} is {last['status']}")
            return
        await ctx.event("onboarding.droid_starting", {"robot_id": v.robot["id"], "robot": v.robot.get("name")})
        # A droid turn takes ~1-3 min: run it off the flywheel's await so other tickets keep moving.
        asyncio.get_running_loop().create_task(self._launch(ctx), name=f"tdm32-droid-t{ctx.ticket_id}")

    async def _launch(self, ctx: Any) -> None:
        try:
            from rdf_factory import FactoryClient

            async with (self.client_factory or FactoryClient)() as fc:
                r = await launch_onboarding_droid(fc, ctx.ticket_id)
            ctx.log("step.onboarding_droid", agent_session_id=r.agent_session_id, status=r.status, credits=r.credits)
        except Exception as e:  # noqa: BLE001 - never crash the flywheel; the ticket waits for a human
            log.exception("onboarding droid could not start")
            await ctx.event("onboarding.droid_error", {"error": f"{type(e).__name__}: {e}"[:500]})


def main(argv: list[str] | None = None) -> int:
    """``python -m rdf_factory.droids.onboarding <ticket_id>``: start a droid by hand."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m rdf_factory.droids.onboarding <ticket_id>", file=sys.stderr)
        return 2

    async def go() -> int:
        from rdf_factory import FactoryClient

        async with FactoryClient() as fc:
            r = await launch_onboarding_droid(fc, int(argv[0]))
        print(json.dumps(r.to_dict(), indent=2, default=str))
        return 0 if r.ok else 1

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
