"""TDM-32 [HIL]: the human half of onboarding a REAL arm. Not on the droid's allow-list.

The Onboarding Droid never moves hardware: on a real robot its check is read-only and its
``publish`` parks the droid on ``WAITING_FOR {"table":"event_log","type":"onboarding.hil_confirmed.t<ticket>"}``.
A person at the arm, physical e-stop in hand, then:

    uv run --project services/robot-client python -m rdf_robot_client.onboarding_hil checklist --ticket <id>
        prints the motion checks the droid could not do, its adapter config / reset routine /
        success check (from the onboarding.hil_requested event) and the commands to run
    ... runs them (docs/yam-sdk-notes.md section 8, demo-square, docs/safety-jetson-checklist.md) ...
    uv run --project services/robot-client python -m rdf_robot_client.onboarding_hil confirm --ticket <id> --by <name> [--notes "..."]
        appends onboarding.hil_confirmed.t<id> (actor human:<name>); the TDM-27 resume bridge wakes
        the droid, whose next publish marks the robot onboarded (adapter_version ...+onb.t<id>.hil)

``--failed "<what went wrong>"`` on confirm records onboarding.hil_failed.t<id> instead, which does
not wake the droid; fix the setup and re-run the checklist. Uses SPACETIME_* from the environment
or .env (rdf_common settings), like the robot client.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from .onboarding import HIL_MOTION_CHECKS
from .onboarding_tool import hil_event_type


def _client() -> Any:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "robot-client"))


def _requested(c: Any, ticket_id: int) -> dict | None:
    rows = sorted(c.sql("SELECT * FROM event_log WHERE type = 'onboarding.hil_requested'"), key=lambda r: int(r["id"]))
    for r in reversed(rows):
        try:
            p = json.loads(r["payload_json"] or "{}")
        except ValueError:
            continue
        if int(p.get("ticket_id") or 0) == ticket_id:
            return p
    return None


def checklist(ticket_id: int, c: Any = None) -> str:
    c = c or _client()
    req = _requested(c, ticket_id)
    lines = [f"Onboarding HIL checklist for ticket {ticket_id} (physical e-stop in hand; nobody near the arm's reach)."]
    if req is None:
        lines.append("No onboarding.hil_requested event for this ticket yet: the generic checks below still apply.")
    else:
        lines.append(f"Robot: {req.get('robot')} (id {req.get('robot_id')}); droid session {req.get('agent_session_id')}.")
        rr = req.get("reset_routine") or {}
        sc = req.get("success_check") or {}
        if rr:
            lines.append(f"Reset routine: home {rr.get('home_joints')}, gripper {rr.get('gripper')}; human: {rr.get('human_steps')}")
        if sc:
            lines.append(f"Success check (operator decides): success when {sc.get('success_when')}; time limit {sc.get('time_limit_s')} s")
        for name, g in (req.get("checks") or {}).items():
            if g.get("warnings"):
                lines.append(f"Droid warnings [{name}]: " + " | ".join(g["warnings"]))
    lines.append("Motion checks:")
    lines += [f"  [ ] {i}. {s}" for i, s in enumerate((req or {}).get("hil_required") or HIL_MOTION_CHECKS, 1)]
    lines += ["Commands: rdf-robot-client demo-square --side 0.0 --no-gripper (homing only); demo-square --side 0.0;",
              "          rdf-robot-client demo-square (CP-1); demo-square --rest-at-end before shutdown.",
              f"Then: python -m rdf_robot_client.onboarding_hil confirm --ticket {ticket_id} --by <your name> --notes \"...\""]
    return "\n".join(lines)


def confirm(ticket_id: int, by: str, notes: str = "", failed: str = "", c: Any = None) -> dict:
    who = re.sub(r"[^a-z0-9_.-]", "-", by.strip().lower()) or "operator"
    c = c or _client()
    req = _requested(c, ticket_id)
    payload = {"ticket_id": ticket_id, "by": by, "notes": notes, "robot_id": (req or {}).get("robot_id"),
               "checks_done": list((req or {}).get("hil_required") or HIL_MOTION_CHECKS)}
    if failed:
        type_ = f"onboarding.hil_failed.t{ticket_id}"
        payload["failed"] = failed
    else:
        type_ = hil_event_type(ticket_id)
    c.call_reducer("append_event", f"human:{who}", type_, json.dumps(payload))
    return {"logged": type_, **payload}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_robot_client.onboarding_hil", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("checklist")
    p.add_argument("--ticket", type=int, required=True)
    p = sub.add_parser("confirm")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--by", required=True, help="who ran the checks at the arm")
    p.add_argument("--notes", default="")
    p.add_argument("--failed", default="", help="record a FAILED HIL run instead (does not wake the droid)")
    a = ap.parse_args(argv)
    if a.cmd == "checklist":
        print(checklist(a.ticket))
    else:
        print(json.dumps(confirm(a.ticket, a.by, a.notes, a.failed), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
