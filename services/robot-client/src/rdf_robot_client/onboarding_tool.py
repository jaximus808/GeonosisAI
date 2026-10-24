"""TDM-32: the Onboarding Droid's one allow-listed shell tool.

    uv run --frozen --project services/robot-client python -m rdf_robot_client.onboarding_tool <cmd> --ctx <ticket dir>/droid_context.json

    probe     the ticket, its robot row, a draft adapter config, the file templates and where to write them
    check     run every onboarding check on <ticket dir>/adapter_config.yaml -> <ticket dir>/checks.json
    publish   validate success_check.yaml / reset_routine.yaml / onboarding_report.md, then
                sim robot:      set robot.adapter_version (+ the onboarding block in camera_config_json)
                                and log onboarding.report -> the flywheel moves the ticket to baseline
                hardware robot: log onboarding.hil_requested and print the WAITING_FOR line (a human runs
                                the motion checks and confirms with ``rdf_robot_client.onboarding_hil``);
                                after that confirmation, the same command publishes

Every command prints one JSON object. Guardrails live here, not in the droid's brief:
  * only the database named in droid_context.json (written by the launcher); a different
    SPACETIME_HOST / SPACETIME_DB_NAME in the environment is refused;
  * files are read only from the ticket dir (plus the repo's own camera / safety configs);
  * safety overrides may only tighten (``onboarding.tighten_only``);
  * on hardware nothing connects to or moves the arm; a simulated config can never onboard a robot
    whose embodiment is not ``sim_*``, and a hardware robot is published only after the human's
    ``onboarding.hil_confirmed.t<ticket>`` event;
  * the success check must say ``decided_by: operator`` and ``auto_decide: false``.
The droid's records are logged with actor ``droid:onboarding``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import onboarding as O

ROLE = "onboarding_droid"
ACTOR = "droid:onboarding"
REPORT_MAX = 8000
HIL_TIMEOUT_S = 24 * 3600
FILES = {"adapter_config": "adapter_config.yaml", "success_check": "success_check.yaml",
         "reset_routine": "reset_routine.yaml", "report": "onboarding_report.md", "checks": "checks.json"}


class ToolError(RuntimeError):
    pass


def hil_event_type(ticket_id: int) -> str:
    return f"onboarding.hil_confirmed.t{int(ticket_id)}"


def hil_waiting_line(ticket_id: int) -> str:
    """WAITING_FOR the human's confirmation row (TDM-27 predicate: some event_log row of that type).

    24 h deadline (TDM-31 ``timeout_s``): a person may need a day to get to the arm; a timed-out wake
    just runs ``publish`` again, which re-requests the HIL run and parks again."""
    return "WAITING_FOR " + json.dumps({"table": "event_log", "type": hil_event_type(ticket_id), "timeout_s": HIL_TIMEOUT_S},
                                       separators=(",", ":"))


SUCCESS_TEMPLATE = {
    "ticket_id": "<ticket id>",
    "task": "<the ticket's task text, verbatim>",
    "decided_by": "operator",
    "auto_decide": False,
    "camera": "top",
    "start_state": "<what the scene looks like when a trial starts>",
    "success_when": ["<observable condition the operator checks, e.g. 'the whole cup is inside the bin'>"],
    "failure_when": ["<e.g. 'cup knocked over or outside the bin when the time limit is reached'>"],
    "time_limit_s": 60,
    "ambiguous": "<what the operator records when it is unclear (default: failure, with a note)>",
    "operator_prompt": "<one or two lines the eval UI shows the operator>",
}
RESET_TEMPLATE = {
    "home_joints": "<list of dof joint angles, normally the adapter's home_joints>",
    "gripper": 1.0,
    "speed_fraction": 0.25,
    "arm_steps": ["hold", "move_joints(home_joints) through the SafetyLayer at speed_fraction", "open gripper"],
    "human_steps": ["<what the person puts back between trials, e.g. 'cup upright on the start mark'>"],
    "requires_human": True,
    "operator_prompt": "<the text shown to the person doing the reset>",
    "est_seconds": 30,
}


# --------------------------------------------------------------------------- context / db

class Ctx:
    def __init__(self, path: str, session: int | None = None, client: Any = None):
        p = Path(path)
        if not p.exists():
            raise ToolError(f"context file {p} not found (pass the --ctx path from your brief)")
        self.tdir = p.parent.resolve()
        d = json.loads(p.read_text())
        self.host, self.db = str(d["host"]).rstrip("/"), str(d["db"])
        self.ticket_id = int(d["ticket_id"])
        env_host = os.environ.get("SPACETIME_HOST", "").rstrip("/")
        env_db = os.environ.get("SPACETIME_DB_NAME", "")
        if (env_host and env_host != self.host) or (env_db and env_db != self.db):
            raise ToolError(f"SPACETIME_HOST/DB_NAME in the environment ({env_host}/{env_db}) differ from the "
                            f"context ({self.host}/{self.db}); refusing to guess which database to write")
        if client is None:
            token = os.environ.get("SPACETIME_TOKEN", "").strip()
            if not token:
                raise ToolError("SPACETIME_TOKEN is not set in the environment")
            from rdf_common.spacetime_client import SpacetimeClient

            client = SpacetimeClient(self.host, self.db, token)
        self.c = client
        self._session = session

    def sql(self, q: str) -> list[dict]:
        return [dict(r) for r in self.c.sql(q)]

    def one(self, q: str) -> dict | None:
        rows = self.sql(q)
        return rows[0] if rows else None

    def file(self, key: str) -> Path:
        return self.tdir / FILES[key]

    @property
    def session(self) -> int:
        if self._session:
            return int(self._session)
        rows = [r for r in self.sql(f"SELECT * FROM agent_session WHERE ticket_id = {self.ticket_id}")
                if r.get("role") == ROLE and r.get("status") in ("starting", "running", "resuming")]
        self._session = int(max(rows, key=lambda r: int(r["id"]))["id"]) if rows else 0
        return self._session

    def ticket(self) -> dict:
        t = self.one(f"SELECT * FROM ticket WHERE id = {self.ticket_id}")
        if t is None:
            raise ToolError(f"ticket {self.ticket_id} not found")
        return t

    def robot(self, ticket: dict) -> dict:
        rid = int(ticket.get("robot_id") or 0)
        r = self.one(f"SELECT * FROM robot WHERE id = {rid}") if rid else None
        if r is None:
            raise ToolError(f"ticket {self.ticket_id} has no registered robot (robot_id {rid})")
        return r

    def events(self, type_: str) -> list[dict]:
        safe = type_.replace("'", "''")
        return sorted(self.sql(f"SELECT * FROM event_log WHERE type = '{safe}'"), key=lambda r: int(r["id"]))

    def event(self, type_: str, payload: dict) -> None:
        body = {"ticket_id": self.ticket_id, "agent_session_id": self.session, **payload}
        self.c.call_reducer("append_event", ACTOR, type_, json.dumps(body, default=str)[:60_000])


def _robot_summary(r: dict) -> dict:
    try:
        cc = json.loads(r.get("camera_config_json") or "{}")
    except ValueError:
        cc = {}
    return {"id": r["id"], "name": r["name"], "embodiment": r.get("embodiment"), "status": r.get("status"),
            "adapter_version": r.get("adapter_version"), "last_heartbeat": r.get("last_heartbeat"),
            "camera_config_keys": sorted(cc) if isinstance(cc, dict) else [],
            "arm": cc.get("arm") if isinstance(cc, dict) else None,
            "previous_onboarding": (cc.get("onboarding") or {}).get("status") if isinstance(cc, dict) else None}


def _robot_is_sim(robot: dict) -> bool:
    return str(robot.get("embodiment") or "").startswith("sim")


def _within(ctx: Ctx, p: Path) -> Path:
    p = p if p.is_absolute() else ctx.tdir / p
    if ctx.tdir not in p.resolve().parents:
        raise ToolError(f"{p} must live under the ticket dir {ctx.tdir}")
    return p


def _load_adapter_config(ctx: Ctx, path: str | None) -> tuple[dict, Path]:
    p = _within(ctx, Path(path)) if path else ctx.file("adapter_config")
    if not p.exists():
        raise ToolError(f"{p} not found: write it first (probe prints a draft)")
    try:
        return O.load_yaml(p), p
    except O.OnboardingError as e:
        raise ToolError(str(e)) from e


# --------------------------------------------------------------------------- commands

def cmd_probe(ctx: Ctx, _a: argparse.Namespace) -> dict:
    t = ctx.ticket()
    r = ctx.robot(t)
    draft = O.draft_adapter_config(r)
    existing = {k: ctx.file(k).exists() for k in FILES}
    safety = O.load_yaml(O.DEFAULT_SAFETY)
    try:  # static facts of the drafted adapter (constructed, never connected)
        arm, notes = O.build_adapter(draft)
        facts: dict[str, Any] = {"adapter_version": arm.adapter_version, "embodiment": arm.embodiment, "dof": arm.dof,
                                 "joint_names": arm.joint_names, "home_joints": [round(v, 4) for v in arm.home_joints],
                                 "home_tool_xyz": [round(v, 4) for v in arm.fk(arm.home_joints).xyz],
                                 "limits": {"lower": list(arm.limits.lower), "upper": list(arm.limits.upper)}, **notes}
    except Exception as e:  # noqa: BLE001
        facts = {"error": f"draft adapter does not construct: {type(e).__name__}: {e}"}
    return {
        "ticket": {k: t.get(k) for k in ("id", "task_description", "status", "robot_id", "target_success")},
        "robot": _robot_summary(r), "agent_session_id": ctx.session,
        "mode": "sim" if _robot_is_sim(r) else "hardware",
        "mode_note": ("simulated robot: check drives the simulated arm through the safety layer; publish marks it "
                      "onboarded" if _robot_is_sim(r) else
                      "HARDWARE robot: check is read-only (the arm is never connected or moved); publish parks on a "
                      "human HIL confirmation before marking it onboarded"),
        "draft_adapter_config": draft, "draft_adapter_facts": facts,
        "adapters": sorted(O.ADAPTERS), "safety_profiles": sorted((safety.get("profiles") or {})),
        "repo_cameras_config": O._rel(O.DEFAULT_CAMERAS),
        "write_here": {k: str(ctx.file(k)) for k in ("adapter_config", "success_check", "reset_routine", "report")},
        "existing_files": existing,
        "templates": {"success_check.yaml": {**SUCCESS_TEMPLATE, "ticket_id": ctx.ticket_id,
                                             "task": t.get("task_description")},
                      "reset_routine.yaml": RESET_TEMPLATE},
        "rules": ["safety_overrides may only tighten the profile (smaller caps / box, larger margins); never verified: true",
                  "success is decided by the operator: decided_by operator, auto_decide false",
                  "write files with your file tools; this tool is the only command"],
    }


def _summary(res: dict) -> dict:
    keep = {"contract": ("class", "adapter_version", "embodiment", "yam_sim_backend", "live_steps", "home_err_rad",
                         "ee_move_err_m", "estop_return_ms", "live"),
            "cameras": ("path", "cameras", "reference", "synthetic_capture", "device_nodes"),
            "safety": ("profile", "verified", "tightened", "home_tool_xyz", "safe_joint_range"),
            "kinematics": ("samples", "ik_success_rate", "pos_err_max_mm", "rot_err_max_mrad", "ik_ms_p50", "ik_ms_p95",
                           "backend"),
            "hardware_probe": ("i2rt_sdk_importable", "can", "robot_status", "note")}
    out = {}
    for name, g in (res.get("groups") or {}).items():
        out[name] = {"ok": g["ok"], "errors": g["errors"], "warnings": g["warnings"],
                     "info": {k: g["info"][k] for k in keep.get(name, ()) if k in g["info"]}}
    return out


def cmd_check(ctx: Ctx, a: argparse.Namespace) -> dict:
    t = ctx.ticket()
    robot = ctx.robot(t)
    cfg, path = _load_adapter_config(ctx, a.config)
    if O.is_sim(cfg) and not _robot_is_sim(robot):
        raise ToolError(f"robot {robot['name']} has embodiment {robot.get('embodiment')!r} (hardware) but the config "
                        "is a simulation: onboard the real adapter (sim: false); the check is read-only on hardware")
    if cfg.get("robot") not in (None, robot["name"]):
        raise ToolError(f"adapter_config.robot is {cfg.get('robot')!r}, this ticket's robot is {robot['name']!r}")
    t0 = time.monotonic()
    res = O.run_checks(cfg, robot=robot, ticket_dir=ctx.tdir)
    res.update({"ticket_id": ctx.ticket_id, "robot_id": robot["id"], "robot": robot["name"],
                "config_path": str(path), "config_sha256": O.sha256_file(path), "checked_at": time.time(),
                "seconds": round(time.monotonic() - t0, 1)})
    ctx.file("checks").write_text(json.dumps(res, indent=1, default=str))
    return {"all_ok": res["ok"], "mode": res["mode"], "config_errors": res["config_errors"], "groups": _summary(res),
            "hil_required": res["hil_required"], "hil_note": res.get("hil_note"), "checks_file": str(ctx.file("checks")),
            "seconds": res["seconds"],
            "next": ("write success_check.yaml, reset_routine.yaml and onboarding_report.md, then publish"
                     if res["ok"] else "fix adapter_config.yaml (only what the errors name) and run check again")}


def _published_onboarding(cfg: dict, checks: dict, success: dict, reset: dict, files: dict, status: str,
                          adapter_version: str, extra: dict | None = None) -> dict:
    return {"status": status, "ticket_id": checks["ticket_id"], "adapter_version": adapter_version,
            "adapter": cfg.get("adapter"), "kwargs": cfg.get("kwargs") or {}, "safety_profile": cfg.get("safety_profile"),
            "safety_overrides": cfg.get("safety_overrides") or {}, "cameras_config": cfg.get("cameras_config"),
            "checks_ok": {k: g["ok"] for k, g in checks["groups"].items()}, "checked_at": checks["checked_at"],
            "success_check": success, "reset_routine": reset, "files": files,
            "hil_pending": [] if status == "passed_hil" else checks.get("hil_required", []), **(extra or {})}


def cmd_publish(ctx: Ctx, a: argparse.Namespace) -> dict:
    t = ctx.ticket()
    robot = ctx.robot(t)
    cfg, cpath = _load_adapter_config(ctx, None)
    cp = ctx.file("checks")
    if not cp.exists():
        raise ToolError("no checks.json: run check first")
    checks = json.loads(cp.read_text())
    if checks.get("config_sha256") != O.sha256_file(cpath):
        raise ToolError("adapter_config.yaml changed since the last check: run check again")
    if not checks.get("ok"):
        raise ToolError("the last check did not pass; fix and re-run check: "
                        + json.dumps({k: g["errors"] for k, g in checks["groups"].items() if g["errors"]})[:1200])
    if checks.get("mode") == "sim" and not _robot_is_sim(robot):
        raise ToolError("a simulated check cannot onboard a hardware robot")
    errors: list[str] = []
    try:
        success = O.load_yaml(ctx.file("success_check"))
        cams = (checks["groups"].get("cameras") or {}).get("info", {}).get("cameras") or list(O.POLICY_CAMERAS)
        errors += [f"success_check.yaml: {e}" for e in O.validate_success_check(success, ticket_id=ctx.ticket_id, cameras=cams)]
    except O.OnboardingError as e:
        success, errors = {}, errors + [str(e)]
    arm, _ = O.build_adapter(cfg)
    scfg, _, _ = O.safety_config_for(cfg, arm)
    try:
        reset = O.load_yaml(ctx.file("reset_routine"))
        errors += [f"reset_routine.yaml: {e}" for e in O.validate_reset_routine(reset, arm, scfg)]
    except O.OnboardingError as e:
        reset, errors = {}, errors + [str(e)]
    rp = ctx.file("report")
    report = rp.read_text()[:REPORT_MAX] if rp.exists() else ""
    if len(report.strip()) < 200:
        errors.append("onboarding_report.md: missing or too short (what was checked, results with numbers, "
                      "warnings, what the human still has to do)")
    if errors:
        raise ToolError("publish refused: " + "; ".join(errors))
    files = {k: str(ctx.file(k)) for k in ("adapter_config", "success_check", "reset_routine", "report", "checks")}
    groups = _summary(checks)
    base_payload = {"robot_id": robot["id"], "robot": robot["name"], "mode": checks["mode"], "files": files,
                    "checks": groups, "success_check": success, "reset_routine": reset, "report": report,
                    "hil_required": checks.get("hil_required", [])}

    hil = None
    if checks["mode"] == "hardware":
        confirmed = ctx.events(hil_event_type(ctx.ticket_id))
        if not confirmed:
            ctx.event("onboarding.hil_requested", {**base_payload, "confirm_with":
                      f"uv run --project services/robot-client python -m rdf_robot_client.onboarding_hil confirm "
                      f"--ticket {ctx.ticket_id} --by <your name>"})
            return {"published": False, "parked": True, "waiting_line": hil_waiting_line(ctx.ticket_id),
                    "hil_required": checks.get("hil_required", []),
                    "next": "write resume_payload.json, print RESUME_PAYLOAD, end with waiting_line as the final line; "
                            "when woken, run publish again"}
        hil = json.loads(confirmed[-1].get("payload_json") or "{}") | {"actor": confirmed[-1].get("actor")}

    status = "passed_hil" if hil else "passed_sim"
    adapter_version = f"{arm.adapter_version}+onb.t{ctx.ticket_id}" + (".hil" if hil else "")
    try:
        cc = json.loads(robot.get("camera_config_json") or "{}")
    except ValueError:
        cc = {}
    if not isinstance(cc, dict):
        cc = {}
    cc["onboarding"] = _published_onboarding(cfg, checks, success, reset, files, status, adapter_version,
                                             {"hil_confirmation": hil} if hil else None)
    ctx.c.call_reducer("register_robot", robot["name"], robot["embodiment"], adapter_version, json.dumps(cc, default=str))
    after = ctx.one(f"SELECT * FROM robot WHERE id = {int(robot['id'])}") or {}
    if after.get("adapter_version") != adapter_version:
        raise ToolError(f"register_robot returned but robot.adapter_version is {after.get('adapter_version')!r}")
    ctx.event("onboarding.report", {**base_payload, "status": status, "adapter_version": adapter_version,
                                    "previous_adapter_version": robot.get("adapter_version"),
                                    "hil_confirmation": hil})
    return {"published": True, "status": status, "robot": robot["name"], "adapter_version": adapter_version,
            "hil_pending": cc["onboarding"]["hil_pending"], "next": "end your reply with DONE"}


COMMANDS = {"probe": cmd_probe, "check": cmd_check, "publish": cmd_publish}


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="onboarding_tool", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--ctx", required=True, help="droid_context.json from your brief")
    ap.add_argument("--session", type=int, default=None, help="your agent_session id (inferred if omitted)")
    ap.add_argument("--config", default=None, help="check: adapter config (default <ticket dir>/adapter_config.yaml)")
    a = ap.parse_args(argv)
    import logging

    logging.disable(logging.WARNING)  # keep stdout one JSON object (adapters log at INFO/WARNING)
    try:
        ctx = Ctx(a.ctx, a.session, client)
        out = COMMANDS[a.cmd](ctx, a)
        print(json.dumps({"ok": True, **out}, indent=1, default=str))
        return 0
    except (ToolError, O.OnboardingError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    except Exception as e:  # noqa: BLE001 - the droid reads this; keep it one JSON line
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"[:1500]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
