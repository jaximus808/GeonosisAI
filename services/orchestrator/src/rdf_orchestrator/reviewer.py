"""Episode reviewer (TDM-25, flywheel step 6 "Review"): deterministic checks, then a Claude judgement.

Pure logic plus media I/O; no subscription. The bus handler (``handlers/reviewer.py``) and the proof
script (``scripts/tdm25_prove_reviewer.py``) call it:

    cfg = load_config()                                   # config/review.yaml
    decision = review_episode(episode_row, ticket_row, config=cfg, judge=ClaudeJudge(cfg))
    record_review(stdb, episode_row["id"], decision)      # qa_result rows + submit_review

Order, and why:
  1. Fetch the actions parquet (``episode.actions_uri``; file://, http(s) or a TDM-4 storage URI) and
     run ``rdf_common.review_checks.run_checks`` (duration, frame drops, jerk, takeover timestamps,
     failure frames before the takeover, success flag). Any failure rejects with that check's
     reason and Claude is not called (no spend on an episode that is already out).
  2. Grab ``frames`` keyframes from the preferred camera mp4 (``episode.cameras_json``) with ffmpeg.
     No readable video -> reject (``camera_media``): we do not pay for what nobody can see.
  3. One Claude call (``claude-opus-5-5``, vision, structured output) judges task relevance: is the
     recording a demonstration of the ticket's task, and does the video contradict the success flag?
     ``on_task == false`` or ``task_completed == "no"`` rejects with Claude's reason.
  4. Everything passed -> accept. ``submit_review`` then reserves the payout (``payment`` pending, or
     blocked_budget over the cap) inside the same transaction (TDM-11).

Infrastructure failures (download error on our side, Claude API error, refusal) raise
``ReviewError`` and NOTHING is written: reviews are final, so an outage must not turn into a
rejection the contributor cannot appeal. The handler logs ``review.error`` and retries with backoff,
bounded (``llm_retry.py``; a refusal is retried with a context preface). After the last attempt it
logs ``review.failed`` and writes nothing else: the episode stays pending for a human, and an
``append_event review.retry_requested {episode_id}`` starts a fresh round of attempts.

``metrics_json`` (on the review row, and in the ``review.accepted|rejected`` event)::

    {"reviewer": "tdm25", "config_version": 1, "decided_by": "deterministic" | "camera_media" | "claude" | "all_passed",
     "checks": {"actions": {"status": "pass", ...}, "duration": {...}, "frame_drops": {...}, "jerk": {...},
                "takeover_timestamps": {...}, "failure_before_takeover": {...}, "success": {...},
                "camera_media": {...}, "claude": {...}},
     "claude": {"model": ..., "on_task": true, "task_completed": "yes", "observations": ..., "issues": [...],
                "reason": ..., "frames": ["exo t=1.5s (15%)", ...], "usage": {...}} | {"skipped": "..."},
     "thresholds": {...}}
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, ValidationError
from rdf_common.episode_format import EpisodeActions, EpisodeFormatError, cameras_from_json, read_actions
from rdf_common.review_checks import FAIL, PASS, SKIP, CheckResult, ChecksReport, ReviewThresholds, run_checks, ts_us

from .log import get_logger

log = get_logger("reviewer")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "review.yaml"
REVIEW_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
ACTOR = "orchestrator.reviewer"
EVENT_ERROR = "review.error"
EVENT_FAILED = "review.failed"  # retries exhausted: the episode stays pending for a human (llm_retry.py)
EVENT_RETRY_REQUESTED = "review.retry_requested"  # {episode_id}: a human restarts the retry count
CAMERA_PREFERENCE = ("exo", "scene", "front", "top", "overhead", "third", "main", "cam0", "wrist")
MAX_TEXT = 1500  # per free-text field kept in metrics_json (the reducer caps the whole JSON at 16 KB)


class ReviewError(RuntimeError):
    """An infrastructure failure: do not write a review, retry later."""


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class ReviewConfig:
    thresholds: ReviewThresholds = field(default_factory=ReviewThresholds)
    claude_enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "high"
    frames: int = 3
    frame_width: int = 512
    version: int = 1


def load_config(path: str | Path | None = None, **threshold_overrides: Any) -> ReviewConfig:
    import yaml

    p = Path(path or os.environ.get("RDF_REVIEW_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(p.read_text()) or {}
    th = ReviewThresholds.from_mapping(raw)
    if threshold_overrides:
        th = th.replace(**threshold_overrides)
    c = raw.get("claude") or {}
    unknown = set(c) - {"enabled", "model", "effort", "frames", "frame_width"}
    if unknown:
        raise ValueError(f"review config: unknown key(s) claude.{sorted(unknown)}")
    return ReviewConfig(th, bool(c.get("enabled", True)), str(c.get("model", "claude-opus-5-5")),
                        str(c.get("effort", "high")), int(c.get("frames", 3)), int(c.get("frame_width", 512)),
                        int(raw.get("version", 1)))


# --------------------------------------------------------------------------- media

def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _signed_url(uri: str) -> str:
    from .storage import backend_for_uri

    return backend_for_uri(uri).signed_get(uri).url


@contextmanager
def fetch_local(uri: str, *, suffix: str = "", signed_get: Callable[[str], str] | None = None,
                timeout_s: float = 120.0) -> Iterator[Path]:
    """A local path for a media URI: file:// and bare paths as-is; http(s) and storage URIs
    (modal-vol://, s3://; via TDM-4 signed GET) downloaded to a temp file for the block."""
    parts = urlsplit(uri)
    if parts.scheme in ("", "file"):
        p = Path(unquote(parts.path) if parts.scheme == "file" else uri)
        if not p.exists():
            raise FileNotFoundError(f"{uri} does not exist")
        yield p
        return
    import httpx

    url = uri if parts.scheme in ("http", "https") else (signed_get or _signed_url)(uri)
    with tempfile.TemporaryDirectory(prefix="tdm25-review-") as d:
        path = Path(d) / f"media{suffix}"
        with httpx.stream("GET", url, timeout=timeout_s, follow_redirects=True) as r:
            r.raise_for_status()
            with path.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        yield path


def load_actions(episode: Any, **kw) -> tuple[EpisodeActions | None, str | None]:
    """(actions, None) or (None, why it could not be read). A missing / non-parquet file is the
    contributor's problem (a reject); a storage outage on our side raises ReviewError."""
    uri = (_g(episode, "actions_uri") or "").strip()
    if not uri:
        return None, "episode has no actions_uri"
    try:
        with fetch_local(uri, suffix=".parquet", **kw) as p:
            return read_actions(p), None
    except EpisodeFormatError as e:
        return None, str(e)
    except FileNotFoundError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        import httpx

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404):
            return None, f"actions file not found in storage (HTTP {e.response.status_code})"
        raise ReviewError(f"could not fetch actions {uri}: {type(e).__name__}: {e}") from e


def pick_camera(cams: dict[str, str]) -> tuple[str, str] | None:
    if not cams:
        return None
    for pref in CAMERA_PREFERENCE:
        for name in sorted(cams):
            if pref in name.lower():
                return name, cams[name]
    name = sorted(cams)[0]
    return name, cams[name]


def keyframe_times(episode: Any, actions: EpisodeActions | None, n: int = 3) -> list[tuple[str, float]]:
    """(label, seconds from the start of the video). Takeovers: the failure just before takeover_ts,
    the middle of the human segment, and the end. Otherwise evenly from 15% to 85%."""
    dur = (_g(episode, "duration_ms") or 0) / 1000.0
    t0 = int(actions.timestamp_us[0]) if actions is not None and actions.n else None
    tk, hb = ts_us(_g(episode, "takeover_ts")), ts_us(_g(episode, "handback_ts"))
    if t0 is not None and tk is not None and hb is not None and hb > tk and n >= 3:
        a, b = (tk - t0) / 1e6, (hb - t0) / 1e6
        pts = [("policy failure just before the human takeover", max(0.0, a - 0.3)),
               ("during the human takeover", (a + b) / 2), ("end of the episode", max(0.0, dur * 0.95))]
        return pts[:n]
    if n <= 1:
        return [("middle", dur * 0.5)]
    fr = [0.15 + 0.7 * i / (n - 1) for i in range(n)]
    return [(f"{int(f * 100)}% through", dur * f) for f in fr]


def extract_frames(video: Path, times: list[tuple[str, float]], width: int = 512) -> list[tuple[str, bytes]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ReviewError("ffmpeg is not installed; cannot extract review frames")
    out = []
    for label, t in times:
        cmd = [ffmpeg, "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(video), "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            out.append((f"t={t:.1f}s, {label}", r.stdout))
    return out


@dataclass
class Keyframes:
    camera: str | None
    frames: list[tuple[str, bytes]]
    error: str | None = None


def grab_keyframes(episode: Any, actions: EpisodeActions | None, n: int = 3, width: int = 512, **kw) -> Keyframes:
    cam = pick_camera(cameras_from_json(_g(episode, "cameras_json")))
    if cam is None:
        return Keyframes(None, [], "episode has no camera video in cameras_json")
    name, uri = cam
    try:
        with fetch_local(uri, suffix=".mp4", **kw) as p:
            frames = extract_frames(p, keyframe_times(episode, actions, n), width)
    except ReviewError:
        raise
    except FileNotFoundError as e:
        return Keyframes(name, [], str(e))
    except Exception as e:  # noqa: BLE001
        import httpx

        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (403, 404):
            return Keyframes(name, [], f"camera video not found in storage (HTTP {e.response.status_code})")
        raise ReviewError(f"could not fetch camera video {uri}: {type(e).__name__}: {e}") from e
    if not frames:
        return Keyframes(name, [], f"no frames could be decoded from the {name} video")
    return Keyframes(name, [(f"{name} camera, {lbl}", b) for lbl, b in frames])


# --------------------------------------------------------------------------- Claude judgement

class Judgement(BaseModel):
    observations: str
    on_task: bool
    task_completed: Literal["yes", "no", "unclear"]
    issues: list[str] = Field(default_factory=list)
    reason: str


JUDGEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "observations": {"type": "string",
                         "description": "What each frame shows: the objects, where the gripper is, what changed between frames."},
        "on_task": {"type": "boolean",
                    "description": "True if the recording is an attempt at the ticket's task with the task's objects."},
        "task_completed": {"type": "string", "enum": ["yes", "no", "unclear"],
                           "description": "Does the final frame show the task's goal state?"},
        "issues": {"type": "array", "items": {"type": "string"},
                   "description": "Concrete problems seen, e.g. 'the red cup never moves'. Empty if none."},
        "reason": {"type": "string",
                   "description": "One or two sentences for the contributor explaining the verdict, citing what the frames show."},
    },
    "required": ["observations", "on_task", "task_completed", "issues", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You review robot demonstration recordings before a contributor is paid for them. \
Each recording is supposed to teach a robot arm one task. Deterministic checks (duration, frame drops, \
motion smoothness, takeover timestamps, the operator's success flag) have already passed; your job is the \
part they cannot check: is this recording actually a demonstration of the ticket's task?

You get the task description, a summary of the episode, and a few keyframes from one camera in time order.

Decide:
- on_task: true when the arm is attempting the described task with the described objects (approaching, \
grasping, moving or placing the right object toward the right target). False when it manipulates other \
objects, moves aimlessly, does a different task, or the task's objects are not involved at all.
- task_completed: "yes" if the last frame shows the task's goal state, "no" if the frames show it was \
not achieved (object still in its start position, dropped, in the wrong place), "unclear" if the frames \
cannot tell (occlusion, camera angle).
For recovery takeovers, the first frame is meant to show the policy's failure; judge the episode by \
whether the human segment completes the task.

Judge task relevance from what the frames show. Image quality, resolution, lighting or rendering style \
are not reasons to reject unless the frames are unreadable. Base every statement on the frames; do not \
assume motion you cannot see. The reason is shown to the contributor, so make it concrete and polite."""


class Judge(Protocol):
    model: str

    def judge(self, messages: list[dict]) -> Judgement: ...


class ClaudeJudge:
    """One structured-output call with vision through ``rdf_common.llm`` (RDF_LLM_BACKEND: api | droid |
    claude_code). On the API: server-side fallback is on for refusals. ``client``: an Anthropic-like client
    (pins api) or an ``rdf_common.llm.LLM``; None = the shared switchable client."""

    step = "review"
    what = "the review"

    def __init__(self, config: ReviewConfig | None = None, client: Any = None):
        cfg = config or ReviewConfig()
        self.requested_model, self.effort = cfg.model, cfg.effort
        self._client = client
        self.last_usage: dict | None = None
        self.last_meta: dict | None = None

    @property
    def model(self) -> str:
        """The requested model; after a call, the model that answered (with the backend if not the API)."""
        from rdf_common.llm import describe_result

        return describe_result(self.last_meta) if self.last_meta else self.requested_model

    @model.setter
    def model(self, v: str) -> None:
        self.requested_model = v

    def _structured(self, messages: list[dict], *, system: str, schema: dict, err: type[Exception], out: Any) -> Any:
        import anthropic
        from rdf_common.llm import LLMError, as_llm, usage_summary

        try:
            r = as_llm(self._client).complete_structured(
                step=self.step, system=system, messages=messages, schema=schema, model=self.requested_model,
                effort=self.effort, max_tokens=16000, thinking=True)
        except anthropic.APIError as e:
            raise err(f"Claude API error: {type(e).__name__}: {e}") from e
        except LLMError as e:
            self.last_usage, self.last_meta = usage_summary(e), getattr(e, "meta", None)
            if e.reason == "refusal":
                raise err(f"Claude refused {self.what} (category={e.category})") from e
            if e.reason == "max_tokens":
                raise err("Claude output hit max_tokens") from e
            if e.reason in ("bad_json", "schema"):
                raise err(f"Claude structured output did not validate: {e}") from e
            raise err(f"Claude {e.backend or 'LLM'} error: {e}") from e
        self.last_usage, self.last_meta = usage_summary(r), r.meta
        if not r.text:
            raise err(f"no text block in Claude response (stop_reason={r.meta.get('stop_reason')})")
        try:
            return out.model_validate_json(r.text)
        except ValidationError as e:
            raise err(f"Claude structured output did not validate: {e}") from e

    def judge(self, messages: list[dict]) -> Judgement:
        return self._structured(messages, system=SYSTEM_PROMPT, schema=JUDGEMENT_SCHEMA, err=ReviewError, out=Judgement)


def _summary(ticket: Any, episode: Any, report: ChecksReport) -> str:
    m = report.metrics()
    lines = [
        f"Task: {_g(ticket, 'task_description') or '(no description)'}",
        f"Episode {_g(episode, 'id')}: kind {_g(episode, 'kind')}, "
        f"{(_g(episode, 'duration_ms') or 0) / 1000:.1f} s, {_g(episode, 'frame_count')} frames, "
        f"operator success flag: {_g(episode, 'success')}.",
    ]
    if "failure_before_takeover" in m and m["failure_before_takeover"].get("status") == PASS:
        f = m["failure_before_takeover"]
        lines.append(f"Recovery takeover: the policy drove the first {f.get('pre_takeover_s')} s, then a human took "
                     f"over for {f.get('takeover_s')} s and handed back.")
    elif _g(episode, "kind") == "clean_teleop":
        lines.append("Clean teleoperated demonstration: a human drove the arm for the whole episode.")
    j = m.get("jerk", {})
    lines.append(f"Deterministic checks passed (RMS joint jerk {j.get('jerk_rms')} rad/s^3, "
                 f"frame drop ratio {m.get('frame_drops', {}).get('drop_ratio')}).")
    return "\n".join(lines)


def build_messages(ticket: Any, episode: Any, report: ChecksReport, frames: list[tuple[str, bytes]]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": _summary(ticket, episode, report)},
                           {"type": "text", "text": f"{len(frames)} keyframes follow, in time order."}]
    for label, jpeg in frames:
        content.append({"type": "text", "text": f"Frame: {label}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": "Is this recording a demonstration of the task? Give your verdict."})
    return [{"role": "user", "content": content}]


# --------------------------------------------------------------------------- the decision

@dataclass
class ReviewDecision:
    episode_id: int
    accepted: bool
    reason: str
    decided_by: str  # deterministic | camera_media | claude | all_passed
    checks: list[CheckResult]  # deterministic checks + camera_media + claude, one qa_result row each
    metrics: dict

    def metrics_json(self) -> str:
        # The reducer caps metrics_json at 16 KB: drop the bulky parts first, never cut JSON mid-way.
        m = dict(self.metrics)
        for step in ("full", "thresholds", "observations", "check_details"):
            if step == "thresholds":
                m.pop("thresholds", None)
            elif step == "observations" and isinstance(m.get("claude"), dict):
                m["claude"] = {k: v for k, v in m["claude"].items() if k != "observations"}
            elif step == "check_details":
                m["checks"] = {k: {"status": v.get("status"), "reason": v.get("reason", "")}
                               for k, v in (m.get("checks") or {}).items()}
            s = json.dumps(m, default=str)
            if len(s) <= 15000:
                return s
        return json.dumps({"reviewer": "tdm25", "decided_by": self.decided_by, "truncated": True})


def _clip(s: str, n: int = MAX_TEXT) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def review_episode(episode: Any, ticket: Any, *, config: ReviewConfig | None = None, judge: Judge | None = None,
                   **fetch_kw) -> ReviewDecision:
    """Review one episode. Raises ReviewError on infrastructure failure (write nothing then)."""
    cfg = config or load_config()
    eid = int(_g(episode, "id") or 0)
    actions, err = load_actions(episode, **fetch_kw)
    report = run_checks(episode, actions, cfg.thresholds, actions_error=err)
    checks = list(report.results)
    metrics: dict[str, Any] = {"reviewer": "tdm25", "config_version": cfg.version, "kind": _g(episode, "kind"),
                               "thresholds": cfg.thresholds.to_dict()}

    def done(accepted: bool, reason: str, by: str, claude: dict) -> ReviewDecision:
        metrics.update(decided_by=by, claude=claude,
                       checks={r.name: {"status": r.status, **({"reason": r.reason} if r.reason else {}), **r.detail}
                               for r in checks})
        return ReviewDecision(eid, accepted, reason, by, checks, metrics)

    if not report.passed:
        checks.append(CheckResult("camera_media", SKIP, "skipped: deterministic checks failed"))
        checks.append(CheckResult("claude", SKIP, "skipped: deterministic checks failed"))
        return done(False, f"Rejected: {report.reason()}.", "deterministic", {"skipped": "deterministic checks failed"})

    kf = grab_keyframes(episode, actions, cfg.frames, cfg.frame_width, **fetch_kw)
    media = CheckResult("camera_media", PASS if kf.frames else FAIL, "" if kf.frames else (kf.error or "no frames"),
                        {"camera": kf.camera, "frames": [lbl for lbl, _ in kf.frames]})
    checks.append(media)
    if not kf.frames:
        checks.append(CheckResult("claude", SKIP, "skipped: no camera frames"))
        return done(False, f"Rejected: camera video unusable ({media.reason}).", "camera_media",
                    {"skipped": "no camera frames"})

    if not cfg.claude_enabled or judge is None:
        if cfg.claude_enabled:
            raise ReviewError("Claude judgement is enabled but no judge was given")
        checks.append(CheckResult("claude", SKIP, "Claude judgement disabled in config"))
        return done(True, "Accepted: all deterministic checks passed (Claude judgement disabled).", "all_passed",
                    {"skipped": "disabled in config"})

    v = judge.judge(build_messages(ticket, episode, report, kf.frames))
    claude = {"model": getattr(judge, "model", None), "on_task": v.on_task, "task_completed": v.task_completed,
              "observations": _clip(v.observations), "issues": [_clip(i, 300) for i in v.issues[:8]],
              "reason": _clip(v.reason), "frames": [lbl for lbl, _ in kf.frames],
              "usage": getattr(judge, "last_usage", None)}
    why = _clip(v.reason, 600)
    if not v.on_task:
        checks.append(CheckResult("claude", FAIL, f"off-task: {why}", {k: claude[k] for k in ("on_task", "task_completed", "issues")}))
        return done(False, f"Rejected by Claude review: recording is not a demonstration of the task. {why}", "claude", claude)
    if v.task_completed == "no":
        checks.append(CheckResult("claude", FAIL, f"task not completed in the video: {why}",
                                  {k: claude[k] for k in ("on_task", "task_completed", "issues")}))
        return done(False, f"Rejected by Claude review: the video shows the task was not completed. {why}", "claude", claude)
    checks.append(CheckResult("claude", PASS, "", {k: claude[k] for k in ("on_task", "task_completed")}))
    return done(True, f"Accepted: all checks passed. Claude: {why}", "all_passed", claude)


# --------------------------------------------------------------------------- SpacetimeDB I/O
# ``stdb`` is an ``rdf_common.spacetime_client.SpacetimeClient`` (sync HTTP: reducer errors raise).

def record_review(stdb: Any, decision: ReviewDecision) -> None:
    """One ``record_qa_result`` per check (TDM-14 reads them for purchased episodes; latest per
    check_name wins, so a retry is harmless), then the final ``submit_review``."""
    for c in decision.checks:
        stdb.call_reducer("record_qa_result", decision.episode_id, c.name, c.status != FAIL, c.detail_json())
    stdb.call_reducer("submit_review", decision.episode_id, decision.accepted, decision.reason, decision.metrics_json())


def log_error(stdb: Any, episode: Any, error: str, **extra: Any) -> None:
    """One failed attempt (``extra``: attempt, kind, max_attempts, retry_in_s from ``llm_retry``)."""
    stdb.call_reducer("append_event", ACTOR, EVENT_ERROR, json.dumps({
        "episode_id": _g(episode, "id"), "ticket_id": _g(episode, "ticket_id"), **extra, "error": error[:2000]},
        default=str))


def log_failed(stdb: Any, episode: Any, **fields: Any) -> None:
    """Terminal: retries exhausted. Still no review (an outage is never a rejection)."""
    stdb.call_reducer("append_event", ACTOR, EVENT_FAILED, json.dumps({
        "episode_id": _g(episode, "id"), "ticket_id": _g(episode, "ticket_id"), **fields}, default=str))
