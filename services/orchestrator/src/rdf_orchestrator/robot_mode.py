"""TDM-107: per-ticket robot mode. A ticket runs on the **sim** robot or on the **real** YAM.

    sim   the MuJoCo sim robot (``rdf-robot-client --adapter mujoco``) running the ticket's deployed
          scene. Practice, sim data collection and sim evals happen here.
    real  the Jetson YAM.

There is no mode column. The mode is a property of the robot row, and a ticket's mode is the mode
of the robot it is bound to (``ticket.robot_id``):

  * a robot is **sim** when ``robot.camera_config_json`` has ``"sim": true`` (the robot client sets
    it for the sim adapters, ``rdf_robot_client.main``); anything else is **real**.
  * every task / eval_run / episode row copies ``ticket.robot_id`` when it is written, so each row
    keeps the robot it ran on. A row is labelled sim by looking up *its own* robot_id, forever,
    however often the ticket switches afterwards.

Switching (operator only): ``POST /tickets/{id}/robot_mode {mode, robot_id?, reason?}`` calls the
``set_ticket_robot`` reducer (``spacetime/spacetimedb/src/reducers/robot_mode.ts``), which moves
``ticket.robot_id`` and logs ``ticket.robot_mode_changed``. New tasks and evals then route to the new
robot with no other change. It is refused while an eval of the ticket is running or a job command
is open. Open (unclaimed) collection tasks on the old robot are cancelled so the dispatcher re-posts
them on the new one.

Keeping sim out of real numbers:
  * ``same_mode_evals``: the flywheel and the eval step only count evals that ran on a robot of the
    ticket's current mode. A real-mode ticket never ships or diagnoses on a sim eval (and a sim
    ticket never on a real one); switching makes the stage re-run its eval on the new robot.
  * ``training_episode_ids``: training takes sim episodes only if run_settings
    ``hyperparams.include_sim_data`` is on. Unset = on for a sim-mode ticket, off for a real one.
  * the report (``report.py``) splits every eval into ``by_mode.real`` / ``by_mode.sim``; its
    before/after numbers are the current mode's only.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Any, Iterable, Literal, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .log import get_logger, ticket_id_var

log = get_logger("robot_mode")

SIM, REAL = "sim", "real"
MODES = (SIM, REAL)
RobotMode = Literal["sim", "real"]
CHANGED_EVENT = "ticket.robot_mode_changed"
REDUCER = "set_ticket_robot"
TERMINAL = ("shipped", "failed", "cancelled")
ONLINE = ("online", "busy", "paused")
COLLECTION_TASKS = ("clean_teleop", "recovery_takeover")
DATASET_KINDS = ("clean_teleop", "recovery_takeover", "purchased")


# --------------------------------------------------------------------------- pure helpers

def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def robot_config(robot: Any) -> dict:
    raw = (robot or {}).get("camera_config_json") if isinstance(robot, dict) else getattr(robot, "camera_config_json", None)
    try:
        cfg = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def robot_is_sim(robot: Any) -> bool:
    """True for the sim robots (camera_config_json.sim = true; same rule as the reducer)."""
    return robot_config(robot).get("sim") is True


def robot_can_eval(robot: Any) -> bool:
    """Can this robot run policy jobs (run_eval / run_baseline)? Sim robots: yes. A real robot only
    when ``camera_config_json.policy`` is true; a real robot with ``teleop: true`` and no ``policy``
    flag is teleop-only (tonight's ``tdm119_teleop`` refuses policy jobs unless TDM119_ALLOW_POLICY=1).
    A real robot with neither flag (older clients) keeps the pre-flag behaviour: it can eval."""
    if robot is None:
        return False
    cfg = robot_config(robot)
    if cfg.get("sim") is True:
        return True
    pol = cfg.get("policy")
    if isinstance(pol, bool):
        return pol
    return cfg.get("teleop") is not True


def robot_is_teleop_only(robot: Any) -> bool:
    """A real robot that can collect data (teleop) but can't run evals."""
    return robot is not None and not robot_can_eval(robot)


# TDM-126: base pi05_base is DROID-layout (8-dim, unmapped on the YAM) and never goes to a real YAM. The "before"
# policy on the real arm is the YAM-adapted pi0.5 trained 200 steps on public YAM data (TDM-40 smoke run),
# which serves the 7-dim joint_pos_abs YAM spec. Override: RDF_REAL_YAM_BASELINE_CHECKPOINT (same allow-list).
REAL_YAM_BASELINE_DEFAULT = "modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199"
REAL_YAM_BASELINE_CHECKPOINT = os.environ.get("RDF_REAL_YAM_BASELINE_CHECKPOINT") or REAL_YAM_BASELINE_DEFAULT
REAL_CHECKPOINT_PREFIX = "modal-vol://rdf-checkpoints/"


def real_checkpoint_ok(uri: Any) -> bool:
    """A checkpoint a real YAM may run: a real Modal volume checkpoint (no fake://, no stub, no base pi0.5)."""
    u = str(uri or "")
    return u.startswith(REAL_CHECKPOINT_PREFIX) and "fake" not in u and "stub-no-gpu" not in u


def is_real_policy_arm(robot: Any) -> bool:
    """TDM-126: a real arm that explicitly advertises ``policy: true`` (the Jetson's tdm119_teleop with
    TDM119_ALLOW_POLICY=1 on yam-01). Its policy jobs get the real-arm rules: YAM-mapped checkpoints only."""
    return robot is not None and not robot_is_sim(robot) and robot_config(robot).get("policy") is True


def baseline_checkpoint_for(robot: Any, default: str) -> str:
    """TDM-126: the baseline policy a ticket's robot evaluates. The real YAM (``is_real_policy_arm``) gets the
    YAM-adapted smoke checkpoint instead of base pi05_base (DROID layout, unmapped on the YAM); others keep
    ``default``."""
    return REAL_YAM_BASELINE_CHECKPOINT if is_real_policy_arm(robot) else default


CANNOT_EVAL_EVENT = "eval.robot_cannot_eval"
SPLIT_AUTO_EVENT = "ticket.split_auto"
PREFERRED_SIM_ROBOT = "sim-yam-mujoco"


def split_note(real_name: Any) -> str:
    return (f"{real_name} can collect data but can't run evals tonight, so this ticket evaluates on the sim "
            f"robot and collects on {real_name}.")


def pick_split_sim_robot(robots: list[dict]) -> dict | None:
    """The online sim robot a teleop-only real ticket evaluates on (prefers ``sim-yam-mujoco``)."""
    live = [r for r in candidates(robots, SIM) if r.get("status") in ONLINE]
    if not live:
        return None
    pref = [r for r in live if r.get("name") == PREFERRED_SIM_ROBOT]
    return (pref or sorted(live, key=lambda r: _i(r.get("id"))))[0]


def robot_mode(robot: Any) -> RobotMode | None:
    """'sim' | 'real' for a robot row, None when there is no row."""
    if robot is None:
        return None
    return SIM if robot_is_sim(robot) else REAL


def hyperparams(settings: Any) -> dict:
    raw = (settings or {}).get("hyperparams_json") if isinstance(settings, dict) else None
    try:
        hp = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return hp if isinstance(hp, dict) else {}


def include_sim_data(settings: Any, mode: str | None) -> bool:
    """run_settings ``hyperparams.include_sim_data``; unset = on for a sim ticket, off for a real one."""
    v = hyperparams(settings).get("include_sim_data")
    return v if isinstance(v, bool) else mode == SIM


# --------------------------------------------------------------------------- TDM-125: split-robot tickets
# A ticket may collect its data on another robot than the one it evals on: run_settings
# ``hyperparams.collect_robot_id`` (0 / unset = the ticket's robot). Collection tasks (demos,
# takeovers) and their resets go to that robot; baseline / eval stay on ``ticket.robot_id``.
COLLECT_ROBOT_EVENT = "ticket.collect_robot_set"


def collect_robot_id(settings: Any, ticket: Any = None) -> int:
    """The robot collection runs on: hyperparams.collect_robot_id, else the ticket's robot."""
    v = _i(hyperparams(settings).get("collect_robot_id"))
    if v > 0:
        return v
    return _i((ticket or {}).get("robot_id") if isinstance(ticket, dict) else getattr(ticket, "robot_id", 0))


def is_split(settings: Any, ticket: Any) -> bool:
    """True when the ticket collects on another robot than it evals on."""
    v = _i(hyperparams(settings).get("collect_robot_id"))
    tr = _i((ticket or {}).get("robot_id") if isinstance(ticket, dict) else getattr(ticket, "robot_id", 0))
    return v > 0 and v != tr


def collect_capabilities(collect_robot: Any, eval_robot: Any) -> dict:
    """What collection on ``collect_robot`` can produce (the planner's input).

    The collect robot of a split ticket (e.g. the real yam-01) has no policy of this ticket running on
    it (the policy is deployed to the eval robot), so only clean demos can be collected there: a recovery
    takeover needs the policy rolling out on the arm. A real one's resets go to people in person and
    never auto-resolve (sim auto-reset is for sim robots only)."""
    split = collect_robot is not None and eval_robot is not None and _i(_get(collect_robot, "id")) != _i(_get(eval_robot, "id"))
    mode = robot_mode(collect_robot)
    clean_only = bool(split)
    return {
        "robot_id": _i(_get(collect_robot, "id")) or None,
        "robot_name": _get(collect_robot, "name"),
        "robot_mode": mode,
        "status": _get(collect_robot, "status"),
        "split_from_eval_robot": split,
        "data_types": ["clean_teleop"] if clean_only else ["clean_teleop", "recovery_takeover"],
        "clean_only": clean_only,
        "why": (f"data is collected on {_get(collect_robot, 'name')} ({'the real arm' if mode == REAL else 'a sim robot'}), "
                "which has no policy deployed for this ticket (evals run on another robot), so recovery takeovers "
                "are impossible there; only clean teleop demos") if clean_only else "",
        "resets": "in_person" if mode == REAL else "auto_sim",
    }


def _get(row: Any, k: str) -> Any:
    if row is None:
        return None
    return row.get(k) if isinstance(row, dict) else getattr(row, k, None)


def robots_by_id(rows: Iterable[dict]) -> dict[int, dict]:
    return {_i(r.get("id")): r for r in rows}


def row_mode(row: dict, robots: dict[int, dict]) -> RobotMode | None:
    """The mode of the robot a task / eval_run / episode row ran on (None: robot row unknown)."""
    return robot_mode(robots.get(_i(row.get("robot_id"))))


def same_mode_evals(eval_runs: list, robots: dict[int, dict], mode: str | None) -> list:
    """The eval runs that ran on a robot of ``mode``. A run whose robot row is unknown is kept (a view
    built without robot rows keeps the pre-TDM-107 behaviour); ``mode`` None: unchanged."""
    if mode is None:
        return eval_runs
    return [r for r in eval_runs if row_mode(r, robots) in (mode, None)]


def training_episode_ids(episodes: list, robots: dict[int, dict], settings: Any, mode: str | None) -> tuple[list[int], list[int]]:
    """(accepted dataset episode ids to train on, sim episode ids left out by include_sim_data)."""
    use_sim = include_sim_data(settings, mode)
    keep, dropped = [], []
    for e in episodes:
        if e.get("kind") not in DATASET_KINDS or e.get("review_status") != "accepted":
            continue
        if not use_sim and row_mode(e, robots) == SIM:
            dropped.append(_i(e["id"]))
        else:
            keep.append(_i(e["id"]))
    return keep, dropped


def public_robot(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {"id": _i(r["id"]), "name": r.get("name"), "status": r.get("status"), "embodiment": r.get("embodiment"),
            "robot_mode": robot_mode(r)}


def candidates(robots: list[dict], mode: str) -> list[dict]:
    """Robots of ``mode`` that auto-selection may pick (names starting ``test.`` are ignored)."""
    return [r for r in robots if robot_mode(r) == mode and not str(r.get("name") or "").startswith("test.")]


class RobotModeError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def http(self) -> HTTPException:
        return HTTPException(self.status, detail={"code": self.code, "message": self.message, **self.extra})


def pick_robot_for_mode(robots: list[dict], mode: str, robot_id: int | None = None, *,
                        allow_offline: bool = False) -> tuple[dict, str]:
    """The robot a ticket in ``mode`` should run on -> (row, how it was chosen).

    ``robot_id`` given: that robot, which must be of ``mode`` (``robot_mode_mismatch`` otherwise).
    Else the only online robot of that mode; with ``allow_offline`` (a switch, where an offline target
    is fine: tasks wait for it) also the only robot of that mode when none is online."""
    if robot_id is not None:
        r = next((r for r in robots if _i(r.get("id")) == robot_id), None)
        if r is None:
            raise RobotModeError(422, "unknown_robot", f"No robot with id {robot_id}.",
                                 robots=[public_robot(x) for x in robots])
        if robot_mode(r) != mode:
            raise RobotModeError(422, "robot_mode_mismatch",
                                 f"Robot {robot_id} ({r.get('name')}) is a {robot_mode(r)} robot, not {mode}.",
                                 robots=[public_robot(x) for x in candidates(robots, mode)])
        return r, "requested"
    pool = candidates(robots, mode)
    live = [r for r in pool if r.get("status") in ONLINE]
    if len(live) == 1:
        return live[0], f"only_online_{mode}_robot"
    if len(live) > 1:
        raise RobotModeError(422, "multiple_online_robots", f"Several {mode} robots are online. Pass robot_id to pick one.",
                             robots=[public_robot(r) for r in live])
    if allow_offline and len(pool) == 1:
        return pool[0], f"only_{mode}_robot_offline"
    raise RobotModeError(422, "no_online_robot", f"No {mode} robot is online. Start one or pass robot_id.",
                         robots=[public_robot(r) for r in pool])


# --------------------------------------------------------------------------- service

class Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


def _payload(e: dict) -> dict:
    try:
        p = json.loads(e.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return p if isinstance(p, dict) else {}


class RobotModeService:
    def __init__(self, stdb: Stdb) -> None:
        self.stdb = stdb
        self._lock = asyncio.Lock()

    async def _ticket(self, ticket_id: int) -> dict:
        rows = await self.stdb.sql(f"SELECT * FROM ticket WHERE id = {int(ticket_id)}")
        if not rows:
            raise RobotModeError(404, "ticket_not_found", f"No ticket {ticket_id}.")
        return rows[0]

    async def state(self, ticket_id: int) -> dict:
        ticket = await self._ticket(ticket_id)
        robots, settings, events = await asyncio.gather(
            self.stdb.sql("SELECT * FROM robot"),
            self.stdb.sql(f"SELECT * FROM run_settings WHERE ticket_id = {int(ticket_id)}"),
            self.stdb.sql(f"SELECT * FROM event_log WHERE type = '{CHANGED_EVENT}'"),
        )
        by_id = robots_by_id(robots)
        robot = by_id.get(_i(ticket.get("robot_id")))
        mode = robot_mode(robot)
        rs = settings[0] if settings else None
        explicit = hyperparams(rs).get("include_sim_data")
        history = [{"event_id": _i(e.get("id")), "ts": _i(e.get("ts")), **_payload(e)}
                   for e in sorted(events, key=lambda e: _i(e.get("id"))) if _i(_payload(e).get("ticket_id")) == ticket_id]
        return {
            "ticket_id": int(ticket_id),
            "status": ticket.get("status"),
            "robot_mode": mode,
            "robot": public_robot(robot),
            "include_sim_data": include_sim_data(rs, mode),
            "include_sim_data_explicit": explicit if isinstance(explicit, bool) else None,
            "robots": {m: [public_robot(r) for r in candidates(robots, m)] for m in MODES},
            "history": history,
            "switchable": ticket.get("status") not in TERMINAL,
        }

    async def switch(self, ticket_id: int, mode: str, robot_id: int | None = None, *, by: str = "operator",
                     reason: str = "") -> dict:
        if mode not in MODES:
            raise RobotModeError(422, "bad_mode", f"mode must be sim or real, got {mode!r}.")
        async with self._lock:
            ticket = await self._ticket(ticket_id)
            if ticket.get("status") in TERMINAL:
                raise RobotModeError(409, "ticket_frozen", f"Ticket {ticket_id} is {ticket.get('status')}; its robot is frozen.")
            robots = await self.stdb.sql("SELECT * FROM robot")
            target, how = pick_robot_for_mode(robots, mode, robot_id, allow_offline=True)
            from_id = _i(ticket.get("robot_id"))
            if _i(target["id"]) == from_id:
                out = await self.state(ticket_id)
                return {**out, "changed": False, "selection": how, "cancelled_task_ids": []}
            evals = await self.stdb.sql(f"SELECT * FROM eval_run WHERE ticket_id = {int(ticket_id)}")
            live = [r for r in evals if r.get("status") in ("queued", "running")]
            if live:
                raise RobotModeError(409, "eval_running", f"Eval run {live[-1]['id']} is {live[-1]['status']} on robot "
                                     f"{live[-1].get('robot_id')}. Switch after it finishes (or stop it first).",
                                     eval_run_id=_i(live[-1]["id"]))
            from rdf_common.spacetime_client import SpacetimeError

            try:
                await self.stdb.call_reducer(REDUCER, int(ticket_id), int(target["id"]), by[:100], reason[:500])
            except SpacetimeError as e:
                if e.status == 404 or "no such reducer" in e.message.lower():
                    raise RobotModeError(503, "reducer_not_published",
                                         f"The {REDUCER} reducer is not on this database yet (TDM-107 needs one module "
                                         "publish). Nothing was changed.") from e
                raise RobotModeError(409 if e.status == 530 else 502, "switch_rejected", e.message) from e
            cancelled = await self._cancel_stale_tasks(ticket_id, from_id)
            log.info("robot_mode.switched", extra={"fields": {"ticket_id": ticket_id, "from_robot_id": from_id,
                                                               "to_robot_id": _i(target["id"]), "mode": mode, "by": by,
                                                               "cancelled_task_ids": cancelled}})
            out = await self.state(ticket_id)
            return {**out, "changed": True, "selection": how, "cancelled_task_ids": cancelled}

    async def _cancel_stale_tasks(self, ticket_id: int, old_robot_id: int) -> list[int]:
        """Open (never claimed) collection tasks still on the old robot: cancel them so the dispatcher
        posts their slots again on the ticket's new robot. Claimed tasks are left to finish."""
        tasks = await self.stdb.sql(f"SELECT * FROM task WHERE ticket_id = {int(ticket_id)}")
        out = []
        for t in tasks:
            if t.get("status") == "open" and t.get("type") in COLLECTION_TASKS and _i(t.get("robot_id")) == old_robot_id:
                try:
                    await self.stdb.call_reducer("cancel_task", _i(t["id"]), "robot_mode_changed")
                    out.append(_i(t["id"]))
                except Exception as e:  # noqa: BLE001 - claimed in the meantime: let it finish
                    log.info("robot_mode.cancel_skipped", extra={"fields": {"task_id": t["id"], "error": str(e)[:200]}})
        return out


# --------------------------------------------------------------------------- routes

robot_mode_router = APIRouter(tags=["robot-mode"])


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: RobotMode
    robot_id: int | None = Field(default=None, ge=1, description="default: the only robot of that mode")
    reason: str = Field(default="", max_length=500)


_client: Any = None


async def get_robot_mode_service() -> RobotModeService:
    global _client
    if _client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _client = RobotModeService(AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator")))
    return _client


def get_robot_mode_operator_key() -> str | None:
    """RDF_OPERATOR_KEY (env or repo .env): the key the web proxy adds after operator login."""
    from rdf_common.settings import RdfSettings

    class _Tdm107Settings(RdfSettings):
        rdf_operator_key: SecretStr | None = None

    k = _Tdm107Settings().rdf_operator_key
    return k.get_secret_value() if k else None


@robot_mode_router.get("/tickets/{ticket_id}/robot_mode")
async def get_robot_mode(ticket_id: int, svc: RobotModeService = Depends(get_robot_mode_service)) -> dict:
    """The ticket's mode (from its robot), the robots of each mode, include_sim_data and the switch history."""
    try:
        return await svc.state(ticket_id)
    except RobotModeError as e:
        raise e.http() from None


@robot_mode_router.post("/tickets/{ticket_id}/robot_mode")
async def switch_robot_mode(ticket_id: int, body: SwitchRequest,
                            x_human_id: str = Header(default="operator", pattern=r"^[A-Za-z0-9._@:+-]{1,64}$"),
                            x_rdf_operator_key: str | None = Header(default=None),
                            operator_key: str | None = Depends(get_robot_mode_operator_key),
                            svc: RobotModeService = Depends(get_robot_mode_service)) -> dict:
    """Operator only: move the ticket to the sim or the real robot. Logs ``ticket.robot_mode_changed``."""
    if not operator_key:
        raise HTTPException(503, "robot mode switching is off: set RDF_OPERATOR_KEY for the orchestrator and the web server")
    if not x_rdf_operator_key or not hmac.compare_digest(x_rdf_operator_key.encode(), operator_key.encode()):
        raise HTTPException(401, "operator login required to switch a ticket's robot")
    token = ticket_id_var.set(ticket_id)
    try:
        return await svc.switch(ticket_id, body.mode, body.robot_id, by=f"human:{x_human_id}", reason=body.reason)
    except RobotModeError as e:
        raise e.http() from None
    finally:
        ticket_id_var.reset(token)
