"""TDM-48: episode recorder. One mp4 per camera + ``actions.parquet``, written to disk first, then
uploaded through the orchestrator's signed URLs and registered with ``create_episode``.

Format: ``rdf_common.episode_format`` (TDM-25) is the one definition; this module only produces it.

    from rdf_robot_client.recorder import Recorder, EpisodeUploader

    up = EpisodeUploader.from_client(client, orchestrator_url)   # background upload queue
    rec = Recorder(client, rig, uploader=up)                      # client: RobotClient (tick source)
    rec.start(ticket_id, "recovery_takeover", task_id=7, contributor_id=3)
    ...                                     # the job streams commands through run_control_loop
    rec.mark_takeover()                     # policy paused, human gets control
    rec.mark_handback()                     # operator tapped done, policy drives again
    ep = rec.stop(success=True, note="")    # finalises local files (~1 s), queues the upload
    ep.dir / "actions.parquet"; ep.meta["frame_count"]
    up.wait_idle(60); up.status(ep.key)     # {"state": "done", "episode_id": 12, ...}

In a job handler use ``recorder_for(ctx)``: the shared instance in ``ctx.services["recorder"]``,
built on first use from ``services["cameras"]`` and ``$ORCHESTRATOR_URL`` (no main.py wiring).

``client`` is anything with ``add_tick_listener(fn)``, ``remove_tick_listener(fn)`` and
``control_tick()`` (``RobotClient``, or ``LocalArmDriver`` below for laptops/tests). ``rig`` is a
started ``cameras.CameraRig``.

Timeline and clocks
-------------------
* One parquet row per synced sample of the rig's reference camera (``add_synced_callback``):
  row ``i`` <-> frame ``i`` of EVERY camera's mp4 (each camera writes the frame the rig paired with
  that sample), so ``frame_index`` is the video frame number, as TDM-39 ``convert.py`` assumes.
* Everything is stamped on ONE clock, ``time.monotonic_ns()``: ``Frame.t_ns`` (camera capture time)
  and ``ControlTick.t_ns`` (control loop). The robot state of row ``i`` is the control ticks
  interpolated to the frame's ``t_ns`` (linear for joints / gripper / IK target position, the
  earlier tick for quaternion, ``joint_cmd`` presence and ``control_source``).
* Wall-clock ``timestamp_us`` = one anchor taken at ``start()`` (the tightest of a few
  monotonic/wall pairs) + monotonic offset. ``takeover_ts`` / ``handback_ts`` are converted with
  the same anchor, so they sit on exactly the same clock as ``timestamp_us`` even if NTP slews the
  wall clock mid-episode.
* Control ticks come from ``add_tick_listener`` (every command ``run_control_loop`` streams).
  When nothing streams (arm held / idle) a sampler thread pulls ``control_tick()`` at the
  control rate, so rows always have a measured state near their frame time.

Takeovers
---------
``mark_takeover()`` / ``mark_handback()`` stamp now (or pass ``t_ns``). Inside the
[takeover, handback) window ticks reported ``idle`` (arm held while the operator's clutch is
released, or while the phone connects) are recorded as ``human``: the human has control authority
then. So is the ~3-period echo of the last ``policy`` tick right after the mark (``control_tick()``
keeps reporting the last streamed source that long; 6 periods are relabeled to cover the
sampler's pull spacing). Outside it ``control_source`` is what the control loop reported. Without explicit marks a
takeover-kind recording infers them from the stream (first ``policy -> human`` switch, then the
next ``human -> policy`` switch). If a takeover is never handed back, ``handback_ts`` is the stop
time (``meta.takeover.handback_inferred``).

Video
-----
H.264 mp4 at the camera fps, yuv420p, keyframe every second, ``+faststart``. The encoder is
probed once per process (encoding a few tiny frames) in this order, first one that works wins:
``gst-nvv4l2h264enc`` (Jetson NVENC via GStreamer; Orin AGX/NX, NOT Orin Nano which has no
encoder), ``ffmpeg-h264_nvmpi`` (jetson-ffmpeg), ``ffmpeg-libx264`` (software, the default on a
laptop and on a Nano), ``pyav-libx264``, ``cv2-avc1``. Force one with ``RDF_RECORDER_ENCODER``.

Resilience
----------
* Callbacks on the control/capture threads only append to a buffer / ``put_nowait`` into a bounded
  queue. If the writer falls behind, whole samples are dropped (row and frames together, counted
  in ``meta.drops``), so the frame <-> row mapping never breaks and nothing ever blocks the arm.
* Each episode is a directory under ``$RDF_EPISODES_DIR`` (default ``~/.rdf/episodes``):
  ``<camera>.mp4``, ``actions.parquet``, ``meta.json`` (written at start as ``recording``, final at
  stop) and ``upload.json`` (the upload queue state). Nothing depends on the network until upload.
* ``EpisodeUploader`` runs one background thread: sign-batch -> PUT each file (skipping ones
  already uploaded) -> ``create_episode``. Transient failures (connection errors, 5xx, 408/429)
  retry with exponential backoff forever; a deterministic refusal (4xx from the media API,
  reducer threw) marks the episode ``failed`` with the reason and keeps the files. On boot it
  rescans the directory and resumes every unfinished episode. ``create_episode`` is made
  idempotent by first looking up an episode row with the same ``storage_uri``.

CLI (laptop proof, SimArm + synthetic cameras; no SpacetimeDB needed unless --upload)::

    python -m rdf_robot_client.recorder record --seconds 20 --kind recovery_takeover \\
        --takeover-at 6 --handback-at 14 [--ticket 1 --upload --orchestrator-url http://127.0.0.1:8031]
    python -m rdf_robot_client.recorder check ~/.rdf/episodes/<key>      # rdf_common.review_checks
    python -m rdf_robot_client.recorder upload-pending --orchestrator-url ...   # drain the queue
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from rdf_common.episode_format import ACTIONS_FILENAME, make_metadata, write_actions

from .adapters.base import ControlTick

TAKEOVER_KINDS = ("recovery_takeover",)
META_FILENAME = "meta.json"
UPLOAD_FILENAME = "upload.json"
DEFAULT_ROOT = Path(os.environ.get("RDF_EPISODES_DIR") or Path.home() / ".rdf" / "episodes")
MAX_SECONDS = 180.0  # a forgotten recording is capped here (the reviewer rejects > 120 s anyway)


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "recorder", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write (tmp + rename), so a crash never leaves half a meta/upload file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=_jsonable))
    os.replace(tmp, path)


def _jsonable(v: Any) -> Any:
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, Path):
        return str(v)
    return str(v)


def new_episode_key() -> str:
    """Local id of a recording, also the ``episode_id`` path segment of the storage key."""
    return time.strftime("rec-%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]


# ===================================================================================== clocks

@dataclass(frozen=True)
class ClockAnchor:
    """``wall_us = wall_anchor_us + (t_ns - mono_anchor_ns) / 1000``: monotonic -> unix micros."""

    mono_ns: int
    wall_us: int
    err_us: float  # half-width of the tightest bracket (how well the pair was measured)

    @classmethod
    def now(cls, samples: int = 7) -> ClockAnchor:
        best: tuple[int, int, int] | None = None
        for _ in range(samples):
            m0 = time.monotonic_ns()
            w = time.time_ns()
            m1 = time.monotonic_ns()
            if best is None or (m1 - m0) < (best[2] - best[0]):
                best = (m0, w, m1)
        m0, w, m1 = best  # type: ignore[misc]
        return cls(mono_ns=(m0 + m1) // 2, wall_us=w // 1000, err_us=(m1 - m0) / 2000.0)

    def wall_us_of(self, t_ns: int) -> int:
        return self.wall_us + (int(t_ns) - self.mono_ns) // 1000


# ===================================================================================== ticks

class TickSource(Protocol):
    def add_tick_listener(self, fn: Callable[[ControlTick], None]) -> None: ...
    def remove_tick_listener(self, fn: Callable[[ControlTick], None]) -> None: ...
    def control_tick(self) -> ControlTick: ...


class _TickBuffer:
    """Thread-safe, time-ordered ControlTicks (monotonic ``t_ns``)."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._ticks: deque[ControlTick] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.pushed = 0
        self.last_push_ns = 0

    def push(self, tick: ControlTick) -> None:
        if not tick.t_ns:
            return
        with self._lock:
            if self._ticks and tick.t_ns < self._ticks[-1].t_ns:
                return  # out of order (pulled tick raced a streamed one): keep the stream monotonic
            self._ticks.append(tick)
            self.pushed += 1
            self.last_push_ns = tick.t_ns

    def latest_t_ns(self) -> int:
        with self._lock:
            return self._ticks[-1].t_ns if self._ticks else 0

    def bracket(self, t_ns: int) -> tuple[ControlTick | None, ControlTick | None]:
        """(last tick at or before t, first tick after t)."""
        with self._lock:
            ticks = list(self._ticks)
        ts = [k.t_ns for k in ticks]
        i = bisect.bisect_right(ts, t_ns)
        return (ticks[i - 1] if i > 0 else None), (ticks[i] if i < len(ticks) else None)

    def prune_before(self, t_ns: int) -> None:
        with self._lock:
            while len(self._ticks) > 2 and self._ticks[1].t_ns < t_ns:
                self._ticks.popleft()


def _lerp(a: Sequence[float], b: Sequence[float], w: float) -> list[float]:
    return [float(x) + (float(y) - float(x)) * w for x, y in zip(a, b)]


def tick_at(t_ns: int, a: ControlTick | None, b: ControlTick | None, max_gap_ns: int) -> tuple[ControlTick, str]:
    """The robot state at ``t_ns`` from the bracketing ticks -> (tick, how) where how is
    ``interp`` | ``prev`` | ``next``. Interpolates only when both ticks exist and are close."""
    if a is None and b is None:
        raise ValueError("no ticks")
    if a is None:
        return b, "next"  # type: ignore[return-value]
    if b is None or b.t_ns - a.t_ns > max_gap_ns or b.t_ns == a.t_ns:
        return a, "prev"
    w = (t_ns - a.t_ns) / (b.t_ns - a.t_ns)
    jc = None
    if a.joint_cmd is not None and b.joint_cmd is not None:
        jc = tuple(_lerp(a.joint_cmd, b.joint_cmd, w))
    elif a.joint_cmd is not None:
        jc = a.joint_cmd
    return ControlTick(
        timestamp_us=0, joint_pos=tuple(_lerp(a.joint_pos, b.joint_pos, w)),
        gripper_pos=float(a.gripper_pos + (b.gripper_pos - a.gripper_pos) * w),
        ik_target_pos=tuple(_lerp(a.ik_target_pos, b.ik_target_pos, w)),  # type: ignore[arg-type]
        ik_target_quat=a.ik_target_quat, gripper_cmd=float(a.gripper_cmd + (b.gripper_cmd - a.gripper_cmd) * w),
        control_source=a.control_source, joint_cmd=jc, t_ns=t_ns,
    ), "interp"


# ===================================================================================== video encoders

@dataclass(frozen=True)
class EncoderSpec:
    name: str        # gst-nvv4l2h264enc | ffmpeg-h264_nvmpi | ffmpeg-libx264 | pyav-libx264 | cv2-avc1
    kind: str        # gst | ffmpeg | pyav | cv2
    codec: str = ""  # ffmpeg/pyav codec name
    hardware: bool = False


ENCODER_CANDIDATES: tuple[EncoderSpec, ...] = (
    EncoderSpec("gst-nvv4l2h264enc", "gst", "nvv4l2h264enc", True),
    EncoderSpec("ffmpeg-h264_nvmpi", "ffmpeg", "h264_nvmpi", True),
    EncoderSpec("ffmpeg-libx264", "ffmpeg", "libx264", False),
    EncoderSpec("pyav-libx264", "pyav", "libx264", False),
    EncoderSpec("cv2-avc1", "cv2", "avc1", False),
)


class VideoWriter:
    """Write BGR frames to an H.264 mp4. ``write`` may block briefly (pipe back-pressure); it is
    called from the camera's encoder thread, never from capture or control threads."""

    def __init__(self, spec: EncoderSpec, path: Path, width: int, height: int, fps: float,
                 log_path: Path | None = None, x264_preset: str | None = None) -> None:
        self.spec, self.path, self.width, self.height, self.fps = spec, Path(path), width, height, fps
        self.frames = 0
        self._proc: subprocess.Popen | None = None
        self._av: Any = None
        self._cv: Any = None
        self._log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        preset = x264_preset or os.environ.get("RDF_RECORDER_X264_PRESET", "veryfast")
        fr = _fps_fraction(fps)
        gop = max(1, int(round(fps)))
        if spec.kind == "ffmpeg":
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                   "-s", f"{width}x{height}", "-framerate", fr, "-i", "pipe:0", "-an",
                   "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", spec.codec, "-pix_fmt", "yuv420p",
                   "-g", str(gop), "-r", fr]
            if spec.codec == "libx264":
                cmd += ["-preset", preset, "-crf", "23"]
            else:
                cmd += ["-b:v", "4M"]
            cmd += ["-movflags", "+faststart", "-f", "mp4", str(self.path)]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._log)
        elif spec.kind == "gst":
            num, den = fr.split("/")
            cmd = ["gst-launch-1.0", "-q", "-e", "fdsrc", "fd=0", "!",
                   "rawvideoparse", f"width={width}", f"height={height}", "format=bgr",
                   f"framerate={num}/{den}", "!", "videoconvert", "!", "video/x-raw,format=I420", "!",
                   "nvvidconv", "!", "video/x-raw(memory:NVMM),format=I420", "!",
                   spec.codec, "bitrate=4000000", f"iframeinterval={gop}", "insert-sps-pps=true", "!",
                   "h264parse", "!", "qtmux", "faststart=true", "!", "filesink", f"location={self.path}"]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._log)
        elif spec.kind == "pyav":
            import av  # type: ignore[import-not-found]

            self._av = av.open(str(self.path), "w", options={"movflags": "+faststart"})
            st = self._av.add_stream(spec.codec, rate=int(round(fps)))
            st.width, st.height, st.pix_fmt = width + (width % 2), height + (height % 2), "yuv420p"
            st.options = {"preset": preset, "crf": "23", "g": str(gop)}
            self._av_stream = st
        elif spec.kind == "cv2":
            import cv2

            self._cv = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*spec.codec), fps, (width, height))
            if not self._cv.isOpened():
                raise RuntimeError(f"cv2.VideoWriter({spec.codec}) did not open")
        else:  # pragma: no cover
            raise ValueError(spec.kind)

    def write(self, img: np.ndarray) -> None:
        if img.shape[0] != self.height or img.shape[1] != self.width:
            import cv2

            img = cv2.resize(img, (self.width, self.height), interpolation=cv2.INTER_AREA)
        if self._proc is not None:
            if self._proc.poll() is not None:
                raise RuntimeError(f"{self.spec.name} exited with {self._proc.returncode}")
            self._proc.stdin.write(np.ascontiguousarray(img).data)  # type: ignore[union-attr]
        elif self._av is not None:
            import av  # type: ignore[import-not-found]

            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format="bgr24")
            for pkt in self._av_stream.encode(frame):
                self._av.mux(pkt)
        else:
            self._cv.write(np.ascontiguousarray(img))
        self.frames += 1

    def close(self, timeout: float = 30.0) -> None:
        try:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()  # type: ignore[union-attr]
                except BrokenPipeError:
                    pass
                try:
                    rc = self._proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    raise RuntimeError(f"{self.spec.name} did not finish within {timeout:.0f}s")
                if rc != 0:
                    raise RuntimeError(f"{self.spec.name} exited with {rc}")
            elif self._av is not None:
                for pkt in self._av_stream.encode():
                    self._av.mux(pkt)
                self._av.close()
            elif self._cv is not None:
                self._cv.release()
        finally:
            if self._log is not subprocess.DEVNULL:
                self._log.close()  # type: ignore[union-attr]


def _fps_fraction(fps: float) -> str:
    if abs(fps - round(fps)) < 1e-6:
        return f"{int(round(fps))}/1"
    return f"{int(round(fps * 1001))}/1001"


_ENCODER_CACHE: dict[str, EncoderSpec] = {}
_ENCODER_LOCK = threading.Lock()


def _encoder_available(spec: EncoderSpec) -> bool:
    if spec.kind == "ffmpeg":
        if not shutil.which("ffmpeg"):
            return False
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any(line.split()[1:2] == [spec.codec] for line in out.splitlines() if line.strip())
    if spec.kind == "gst":
        if not (shutil.which("gst-launch-1.0") and shutil.which("gst-inspect-1.0")):
            return False
        try:
            return subprocess.run(["gst-inspect-1.0", spec.codec], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    if spec.kind == "pyav":
        try:
            import av  # type: ignore[import-not-found] # noqa: F401
        except ImportError:
            return False
        return True
    return True  # cv2 is a hard dependency; the probe decides


def probe_encoder(spec: EncoderSpec, width: int = 64, height: int = 48, fps: float = 30.0) -> str | None:
    """Encode 5 tiny frames with ``spec``; None if it works, else why not."""
    if not _encoder_available(spec):
        return "not installed"
    d = Path(tempfile.mkdtemp(prefix="rdf-tdm48-probe-"))
    try:
        p = d / "probe.mp4"
        w = VideoWriter(spec, p, width, height, fps, log_path=d / "probe.log")
        for k in range(5):
            w.write(np.full((height, width, 3), 40 * k, np.uint8))
        w.close(timeout=20)
        if not p.exists() or p.stat().st_size < 100:
            return "produced no output"
        n = count_video_frames(p)
        if n is not None and n != 5:
            return f"wrote {n} frames instead of 5"
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"[:200]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def select_encoder(prefer: str | None = None) -> EncoderSpec:
    """The first working encoder (probed once per process). ``prefer``/``RDF_RECORDER_ENCODER``
    names one from ``ENCODER_CANDIDATES`` to force it (it must still pass the probe)."""
    prefer = prefer or os.environ.get("RDF_RECORDER_ENCODER") or None
    key = prefer or "auto"
    with _ENCODER_LOCK:
        if key in _ENCODER_CACHE:
            return _ENCODER_CACHE[key]
        cands = [c for c in ENCODER_CANDIDATES if c.name == prefer] if prefer else list(ENCODER_CANDIDATES)
        if prefer and not cands:
            raise ValueError(f"unknown encoder {prefer!r}; one of {[c.name for c in ENCODER_CANDIDATES]}")
        why: dict[str, str] = {}
        for c in cands:
            err = probe_encoder(c)
            if err is None:
                _log("video encoder selected", encoder=c.name, hardware=c.hardware,
                     skipped=why or None)
                _ENCODER_CACHE[key] = c
                return c
            why[c.name] = err
        raise RuntimeError(f"no working H.264 encoder: {why}")


def count_video_frames(path: Path) -> int | None:
    """Decoded frame count (ffprobe if present, else OpenCV), None if unknown."""
    if shutil.which("ffprobe"):
        try:
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip().split(",")[0].isdigit():
                return int(r.stdout.strip().split(",")[0])
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        import cv2

        cap = cv2.VideoCapture(str(path))
        n = 0
        while cap.read()[0]:
            n += 1
        cap.release()
        return n
    except Exception:  # noqa: BLE001
        return None


class _CameraEncoder:
    """One camera's encoder on its own thread, fed through a bounded queue."""

    def __init__(self, name: str, spec: EncoderSpec, path: Path, fps: float, log_path: Path) -> None:
        self.name, self.spec, self.path, self.fps, self.log_path = name, spec, path, fps, log_path
        self.q: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=int(fps * 2))
        self.writer: VideoWriter | None = None
        self.error: str | None = None
        self.size: tuple[int, int] | None = None
        self._t = threading.Thread(target=self._run, name=f"rec-enc-{name}", daemon=True)
        self._t.start()

    def put(self, img: np.ndarray) -> None:
        self.q.put(img)  # blocks the writer thread (not capture) if the encoder is behind

    def _run(self) -> None:
        while True:
            img = self.q.get()
            if img is None:
                return
            if self.error:
                continue
            try:
                if self.writer is None:
                    h, w = img.shape[:2]
                    self.size = (w, h)
                    self.writer = VideoWriter(self.spec, self.path, w, h, self.fps, self.log_path)
                self.writer.write(img)
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"[:300]
                _log("encoder failed", camera=self.name, encoder=self.spec.name, error=self.error)

    def close(self) -> int:
        self.q.put(None)
        self._t.join(60)
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception as e:  # noqa: BLE001
                self.error = self.error or f"{type(e).__name__}: {e}"[:300]
        return self.writer.frames if self.writer else 0


# ===================================================================================== local episode

@dataclass
class LocalEpisode:
    """A finished recording on disk."""

    dir: Path
    meta: dict[str, Any]

    @property
    def key(self) -> str:
        return str(self.meta["episode_key"])

    @property
    def actions_path(self) -> Path:
        return self.dir / ACTIONS_FILENAME

    @property
    def video_paths(self) -> dict[str, Path]:
        return {c: self.dir / v["file"] for c, v in (self.meta.get("cameras") or {}).items()}

    def episode_row(self, **overrides: Any) -> dict[str, Any]:
        """The ``episode`` row ``create_episode`` will produce (for ``review_checks.run_checks``)."""
        m = self.meta
        row = {"kind": m["kind"], "duration_ms": m["duration_ms"], "frame_count": m["frame_count"],
               "takeover_ts": m.get("takeover_ts"), "handback_ts": m.get("handback_ts"),
               "success": m.get("success"), "ticket_id": m.get("ticket_id")}
        row.update(overrides)
        return row

    @classmethod
    def load(cls, d: str | Path) -> LocalEpisode:
        d = Path(d)
        return cls(d, json.loads((d / META_FILENAME).read_text()))


def check_local(ep: LocalEpisode | str | Path, thresholds: Any = None, **row_overrides: Any):
    """Run the reviewer's deterministic checks (``rdf_common.review_checks``) on a local recording.

    Synthetic recordings (SimArm / synthetic cameras) carry ``fixture: true``; they are checked with
    ``allow_fixtures`` so every other check still has to pass on its merits."""
    from rdf_common.episode_format import read_actions
    from rdf_common.review_checks import ReviewThresholds, run_checks

    ep = ep if isinstance(ep, LocalEpisode) else LocalEpisode.load(ep)
    th = thresholds or ReviewThresholds()
    if ep.meta.get("fixture") and not th.allow_fixtures:
        th = th.replace(allow_fixtures=True)
    try:
        actions, err = read_actions(ep.actions_path), None
    except Exception as e:  # noqa: BLE001
        actions, err = None, str(e)
    return run_checks(ep.episode_row(**row_overrides), actions, th, actions_error=err)


# ===================================================================================== recorder

class _Session:
    def __init__(self, rec: Recorder, d: Path, key: str, info: dict[str, Any]) -> None:
        self.rec, self.dir, self.key, self.info = rec, d, key, info
        self.anchor = ClockAnchor.now()
        self.start_ns = time.monotonic_ns()
        self.stop_ns: int | None = None
        self.q: queue.Queue[Any] = queue.Queue(maxsize=int(rec.fps * 4))
        self.ticks = _TickBuffer()
        self.cams = list(rec.rig.names)
        self.encoders = {c: _CameraEncoder(c, rec.encoder, d / f"{c}.mp4", rec.fps, d / f"encoder-{c}.log")
                         for c in self.cams}
        self.rows: dict[str, list] = {k: [] for k in (
            "t_ns", "joint_pos", "gripper_pos", "ik_target_pos", "ik_target_quat", "gripper_cmd",
            "control_source", "joint_cmd")}
        self.last_seq: dict[str, int] = {}
        self.unique: dict[str, int] = {c: 0 for c in self.cams}
        self.reused: dict[str, int] = {c: 0 for c in self.cams}
        self.drops = {"queue_full": 0, "preroll_skipped": 0, "out_of_sync": 0, "tick_waits_timed_out": 0}
        self.how = {"interp": 0, "prev": 0, "next": 0}
        self.pulled = 0
        self.takeover_ns: int | None = None
        self.handback_ns: int | None = None
        self.truncated = False
        self.done = threading.Event()
        self.error: str | None = None
        self._unsub: Callable[[], None] | None = None
        self._last_frame: dict[str, Any] = {}

    # ---- hot-path callbacks (never block)
    def on_tick(self, tick: ControlTick) -> None:
        self.ticks.push(tick)

    def on_synced(self, s: Any) -> None:
        if s.t_ns < self.start_ns:
            return
        try:
            self.q.put_nowait(s)
        except queue.Full:
            self.drops["queue_full"] += 1

    # ---- threads
    def sampler(self) -> None:
        """Pull control_tick() when nothing streamed recently (arm held / idle)."""
        period_ns = int(1e9 / self.rec.control_rate_hz)
        while not self.done.is_set():
            now = time.monotonic_ns()
            if now - self.ticks.last_push_ns > 1.5 * period_ns:
                try:
                    t = self.rec.source.control_tick()
                    self.ticks.push(t)
                    self.pulled += 1
                except Exception as e:  # noqa: BLE001
                    _log("control_tick pull failed", error=repr(e)[:200])
            self.done.wait(period_ns / 2e9)

    def writer(self) -> None:
        rec = self.rec
        period_ns = int(1e9 / rec.control_rate_hz)
        max_gap_ns = int(3.5 * period_ns)
        wait_ns = int(0.25e9)
        cap_ns = self.start_ns + int(rec.max_seconds * 1e9)
        try:
            while True:
                stop = self.stop_ns
                try:
                    s = self.q.get(timeout=0.05)
                except queue.Empty:
                    if stop is not None and time.monotonic_ns() > stop + int(0.5e9):
                        break
                    continue
                if stop is not None and s.t_ns >= stop:
                    break
                if s.t_ns >= cap_ns:
                    self.truncated = True
                    continue
                self._write_sample(s, max_gap_ns, wait_ns)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"[:300]
            _log("recorder writer failed", key=self.key, error=self.error)
        finally:
            self.done.set()

    def _write_sample(self, s: Any, max_gap_ns: int, wait_ns: int) -> None:
        if any(c not in s.frames and c not in self.last_seq for c in self.cams):
            self.drops["preroll_skipped"] += 1  # a camera has not delivered its first frame yet
            return
        # wait (bounded) until a control tick after the frame exists, so we can interpolate
        deadline = time.monotonic_ns() + wait_ns
        while self.ticks.latest_t_ns() < s.t_ns and time.monotonic_ns() < deadline:
            time.sleep(0.004)
        a, b = self.ticks.bracket(s.t_ns)
        if a is None and b is None:
            self.drops["preroll_skipped"] += 1
            return
        if b is None:
            self.drops["tick_waits_timed_out"] += 1
        tick, how = tick_at(s.t_ns, a, b, max_gap_ns)
        self.how[how] += 1
        if not s.in_sync:
            self.drops["out_of_sync"] += 1
        for c in self.cams:
            f = s.frames.get(c)
            if f is None:
                f = self._last_frame[c]
                self.reused[c] += 1
            elif self.last_seq.get(c) == f.seq:
                self.reused[c] += 1
            else:
                self.unique[c] += 1
            self.last_seq[c] = f.seq
            self._last_frame[c] = f
            self.encoders[c].put(f.image)
        r = self.rows
        r["t_ns"].append(int(s.t_ns))
        r["joint_pos"].append(list(tick.joint_pos))
        r["gripper_pos"].append(float(tick.gripper_pos))
        r["ik_target_pos"].append(list(tick.ik_target_pos))
        r["ik_target_quat"].append(list(tick.ik_target_quat))
        r["gripper_cmd"].append(float(tick.gripper_cmd))
        r["control_source"].append(tick.control_source)
        r["joint_cmd"].append(list(tick.joint_cmd) if tick.joint_cmd is not None else None)
        self.ticks.prune_before(s.t_ns - int(1e9))


class Recorder:
    """Records episodes from a tick source + camera rig. One recording at a time. See module doc."""

    def __init__(self, source: TickSource, rig: Any, *, uploader: EpisodeUploader | None = None,
                 root: str | Path | None = None, fps: float | None = None, encoder: str | None = None,
                 fixture: bool | None = None, embodiment: str | None = None,
                 joint_names: Sequence[str] | None = None, max_seconds: float = MAX_SECONDS,
                 on_event: Callable[..., None] | None = None) -> None:
        if rig is None:
            raise ValueError("the recorder needs a started CameraRig (cameras are part of every episode)")
        self.source, self.rig, self.uploader = source, rig, uploader
        self.root = Path(root or (uploader.root if uploader else DEFAULT_ROOT))
        self.root.mkdir(parents=True, exist_ok=True)
        ref = rig.config.camera(rig.reference)
        self.fps = float(fps or ref.fps)
        self.encoder = select_encoder(encoder)
        arm = getattr(source, "arm", None)
        self.control_rate_hz = float(getattr(arm, "control_rate_hz", 30.0) or 30.0)
        self.embodiment = embodiment or getattr(arm, "embodiment", "unknown")
        names = joint_names or getattr(arm, "joint_names", None)
        self.joint_names = list(names) if names else None
        synthetic_cams = any(c.source == "synthetic" for c in rig.config.cameras)
        sim_arm = any(s in str(self.embodiment) for s in ("sim", "fake"))
        self.fixture = bool(fixture) if fixture is not None else (synthetic_cams or sim_arm)
        self.max_seconds = float(max_seconds)
        self._on_event = on_event or getattr(source, "event", None)
        self._lock = threading.Lock()
        self._s: _Session | None = None
        self._threads: list[threading.Thread] = []
        self.last: LocalEpisode | None = None

    @classmethod
    def from_client(cls, client: Any, rig: Any, orchestrator_url: str | None, **kw: Any) -> Recorder:
        """Recorder + background uploader wired to a ``RobotClient`` (main.py)."""
        up = EpisodeUploader.from_client(client, orchestrator_url, root=kw.pop("root", None)).start() \
            if orchestrator_url else None
        return cls(client, rig, uploader=up, **kw)

    # ---- state
    @property
    def recording(self) -> bool:
        return self._s is not None

    @property
    def current_key(self) -> str | None:
        return self._s.key if self._s else None

    def _event(self, type_: str, **payload: Any) -> None:
        if self._on_event:
            try:
                self._on_event(type_, **payload)
            except Exception:  # noqa: BLE001
                pass

    # ---- API
    def start(self, ticket_id: int, kind: str, task_id: int = 0, contributor_id: int = 0, *,
              checkpoint_uri: str | None = None, note: str = "") -> str:
        """Begin recording; returns the local episode key. Raises if a recording is running."""
        with self._lock:
            if self._s is not None:
                raise RuntimeError(f"already recording {self._s.key}")
            if not self.rig.running:
                raise RuntimeError("camera rig is not running")
            key = new_episode_key()
            d = self.root / key
            d.mkdir(parents=True)
            info = {"ticket_id": int(ticket_id), "kind": str(kind), "task_id": int(task_id or 0),
                    "contributor_id": int(contributor_id or 0), "checkpoint_uri": checkpoint_uri, "note": note}
            s = _Session(self, d, key, info)
            _write_json(d / META_FILENAME, {"episode_key": key, "status": "recording", **info,
                                            "started_at_us": s.anchor.wall_us_of(s.start_ns)})
            self.source.add_tick_listener(s.on_tick)
            try:
                s.ticks.push(self.source.control_tick())  # a state sample at t0
            except Exception:  # noqa: BLE001
                pass
            s._unsub = self.rig.add_synced_callback(s.on_synced)
            self._threads = [threading.Thread(target=s.writer, name="rec-writer", daemon=True),
                             threading.Thread(target=s.sampler, name="rec-sampler", daemon=True)]
            for t in self._threads:
                t.start()
            self._s = s
        _log("recording started", key=key, **info, encoder=self.encoder.name, cameras=s.cams, fixture=self.fixture)
        self._event("robot.episode_recording", episode_key=key, ticket_id=int(ticket_id), kind=kind)
        return key

    def mark_takeover(self, t_ns: int | None = None) -> int:
        """Human takes control now (or at monotonic ``t_ns``). Returns takeover_ts (unix us)."""
        s = self._require()
        s.takeover_ns = int(t_ns or time.monotonic_ns())
        s.handback_ns = None
        return s.anchor.wall_us_of(s.takeover_ns)

    def mark_handback(self, t_ns: int | None = None) -> int:
        """Control returns to the policy now (or at ``t_ns``). Returns handback_ts (unix us)."""
        s = self._require()
        if s.takeover_ns is None:
            raise RuntimeError("mark_handback without mark_takeover")
        s.handback_ns = int(t_ns or time.monotonic_ns())
        return s.anchor.wall_us_of(s.handback_ns)

    def stop(self, success: bool | None, note: str = "", *, upload: bool = True) -> LocalEpisode:
        """Stop, finalise the files on disk and (if an uploader is set) queue the upload.
        Blocks for the encoders to flush (about a second), never for the network."""
        with self._lock:
            s = self._require()
            s.stop_ns = time.monotonic_ns()
        s.done.wait(10.0)
        for t in self._threads:
            t.join(5.0)
        try:
            self.source.remove_tick_listener(s.on_tick)
        except Exception:  # noqa: BLE001
            pass
        if s._unsub:
            s._unsub()
        try:
            ep = self._finalize(s, success, note)
        finally:
            with self._lock:
                self._s = None
        self.last = ep
        if upload and self.uploader is not None and ep.meta["status"] == "recorded":
            self.uploader.enqueue(ep.dir)
        return ep

    def abort(self, reason: str = "aborted") -> None:
        """Stop without keeping the episode (files are deleted)."""
        s = self._s
        if s is None:
            return
        ep = self.stop(None, reason, upload=False)
        shutil.rmtree(ep.dir, ignore_errors=True)
        _log("recording aborted", key=ep.key, reason=reason)

    def _require(self) -> _Session:
        s = self._s
        if s is None:
            raise RuntimeError("not recording")
        return s

    # ---- finalise
    def _finalize(self, s: _Session, success: bool | None, note: str) -> LocalEpisode:
        frames_written = {c: e.close() for c, e in s.encoders.items()}
        enc_errors = {c: e.error for c, e in s.encoders.items() if e.error}
        r = s.rows
        n = len(r["t_ns"])
        t_ns = np.asarray(r["t_ns"], dtype=np.int64)
        ts_us = np.asarray([s.anchor.wall_us_of(t) for t in t_ns], dtype=np.int64)
        src = list(r["control_source"])

        # takeover window: explicit marks, else inferred from the control_source stream
        tk_ns, hb_ns = s.takeover_ns, s.handback_ns
        inferred: dict[str, str] = {}
        if tk_ns is None and s.info["kind"] in TAKEOVER_KINDS and n:
            seen_policy = False
            for i, v in enumerate(src):
                if v == "policy":
                    seen_policy = True
                elif v == "human" and seen_policy:
                    tk_ns = int(t_ns[i])
                    inferred["takeover"] = "control_source policy->human"
                    for j in range(i + 1, n):
                        if src[j] == "policy":
                            hb_ns = int(t_ns[j])
                            inferred["handback"] = "control_source human->policy"
                            break
                    break
        if tk_ns is not None and hb_ns is None:
            hb_ns = int(t_ns[-1]) if n and t_ns[-1] > tk_ns else int(s.stop_ns or time.monotonic_ns())
            inferred["handback"] = "stop (never handed back)"
        relabeled = 0
        if tk_ns is not None and hb_ns is not None:
            # control_tick() keeps reporting the last streamed source for ~3 control periods after the
            # stream stops, so "policy" right after the pause is that echo, not the policy driving
            echo_ns = tk_ns + int(6e9 / self.control_rate_hz)  # 3 echo periods + sampler pull spacing
            for i in range(n):
                if tk_ns <= t_ns[i] < hb_ns and (src[i] == "idle" or (src[i] == "policy" and t_ns[i] < echo_ns)):
                    src[i] = "human"
                    relabeled += 1

        cams_meta = {}
        for c in s.cams:
            size = s.encoders[c].size or (0, 0)
            cams_meta[c] = {"file": f"{c}.mp4", "frames": int(s.unique[c]), "written": int(frames_written[c]),
                            "reused": int(s.reused[c]), "fps": self.fps, "width": size[0], "height": size[1],
                            "encoder": self.encoder.name}
        status = "recorded"
        problems = []
        if n < 2:
            status, problems = "empty", [f"only {n} rows recorded"]
        if s.error:
            status, problems = "failed", problems + [f"writer: {s.error}"]
        if enc_errors:
            status, problems = "failed", problems + [f"encoder {c}: {e}" for c, e in enc_errors.items()]
        for c, w in frames_written.items():
            if status == "recorded" and w != n:
                status, problems = "failed", problems + [f"{c}.mp4 has {w} frames for {n} rows"]

        duration_ms = int(round((int(ts_us[-1]) - int(ts_us[0])) / 1000)) if n >= 2 else 0
        md_note = note or s.info.get("note") or ""
        if self.fixture:
            md_note = ("[SIM] synthetic recording (sim arm and/or synthetic cameras), not real robot data. "
                       + md_note).strip()
        if n:
            jc = r["joint_cmd"]
            columns: dict[str, Any] = {
                "frame_index": np.arange(n, dtype=np.int32), "timestamp_us": ts_us,
                "joint_pos": np.asarray(r["joint_pos"], np.float32), "gripper_pos": np.asarray(r["gripper_pos"], np.float32),
                "ik_target_pos": np.asarray(r["ik_target_pos"], np.float32),
                "ik_target_quat": np.asarray(r["ik_target_quat"], np.float32),
                "gripper_cmd": np.asarray(r["gripper_cmd"], np.float32), "control_source": src,
            }
            if all(v is not None for v in jc):
                columns["joint_cmd"] = np.asarray(jc, np.float32)
            jn = self.joint_names or [f"j{i + 1}" for i in range(len(r["joint_pos"][0]))]
            md = make_metadata(fps=self.fps, joint_names=jn, embodiment=str(self.embodiment),
                               cameras={c: {k: v for k, v in m.items() if k in ("frames", "fps", "width", "height")}
                                        for c, m in cams_meta.items()},
                               fixture=self.fixture, note=md_note)
            md["recorder"] = {"version": 1, "episode_key": s.key, "clock": "monotonic->wall anchor"}
            write_actions(s.dir / ACTIONS_FILENAME, columns, md)

        meta = {
            "episode_key": s.key, "status": status, "problems": problems, **s.info, "note": md_note,
            "success": success, "fixture": self.fixture, "embodiment": self.embodiment,
            "fps": self.fps, "control_rate_hz": self.control_rate_hz,
            "frame_count": n, "duration_ms": duration_ms,
            "started_at_us": s.anchor.wall_us_of(s.start_ns), "stopped_at_us": s.anchor.wall_us_of(s.stop_ns or 0),
            "first_frame_us": int(ts_us[0]) if n else None, "last_frame_us": int(ts_us[-1]) if n else None,
            "takeover_ts": s.anchor.wall_us_of(tk_ns) if tk_ns is not None else None,
            "handback_ts": s.anchor.wall_us_of(hb_ns) if hb_ns is not None else None,
            "takeover": {"marked": s.takeover_ns is not None, "inferred": inferred, "idle_relabeled_human": relabeled},
            "control_source_counts": {k: src.count(k) for k in sorted(set(src))},
            "cameras": cams_meta, "encoder": {"name": self.encoder.name, "hardware": self.encoder.hardware},
            "drops": dict(s.drops), "truncated_at_max_seconds": s.truncated,
            "ticks": {"pushed": s.ticks.pushed, "pulled": s.pulled, "state_at_frame": dict(s.how)},
            "clock": {"anchor_mono_ns": s.anchor.mono_ns, "anchor_wall_us": s.anchor.wall_us,
                      "anchor_err_us": round(s.anchor.err_us, 2)},
            "files": [f"{c}.mp4" for c in s.cams] + ([ACTIONS_FILENAME] if n else []) + [META_FILENAME],
        }
        _write_json(s.dir / META_FILENAME, meta)
        ep = LocalEpisode(s.dir, meta)
        _log("recording stopped", key=s.key, status=status, frames=n, duration_ms=duration_ms,
             takeover_ts=meta["takeover_ts"], handback_ts=meta["handback_ts"], drops=s.drops, problems=problems or None)
        self._event("robot.episode_recorded", episode_key=s.key, ticket_id=s.info["ticket_id"], kind=s.info["kind"],
                    status=status, frame_count=n, duration_ms=duration_ms, success=success)
        return ep


_SERVICE_LOCK = threading.Lock()


def recorder_for(ctx_or_client: Any, *, orchestrator_url: str | None = None,
                 root: str | Path | None = None) -> Recorder | None:
    """The robot client's shared Recorder (``services["recorder"]``), built on first use from
    ``services["cameras"]`` (TDM-47 rig) and ``$ORCHESTRATOR_URL``. Pass a ``JobContext`` or the
    ``RobotClient``. Returns None when the client runs without cameras. Job handlers use it as::

        rec = recorder_for(ctx)
        if rec:
            rec.start(ctx.command.ticket_id, "recovery_takeover", task_id, contributor_id)
        ...  # ctx.run_control_loop(...); rec.mark_takeover() / rec.mark_handback()
        if rec:
            rec.stop(success=ok)            # local files now; upload + create_episode in background
    """
    client = getattr(ctx_or_client, "client", ctx_or_client)
    services: dict[str, Any] = client.services
    with _SERVICE_LOCK:
        rec = services.get("recorder")
        if rec is None:
            rig = services.get("cameras")
            if rig is None:
                return None
            url = orchestrator_url or os.environ.get("ORCHESTRATOR_URL") or "http://localhost:8000"
            rec = Recorder.from_client(client, rig, url, root=root)
            services["recorder"] = rec
        return rec


# ===================================================================================== uploader

class PermanentUploadError(RuntimeError):
    """Retrying will not help (bad request, reducer refused)."""


_TRANSIENT_HTTP = {408, 425, 429}


class EpisodeUploader:
    """Disk-backed upload queue: signed PUTs through the orchestrator, then ``create_episode``.

    ``stdb`` is a ``rdf_common.spacetime_client.SpacetimeClient`` acting as the robot_client
    identity (``call_reducer`` + ``sql``). One thread; never touches the control loop."""

    def __init__(self, orchestrator_url: str, stdb: Any, *, root: str | Path | None = None,
                 http_timeout: float = 30.0, put_timeout: float = 600.0, max_backoff_s: float = 60.0,
                 on_event: Callable[..., None] | None = None) -> None:
        self.base = orchestrator_url.rstrip("/")
        self.stdb = stdb
        self.root = Path(root or DEFAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self.http_timeout, self.put_timeout, self.max_backoff_s = http_timeout, put_timeout, max_backoff_s
        self._on_event = on_event
        self._pending: dict[str, float] = {}  # dir -> next attempt (monotonic)
        self._cv = threading.Condition()
        self._busy = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._http: Any = None

    @classmethod
    def from_client(cls, client: Any, orchestrator_url: str, **kw: Any) -> EpisodeUploader:
        from rdf_common.spacetime_client import SpacetimeClient

        return cls(orchestrator_url, SpacetimeClient(config=client.stdb), on_event=getattr(client, "event", None), **kw)

    # ---- lifecycle
    def start(self) -> EpisodeUploader:
        import httpx

        if self._thread is not None:
            return self
        self._http = httpx.Client(timeout=self.http_timeout)
        self.rescan()
        self._thread = threading.Thread(target=self._run, name="rec-uploader", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(10)
        if self._http:
            self._http.close()

    def rescan(self) -> int:
        """Queue every recorded episode under ``root`` whose upload has not finished."""
        n = 0
        for d in sorted(self.root.iterdir()) if self.root.exists() else []:
            if not (d / META_FILENAME).exists():
                continue
            try:
                meta = json.loads((d / META_FILENAME).read_text())
            except ValueError:
                continue
            if meta.get("status") != "recorded":
                continue
            st = self._state(d)
            if st.get("state") in ("done", "failed"):
                continue
            self.enqueue(d)
            n += 1
        return n

    def enqueue(self, d: str | Path) -> None:
        d = Path(d)
        st = self._state(d)
        if not st:
            self._save(d, {"state": "pending", "attempts": 0, "uploaded": {}, "queued_at": time.time()})
        with self._cv:
            self._pending[str(d)] = 0.0
            self._cv.notify_all()

    def status(self, key_or_dir: str | Path) -> dict[str, Any]:
        d = Path(key_or_dir)
        if not d.is_absolute() and not d.exists():
            d = self.root / str(key_or_dir)
        return self._state(d)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """True once nothing is due (pending retries in backoff count as not idle)."""
        end = time.monotonic() + timeout
        with self._cv:
            while self._pending or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.2))
        return True

    # ---- state file
    @staticmethod
    def _state(d: Path) -> dict[str, Any]:
        p = Path(d) / UPLOAD_FILENAME
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            return {}

    @staticmethod
    def _save(d: Path, st: dict[str, Any]) -> None:
        _write_json(Path(d) / UPLOAD_FILENAME, st)

    # ---- worker
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                now = time.monotonic()
                due = [d for d, t in self._pending.items() if t <= now]
                if not due:
                    nxt = min(self._pending.values(), default=now + 1.0)
                    self._cv.wait(max(0.05, min(1.0, nxt - now)))
                    continue
                d = min(due)
                self._busy = True
                del self._pending[d]
            try:
                self._process(Path(d))
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def upload_now(self, d: str | Path) -> dict[str, Any]:
        """Synchronous single attempt (CLI / tests). Returns the upload state."""
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=self.http_timeout)
        if not self._state(Path(d)):
            self._save(Path(d), {"state": "pending", "attempts": 0, "uploaded": {}, "queued_at": time.time()})
        self._process(Path(d), requeue=False)
        return self._state(Path(d))

    def _process(self, d: Path, requeue: bool = True) -> None:
        st = self._state(d) or {"state": "pending", "attempts": 0, "uploaded": {}}
        if st.get("state") in ("done", "failed"):
            return
        st["attempts"] = int(st.get("attempts") or 0) + 1
        try:
            ep = LocalEpisode.load(d)
            self._upload_files(ep, st)
            self._create_episode(ep, st)
            st.update(state="done", error=None, done_at=time.time())
            self._save(d, st)
            _log("episode uploaded", key=ep.key, episode_id=st.get("episode_id"), storage_uri=st.get("storage_uri"),
                 attempts=st["attempts"])
            self._event("robot.episode_uploaded", episode_key=ep.key, episode_id=st.get("episode_id"),
                        ticket_id=ep.meta.get("ticket_id"), storage_uri=st.get("storage_uri"), attempts=st["attempts"])
        except PermanentUploadError as e:
            st.update(state="failed", error=str(e)[:1000], failed_at=time.time())
            self._save(d, st)
            _log("episode upload failed permanently (files kept)", dir=str(d), error=str(e)[:300])
            self._event("robot.episode_upload_failed", episode_key=d.name, error=str(e)[:500])
        except Exception as e:  # noqa: BLE001 - network blip etc.: retry with backoff
            delay = min(self.max_backoff_s, 2.0 ** min(st["attempts"] - 1, 10))
            st.update(state="retrying", error=f"{type(e).__name__}: {e}"[:1000], next_retry_in_s=delay)
            self._save(d, st)
            _log("episode upload will retry", dir=str(d), attempt=st["attempts"], in_s=delay, error=str(e)[:200])
            if requeue and not self._stop.is_set():
                with self._cv:
                    self._pending[str(d)] = time.monotonic() + delay

    def _event(self, type_: str, **payload: Any) -> None:
        if self._on_event:
            try:
                self._on_event(type_, **payload)
            except Exception:  # noqa: BLE001
                pass

    def _upload_files(self, ep: LocalEpisode, st: dict[str, Any]) -> None:
        files = [f for f in ep.meta["files"] if (ep.dir / f).exists()]
        uploaded: dict[str, str] = st.setdefault("uploaded", {})
        todo = [f for f in files if f not in uploaded]
        if not todo:
            return
        types = {".mp4": "video/mp4", ".parquet": "application/vnd.apache.parquet", ".json": "application/json"}
        body = {"ticket_id": str(ep.meta["ticket_id"]), "episode_id": ep.key,
                "files": [{"filename": f, "content_type": types.get(Path(f).suffix, "application/octet-stream")}
                          for f in todo]}
        r = self._http.post(f"{self.base}/uploads/sign-batch", json=body, timeout=self.http_timeout)
        self._raise_for(r, "sign-batch")
        signed = r.json()["uploads"]
        for f, s in zip(todo, signed):
            p = ep.dir / f
            headers = {**(s.get("headers") or {}), "Content-Length": str(p.stat().st_size)}
            with open(p, "rb") as fh:
                pr = self._http.request(s.get("method") or "PUT", s["upload_url"], content=fh, headers=headers,
                                        timeout=self.put_timeout)
            self._raise_for(pr, f"PUT {f}")
            uploaded[f] = s["storage_uri"]
            self._save(ep.dir, st)  # progress survives a crash: finished files are not re-sent

    @staticmethod
    def _raise_for(r: Any, what: str) -> None:
        if r.status_code < 300:
            return
        msg = f"{what}: HTTP {r.status_code} {r.text[:300]}"
        if 400 <= r.status_code < 500 and r.status_code not in _TRANSIENT_HTTP:
            raise PermanentUploadError(msg)
        raise RuntimeError(msg)

    def _create_episode(self, ep: LocalEpisode, st: dict[str, Any]) -> None:
        from rdf_common.spacetime_client import SpacetimeError

        up = st["uploaded"]
        m = ep.meta
        actions_uri = up.get(ACTIONS_FILENAME, "")
        any_uri = actions_uri or next(iter(up.values()))
        storage_uri = any_uri.rsplit("/", 1)[0]  # the episode's prefix: .../tickets/{t}/episodes/{key}
        cameras = {c: up[v["file"]] for c, v in (m.get("cameras") or {}).items() if v["file"] in up}
        st["storage_uri"], st["cameras"] = storage_uri, cameras
        existing = self._find_episode(storage_uri)
        if existing is None:
            try:
                self.stdb.call_reducer(
                    "create_episode", int(m["ticket_id"]), int(m.get("task_id") or 0), int(m.get("contributor_id") or 0),
                    m["kind"], storage_uri, actions_uri, json.dumps(cameras), int(m["frame_count"]),
                    int(m["duration_ms"]), m.get("takeover_ts"), m.get("handback_ts"), m.get("success"),
                    m.get("checkpoint_uri"))
            except SpacetimeError as e:
                # 530 = the reducer threw (ticket closed, task already has an episode, ...): deterministic
                if e.status == 530 or (400 <= e.status < 500 and e.status not in _TRANSIENT_HTTP):
                    again = self._find_episode(storage_uri)  # maybe our previous attempt landed after all
                    if again is None:
                        raise PermanentUploadError(f"create_episode refused: {e.message[:500]}") from e
                else:
                    raise
            existing = self._find_episode(storage_uri)
        st["episode_id"] = int(existing["id"]) if existing else None

    def _find_episode(self, storage_uri: str) -> dict[str, Any] | None:
        safe = storage_uri.replace("'", "''")
        rows = self.stdb.sql(f"SELECT * FROM episode WHERE storage_uri = '{safe}'")
        return rows[0] if rows else None


# ===================================================================================== laptop driver

class LocalArmDriver:
    """A minimal tick source for laptops/tests: streams ``step(t, state) -> Target`` to an arm at
    its control rate on a thread and emits ``ControlTick``s exactly like ``RobotClient`` does
    (``record_command`` / ``control_tick``). ``source`` is a callable ``t -> 'policy'|'human'|'idle'``;
    ``idle`` ticks hold the arm (no command), like a paused rollout."""

    def __init__(self, arm: Any, step: Callable[[float, Any], Any], source: Callable[[float], str]) -> None:
        self.arm, self.step, self.source = arm, step, source
        self._listeners: list[Callable[[ControlTick], None]] = []
        self._last: tuple[Any, float, str, float] | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self.t0 = 0.0

    def add_tick_listener(self, fn: Callable[[ControlTick], None]) -> None:
        self._listeners.append(fn)

    def remove_tick_listener(self, fn: Callable[[ControlTick], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def control_tick(self) -> ControlTick:
        st = self.arm.read_state()
        recent = self._last is not None and time.monotonic() - self._last[3] < 3.0 / self.arm.control_rate_hz
        pose, g, src = (self._last[0], self._last[1], self._last[2]) if recent else (st.ee_pose, st.gripper, "idle")  # type: ignore[index]
        return ControlTick(timestamp_us=int(time.time() * 1e6), joint_pos=st.joints, gripper_pos=st.gripper,
                           ik_target_pos=pose.xyz, ik_target_quat=pose.quat, gripper_cmd=g, control_source=src,
                           joint_cmd=self.arm.last_joint_cmd, t_ns=time.monotonic_ns())

    def start(self) -> LocalArmDriver:
        self.t0 = time.monotonic()
        self._t = threading.Thread(target=self._run, name="local-arm-driver", daemon=True)
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(3)

    def _run(self) -> None:
        from .adapters.base import EETarget

        period = 1.0 / self.arm.control_rate_hz
        nxt = time.monotonic()
        while not self._stop.is_set():
            t = time.monotonic() - self.t0
            src = self.source(t)
            if src == "idle":
                self.arm.hold()
            else:
                target = self.step(t, self.arm.read_state())
                self.arm.command(target)
                pose = target.pose if isinstance(target, EETarget) else self.arm.fk(target.joints)
                g = target.gripper if target.gripper is not None else self.arm.read_state().gripper
                self._last = (pose, float(g), src, time.monotonic())
                tick = self.control_tick()
                for fn in list(self._listeners):
                    fn(tick)
            nxt += period
            d = nxt - time.monotonic()
            if d > 0:
                self._stop.wait(d)
            else:
                nxt = time.monotonic()


def sim_motion(home: Sequence[float]) -> Callable[[float, Any], Any]:
    """A smooth demo motion around ``home`` (sine sweeps; the human segment moves differently)."""
    from .adapters.base import JointTarget

    def step(t: float, _st: Any) -> Any:
        q = list(home)
        q[0] += 0.4 * math.sin(2 * math.pi * 0.2 * t)
        q[1] += 0.15 * math.sin(2 * math.pi * 0.35 * t)
        q[2] += 0.1 * math.sin(2 * math.pi * 0.15 * t)
        return JointTarget(tuple(q), gripper=0.5 + 0.5 * math.cos(2 * math.pi * 0.1 * t))

    return step


# ===================================================================================== CLI

def _source_schedule(kind: str, takeover_at: float | None, handback_at: float | None,
                     pause_s: float) -> Callable[[float], str]:
    if kind not in TAKEOVER_KINDS or takeover_at is None:
        src = "human" if kind in ("clean_teleop",) else "policy"
        return lambda t: src

    def f(t: float) -> str:
        if t < takeover_at:
            return "policy"
        if t < takeover_at + pause_s:
            return "idle"  # paused, operator connecting
        if handback_at is None or t < handback_at:
            return "human"
        return "policy"

    return f


def _cmd_record(a: argparse.Namespace) -> int:
    from .adapters.sim import SimArm
    from .cameras import CameraRig

    arm = SimArm()
    arm.connect()
    rig = CameraRig.from_config(synthetic=True).start()
    if not rig.wait_ready(10):
        print("cameras not ready", file=sys.stderr)
        return 2
    sched = _source_schedule(a.kind, a.takeover_at, a.handback_at, a.pause_s)
    drv = LocalArmDriver(arm, sim_motion(arm.home_joints), sched).start()
    up = None
    if a.upload:
        from rdf_common.spacetime_client import (
            SpacetimeClient,
            SpacetimeConfig,
            load_dotenv,
        )

        load_dotenv()
        token = Path(a.token_file).read_text().strip() if a.token_file else None
        up = EpisodeUploader(a.orchestrator_url, SpacetimeClient(config=SpacetimeConfig.resolve(a.host, a.db, token)),
                             root=a.root).start()
    rec = Recorder(drv, rig, uploader=up, root=a.root, encoder=a.encoder)
    rec.start(a.ticket, a.kind, a.task, a.contributor, note="recorder CLI sim run")
    drv.t0 = t0 = time.monotonic()  # the driver's schedule and the marks share one time base
    marked_tk = marked_hb = False
    while time.monotonic() - t0 < a.seconds:
        t = time.monotonic() - t0
        if a.takeover_at is not None and not marked_tk and t >= a.takeover_at:
            rec.mark_takeover()
            marked_tk = True
        if a.handback_at is not None and marked_tk and not marked_hb and t >= a.handback_at:
            rec.mark_handback()
            marked_hb = True
        time.sleep(0.01)
    ep = rec.stop(success=not a.failure)
    drv.stop()
    rig.stop()
    arm.disconnect()
    report = check_local(ep)
    out = {"dir": str(ep.dir), "status": ep.meta["status"], "frame_count": ep.meta["frame_count"],
           "duration_ms": ep.meta["duration_ms"], "takeover_ts": ep.meta["takeover_ts"],
           "handback_ts": ep.meta["handback_ts"], "drops": ep.meta["drops"], "encoder": ep.meta["encoder"],
           "review_checks_passed": report.passed, "checks": {r.name: r.status for r in report.results},
           "review_reason": report.reason() or None}
    if up is not None:
        up.wait_idle(a.upload_timeout)
        out["upload"] = up.status(ep.dir)
        up.stop()
    print(json.dumps(out, indent=2, default=str))
    return 0 if report.passed and ep.meta["status"] == "recorded" else 1


def _cmd_check(a: argparse.Namespace) -> int:
    report = check_local(a.dir)
    print(json.dumps({"passed": report.passed, "reason": report.reason() or None, "metrics": report.metrics()},
                     indent=2, default=str))
    return 0 if report.passed else 1


def _cmd_upload_pending(a: argparse.Namespace) -> int:
    from rdf_common.spacetime_client import (
        SpacetimeClient,
        SpacetimeConfig,
        load_dotenv,
    )

    load_dotenv()
    token = Path(a.token_file).read_text().strip() if a.token_file else None
    up = EpisodeUploader(a.orchestrator_url, SpacetimeClient(config=SpacetimeConfig.resolve(a.host, a.db, token)),
                         root=a.root).start()
    print(json.dumps({"queued": len(up._pending)}), file=sys.stderr)
    ok = up.wait_idle(a.upload_timeout)
    up.stop()
    return 0 if ok else 1


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rdf_robot_client.recorder", description="TDM-48 episode recorder")
    sub = p.add_subparsers(dest="cmd", required=True)

    def conn(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=None, help=f"episode directory (default {DEFAULT_ROOT})")
        sp.add_argument("--orchestrator-url", default=os.environ.get("ORCHESTRATOR_URL") or "http://localhost:8000")
        sp.add_argument("--host", default=None)
        sp.add_argument("--db", default=None)
        sp.add_argument("--token-file", default=None, help="robot_client identity token (default $SPACETIME_TOKEN)")
        sp.add_argument("--upload-timeout", type=float, default=120.0)

    r = sub.add_parser("record", help="laptop: record SimArm + synthetic cameras")
    conn(r)
    r.add_argument("--seconds", type=float, default=20.0)
    r.add_argument("--ticket", type=int, default=0)
    r.add_argument("--task", type=int, default=0)
    r.add_argument("--contributor", type=int, default=0)
    r.add_argument("--kind", default="clean_teleop", choices=["baseline", "eval", "clean_teleop", "recovery_takeover"])
    r.add_argument("--takeover-at", type=float, default=None)
    r.add_argument("--handback-at", type=float, default=None)
    r.add_argument("--pause-s", type=float, default=0.5, help="idle gap between policy pause and human control")
    r.add_argument("--failure", action="store_true", help="mark success=false")
    r.add_argument("--encoder", default=None)
    r.add_argument("--upload", action="store_true")
    r.set_defaults(fn=_cmd_record)

    c = sub.add_parser("check", help="run rdf_common.review_checks on a local episode dir")
    c.add_argument("dir")
    c.set_defaults(fn=_cmd_check)

    u = sub.add_parser("upload-pending", help="upload every unfinished episode under --root")
    conn(u)
    u.set_defaults(fn=_cmd_upload_pending)

    a = p.parse_args(list(argv) if argv is not None else None)
    return a.fn(a)


__all__ = ["ClockAnchor", "EncoderSpec", "EpisodeUploader", "LocalArmDriver", "LocalEpisode",
           "PermanentUploadError", "Recorder", "check_local", "count_video_frames", "recorder_for", "select_encoder",
           "tick_at"]

if __name__ == "__main__":
    raise SystemExit(main())
