"""TDM-62: the robot's scene-reset routine, i.e. what a person at the table does for a ``reset`` task.

A routine is a few plain steps plus an optional reference photo of the START state, stored per
robot in the media store (TDM-4) so the onboarding step (CONTEXT.md: "a Droid writes or validates
... the reset routine"), the operator CLI and the reset page all read and write one place:

    robots/<robot_id>/reset_routine.json   {"v": 1, "steps": [...], "reference_uri": ..., "updated_at": ..., "source": ...}
    robots/<robot_id>/reset_reference.jpg  photo of the start state (optional)

Precedence: a routine stored here (operator) > the TDM-32 Onboarding Droid's ``reset_routine.yaml``
as published in ``robot.camera_config_json["onboarding"]["reset_routine"]`` (``human_steps``,
``operator_prompt``, ``est_seconds``) > ``default_steps``. Nothing in SpacetimeDB changes. Set one with
``services/orchestrator/scripts/tdm62_reset_routine.py`` (``set --robot-id N --step ... --photo``).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .log import get_logger

log = get_logger("reset_routine")

MAX_STEPS = 8
MAX_STEP_CHARS = 200
MAX_PHOTO_BYTES = 6 * 1024 * 1024


def routine_key(robot_id: int) -> str:
    return f"robots/{int(robot_id)}/reset_routine.json"


def reference_key(robot_id: int) -> str:
    return f"robots/{int(robot_id)}/reset_reference.jpg"


def evidence_prefix(ticket_id: int, task_id: int) -> str:
    return f"tickets/{int(ticket_id)}/resets/{int(task_id)}/"


def evidence_key(ticket_id: int, task_id: int, source: str, ts: float | None = None) -> str:
    safe = "camera" if source == "camera" else "phone"
    return f"{evidence_prefix(ticket_id, task_id)}{safe}-{int((ts or time.time()) * 1000)}.jpg"


def default_steps(task_description: str = "") -> list[str]:
    """Generic steps when onboarding has not stored a routine for the robot."""
    del task_description  # the page shows it as the title; the steps stay short and generic
    return [
        "Check the banner at the top says Safe to reset. If it says the arm may move, wait.",
        "Put every object the robot touched back on its start spot, as in the picture above.",
        "Clear anything else off the table (dropped or extra items).",
        "Step back out of the arm’s reach.",
        "Tap Done. The robot starts its next try right away.",
    ]


def is_image(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"


@dataclass
class ResetRoutine:
    robot_id: int
    steps: list[str] = field(default_factory=list)
    reference_uri: str | None = None
    updated_at: float | None = None
    source: str = "default"  # default | onboarding | stored
    prompt: str | None = None  # onboarding operator_prompt: one line shown above the steps
    est_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_steps(steps: list[Any]) -> list[str]:
    out = [str(s).strip()[:MAX_STEP_CHARS] for s in steps if str(s).strip()]
    return out[:MAX_STEPS]


def onboarding_routine(camera_config_json: Any) -> dict:
    """The TDM-32 onboarding reset routine from ``robot.camera_config_json`` ({} if none)."""
    try:
        cc = json.loads(camera_config_json) if isinstance(camera_config_json, str) else (camera_config_json or {})
        rr = ((cc or {}).get("onboarding") or {}).get("reset_routine") or {}
        return rr if isinstance(rr, dict) else {}
    except (TypeError, ValueError, AttributeError):
        return {}


def from_onboarding(robot_id: int, spec: dict) -> ResetRoutine | None:
    """Steps for the person: safety check, the onboarding ``human_steps``, step back, tap Done."""
    human = _clean_steps(spec.get("human_steps") or [])
    if not human:
        return None
    base = default_steps()
    steps = _clean_steps([base[0], *human, base[-2], base[-1]])
    if len(steps) < len(human) + 3:  # too many human steps for MAX_STEPS: keep them all, drop the extras
        steps = _clean_steps([*human, base[-1]])
    est = spec.get("est_seconds")
    return ResetRoutine(robot_id=int(robot_id), steps=steps, source="onboarding",
                        prompt=(str(spec.get("operator_prompt") or "").strip()[:MAX_STEP_CHARS] or None),
                        est_seconds=float(est) if isinstance(est, (int, float)) else None)


def load(robot_id: int, task_description: str = "", *, onboarding: dict | None = None, backend: Any = None,
         http: Any = None) -> ResetRoutine:
    """Stored routine > onboarding routine > default steps. Never raises (the page must always render)."""
    r = (from_onboarding(robot_id, onboarding) if onboarding else None) or \
        ResetRoutine(robot_id=int(robot_id), steps=default_steps(task_description))
    try:
        from . import storage

        be = backend or storage.get_backend()
        names = {o.key for o in be.list(f"robots/{int(robot_id)}/", limit=50)}
        if reference_key(robot_id) in names:
            r.reference_uri = storage.make_uri(be.scheme, be.bucket, reference_key(robot_id))
        if routine_key(robot_id) in names:
            import httpx

            url = be.signed_get(routine_key(robot_id), ttl_s=60).url
            resp = (http or httpx).get(url, timeout=10.0)
            resp.raise_for_status()
            doc = resp.json()
            steps = _clean_steps(doc.get("steps") or [])
            if steps:
                r.steps, r.source = steps, "stored"
            r.updated_at = doc.get("updated_at")
            r.reference_uri = doc.get("reference_uri") or r.reference_uri
            if r.reference_uri:
                r.source = "stored"
    except Exception as e:  # noqa: BLE001 - storage down or not configured: defaults
        log.info("reset_routine.load_default", extra={"fields": {"robot_id": robot_id, "why": f"{type(e).__name__}: {e}"[:200]}})
    return r


def save(robot_id: int, steps: list[str], photo: bytes | None = None, *, source: str = "operator",
         backend: Any = None) -> ResetRoutine:
    """Store the routine (and the start-state photo). Overwrites the previous one."""
    from . import storage

    be = backend or storage.get_backend()
    steps = _clean_steps(steps)
    if not steps:
        raise ValueError("a reset routine needs at least one step")
    ref = None
    if photo is not None:
        if len(photo) > MAX_PHOTO_BYTES or not is_image(photo):
            raise ValueError("reference photo must be a JPEG/PNG under 6 MB")
        ref = be.put(reference_key(robot_id), photo, "image/jpeg")
    else:
        prev = load(robot_id, backend=be)
        ref = prev.reference_uri
    doc = {"v": 1, "robot_id": int(robot_id), "steps": steps, "reference_uri": ref, "updated_at": time.time(),
           "source": source}
    be.put(routine_key(robot_id), json.dumps(doc, indent=2).encode(), "application/json")
    return ResetRoutine(robot_id=int(robot_id), steps=steps, reference_uri=ref, updated_at=doc["updated_at"],
                        source="stored")


__all__ = ["ResetRoutine", "default_steps", "evidence_key", "evidence_prefix", "from_onboarding", "is_image", "load",
           "onboarding_routine",
           "reference_key", "routine_key", "save"]
