"""LLM QA for purchased dataset episodes (TDM-93): task relevance and success, with reasons.

Purchased episodes come from third-party sellers, so before we pay for one we ask Claude two things
the deterministic checks cannot answer: is this the ticket's task (the same objects, the same kind of
scene), and did it succeed? The bus handler is ``handlers/llm_qa.py``; the proof is
``scripts/tdm93_prove_llm_qa.py``.

Pipeline, per purchased episode:

  1. GATE (explicit, ``gate_status``): TDM-92's deterministic QA must have finished and passed. TDM-92
     writes a summary ``qa_result`` row ``deterministic_qa`` LAST, ``passed=true`` only when every
     deterministic check passed. We run only when that latest row passed, no other latest
     ``qa_result`` row failed, and the episode has no ``review`` yet. On a deterministic failure TDM-92
     writes the rejecting review itself, and Claude is never called.
  2. One Claude call (``claude-opus-5-5``, vision, structured output) per episode. Every frame goes
     into that one call: start / middle / end from the exo camera and from the wrist camera (6 frames),
     plus a text summary of the actions parquet (gripper close/open times, end-effector travel, who
     drove). The prompt includes the ticket's task_description and the policy prompt from run_settings.
  3. Verdict -> two ``qa_result`` rows, then the final ``submit_review``:
       llm_relevance  passed = on_task (the right task with the ticket's objects and scene)
       llm_success    passed = task_succeeded == "yes" and quality >= min_quality (default 3)
       accepted = on_task and task_succeeded == "yes" and quality >= min_quality
     A rejection's reason names the mismatch or what failed, so the seller sees why.
  4. Guard (``acceptance_guard``): after each verdict, log ``qa.acceptance_rate`` for the offer, with
     ``suspect=true`` when more than 95% are accepted over at least 5 reviews. An LLM reviewer that
     approves everything gets flagged instead of trusted.

Frame extraction and media fetch reuse the TDM-25 reviewer (``reviewer.fetch_local``,
``reviewer.extract_frames``, ``reviewer.load_actions``), and the Claude client set-up reuses
``reviewer.ClaudeJudge``.

Outages are not rejections. A storage error on our side, a Claude API error, a refusal or invalid
output raises ``LlmQaError`` (a ``reviewer.ReviewError``), and NOTHING is written except a
``qa.error`` event. The episode stays pending; the handler retries with backoff, bounded and counted
from those ``qa.error`` rows (``llm_retry.py``; a refusal is retried with a context preface). After the
last attempt it logs ``qa.failed`` and still writes no review: the episode waits for a human, and
``append_event qa.retry_requested {episode_id}`` starts a fresh round.

Cost: one call per episode, with all frames in that call. ``call_cap`` limits calls per offer to
max(4, 2 x needed_count), retries included. Once an offer reaches the cap, its episodes stay pending
and a ``qa.llm_capped`` event is logged.
"""
from __future__ import annotations

import base64
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from rdf_common.episode_format import EpisodeActions, cameras_from_json
from rdf_common.review_checks import FAIL, PASS, CheckResult

from . import reviewer as rv
from .log import get_logger

log = get_logger("llm_qa")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_qa.yaml"
ACTOR = "orchestrator.llm_qa"
EVENT_ERROR = "qa.error"
EVENT_FAILED = "qa.failed"  # retries exhausted: the episode stays pending for a human (llm_retry.py)
EVENT_RETRY_REQUESTED = "qa.retry_requested"  # {episode_id}: a human restarts the retry count
EVENT_CAPPED = "qa.llm_capped"
EVENT_RATE = "qa.acceptance_rate"
CHECK_RELEVANCE = "llm_relevance"
CHECK_SUCCESS = "llm_success"
LLM_CHECKS = (CHECK_RELEVANCE, CHECK_SUCCESS)
WRIST_HINTS = ("wrist", "hand", "gripper", "eef")


class LlmQaError(rv.ReviewError):
    """An infrastructure failure (ours, not the seller's): write nothing but a qa.error event."""


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class LlmQaConfig:
    summary_check: str = "deterministic_qa"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    cameras: int = 2
    points: tuple[float, ...] = (0.03, 0.5, 0.97)
    frame_width: int = 448
    min_quality: int = 3
    max_calls_per_offer_factor: float = 2.0
    max_calls_per_offer_min: int = 4
    guard_max_rate: float = 0.95
    guard_min_reviews: int = 5
    version: int = 1


_SECTIONS = {
    "gate": {"summary_check": "summary_check"},
    "claude": {"model": "model", "effort": "effort", "cameras": "cameras", "points": "points",
               "frame_width": "frame_width", "min_quality": "min_quality"},
    "cost": {"max_calls_per_offer_factor": "max_calls_per_offer_factor",
             "max_calls_per_offer_min": "max_calls_per_offer_min"},
    "guard": {"max_rate": "guard_max_rate", "min_reviews": "guard_min_reviews"},
}


def load_config(path: str | Path | None = None, **overrides: Any) -> LlmQaConfig:
    import yaml

    p = Path(path or os.environ.get("RDF_LLM_QA_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(p.read_text()) or {}
    kw: dict[str, Any] = {"version": int(raw.pop("version", 1))}
    for section, body in raw.items():
        if section not in _SECTIONS:
            raise ValueError(f"llm_qa config: unknown section {section!r}")
        for k, v in (body or {}).items():
            if k not in _SECTIONS[section]:
                raise ValueError(f"llm_qa config: unknown key {section}.{k}")
            kw[_SECTIONS[section][k]] = tuple(float(x) for x in v) if k == "points" else v
    kw.update(overrides)
    cfg = LlmQaConfig(**kw)
    if not 1 <= cfg.min_quality <= 5:
        raise ValueError("llm_qa config: claude.min_quality must be 1..5")
    if not cfg.points or any(not 0.0 <= x <= 1.0 for x in cfg.points):
        raise ValueError("llm_qa config: claude.points must be fractions in [0, 1]")
    return cfg


def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return getattr(row, name, default)


# --------------------------------------------------------------------------- gate

@dataclass
class Gate:
    ready: bool
    why: str


def latest_checks(qa_rows: Iterable[Any]) -> dict[str, Any]:
    """check_name -> its latest qa_result row (highest id wins, as in TDM-14)."""
    out: dict[str, Any] = {}
    for r in qa_rows:
        name = str(_g(r, "check_name"))
        if name not in out or int(_g(r, "id") or 0) > int(_g(out[name], "id") or 0):
            out[name] = r
    return out


def gate_status(episode: Any, qa_rows: Iterable[Any], has_review: bool, cfg: LlmQaConfig) -> Gate:
    """Should LLM QA run on this episode now? Runs only after TDM-92's deterministic QA passed."""
    if _g(episode, "kind") != "purchased":
        return Gate(False, f"kind {_g(episode, 'kind')} is not purchased (TDM-25 reviews it)")
    if _g(episode, "review_status") != "pending" or has_review:
        return Gate(False, "already reviewed")
    latest = latest_checks(qa_rows)
    summary = latest.get(cfg.summary_check)
    if summary is None:
        return Gate(False, f"waiting for deterministic QA ({cfg.summary_check} not recorded yet)")
    if not bool(_g(summary, "passed")):
        return Gate(False, f"deterministic QA failed ({cfg.summary_check}=false); TDM-92 rejects it")
    failed = sorted(n for n, r in latest.items() if n not in LLM_CHECKS and not bool(_g(r, "passed")))
    if failed:
        return Gate(False, f"deterministic check(s) failed: {', '.join(failed)}")
    return Gate(True, "deterministic QA passed")


# --------------------------------------------------------------------------- cost cap

def call_cap(offer: Any, cfg: LlmQaConfig) -> int:
    needed = int(_g(offer, "needed_count") or 0)
    return max(int(cfg.max_calls_per_offer_min), math.ceil(needed * float(cfg.max_calls_per_offer_factor)))


# --------------------------------------------------------------------------- inputs: actions summary + frames

def _crossings(t_s, g, thr: float = 0.5) -> tuple[list[float], list[float]]:
    closes, opens = [], []
    for i in range(1, len(g)):
        if g[i - 1] >= thr > g[i]:
            closes.append(round(float(t_s[i]), 2))
        elif g[i - 1] < thr <= g[i]:
            opens.append(round(float(t_s[i]), 2))
    return closes, opens


def action_summary(actions: EpisodeActions | None) -> dict:
    """Facts from the actions parquet that frames alone may not show."""
    if actions is None or actions.n < 2:
        return {"available": False}
    import numpy as np

    t = (actions.timestamp_us - actions.timestamp_us[0]) / 1e6
    out: dict[str, Any] = {"available": True, "duration_s": round(float(t[-1]), 2), "ticks": actions.n,
                           "fps": actions.fps}
    g = actions.extra.get("gripper_cmd")
    if g is None:
        g = actions.extra.get("gripper_pos")
    if g is not None and len(g) == actions.n:
        g = np.asarray(g, dtype=float)
        closes, opens = _crossings(t, g)
        out["gripper"] = {"starts": "open" if g[0] >= 0.5 else "closed", "ends": "open" if g[-1] >= 0.5 else "closed",
                          "closes_at_s": closes[:10], "opens_at_s": opens[:10]}
    p = actions.extra.get("ik_target_pos")
    if p is not None and getattr(p, "ndim", 0) == 2 and p.shape[0] == actions.n and p.shape[1] >= 3:
        p = np.asarray(p, dtype=float)
        step = np.linalg.norm(np.diff(p, axis=0), axis=1)
        out["ee"] = {"path_length_m": round(float(step.sum()), 3),
                     "net_displacement_m": round(float(np.linalg.norm(p[-1] - p[0])), 3),
                     "start_xyz_m": [round(float(x), 3) for x in p[0][:3]],
                     "end_xyz_m": [round(float(x), 3) for x in p[-1][:3]],
                     "z_range_m": round(float(p[:, 2].max() - p[:, 2].min()), 3)}
    src = actions.control_source
    if src:
        out["control_source"] = {s: round(src.count(s) / len(src), 2) for s in sorted(set(src))}
    return out


def _summary_text(s: dict) -> str:
    if not s.get("available"):
        return "Action log: not available."
    lines = [f"Action log: {s['duration_s']} s, {s['ticks']} control ticks at {s['fps']} fps."]
    if "gripper" in s:
        gr = s["gripper"]
        lines.append(f"Gripper command: starts {gr['starts']}, ends {gr['ends']}; closes at "
                     f"{gr['closes_at_s'] or 'never'} s, opens at {gr['opens_at_s'] or 'never'} s.")
    if "ee" in s:
        e = s["ee"]
        lines.append(f"End-effector target: travels {e['path_length_m']} m in total, ends "
                     f"{e['net_displacement_m']} m from where it started (start {e['start_xyz_m']}, end "
                     f"{e['end_xyz_m']}, vertical range {e['z_range_m']} m).")
    if "control_source" in s:
        lines.append(f"Who drove (fraction of ticks): {s['control_source']}.")
    return "\n".join(lines)


def pick_cameras(cams: dict[str, str], n: int = 2) -> list[tuple[str, str]]:
    """A scene camera first (TDM-25's preference order, wrist excluded), then a wrist camera."""
    if not cams:
        return []
    wrist = [k for k in sorted(cams) if any(h in k.lower() for h in WRIST_HINTS)]
    scene = rv.pick_camera({k: v for k, v in cams.items() if k not in wrist})
    out = [scene] if scene else []
    if wrist and len(out) < n:
        out.append((wrist[0], cams[wrist[0]]))
    for k in sorted(cams):
        if len(out) >= n:
            break
        if all(k != name for name, _ in out):
            out.append((k, cams[k]))
    return out[:n]


@dataclass
class Frames:
    cameras: list[str]
    frames: list[tuple[str, bytes]]  # (label, jpeg), time-major: start exo, start wrist, mid exo, ...
    errors: dict[str, str] = field(default_factory=dict)


def grab_frames(episode: Any, cfg: LlmQaConfig, **fetch_kw) -> Frames:
    """Start / middle / end frames from each chosen camera, via the TDM-25 reviewer's helpers."""
    dur = (_g(episode, "duration_ms") or 0) / 1000.0
    names = {0: "start", 1: "middle", 2: "end"} if len(cfg.points) == 3 else {}
    times = [(names.get(i, f"{int(f * 100)}% through"), dur * f) for i, f in enumerate(cfg.points)]
    per_cam: dict[str, list[tuple[str, bytes]]] = {}
    errors: dict[str, str] = {}
    for name, uri in pick_cameras(cameras_from_json(_g(episode, "cameras_json")), cfg.cameras):
        try:
            with rv.fetch_local(uri, suffix=".mp4", **fetch_kw) as p:
                got = rv.extract_frames(p, times, cfg.frame_width)
        except rv.ReviewError as e:
            raise LlmQaError(str(e)) from e
        except FileNotFoundError as e:
            errors[name] = str(e)
            continue
        except Exception as e:  # noqa: BLE001
            import httpx

            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404):
                errors[name] = f"video not found in storage (HTTP {e.response.status_code})"
                continue
            raise LlmQaError(f"could not fetch {name} video {uri}: {type(e).__name__}: {e}") from e
        if got:
            per_cam[name] = got
        else:
            errors[name] = f"no frames could be decoded from the {name} video"
    frames: list[tuple[str, bytes]] = []
    for i in range(len(times)):
        for cam, got in per_cam.items():
            if i < len(got):
                frames.append((f"{cam} camera, {got[i][0]}", got[i][1]))
    return Frames(list(per_cam), frames, errors)


# --------------------------------------------------------------------------- Claude judgement

class LlmJudgement(BaseModel):
    observations: str
    scene_matches: bool
    on_task: bool
    mismatch: str = ""
    task_succeeded: Literal["yes", "no", "unclear"]
    quality: int = Field(ge=1, le=5)
    issues: list[str] = Field(default_factory=list)
    reason: str


JUDGEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "observations": {"type": "string", "description":
                         "What the frames show, camera by camera and in time order: the objects, the scene, "
                         "where the gripper is, what changed."},
        "scene_matches": {"type": "boolean", "description":
                          "True if the scene and objects are the ones the ticket's task names (or clearly "
                          "equivalent), false if the objects or setting are different."},
        "on_task": {"type": "boolean", "description":
                    "True if the recording is an attempt at the ticket's task with the ticket's objects."},
        "mismatch": {"type": "string", "description":
                     "When on_task or scene_matches is false: what differs from the ticket, concretely "
                     "(e.g. 'stacks a green block on a yellow block; there is no cup or bin'). Empty otherwise."},
        "task_succeeded": {"type": "string", "enum": ["yes", "no", "unclear"],
                           "description": "Do the end frames show the ticket's goal state?"},
        "quality": {"type": "integer", "enum": [1, 2, 3, 4, 5], "description":
                    "Usefulness as a training demonstration of the ticket's task: 1 useless or a different task, "
                    "3 acceptable, 5 a clean direct demonstration."},
        "issues": {"type": "array", "items": {"type": "string"},
                   "description": "Concrete problems seen. Empty if none."},
        "reason": {"type": "string", "description":
                   "One or two sentences for the seller explaining the verdict, citing what the frames show."},
    },
    "required": ["observations", "scene_matches", "on_task", "mismatch", "task_succeeded", "quality", "issues",
                 "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You check robot-arm demonstration episodes that we are buying from a third-party \
dataset seller, before we pay for them. Each episode is meant to train a robot on one specific task, \
described in the ticket. Deterministic checks (format, embodiment, duration, frame drops, smoothness, \
duplicates) have already passed. Your job is what they cannot check: is this episode a demonstration of \
the ticket's task, and did it succeed?

You get the ticket's task description, the policy prompt if there is one, a summary of the episode's \
action log, and frames from the start, middle and end of the episode, from a scene camera and a wrist \
camera, in time order.

Decide:
- scene_matches: are the task's objects (the same kind, colour and role) and the kind of setting present? \
A seller's dataset recorded for a different task often has different objects.
- on_task: is the arm attempting the ticket's task with the ticket's objects? False when it does a \
different task (for example stacking blocks when the ticket asks to put a cup in a bin), handles other \
objects, or moves aimlessly. When false, say in `mismatch` exactly what differs.
- task_succeeded: "yes" if the end frames show the goal state, "no" if they show it was not reached (the \
object is still where it started, was dropped, or is in the wrong place), "unclear" if the frames cannot \
tell. The action log helps you here: a gripper that never closes cannot have carried anything.
- quality: how useful the episode is as a demonstration of this task, from 1 to 5.

Judge from what the frames show. Low resolution, lighting or a simple rendering style are not reasons \
to reject unless the frames cannot be read. Do not assume motion you cannot see. The seller reads the \
reason, so make it concrete and polite."""


class Judge(Protocol):
    model: str

    def judge(self, messages: list[dict]) -> LlmJudgement: ...


class ClaudeLlmQaJudge(rv.ClaudeJudge):
    """The TDM-25 call shape (structured output + vision + server-side refusal fallback, adaptive
    thinking), with the TDM-93 schema and prompt. Reuses ClaudeJudge's client and API-key handling."""

    def __init__(self, cfg: LlmQaConfig | None = None, client: Any = None):
        c = cfg or LlmQaConfig()
        super().__init__(rv.ReviewConfig(model=c.model, effort=c.effort), client)

    def judge(self, messages: list[dict]) -> LlmJudgement:  # type: ignore[override]
        import anthropic

        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=[rv.FALLBACK_BETA],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": JUDGEMENT_SCHEMA}},
                system=SYSTEM_PROMPT,
                messages=messages,
            )
        except anthropic.APIError as e:
            raise LlmQaError(f"Claude API error: {type(e).__name__}: {e}") from e
        u = getattr(resp, "usage", None)
        self.last_usage = {"input_tokens": getattr(u, "input_tokens", None),
                           "output_tokens": getattr(u, "output_tokens", None), "model": getattr(resp, "model", None)}
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LlmQaError(f"Claude refused the QA call (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise LlmQaError("Claude output hit max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if not text:
            raise LlmQaError(f"no text block in Claude response (stop_reason={resp.stop_reason})")
        try:
            return LlmJudgement.model_validate_json(text)
        except ValidationError as e:
            raise LlmQaError(f"Claude structured output did not validate: {e}") from e


def build_messages(ticket: Any, episode: Any, summary: dict, frames: Frames, prompt: str | None = None) -> list[dict]:
    head = [f"Ticket task: {_g(ticket, 'task_description') or '(no description)'}"]
    if prompt:
        head.append(f"Policy prompt for this task: {prompt}")
    head.append(f"Purchased episode {_g(episode, 'id')} from dataset offer {_g(episode, 'dataset_offer_id')}: "
                f"{(_g(episode, 'duration_ms') or 0) / 1000:.1f} s, {_g(episode, 'frame_count')} frames, "
                f"seller's success flag: {_g(episode, 'success')}.")
    head.append(_summary_text(summary))
    content: list[dict] = [{"type": "text", "text": "\n".join(head)},
                           {"type": "text", "text": f"{len(frames.frames)} frames follow, in time order "
                                                    f"(cameras: {', '.join(frames.cameras)})."}]
    for label, jpeg in frames.frames:
        content.append({"type": "text", "text": f"Frame: {label}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": "Is this episode the ticket's task, and did it succeed? Give your verdict."})
    return [{"role": "user", "content": content}]


# --------------------------------------------------------------------------- the decision

@dataclass
class LlmQaDecision:
    episode_id: int
    accepted: bool
    reason: str
    checks: list[CheckResult]  # llm_relevance, llm_success
    metrics: dict
    called_claude: bool = True

    def metrics_json(self) -> str:
        m = dict(self.metrics)
        s = json.dumps(m, default=str)
        if len(s) > 15000 and isinstance(m.get("claude"), dict):
            m["claude"] = {k: v for k, v in m["claude"].items() if k != "observations"}
            s = json.dumps(m, default=str)
        return s if len(s) <= 15000 else json.dumps({"reviewer": "tdm93_llm_qa", "truncated": True})


def _clip(s: str, n: int = 1200) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def decide(episode_id: int, v: LlmJudgement, cfg: LlmQaConfig, base: dict) -> LlmQaDecision:
    """accepted = on_task and task_succeeded == yes and quality >= min_quality."""
    on_task = bool(v.on_task and v.scene_matches)
    succeeded = v.task_succeeded == "yes"
    good = v.quality >= cfg.min_quality
    why = _clip(v.reason, 600)
    mismatch = _clip(v.mismatch, 400)
    rel_detail = {"on_task": v.on_task, "scene_matches": v.scene_matches, "mismatch": mismatch}
    suc_detail = {"task_succeeded": v.task_succeeded, "quality": v.quality, "min_quality": cfg.min_quality,
                  "issues": [_clip(i, 300) for i in v.issues[:8]]}
    relevance = CheckResult(CHECK_RELEVANCE, PASS if on_task else FAIL,
                            "" if on_task else f"not the ticket's task: {mismatch or why}", rel_detail)
    if succeeded and good:
        success = CheckResult(CHECK_SUCCESS, PASS, "", suc_detail)
    elif not succeeded:
        what = "the end frames show the task was not completed" if v.task_succeeded == "no" else \
            "the frames cannot confirm the task was completed"
        success = CheckResult(CHECK_SUCCESS, FAIL, f"{what}: {why}", suc_detail)
    else:
        success = CheckResult(CHECK_SUCCESS, FAIL, f"quality {v.quality}/5 is below {cfg.min_quality}: {why}", suc_detail)
    accepted = on_task and succeeded and good
    if accepted:
        reason = f"Accepted by LLM QA (quality {v.quality}/5): {why}"
    elif not on_task:
        lead = (mismatch if mismatch.endswith((".", "!", "?")) else mismatch + ".") if mismatch else ""
        reason = f"Rejected by LLM QA: not the ticket's task. {lead + ' ' if lead else ''}{why}".strip()
    else:
        reason = f"Rejected by LLM QA: {success.reason}"
    claude = {"model": base.pop("_model", None), "on_task": v.on_task, "scene_matches": v.scene_matches,
              "mismatch": mismatch, "task_succeeded": v.task_succeeded, "quality": v.quality,
              "observations": _clip(v.observations), "issues": suc_detail["issues"], "reason": _clip(v.reason),
              "usage": base.pop("_usage", None)}
    metrics = {**base, "decided_by": "llm_qa", "accepted": accepted, "claude": claude,
               "checks": {c.name: {"status": c.status, **({"reason": c.reason} if c.reason else {})}
                          for c in (relevance, success)}}
    return LlmQaDecision(episode_id, accepted, _clip(reason, 1500), [relevance, success], metrics)


def judge_episode(episode: Any, ticket: Any, *, cfg: LlmQaConfig, judge: Judge, prompt: str | None = None,
                  **fetch_kw) -> LlmQaDecision:
    """Judge one purchased episode (caller has checked ``gate_status``). Raises LlmQaError on outage."""
    eid = int(_g(episode, "id") or 0)
    try:
        actions, _err = rv.load_actions(episode, **fetch_kw)
    except rv.ReviewError as e:
        raise LlmQaError(str(e)) from e
    summary = action_summary(actions)
    fr = grab_frames(episode, cfg, **fetch_kw)
    base: dict[str, Any] = {"reviewer": "tdm93_llm_qa", "config_version": cfg.version, "kind": "purchased",
                            "dataset_offer_id": _g(episode, "dataset_offer_id"), "cameras": fr.cameras,
                            "frames": [lbl for lbl, _ in fr.frames], "action_summary": summary}
    if fr.errors:
        base["camera_errors"] = fr.errors
    if not fr.frames:
        why = "; ".join(f"{k}: {v}" for k, v in fr.errors.items()) or "episode has no camera video"
        checks = [CheckResult(CHECK_RELEVANCE, FAIL, f"no readable camera frames ({why})", {"skipped_claude": True}),
                  CheckResult(CHECK_SUCCESS, FAIL, "no readable camera frames", {"skipped_claude": True})]
        return LlmQaDecision(eid, False, f"Rejected by LLM QA: camera video unusable ({why}).", checks,
                             {**base, "decided_by": "llm_qa", "accepted": False, "claude": {"skipped": "no frames"}},
                             called_claude=False)
    v = judge.judge(build_messages(ticket, episode, summary, fr, prompt))
    base["_model"] = getattr(judge, "model", None)
    base["_usage"] = getattr(judge, "last_usage", None)
    return decide(eid, v, cfg, base)


# --------------------------------------------------------------------------- SpacetimeDB I/O
# ``stdb`` is an ``rdf_common.spacetime_client.SpacetimeClient`` (sync HTTP; reducer errors raise).

def record(stdb: Any, d: LlmQaDecision) -> None:
    """llm_relevance + llm_success qa_result rows, then the final submit_review (TDM-14 reads all three)."""
    for c in d.checks:
        stdb.call_reducer("record_qa_result", d.episode_id, c.name, c.status != FAIL, c.detail_json())
    stdb.call_reducer("submit_review", d.episode_id, d.accepted, d.reason, d.metrics_json())


def log_event(stdb: Any, type_: str, payload: dict) -> None:
    stdb.call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str)[:60000])


def log_error(stdb: Any, episode: Any, error: str, **extra: Any) -> None:
    """An outage: a qa.error event, never a rejection (``extra``: attempt, kind, ... from llm_retry)."""
    log_event(stdb, EVENT_ERROR, {"stage": "llm_qa", "episode_id": _g(episode, "id"),
                                  "ticket_id": _g(episode, "ticket_id"),
                                  "dataset_offer_id": _g(episode, "dataset_offer_id"), **extra, "error": error[:2000],
                                  "effect": "no review written; episode stays pending and is retried"})


def log_failed(stdb: Any, episode: Any, **fields: Any) -> None:
    """Terminal: retries exhausted. Still no review (an outage is never a rejection)."""
    log_event(stdb, EVENT_FAILED, {"stage": "llm_qa", "episode_id": _g(episode, "id"),
                                   "ticket_id": _g(episode, "ticket_id"),
                                   "dataset_offer_id": _g(episode, "dataset_offer_id"), **fields})


def offer_stats(stdb: Any, offer_id: int) -> dict:
    """Purchased-episode reviews for one offer, from SQL (authoritative, not the bus mirror)."""
    eps = {int(r["id"]) for r in stdb.sql(f"SELECT * FROM episode WHERE dataset_offer_id = {int(offer_id)}")}
    reviews = [r for r in stdb.sql("SELECT * FROM review") if int(r["episode_id"]) in eps] if eps else []
    llm_calls = 0
    if eps:
        llm_calls = sum(1 for r in stdb.sql(f"SELECT * FROM qa_result WHERE check_name = '{CHECK_RELEVANCE}'")
                        if int(r["episode_id"]) in eps and not json.loads(r["detail_json"] or "{}").get("skipped_claude"))
    return {"episodes": len(eps), "reviewed": len(reviews), "accepted": sum(1 for r in reviews if r["accepted"]),
            "llm_calls": llm_calls}


def acceptance_guard(stdb: Any, offer_id: int, ticket_id: Any, cfg: LlmQaConfig) -> dict:
    """Log the offer's acceptance rate; suspect=true when it looks like approve-everything."""
    s = offer_stats(stdb, offer_id)
    rate = s["accepted"] / s["reviewed"] if s["reviewed"] else None
    suspect = bool(rate is not None and s["reviewed"] >= cfg.guard_min_reviews and rate > cfg.guard_max_rate)
    payload = {"dataset_offer_id": offer_id, "ticket_id": ticket_id, "reviewed": s["reviewed"],
               "accepted": s["accepted"], "rate": None if rate is None else round(rate, 3),
               "max_rate": cfg.guard_max_rate, "min_reviews": cfg.guard_min_reviews, "suspect": suspect}
    log_event(stdb, EVENT_RATE, payload)
    if suspect:
        log.warning("llm_qa.suspect_acceptance_rate", extra={"fields": payload})
    return payload


def run_after_deterministic(stdb: Any, episode: Any, ticket: Any, *, cfg: LlmQaConfig | None = None,
                            judge: Judge | None = None, offer: Any = None, prompt: str | None = None,
                            extra_calls: int = 0, log_errors: bool = True, **fetch_kw) -> LlmQaDecision | None:
    """The whole step for one episode, re-reading the gate from SQL. Returns None when it did not run
    (gate closed, cap reached, or an outage, which is logged as qa.error and then re-raised).

    TDM-92 can call this in-process after writing ``deterministic_qa``; the bus handler calls it too
    (with ``log_errors=False``: it logs the qa.error itself, with the retry fields)."""
    cfg = cfg or load_config()
    eid = int(_g(episode, "id"))
    ep = stdb.sql(f"SELECT * FROM episode WHERE id = {eid}")
    episode = ep[0] if ep else episode
    qa = stdb.sql(f"SELECT * FROM qa_result WHERE episode_id = {eid}")
    has_review = bool(stdb.sql(f"SELECT * FROM review WHERE episode_id = {eid}"))
    g = gate_status(episode, qa, has_review, cfg)
    if not g.ready:
        log.info("llm_qa.gate_closed", extra={"fields": {"episode_id": eid, "why": g.why}})
        return None
    offer_id = int(_g(episode, "dataset_offer_id") or 0)
    if offer is None and offer_id:
        rows = stdb.sql(f"SELECT * FROM dataset_offer WHERE id = {offer_id}")
        offer = rows[0] if rows else None
    if prompt is None:
        rs = stdb.sql(f"SELECT * FROM run_settings WHERE ticket_id = {int(_g(episode, 'ticket_id'))}")
        try:
            prompt = (json.loads(rs[0]["hyperparams_json"] or "{}").get("prompt") or None) if rs else None
        except ValueError:
            prompt = None
    cap = call_cap(offer or {}, cfg)
    used = offer_stats(stdb, offer_id)["llm_calls"] + int(extra_calls) if offer_id else 0
    if used >= cap:
        log_event(stdb, EVENT_CAPPED, {"episode_id": eid, "dataset_offer_id": offer_id, "calls": used, "cap": cap,
                                       "effect": "episode stays pending; raise cost.max_calls_per_offer_* to continue"})
        return None
    if judge is None:
        judge = ClaudeLlmQaJudge(cfg)
    try:
        d = judge_episode(episode, ticket, cfg=cfg, judge=judge, prompt=prompt, **fetch_kw)
    except rv.ReviewError as e:
        if log_errors:
            log_error(stdb, episode, f"{type(e).__name__}: {e}")
        raise
    d.metrics["cost"] = {"llm_calls_before": used, "cap": cap}
    record(stdb, d)
    if offer_id:
        acceptance_guard(stdb, offer_id, _g(episode, "ticket_id"), cfg)
    return d
