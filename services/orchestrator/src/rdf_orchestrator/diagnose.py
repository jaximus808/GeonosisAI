"""Failure clustering for finished eval runs (TDM-23, flywheel step 3 "Diagnose").

Pure logic, no SpacetimeDB: the bus handler (``handlers/diagnose.py``) and the CLI script
(``scripts/tdm23_diagnose.py``) gather rows and call ``diagnose_eval_run``.

    inputs  = DiagnoseInputs(ticket=..., eval_run=..., trials=[...], episodes={id: row})
    result  = diagnose_eval_run(inputs, frames=EpisodeFrameSource(), claude=ClaudeClusterer())
    result.clusters  -> list[Cluster]   (one record_failure_cluster call each)
    result.payload() -> dict            (the ``diagnose.clusters`` event_log payload)

How it works:
  1. Take the failed ``eval_trial`` rows (outcome, duration, failure_note) and, per failed trial with a
     recorded episode, 2-3 frames sampled from the episode's camera video (via the TDM-4 storage
     interface, ffmpeg for the frame grab). Missing media degrades to text-only; it never blocks.
  2. One Claude call (``claude-opus-5-5``, vision, structured output) groups the failed trials into
     labelled clusters. The model refers to trials by ``trial_index``; this module maps indices to
     episode ids and computes counts itself, so counts and example ids always point at real rows.
  3. Every failed trial ends up in exactly one cluster (duplicates dropped, stragglers put in an
     "unclassified failure" cluster), so cluster counts sum to the number of failures.

OUTPUT SHAPE (what the planner, TDM-24, reads). The ``failure_cluster`` table only holds
label/count/example_episode_ids (``description`` is empty: the TDM-12 reducer takes no description),
so the full diagnosis goes in one ``event_log`` row, type ``diagnose.clusters``::

    {
      "ticket_id": 7, "iteration": 0, "eval_run_id": 3, "eval_kind": "baseline",
      "checkpoint_uri": "...", "trials": 10, "successes": 3, "failures": 7,
      "model": "claude-opus-5-5",
      "clusters": [
        {
          "failure_cluster_id": 12,          # failure_cluster.id (null if the row was not seen in time)
          "label": "drops mid-grasp",        # short noun phrase, also failure_cluster.label
          "description": "Gripper closes on the cup but it slips out during lift ...",
          "phase": "lift",                   # approach|grasp|lift|transport|place|release|start|other
          "recoverable_mid_episode": true,   # a takeover could finish the task in the same episode
          "recoverability_reason": "Cup stays upright within reach after the slip ...",
          "count": 3,                        # == len(trial_indices)
          "trial_indices": [1, 4, 8],
          "episode_ids": [41, 44, 48],       # every failed episode in the cluster (0s dropped)
          "example_episode_ids": [41, 44, 48]  # up to 3, also failure_cluster.example_episode_ids
        }
      ],
      "recoverable_failures": 3, "unrecoverable_failures": 4,   # sums over clusters
      "reasoning": "...",                    # the model's own account of the grouping
      "media": {"frames_sent": 18, "episodes_with_frames": 6, "episodes_without_frames": [47]}
    }

With 0 failures no clusters are recorded and a ``diagnose.no_failures`` event is logged instead.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from pydantic import BaseModel, Field, ValidationError

from .log import get_logger

log = get_logger("diagnose")

DIAGNOSE_MODEL = os.environ.get("RDF_DIAGNOSE_MODEL", "claude-opus-5-5")
DIAGNOSE_EFFORT = os.environ.get("RDF_DIAGNOSE_EFFORT", "high")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_EXAMPLES = 3
MAX_CLUSTERS = 5
FRAMES_PER_EPISODE = 3
FRAME_FRACTIONS = (0.2, 0.55, 0.9)  # where in the episode to grab frames (start, middle, end)
FRAME_WIDTH = 512
UNCLASSIFIED_LABEL = "unclassified failure"
PHASES = ("start", "approach", "grasp", "lift", "transport", "place", "release", "other")
EVENT_CLUSTERS = "diagnose.clusters"
EVENT_NO_FAILURES = "diagnose.no_failures"
EVENT_ERROR = "diagnose.error"
# Camera names to prefer for the frames, in order (the scene/exo view shows the failure best).
CAMERA_PREFERENCE = ("exo", "scene", "front", "top", "overhead", "third", "main", "cam0")


class DiagnoseError(RuntimeError):
    pass


# --------------------------------------------------------------------------- inputs

def _g(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


@dataclass
class DiagnoseInputs:
    ticket: dict
    eval_run: dict
    trials: list[dict]
    episodes: dict[int, dict] = field(default_factory=dict)

    @property
    def failed(self) -> list[dict]:
        return sorted((t for t in self.trials if not _g(t, "success")), key=lambda t: _g(t, "trial_index", 0))

    @property
    def succeeded(self) -> list[dict]:
        return [t for t in self.trials if _g(t, "success")]


@dataclass
class Frame:
    trial_index: int
    episode_id: int
    label: str  # e.g. "t=3.6s (20%)"
    jpeg: bytes


class FrameSource(Protocol):
    def frames(self, episode: dict, n: int) -> list[tuple[str, bytes]]: ...


# --------------------------------------------------------------------------- frames from storage

def _cameras(episode: dict) -> dict[str, str]:
    raw = _g(episode, "cameras_json") or ""
    try:
        cams = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(cams, dict):
        return {}
    out = {}
    for name, v in cams.items():
        uri = v.get("uri") if isinstance(v, dict) else v
        if isinstance(uri, str) and uri:
            out[str(name)] = uri
    return out


def pick_camera(cams: dict[str, str]) -> tuple[str, str] | None:
    if not cams:
        return None
    for pref in CAMERA_PREFERENCE:
        for name in sorted(cams):
            if pref in name.lower():
                return name, cams[name]
    name = sorted(cams)[0]
    return name, cams[name]


def extract_frames(video: Path, duration_s: float, n: int = FRAMES_PER_EPISODE,
                   width: int = FRAME_WIDTH) -> list[tuple[str, bytes]]:
    """Grab ``n`` JPEG frames at FRAME_FRACTIONS of the video with ffmpeg. [] if ffmpeg is missing."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    out: list[tuple[str, bytes]] = []
    for frac in FRAME_FRACTIONS[:n]:
        t = max(0.0, duration_s * frac) if duration_s > 0 else 0.0
        cmd = [ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
               "-vf", f"scale={width}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            out.append((f"t={t:.1f}s ({int(frac * 100)}%)", r.stdout))
    return out


class EpisodeFrameSource:
    """Frames for an episode from its camera media (``episode.cameras_json``) via TDM-4 storage.

    A camera entry that is already an image (``.jpg``/``.png`` thumbnail) is used as-is; a video is
    downloaded through a signed GET and sampled with ffmpeg. Any failure returns [] (text-only).
    """

    def __init__(self, signed_get: Callable[[str], str] | None = None, timeout_s: float = 120.0):
        self._signed_get = signed_get
        self.timeout_s = timeout_s

    def _url(self, uri: str) -> str:
        if uri.startswith(("http://", "https://")):
            return uri
        if self._signed_get is not None:
            return self._signed_get(uri)
        from .storage import backend_for_uri

        return backend_for_uri(uri).signed_get(uri).url

    def frames(self, episode: dict, n: int = FRAMES_PER_EPISODE) -> list[tuple[str, bytes]]:
        cam = pick_camera(_cameras(episode))
        if cam is None:
            return []
        name, uri = cam
        try:
            import httpx

            url = self._url(uri)
            if uri.lower().endswith((".jpg", ".jpeg")):
                r = httpx.get(url, timeout=self.timeout_s, follow_redirects=True)
                r.raise_for_status()
                return [(f"{name} thumbnail", r.content)]
            with tempfile.TemporaryDirectory(prefix="tdm23-frames-") as d:
                path = Path(d) / "video.mp4"
                with httpx.stream("GET", url, timeout=self.timeout_s, follow_redirects=True) as r:
                    r.raise_for_status()
                    with path.open("wb") as f:
                        for chunk in r.iter_bytes():
                            f.write(chunk)
                dur = (_g(episode, "duration_ms") or 0) / 1000.0
                return [(f"{name} {lbl}", b) for lbl, b in extract_frames(path, dur, n)]
        except (Exception, SystemExit) as e:  # noqa: BLE001 - media is best-effort (load_settings exits on missing env)
            # Never log the exception text: httpx puts the signed URL (with its signature) in it.
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.warning("diagnose.frames_failed", extra={"fields": {
                "episode_id": _g(episode, "id"), "camera": name, "error": type(e).__name__, "http_status": status}})
            return []


def collect_frames(inputs: DiagnoseInputs, source: FrameSource | None,
                   per_episode: int = FRAMES_PER_EPISODE) -> tuple[list[Frame], list[int]]:
    """(frames, failed episode ids that had no frames)."""
    frames: list[Frame] = []
    missing: list[int] = []
    for t in inputs.failed:
        eid = int(_g(t, "episode_id") or 0)
        if not eid:
            continue
        ep = inputs.episodes.get(eid)
        got = source.frames(ep, per_episode) if (source is not None and ep is not None) else []
        if not got:
            missing.append(eid)
        for label, jpeg in got[:per_episode]:
            frames.append(Frame(int(_g(t, "trial_index")), eid, label, jpeg))
    return frames, missing


# --------------------------------------------------------------------------- the Claude call

class ModelCluster(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    description: str
    phase: str
    recoverable_mid_episode: bool
    recoverability_reason: str
    trial_indices: list[int]


class ModelDiagnosis(BaseModel):
    reasoning: str
    clusters: list[ModelCluster]


DIAGNOSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string",
                      "description": "How you grouped the failures and why; cite trial indices and what the notes/frames show."},
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short lowercase noun phrase, 2-5 words, e.g. 'drops mid-grasp'."},
                    "description": {"type": "string", "description": "One or two sentences: what goes wrong, observably."},
                    "phase": {"type": "string", "enum": list(PHASES)},
                    "recoverable_mid_episode": {"type": "boolean"},
                    "recoverability_reason": {"type": "string"},
                    "trial_indices": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["label", "description", "phase", "recoverable_mid_episode",
                             "recoverability_reason", "trial_indices"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reasoning", "clusters"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""You diagnose why a robot manipulation policy failed during an evaluation run.

You get every failed trial of one eval run: its trial index, duration, the operator's or robot's \
failure note, and (when recorded) a few camera frames from the start, middle and end of the episode. \
Successful-trial durations are given for contrast.

Group the failed trials into failure clusters: trials belong together when the robot fails in the \
same observable way, at the same phase of the task, such that the same kind of corrective data would \
fix them. Rules:
- Every failed trial index goes in exactly one cluster. Do not invent trial indices.
- Prefer fewer, well-separated clusters. Merge clusters that differ only in wording or minor detail. \
Use at most {MAX_CLUSTERS} clusters. A single trial can be its own cluster only if it clearly fails \
differently from all the others.
- Labels are short lowercase noun phrases a dashboard can show: "drops mid-grasp", "misses the cup", \
"never moves", "knocks the cup over". No trial numbers in labels.
- Base every claim on the notes and frames. If the evidence for a trial is thin, say so in the reasoning \
and put it with the cluster its note matches best.

recoverable_mid_episode decides whether we collect human takeover corrections (the robot is paused in \
the failure and a person nudges it to finish) or full clean demonstrations. It is true when, at the moment \
of failure, the scene is still in a state from which a person could take over and complete the task in the \
same episode: the object is still upright and within reach, the arm is not stuck or in a fault (e.g. a \
grasp that closed on air beside the cup, a cup that slipped back onto the table, a hesitation or stall \
mid-reach). It is false when the episode cannot be finished without a reset or the failure is at the very \
start: the object was knocked over or out of reach, the arm never started moving, a safety stop or \
collision, or the policy goes somewhere unrelated from the first step (a coverage problem that full demos \
fix better). Explain the call in recoverability_reason.

phase is where in the task the failure happens: {", ".join(PHASES)}."""


def _fmt_s(ms: Any) -> str:
    return f"{(ms or 0) / 1000:.1f}s"


def build_messages(inputs: DiagnoseInputs, frames: list[Frame]) -> list[dict]:
    t, er = inputs.ticket, inputs.eval_run
    ok_durs = [int(_g(x, "duration_ms") or 0) for x in inputs.succeeded]
    by_trial: dict[int, list[Frame]] = {}
    for f in frames:
        by_trial.setdefault(f.trial_index, []).append(f)

    head = [
        f"Task: {_g(t, 'task_description') or '(no description)'}",
        f"Eval run {_g(er, 'id')} ({_g(er, 'kind')}), checkpoint {_g(er, 'checkpoint_uri')}: "
        f"{len(inputs.succeeded)}/{len(inputs.trials)} trials succeeded.",
        "Successful trial durations: " + (", ".join(_fmt_s(d) for d in sorted(ok_durs)) if ok_durs else "none")
        + (f" (median {_fmt_s(statistics.median(ok_durs))})" if ok_durs else ""),
        f"Failed trials ({len(inputs.failed)}) follow, each with its note and frames if recorded.",
    ]
    content: list[dict] = [{"type": "text", "text": "\n".join(head)}]
    for tr in inputs.failed:
        idx = int(_g(tr, "trial_index"))
        eid = int(_g(tr, "episode_id") or 0)
        note = (_g(tr, "failure_note") or "").strip() or "(no note)"
        fr = by_trial.get(idx, [])
        line = (f"\nTrial {idx}: FAILED after {_fmt_s(_g(tr, 'duration_ms'))}"
                f"{f', episode {eid}' if eid else ', not recorded'}. Note: {note}"
                f"{f' Frames: {len(fr)}.' if fr else ' No frames.'}")
        content.append({"type": "text", "text": line})
        for f in fr:
            content.append({"type": "text", "text": f"Trial {idx} frame, {f.label}:"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(f.jpeg).decode()}})
    content.append({"type": "text", "text": "\nCluster the failed trials."})
    return [{"role": "user", "content": content}]


class Clusterer(Protocol):
    model: str

    def cluster(self, messages: list[dict]) -> ModelDiagnosis: ...


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None  # the SDK reads it from the environment
    try:
        from rdf_common.settings import OrchestratorSettings, load_settings

        return load_settings(OrchestratorSettings, "orchestrator.diagnose").anthropic_api_key.get_secret_value()
    except BaseException:  # noqa: BLE001 - load_settings exits on missing env; fall back to SDK defaults
        return None


class ClaudeClusterer:
    """One structured-output Messages call. Server-side fallback is on for refusals."""

    def __init__(self, model: str = DIAGNOSE_MODEL, effort: str = DIAGNOSE_EFFORT, client: Any = None):
        self.model, self.effort = model, effort
        self._client = client
        self.last_usage: dict | None = None
        self.last_request_id: str | None = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            key = _api_key()
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    def cluster(self, messages: list[dict]) -> ModelDiagnosis:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        self.last_request_id = getattr(resp, "_request_id", None)
        u = getattr(resp, "usage", None)
        self.last_usage = {"input_tokens": getattr(u, "input_tokens", None),
                           "output_tokens": getattr(u, "output_tokens", None), "model": getattr(resp, "model", None)}
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise DiagnoseError(f"model refused (category={cat})")
        if resp.stop_reason == "max_tokens":
            raise DiagnoseError("model output hit max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if not text:
            raise DiagnoseError(f"no text block in response (stop_reason={resp.stop_reason})")
        try:
            return ModelDiagnosis.model_validate_json(text)
        except ValidationError as e:
            raise DiagnoseError(f"structured output did not validate: {e}") from e


# --------------------------------------------------------------------------- result

@dataclass
class Cluster:
    label: str
    description: str
    phase: str
    recoverable_mid_episode: bool
    recoverability_reason: str
    trial_indices: list[int]
    episode_ids: list[int]
    failure_cluster_id: int | None = None

    @property
    def count(self) -> int:
        return len(self.trial_indices)

    @property
    def example_episode_ids(self) -> list[int]:
        return self.episode_ids[:MAX_EXAMPLES]

    def to_dict(self) -> dict:
        return {
            "failure_cluster_id": self.failure_cluster_id, "label": self.label, "description": self.description,
            "phase": self.phase, "recoverable_mid_episode": self.recoverable_mid_episode,
            "recoverability_reason": self.recoverability_reason, "count": self.count,
            "trial_indices": self.trial_indices, "episode_ids": self.episode_ids,
            "example_episode_ids": self.example_episode_ids,
        }


@dataclass
class Diagnosis:
    inputs: DiagnoseInputs
    clusters: list[Cluster]
    reasoning: str
    model: str | None
    frames_sent: int = 0
    episodes_without_frames: list[int] = field(default_factory=list)
    usage: dict | None = None
    repairs: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        er, t = self.inputs.eval_run, self.inputs.ticket
        rec = sum(c.count for c in self.clusters if c.recoverable_mid_episode)
        failures = len(self.inputs.failed)
        with_frames = len({f for f in self._episodes_with_frames()})
        return {
            "ticket_id": _g(t, "id"), "iteration": _g(t, "iteration"), "eval_run_id": _g(er, "id"),
            "eval_kind": _g(er, "kind"), "checkpoint_uri": _g(er, "checkpoint_uri"),
            "trials": len(self.inputs.trials), "successes": len(self.inputs.succeeded), "failures": failures,
            "model": self.model, "clusters": [c.to_dict() for c in self.clusters],
            "recoverable_failures": rec, "unrecoverable_failures": failures - rec,
            "reasoning": self.reasoning,
            "media": {"frames_sent": self.frames_sent, "episodes_with_frames": with_frames,
                      "episodes_without_frames": self.episodes_without_frames},
            "repairs": self.repairs, "usage": self.usage,
        }

    def _episodes_with_frames(self) -> Iterable[int]:
        missing = set(self.episodes_without_frames)
        for t in self.inputs.failed:
            eid = int(_g(t, "episode_id") or 0)
            if eid and eid not in missing:
                yield eid


def reconcile(inputs: DiagnoseInputs, md: ModelDiagnosis) -> tuple[list[Cluster], list[str]]:
    """Map the model's trial indices onto real failed trials: drop unknown/duplicate indices, put
    stragglers in an 'unclassified failure' cluster, merge clusters that share a label, drop empties."""
    failed = {int(_g(t, "trial_index")): int(_g(t, "episode_id") or 0) for t in inputs.failed}
    repairs: list[str] = []
    seen: set[int] = set()
    by_label: dict[str, Cluster] = {}
    for mc in md.clusters:
        label = " ".join(mc.label.strip().lower().split())[:60] or UNCLASSIFIED_LABEL
        idxs = []
        for i in mc.trial_indices:
            if i not in failed:
                repairs.append(f"dropped trial {i} from '{label}': not a failed trial")
            elif i in seen:
                repairs.append(f"dropped duplicate trial {i} from '{label}'")
            else:
                seen.add(i)
                idxs.append(i)
        phase = mc.phase if mc.phase in PHASES else "other"
        c = by_label.get(label)
        if c is None:
            by_label[label] = Cluster(label, mc.description.strip(), phase, mc.recoverable_mid_episode,
                                      mc.recoverability_reason.strip(), idxs, [])
        else:
            repairs.append(f"merged two clusters labelled '{label}'")
            c.trial_indices += idxs
    left = sorted(set(failed) - seen)
    if left:
        repairs.append(f"trials {left} were not assigned; put in '{UNCLASSIFIED_LABEL}'")
        c = by_label.setdefault(UNCLASSIFIED_LABEL, Cluster(
            UNCLASSIFIED_LABEL, "Failed trials the model did not assign to a cluster.", "other", False,
            "Unknown failure mode; treated as not recoverable.", [], []))
        c.trial_indices += left
    out = []
    for c in by_label.values():
        if not c.trial_indices:
            continue
        c.trial_indices.sort()
        c.episode_ids = [failed[i] for i in c.trial_indices if failed[i]]
        out.append(c)
    out.sort(key=lambda c: (-c.count, c.trial_indices[0]))
    return out, repairs


def diagnose_eval_run(inputs: DiagnoseInputs, *, claude: Clusterer, frames: FrameSource | None = None) -> Diagnosis:
    """Cluster the failed trials of one finished eval run. 0 failures -> no Claude call, no clusters."""
    if not inputs.failed:
        return Diagnosis(inputs, [], "No failed trials; nothing to cluster.", None)
    fr, missing = collect_frames(inputs, frames)
    md = claude.cluster(build_messages(inputs, fr))
    clusters, repairs = reconcile(inputs, md)
    return Diagnosis(inputs, clusters, md.reasoning, getattr(claude, "model", None), len(fr), missing,
                     getattr(claude, "last_usage", None), repairs)


# --------------------------------------------------------------------------- planner-side reader

def latest_diagnosis(event_log_rows: Iterable[Any], ticket_id: int, iteration: int | None = None) -> dict | None:
    """The newest ``diagnose.clusters`` payload for a ticket (optionally one iteration) from event_log
    rows (``bus.rows("event_log")`` or SQL rows). Planner entry point (TDM-24)."""
    best: tuple[int, dict] | None = None
    for r in event_log_rows:
        if _g(r, "type") != EVENT_CLUSTERS:
            continue
        try:
            p = json.loads(_g(r, "payload_json") or "{}")
        except ValueError:
            continue
        if p.get("ticket_id") != ticket_id or (iteration is not None and p.get("iteration") != iteration):
            continue
        rid = int(_g(r, "id") or 0)
        if best is None or rid > best[0]:
            best = (rid, p)
    return best[1] if best else None


# --------------------------------------------------------------------------- SpacetimeDB I/O
# ``stdb`` is an ``rdf_common.spacetime_client.SpacetimeClient`` (sync HTTP: reducer errors raise,
# which the fire-and-forget socket call would hide) or anything with the same call_reducer/sql.

ACTOR = "orchestrator.diagnose"


def load_inputs(stdb: Any, eval_run_id: int) -> DiagnoseInputs:
    """Read one eval run, its trials, its ticket and the trials' episodes over SQL."""
    runs = stdb.sql(f"SELECT * FROM eval_run WHERE id = {int(eval_run_id)}")
    if not runs:
        raise DiagnoseError(f"unknown eval run {eval_run_id}")
    er = runs[0]
    tickets = stdb.sql(f"SELECT * FROM ticket WHERE id = {int(er['ticket_id'])}")
    if not tickets:
        raise DiagnoseError(f"eval run {eval_run_id}: unknown ticket {er['ticket_id']}")
    trials = stdb.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {int(eval_run_id)}")
    episodes: dict[int, dict] = {}
    for eid in sorted({int(t.get("episode_id") or 0) for t in trials if not t.get("success")} - {0}):
        rows = stdb.sql(f"SELECT * FROM episode WHERE id = {eid}")
        if rows:
            episodes[eid] = rows[0]
    return DiagnoseInputs(tickets[0], er, trials, episodes)


def _cluster_rows(stdb: Any, ticket_id: int) -> list[dict]:
    return stdb.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {int(ticket_id)}")


def record_diagnosis(diag: Diagnosis, stdb: Any) -> dict:
    """Write the diagnosis: one ``record_failure_cluster`` per cluster, then one event_log row
    (``diagnose.clusters``, or ``diagnose.no_failures``) carrying the full payload with the new
    failure_cluster ids. Returns the payload."""
    ticket_id = int(_g(diag.inputs.ticket, "id"))
    if not diag.clusters:
        payload = diag.payload()
        stdb.call_reducer("append_event", ACTOR, EVENT_NO_FAILURES, json.dumps(payload))
        return payload
    before = {int(r["id"]) for r in _cluster_rows(stdb, ticket_id)}
    for c in diag.clusters:
        stdb.call_reducer("record_failure_cluster", ticket_id, c.label, c.count, json.dumps(c.example_episode_ids))
    # Reducers return nothing: find the rows we just inserted (new ids on this ticket, by label).
    new = [r for r in _cluster_rows(stdb, ticket_id) if int(r["id"]) not in before]
    for c in diag.clusters:
        match = next((r for r in new if r["label"] == c.label and int(r["count"]) == c.count), None)
        if match is not None:
            c.failure_cluster_id = int(match["id"])
            new.remove(match)
    payload = diag.payload()
    stdb.call_reducer("append_event", ACTOR, EVENT_CLUSTERS, json.dumps(payload))
    return payload


def log_error(stdb: Any, ticket_id: Any, eval_run_id: Any, error: str) -> None:
    stdb.call_reducer("append_event", ACTOR, EVENT_ERROR,
                      json.dumps({"ticket_id": ticket_id, "eval_run_id": eval_run_id, "error": error[:2000]}))
