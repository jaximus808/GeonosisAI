"""Claude judges a REAL robot's eval trial from its live cameras (handlers/trial_judge.py wires it).

The robot-side runner (``rdf_robot_client.eval_trials``) ends a rollout, holds the arm, logs
``eval.trial_awaiting_outcome`` {eval_run_id, index, robot_id, ticket_id, ...} and waits
``outcome_timeout_s`` for a ``trial_outcome`` command. Nobody stands at the page at 3 AM, so the
orchestrator answers it:

  1. mint a ``view`` stream token (``RDF_STREAM_TOKEN_SECRET``) and GET ``/snapshot/<cam>.jpg`` from the
     robot's stream server (``robot.camera_config_json.stream.http``) for ``top`` and ``wrist``, a few
     frames over ~1 s (the arm holds, so this is the final scene);
  2. one structured vision call through ``rdf_common.llm`` (step ``eval_judge``): strict JSON
     ``{success, confidence, note}``; failure unless the object is clearly in / on the target;
  3. ``issue_robot_command(robot, "trial_outcome", {success, note: "[Claude judge] ... (conf 0.xx)",
     eval_run_id, index})``;
  4. ``eval.trial_judged`` {eval_run_id, index, success, confidence, note, model} in event_log, and a
     ``✻ Judge: fail · <reason>`` thought in the cockpit transcript.

Any snapshot / LLM / reducer failure logs ``eval.judge_failed`` and sends nothing: the robot's own
timeout still records "outcome not confirmed". Sim robots are skipped (they auto-judge from sim state).
``RDF_EVAL_JUDGE=0`` turns it off (default on).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

ACTOR = "orchestrator:eval_judge"
EVENT_AWAITING = "eval.trial_awaiting_outcome"
EVENT_JUDGED = "eval.trial_judged"
EVENT_FAILED = "eval.judge_failed"
NOTE_TAG = "[Claude judge]"
CAMS = ("top", "wrist")
FRAMES_PER_CAM = 2
FRAME_GAP_S = 0.5

SYSTEM_PROMPT = """You are the success judge for one evaluation trial of a robot arm policy on a REAL robot.
The rollout has ended and the arm is holding still, so the camera frames show the FINAL state of the scene.
You get the task instruction and a few frames from a fixed overhead/scene camera ("top") and from the
gripper's wrist camera ("wrist").

Decide whether the task was accomplished in the final state.
- success = true ONLY if the target object is clearly in / on the target location as the task asks
  (e.g. the cube is resting on the plate, not beside it, not still in the gripper above it, not hanging off the edge).
- If you are unsure, the view is blocked, the object is not visible, or it is ambiguous: success = false.
- confidence: 0..1, how sure you are of YOUR verdict.
- note: one short plain sentence (under 15 words) saying where the object ended up, e.g.
  "cube is beside the plate" or "cube sits on the plate".
Answer with the JSON object only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["success", "confidence", "note"],
    "additionalProperties": False,
}


class JudgeError(RuntimeError):
    def __init__(self, stage: str, msg: str):
        super().__init__(msg)
        self.stage = stage


@dataclass
class Verdict:
    success: bool
    confidence: float
    note: str
    model: str = ""

    def command_note(self) -> str:
        return f"{NOTE_TAG} {self.note} (conf {self.confidence:.2f})"[:1000]


def enabled() -> bool:
    return os.environ.get("RDF_EVAL_JUDGE", "1").strip().lower() not in ("0", "false", "off", "no")


def _json(text: Any) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def stream_http_base(robot: dict) -> str | None:
    """The robot stream server's http(s) base: ``camera_config_json.stream.http``, else derived from the ws url."""
    info = _json(robot.get("camera_config_json")).get("stream") or {}
    http = str(info.get("http") or "").strip()
    if not http:
        ws = os.environ.get("RDF_STREAM_URL", "").strip() or str(info.get("url") or "").strip()
        if not ws:
            return None
        http = re.sub(r"/ws/[A-Za-z_]+/?$", "", re.sub(r"^ws", "http", ws))
    return http.rstrip("/")


def cams_of(robot: dict) -> list[str]:
    have = [str(c) for c in (_json(robot.get("camera_config_json")).get("stream") or {}).get("cams") or []]
    picked = [c for c in CAMS if c in have] if have else list(CAMS)
    return picked or have[:2]


def mint_view_token(robot: dict) -> str:
    from rdf_common.stream_token import load_secret, mint

    tok, _ = mint(load_secret(), robot_id=int(robot.get("id") or 0), robot_name=str(robot.get("name") or ""),
                  sub="ops:eval_judge", ttl_s=60, scope="view")
    return tok


def http_snapshot(base: str, cam: str, token: str) -> bytes:
    import httpx

    r = httpx.get(f"{base}/snapshot/{cam}.jpg", params={"token": token}, timeout=10.0, follow_redirects=True)
    r.raise_for_status()
    if not r.content.startswith(b"\xff\xd8"):
        raise RuntimeError(f"snapshot {cam}: not a JPEG ({r.headers.get('content-type')})")
    return r.content


def fetch_frames(robot: dict, *, fetch: Callable[[str, str, str], bytes] = http_snapshot,
                 token: Callable[[dict], str] = mint_view_token, frames: int = FRAMES_PER_CAM,
                 gap_s: float = FRAME_GAP_S, sleep: Callable[[float], None] = time.sleep) -> list[tuple[str, bytes]]:
    """[(label, jpeg)] for each camera, ``frames`` rounds ``gap_s`` apart. Raises JudgeError('snapshot')."""
    base = stream_http_base(robot)
    if not base:
        raise JudgeError("snapshot", "the robot has not published a stream URL (camera_config_json.stream)")
    try:
        tok = token(robot)
    except Exception as e:  # noqa: BLE001 - e.g. RDF_STREAM_TOKEN_SECRET missing; never echo the value
        raise JudgeError("snapshot", f"could not mint a view token: {type(e).__name__}") from None
    out: list[tuple[str, bytes]] = []
    errors: list[str] = []
    for k in range(frames):
        if k:
            sleep(gap_s)
        for cam in cams_of(robot):
            try:
                out.append((f"{cam} frame {k + 1}/{frames}", fetch(base, cam, tok)))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{cam}: {type(e).__name__}: {str(e)[:120]}")
    if not out:
        raise JudgeError("snapshot", "no camera snapshot: " + "; ".join(errors[:4]))
    return out


def build_messages(task: str, frames: list[tuple[str, bytes]], trial: str = "") -> list[dict]:
    content: list[dict] = [{"type": "text", "text": f"Task: {task or '(no task description)'}"
                            + (f"\nTrial: {trial}" if trial else "")
                            + f"\n{len(frames)} camera frames of the final scene follow."}]
    for label, jpeg in frames:
        content.append({"type": "text", "text": f"Frame: {label}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": "Did the robot accomplish the task? Answer with the JSON verdict."})
    return [{"role": "user", "content": content}]


def ask_claude(task: str, frames: list[tuple[str, bytes]], *, trial: str = "", client: Any = None) -> Verdict:
    """One structured vision call through the shared ``rdf_common.llm`` client. Raises JudgeError('llm')."""
    from rdf_common.llm import as_llm, describe_result

    model = os.environ.get("RDF_EVAL_JUDGE_MODEL", "claude-opus-5-5")
    try:
        r = as_llm(client).complete_structured(
            step="eval_judge", system=SYSTEM_PROMPT, messages=build_messages(task, frames, trial), schema=SCHEMA,
            model=model, effort=os.environ.get("RDF_EVAL_JUDGE_EFFORT", "medium"), max_tokens=8000, thinking=True,
            timeout_s=float(os.environ.get("RDF_EVAL_JUDGE_TIMEOUT_S", "75")))
    except Exception as e:  # noqa: BLE001 - API error, refusal, bad JSON: no verdict
        raise JudgeError("llm", f"{type(e).__name__}: {str(e)[:300]}") from None
    d = _json(getattr(r, "text", ""))
    if not isinstance(d.get("success"), bool):
        raise JudgeError("llm", f"no verdict in Claude's answer: {str(getattr(r, 'text', ''))[:200]}")
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    note = re.sub(r"\s+", " ", str(d.get("note") or "")).strip()[:300] or ("looks done" if d["success"] else "not done")
    meta = getattr(r, "meta", None) or {}
    return Verdict(success=bool(d["success"]), confidence=conf, note=note,
                   model=describe_result(meta) if meta else model)


__all__ = ["ACTOR", "EVENT_AWAITING", "EVENT_FAILED", "EVENT_JUDGED", "JudgeError", "Verdict", "ask_claude",
           "build_messages", "enabled", "fetch_frames", "stream_http_base"]
