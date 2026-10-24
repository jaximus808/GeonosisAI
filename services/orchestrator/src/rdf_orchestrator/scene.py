"""TDM-104: each ticket gets an agent-generated, deployable 3D task scene (one spec for MuJoCo and three.js).

Schema and checks: ``rdf_common.scene_spec``. This module writes the spec (Claude through ``rdf_common.llm``,
so ``RDF_LLM_BACKEND`` applies), stores it, and deploys it to a sim robot.

Storage = event_log, no schema publish needed (rows are append-only, so every revision is kept):
  * ``scene.created``  {ticket_id, revision, scene_id, source: llm|manual|default, task, summary, spec, llm?, fixes?}
  * ``scene.generation_failed`` {ticket_id, error, errors?, attempts}
  * ``scene.deployed`` {ticket_id, revision, scene_id, robot_id, deploy_id, by, spec}   <- the robot acts on this
  * ``scene.loaded``   {robot_id, ticket_id, revision, scene_id, deploy_id, ok, error?}  (written by the robot)
A ticket's ACTIVE scene is its newest ``scene.deployed``; a robot's scene is the newest ``scene.deployed``
naming that robot. ``scene_id`` = ``t<ticket>r<revision>``.

Deploy only ever targets a SIM robot (embodiment ``sim_*`` or ``camera_config_json.sim``): the real arm's
table is set up by a person, so a spec is never "loaded" onto it.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping

from rdf_common import scene_spec as ss

from .log import get_logger

log = get_logger("scene")

ACTOR = "orchestrator:scene"
EV_CREATED = "scene.created"
EV_FAILED = "scene.generation_failed"
EV_DEPLOYED = "scene.deployed"
EV_LOADED = "scene.loaded"
EVENT_TYPES = (EV_CREATED, EV_FAILED, EV_DEPLOYED, EV_LOADED)
MODEL = "claude-opus-5-5"
STEP = "scene"

SYSTEM = f"""You design the physical task scene for a robot arm, as JSON, from a customer's task sentence. \
The scene is loaded into a MuJoCo physics simulation (and drawn in a 3D web view) so people can practise \
the task and the robot can collect simulated data. It must be physically sensible and solvable by the arm.

The rig (fixed, do not describe it): an I2RT YAM 6-DoF arm with a parallel gripper, base at the world \
origin, standing on a 96 x 96 cm table whose top is z = 0, centred at x = 0.30, y = 0. World frame = robot \
base frame: x forward (away from the robot), y to the robot's left, z up, metres, yaw in radians about z.

Hard limits (a scene that breaks one is rejected):
- Everything the arm must touch lies {ss.REACH_MIN:.2f}..{ss.REACH_MAX:.2f} m from the base horizontally; \
keep objects between x = 0.18..0.48 and y = -0.25..0.25 to be safe.
- The object the robot picks up (success.object) is dynamic, 1.2..{ss.GRIP_MAX_WIDTH * 100:.0f} cm across its \
narrowest horizontal side (the gripper opens 9.4 cm), and at most {ss.MAX_GRASP_MASS} kg. A 4 cm cube \
(0.05 kg) is the default "block"/"cube".
- Primitives: box (size = FULL extents [sx, sy, sz]), cylinder (radius + FULL height), sphere (radius), \
plate (a fixed dish: radius 0.05..0.09, height 0.012, with a coloured rim). Plates are never dynamic.
- Objects must not overlap. An object whose (x, y) lies over another object (or plate) rests on it: that \
is how you put something on a plate or stack blocks. Leave z null; it is computed.
- Give the task object a spawn_region (a small rectangle, about 10 x 14 cm, inside reach) so its start \
pose is randomized every episode, with spawn_yaw about [-0.7, 0.7]. The spawn region must NOT overlap \
the goal. Other objects normally stay put (spawn_region null).
- success: object_in (the object ends inside a target plate/object or a region) or object_on (stacked on \
top of a target object). Exactly one of target (an object id) or region (a region name).
- Ids and names are lowercase slugs (red_block, plate, goal_zone).

Make the scene match the words: colours named in the task, distractors the task implies ("the EMPTY \
plate" means there is also a plate that is not empty; "the red block" suggests other blocks), and nothing \
else. Use regions with visible=true only for goal zones drawn on the table. Colours are "#rrggbb".

Example. Task "Pick up the cube and place it in the empty plate." ->
{json.dumps({
    "name": "cube_plate", "task": "Pick up the cube and place it in the empty plate.",
    "notes": "A red cube, the empty target plate and a second plate that holds a yellow block.",
    "table_color": None,
    "objects": [
        {"id": "cube", "label": "the red cube", "primitive": "box", "size": [0.04, 0.04, 0.04], "radius": None,
         "height": None, "color": "#db1f1a", "rim_color": None, "mass": 0.05, "dynamic": True, "x": 0.31, "y": -0.14,
         "z": None, "yaw": 0.0, "spawn_region": "cube_spawn", "spawn_yaw": [-0.7, 0.7]},
        {"id": "plate", "label": "the empty plate", "primitive": "plate", "size": None, "radius": 0.075,
         "height": 0.012, "color": "#f0f0eb", "rim_color": "#336bcc", "mass": None, "dynamic": False, "x": 0.31,
         "y": 0.19, "z": None, "yaw": 0.0, "spawn_region": None, "spawn_yaw": None},
        {"id": "full_plate", "label": "the plate with the yellow block", "primitive": "plate", "size": None,
         "radius": 0.075, "height": 0.012, "color": "#f0f0eb", "rim_color": "#bfbfc7", "mass": None,
         "dynamic": False, "x": 0.47, "y": -0.05, "z": None, "yaw": 0.0, "spawn_region": None, "spawn_yaw": None},
        {"id": "filler", "label": "the yellow block", "primitive": "cylinder", "size": None, "radius": 0.028,
         "height": 0.05, "color": "#f2c726", "rim_color": None, "mass": 0.08, "dynamic": True, "x": 0.47, "y": -0.05,
         "z": None, "yaw": 0.0, "spawn_region": None, "spawn_yaw": None}],
    "regions": [{"name": "cube_spawn", "x": [0.25, 0.37], "y": [-0.21, -0.07], "visible": False, "color": None}],
    "success": {"type": "object_in", "object": "cube", "target": "plate", "region": None}})}"""


class SceneGenerationError(RuntimeError):
    def __init__(self, msg: str, *, errors: list[str] | None = None, attempts: int = 0, usage: list[dict] | None = None):
        super().__init__(msg)
        self.errors, self.attempts, self.usage = errors or [], attempts, usage or []


def generate_spec(task: str, *, llm: Any = None, hint: str | None = None, max_attempts: int = 2,
                  effort: str = "medium") -> tuple[dict[str, Any], dict[str, Any]]:
    """Claude writes a scene for ``task``; ``validate`` checks it. One corrective retry with the errors.

    Returns (spec, info{attempts, usage: [...], model}). Raises ``SceneGenerationError``."""
    from rdf_common.llm import as_llm, usage_summary

    client = as_llm(llm)
    prompt = f"Task: {task.strip()}"
    if hint:
        prompt += f"\nOperator note: {hint.strip()}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usages: list[dict] = []
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            r = client.complete_structured(step=STEP, system=SYSTEM, messages=messages, schema=ss.LLM_SCHEMA,
                                           model=MODEL, effort=effort, max_tokens=8000)
        except Exception as e:  # noqa: BLE001 - outage / refusal: the caller logs it, the ticket goes on
            usages.append(usage_summary(e))
            raise SceneGenerationError(f"scene generation failed: {type(e).__name__}: {str(e)[:300]}",
                                       attempts=attempt, usage=usages) from e
        usages.append(usage_summary(r))
        ans = r.parsed
        try:
            raw = ss.from_llm(ans)
            raw["task"] = task.strip()  # the ticket's words, not a paraphrase
            spec = ss.validate(raw)
            return spec, {"attempts": attempt, "usage": usages, "model": (r.meta or {}).get("served_model")}
        except ss.SceneSpecError as e:
            errors = e.errors
        except Exception as e:  # noqa: BLE001 - malformed answer
            errors = [f"$: {type(e).__name__}: {e}"]
        log.info("scene.invalid", extra={"fields": {"attempt": attempt, "errors": errors[:8]}})
        messages = [*messages, {"role": "assistant", "content": json.dumps(ans)},
                    {"role": "user", "content": "That scene was rejected by the validator:\n- " + "\n- ".join(errors[:20])
                     + "\nFix every problem and return the complete corrected scene."}]
    raise SceneGenerationError("the generated scene did not validate", errors=errors, attempts=max_attempts, usage=usages)


# ------------------------------------------------------------------ event rows -> scene state (pure)

def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    p = row.get("payload_json") if isinstance(row, Mapping) else getattr(row, "payload_json", None)
    try:
        d = json.loads(p or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _ts(row: Mapping[str, Any]) -> Any:
    v = row.get("ts")
    if isinstance(v, dict):
        v = v.get("__timestamp_micros_since_unix_epoch__", v)
    return v


def events(rows: Iterable[Mapping[str, Any]], type_: str) -> list[dict[str, Any]]:
    """Rows of one type, oldest first, as payload dicts carrying ``event_id`` and ``ts``."""
    out = []
    for r in rows:
        if (r.get("type") if isinstance(r, Mapping) else getattr(r, "type", None)) != type_:
            continue
        d = _payload(r)
        d["event_id"] = int(r.get("id") or 0)
        d["ts"] = _ts(r)
        out.append(d)
    return sorted(out, key=lambda d: d["event_id"])


def ticket_revisions(rows: Iterable[Mapping[str, Any]], ticket_id: int) -> list[dict[str, Any]]:
    return [e for e in events(rows, EV_CREATED) if int(e.get("ticket_id") or 0) == ticket_id]


def scene_id(ticket_id: int, revision: int) -> str:
    return f"t{ticket_id}r{revision}"


def meta_of(e: Mapping[str, Any]) -> dict[str, Any]:
    """A revision without its spec (lists)."""
    return {k: e.get(k) for k in ("ticket_id", "revision", "scene_id", "source", "task", "summary", "event_id", "ts",
                                  "by", "fixes")}


def ticket_scene(rows: list[Mapping[str, Any]], ticket_id: int, revision: int | None = None) -> dict[str, Any]:
    revs = ticket_revisions(rows, ticket_id)
    deployed = [e for e in events(rows, EV_DEPLOYED) if int(e.get("ticket_id") or 0) == ticket_id]
    failed = [e for e in events(rows, EV_FAILED) if int(e.get("ticket_id") or 0) == ticket_id]
    pick = revs[-1] if revs and revision is None else next((e for e in revs if e.get("revision") == revision), None)
    active = deployed[-1] if deployed else None
    out: dict[str, Any] = {
        "ticket_id": ticket_id,
        "revision": pick.get("revision") if pick else None,
        "scene_id": pick.get("scene_id") if pick else None,
        "spec": pick.get("spec") if pick else None,
        "source": pick.get("source") if pick else None,
        "revisions": [meta_of(e) for e in revs],
        "deployed": _deploy_view(rows, active) if active else None,
        "last_error": ({k: failed[-1].get(k) for k in ("error", "errors", "attempts", "event_id", "ts")}
                       if failed and (not revs or failed[-1]["event_id"] > revs[-1]["event_id"]) else None),
    }
    return out


def _deploy_view(rows: list[Mapping[str, Any]], d: Mapping[str, Any]) -> dict[str, Any]:
    loaded = [e for e in events(rows, EV_LOADED) if e.get("deploy_id") == d.get("deploy_id")]
    last = loaded[-1] if loaded else None
    status = "pending" if last is None else ("loaded" if last.get("ok") else "failed")
    return {"deploy_id": d.get("deploy_id"), "robot_id": d.get("robot_id"), "ticket_id": d.get("ticket_id"),
            "revision": d.get("revision"), "scene_id": d.get("scene_id"), "by": d.get("by"), "event_id": d.get("event_id"),
            "ts": d.get("ts"), "status": status,
            "loaded": {k: last.get(k) for k in ("ok", "error", "event_id", "ts", "objects")} if last else None}


def robot_scene(rows: list[Mapping[str, Any]], robot_id: int) -> dict[str, Any]:
    """What a robot was last told to load (with the spec) and whether it confirmed."""
    mine = [e for e in events(rows, EV_DEPLOYED) if int(e.get("robot_id") or 0) == robot_id]
    if not mine:
        return {"robot_id": robot_id, "deployed": None, "spec": None, "status": "none"}
    d = mine[-1]
    view = _deploy_view(rows, d)
    return {"robot_id": robot_id, "deployed": view, "spec": d.get("spec"), "status": view["status"]}


# ------------------------------------------------------------------ robots

def is_sim_robot(robot: Mapping[str, Any]) -> bool:
    emb = str(robot.get("embodiment") or "")
    try:
        cc = json.loads(robot.get("camera_config_json") or "{}")
    except (TypeError, ValueError):
        cc = {}
    return emb.startswith("sim") or (isinstance(cc, dict) and bool(cc.get("sim")))


def pick_sim_robot(robots: list[Mapping[str, Any]], *, ticket_robot_id: int | None, requested: int | None
                   ) -> tuple[int | None, str | None]:
    """(robot_id, None) or (None, why). Requested > the ticket's robot (if sim) > the one online sim robot."""
    by_id = {int(r["id"]): r for r in robots if r.get("id") is not None}
    if requested:
        r = by_id.get(int(requested))
        if r is None:
            return None, f"robot {requested} not found"
        if not is_sim_robot(r):
            return None, f"robot {requested} ({r.get('name')}) is a real robot: scenes deploy to sim robots only"
        return int(requested), None
    if ticket_robot_id and ticket_robot_id in by_id and is_sim_robot(by_id[ticket_robot_id]):
        return ticket_robot_id, None
    online = [r for r in robots if is_sim_robot(r) and str(r.get("status")) in ("online", "busy", "paused")]
    named = [r for r in online if not str(r.get("name") or "").startswith("test.")] or online
    if len(named) == 1:
        return int(named[0]["id"]), None
    if not named:
        return None, "no sim robot is online (start one: rdf-robot-client --adapter mujoco)"
    return None, "several sim robots are online; pass robot_id: " + ", ".join(f"{r['id']} ({r.get('name')})" for r in named)


# ------------------------------------------------------------------ the store (sync; wrap with asyncio.to_thread)

class SceneStore:
    """event_log-backed scene store. ``sql(query) -> rows`` and ``call(reducer, *args)`` are a sync
    SpacetimeDB client's methods (``rdf_common.spacetime_client.SpacetimeClient``) or test fakes."""

    _lock = threading.Lock()  # revision numbers: read-max-append is serialized in-process

    def __init__(self, sql: Callable[[str], list[dict]], call: Callable[..., Any], *, read_retries: int = 10):
        self.sql, self.call, self.read_retries = sql, call, read_retries

    def rows(self) -> list[dict[str, Any]]:
        q = " OR ".join(f"type = '{t}'" for t in EVENT_TYPES)
        return list(self.sql(f"SELECT * FROM event_log WHERE {q}"))

    def append(self, type_: str, payload: dict[str, Any]) -> None:
        self.call("append_event", ACTOR, type_, json.dumps(payload, default=str, separators=(",", ":")))

    def create(self, ticket_id: int, spec: dict[str, Any], *, source: str, by: str = "orchestrator",
               llm: Any = None, rows: list[dict] | None = None) -> dict[str, Any]:
        spec = ss.validate(spec)
        with self._lock:
            have = ticket_revisions(rows if rows is not None else self.rows(), ticket_id)
            rev = max((int(e.get("revision") or 0) for e in have), default=0) + 1
            sid = scene_id(ticket_id, rev)
            spec = {**spec, "meta": {**spec.get("meta", {}), "ticket_id": ticket_id, "revision": rev, "scene_id": sid,
                                     "source": source}}
            payload = {"ticket_id": ticket_id, "revision": rev, "scene_id": sid, "source": source, "by": by,
                       "task": spec.get("task"), "summary": ss.summary(spec), "spec": spec}
            if spec["meta"].get("fixes"):
                payload["fixes"] = spec["meta"]["fixes"]
            if llm is not None:
                payload["llm"] = llm
            self.append(EV_CREATED, payload)
            self._await(lambda rs: any(e.get("scene_id") == sid for e in ticket_revisions(rs, ticket_id)))
        log.info("scene.created", extra={"fields": {"ticket_id": ticket_id, "revision": rev, "source": source,
                                                    "success": ss.describe_success(spec)}})
        return payload

    def fail(self, ticket_id: int, err: Exception) -> None:
        self.append(EV_FAILED, {"ticket_id": ticket_id, "error": str(err)[:500],
                                "errors": getattr(err, "errors", [])[:20], "attempts": getattr(err, "attempts", 0),
                                "llm": getattr(err, "usage", None)})

    def generate(self, ticket_id: int, task: str, *, hint: str | None = None, by: str = "orchestrator",
                 llm: Any = None) -> dict[str, Any]:
        try:
            spec, info = generate_spec(task, llm=llm, hint=hint)
        except SceneGenerationError as e:
            self.fail(ticket_id, e)
            raise
        return self.create(ticket_id, spec, source="llm", by=by, llm=info)

    def deploy(self, ticket_id: int, robot_id: int, *, revision: int | None = None, by: str = "operator",
               rows: list[dict] | None = None) -> dict[str, Any]:
        rs = rows if rows is not None else self.rows()
        revs = ticket_revisions(rs, ticket_id)
        e = revs[-1] if revs and revision is None else next((x for x in revs if x.get("revision") == revision), None)
        if e is None:
            raise LookupError(f"ticket {ticket_id} has no scene" + (f" revision {revision}" if revision else ""))
        payload = {"ticket_id": ticket_id, "revision": e["revision"], "scene_id": e["scene_id"], "robot_id": robot_id,
                   "deploy_id": uuid.uuid4().hex[:12], "by": by, "summary": e.get("summary"), "spec": e["spec"]}
        self.append(EV_DEPLOYED, payload)
        log.info("scene.deployed", extra={"fields": {k: payload[k] for k in ("ticket_id", "revision", "robot_id", "deploy_id")}})
        return payload

    def _await(self, ok: Callable[[list[dict]], bool]) -> None:
        """Reducers return nothing: read back until our row is visible (keeps revision numbers unique)."""
        for i in range(self.read_retries):
            if ok(self.rows()):
                return
            time.sleep(min(0.05 * (i + 1), 0.3))


__all__ = ["ACTOR", "EVENT_TYPES", "EV_CREATED", "EV_DEPLOYED", "EV_FAILED", "EV_LOADED", "SYSTEM", "SceneGenerationError",
           "SceneStore", "events", "generate_spec", "is_sim_robot", "pick_sim_robot", "robot_scene", "scene_id",
           "ticket_revisions", "ticket_scene"]
