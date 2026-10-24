"""TDM-47: synced capture from the wrist + top (exo/scene) cameras.

One capture thread per camera reads frames as fast as the camera delivers them (30 fps), stamps
each with a monotonic timestamp, applies the configured rotation/flip and appends it to a per-camera
ring buffer. A sync thread pairs every frame of the *reference* camera with the nearest-in-time
frame of every other camera and publishes the pair as a ``SyncedFrames``.

Public API (what the command loop TDM-49, recorder TDM-48, rollout and streamer TDM-55 call)::

    from rdf_robot_client.cameras import CameraRig

    rig = CameraRig.from_config()               # config/cameras.yaml (or $RDF_CAMERAS_CONFIG)
    rig = CameraRig.from_config(synthetic=True) # laptop: fake cameras with the same timing
    rig.start()                                 # or: with CameraRig.from_config() as rig:
    s = rig.latest_synced()                     # SyncedFrames | None: newest pair, <= ~1 frame old
    s.frames["wrist"].image                     # full-res BGR uint8 (read-only), .t_ns monotonic
    s.policy_images()                           # {"observation/image": 224x224 RGB, "observation/wrist_image": ...}
    off = rig.add_synced_callback(fn)           # fn(SyncedFrames) once per reference frame (recorder hook)
    off = rig.add_frame_callback(fn)            # fn(Frame) for every frame of every camera
    q = rig.subscribe_synced(maxsize=300)       # same stream as a queue (drop-oldest when full)
    q = rig.subscribe_frames("top")             # LatestQueue of Frames for one camera (WebRTC track)
    rig.stats(); rig.healthy()                  # per-camera fps / drops / errors, pairing skew
    rig.stop()

Callbacks run on the capture / sync threads: keep them short (hand off to your own queue) or
you will cause dropped frames.

Timestamps: ``Frame.t_ns`` is ``time.monotonic_ns()`` (CLOCK_MONOTONIC on Linux). On V4L2 we use
the driver's buffer timestamp when it is plausible (it is CLOCK_MONOTONIC too and is taken when the
frame finished arriving over USB, so it has no Python scheduling jitter); otherwise the host clock
right after ``grab()``. Other services stamp with the same clock, so robot state can be matched to
frames by ``t_ns``. ``t_wall`` is ``time.time()`` for humans and logs only.

Sync rule (``SyncedFrames``): every reference-camera frame (``reference:`` in the config, default
``top``) defines one synced sample at ``t_ns = ref.t_ns``. For each other camera we wait until it
has delivered a frame at or after that time (or two frame periods pass), then take its frame with the
smallest ``|t - t_ref|``. ``skew_ns`` is the largest such gap; the sample is ``in_sync`` when every
camera has a frame and ``skew_ns <= pair_tolerance_ms`` (default 20 ms, i.e. a bit over half a
frame at 30 fps). Out-of-sync samples are still published, flagged, and counted in ``stats()``; the
recorder decides whether to drop them. A partner frame may be reused by two samples if that camera
dropped a frame.

Dropped frames: a gap between consecutive frames of one camera longer than 1.5 periods counts as
``round(gap / period) - 1`` drops.

CLI (TDM-49 can wire ``rdf-robot-client cameras-check`` to ``cameras.main(["check", ...])``)::

    python -m rdf_robot_client.cameras check [--synthetic | --webcam 0] [--seconds 10]
    python -m rdf_robot_client.cameras list
    python -m rdf_robot_client.cameras serve [--synthetic] [--port 8090]   # MJPEG, see camera_stream
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import os
import queue
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import numpy as np

try:  # cv2 is a hard dependency at runtime, but keep import errors readable
    import cv2
except ImportError as e:  # pragma: no cover
    raise ImportError("rdf_robot_client.cameras needs OpenCV: `uv sync` in services/robot-client "
                      "(opencv-python-headless), or the JetPack system OpenCV") from e

from . import camera_v4l2

log = logging.getLogger("rdf_robot_client.cameras")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "cameras.yaml"
POLICY_SIZE = 224  # S-05a: openpi pi0.5 trains and infers at 224x224 (resize_with_pad)

# S-05a feature schema: LeRobot key in the dataset, and the post-repack key the policy server takes.
DEFAULT_KEYS = {
    "top": ("observation.images.top", "observation/image"),
    "wrist": ("observation.images.wrist", "observation/wrist_image"),
}


# --------------------------------------------------------------------------------------------- config

@dataclass
class CameraConfig:
    name: str                               # "top" | "wrist" (matches the LeRobot image keys)
    source: str = "v4l2"                    # v4l2 | gstreamer | webcam | file | synthetic | mujoco ([SIM] render)
    device: str | int | None = None         # v4l2: /dev/v4l/by-id/... (globs ok) or index; webcam: index; file: path
    pipeline: str | None = None             # gstreamer: full pipeline ending in appsink
    width: int = 640
    height: int = 480
    fps: float = 30.0
    fourcc: str | None = "MJPG"             # MJPG is required for 2x 640x480@30 on one USB 2 bus
    rotate: int = 0                         # 0 | 90 | 180 | 270, clockwise, applied after capture
    flip: str = "none"                      # none | horizontal | vertical
    timestamps: str = "auto"                # auto (driver if plausible) | driver | host
    v4l2_controls: dict[str, Any] = field(default_factory=dict)
    lerobot_key: str | None = None          # default from DEFAULT_KEYS
    policy_key: str | None = None
    # synthetic-only knobs (also handy in tests)
    synthetic_phase_ms: float = 0.0
    synthetic_jitter_ms: float = 0.0
    synthetic_drop_prob: float = 0.0

    def __post_init__(self) -> None:
        keys = DEFAULT_KEYS.get(self.name, (f"observation.images.{self.name}", f"observation/{self.name}_image"))
        self.lerobot_key = self.lerobot_key or keys[0]
        self.policy_key = self.policy_key or keys[1]
        if self.rotate not in (0, 90, 180, 270):
            raise ValueError(f"camera {self.name}: rotate must be 0/90/180/270, got {self.rotate}")
        if self.flip not in ("none", "horizontal", "vertical"):
            raise ValueError(f"camera {self.name}: flip must be none/horizontal/vertical, got {self.flip}")
        if self.source not in ("v4l2", "gstreamer", "webcam", "file", "synthetic", "mujoco"):
            raise ValueError(f"camera {self.name}: unknown source {self.source!r}")

    @property
    def period_ns(self) -> int:
        return int(1e9 / self.fps)


@dataclass
class RigConfig:
    cameras: list[CameraConfig]
    reference: str = "top"
    pair_tolerance_ms: float = 20.0
    buffer_seconds: float = 1.0
    policy_size: int = POLICY_SIZE
    stream: dict[str, Any] = field(default_factory=lambda: {"width": 424, "jpeg_quality": 70, "fps": 15})

    def __post_init__(self) -> None:
        names = [c.name for c in self.cameras]
        if not names:
            raise ValueError("camera config has no cameras")
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate camera names: {names}")
        if self.reference not in names:
            raise ValueError(f"reference camera {self.reference!r} not in {names}")

    def camera(self, name: str) -> CameraConfig:
        for c in self.cameras:
            if c.name == name:
                return c
        raise KeyError(name)


def load_config(path: str | os.PathLike | None = None) -> RigConfig:
    """Read the camera YAML. ``path`` defaults to $RDF_CAMERAS_CONFIG, then config/cameras.yaml."""
    import yaml

    p = Path(path or os.environ.get("RDF_CAMERAS_CONFIG") or DEFAULT_CONFIG_PATH)
    raw = yaml.safe_load(p.read_text()) or {}
    return config_from_dict(raw)


def config_from_dict(raw: dict[str, Any]) -> RigConfig:
    cams_raw = raw.get("cameras") or {}
    if isinstance(cams_raw, dict):
        cams = [CameraConfig(name=n, **(v or {})) for n, v in cams_raw.items()]
    else:
        cams = [CameraConfig(**c) for c in cams_raw]
    rest = {k: v for k, v in raw.items() if k != "cameras"}
    return RigConfig(cameras=cams, **rest)


def synthetic_config(base: RigConfig | None = None, *, webcam: int | None = None) -> RigConfig:
    """Swap every camera to a synthetic source with the same size/fps/orientation (laptop testing).

    With ``webcam=N``, the reference camera uses the laptop webcam N instead (if it opens).
    The synthetic cameras get small phase offsets so the pairing logic has real work to do.
    """
    base = base or load_config()
    cams = []
    for i, c in enumerate(base.cameras):
        if webcam is not None and c.name == base.reference:
            cams.append(replace(c, source="webcam", device=webcam, fourcc=None, v4l2_controls={}))
        else:
            cams.append(replace(c, source="synthetic", device=None, pipeline=None, v4l2_controls={},
                                synthetic_phase_ms=c.synthetic_phase_ms or 7.0 * i,
                                synthetic_jitter_ms=c.synthetic_jitter_ms or 1.0))
    return replace(base, cameras=cams)


# --------------------------------------------------------------------------------------------- frames

def resize_with_pad(img: np.ndarray, size: int = POLICY_SIZE) -> np.ndarray:
    """Letterbox to ``size`` x ``size`` keeping aspect ratio, black padding (openpi resize_with_pad)."""
    h, w = img.shape[:2]
    scale = size / max(h, w)
    nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
    small = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((size, size, 3), dtype=np.uint8)
    y0, x0 = (size - nh) // 2, (size - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = small
    return out


@dataclass(eq=False)
class Frame:
    camera: str
    seq: int                 # per-camera counter since start (gaps do NOT appear here; see drops)
    t_ns: int                # monotonic capture time, ns
    t_wall: float            # time.time() at capture, for humans
    image: np.ndarray        # BGR uint8 HxWx3, read-only, already rotated/flipped
    ts_source: str = "host"  # driver | host | synthetic
    _cache: dict = field(default_factory=dict, repr=False)

    @property
    def t(self) -> float:
        return self.t_ns / 1e9

    def rgb(self) -> np.ndarray:
        if "rgb" not in self._cache:
            self._cache["rgb"] = cv2.cvtColor(self.image, cv2.COLOR_BGR2RGB)
        return self._cache["rgb"]

    def policy_rgb(self, size: int = POLICY_SIZE) -> np.ndarray:
        """224x224 RGB uint8 letterboxed copy for the policy (S-05a). Cached per frame."""
        key = ("policy", size)
        if key not in self._cache:
            self._cache[key] = resize_with_pad(self.rgb(), size)
        return self._cache[key]

    def jpeg(self, width: int | None = None, quality: int = 80) -> bytes:
        """JPEG bytes, optionally downscaled to ``width`` (keeps aspect). Cached per (width, quality)."""
        key = ("jpeg", width, quality)
        if key not in self._cache:
            img = self.image
            if width and width < img.shape[1]:
                h = round(img.shape[0] * width / img.shape[1])
                img = cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            if not ok:
                raise RuntimeError("JPEG encode failed")
            self._cache[key] = buf.tobytes()
        return self._cache[key]


@dataclass(eq=False)
class SyncedFrames:
    seq: int                       # synced-sample counter (one per reference frame)
    t_ns: int                      # the reference frame's timestamp
    frames: dict[str, Frame]       # camera name -> frame (missing if that camera has none yet)
    skew_ns: int                   # max |t_cam - t_ref| over cameras
    in_sync: bool
    reference: str
    policy_keys: dict[str, str] = field(default_factory=dict, repr=False)  # camera -> policy obs key

    @property
    def skew_ms(self) -> float:
        return self.skew_ns / 1e6

    def policy_images(self, size: int = POLICY_SIZE) -> dict[str, np.ndarray]:
        """Policy-server observation images, keyed per S-05a (``observation/image`` etc.)."""
        return {self.policy_keys.get(n, n): f.policy_rgb(size) for n, f in self.frames.items()}


def nearest_frame(frames: list[Frame], t_ns: int) -> Frame | None:
    """The frame (sorted by t_ns) closest in time to ``t_ns``; ties go to the earlier frame."""
    if not frames:
        return None
    ts = [f.t_ns for f in frames]
    i = bisect.bisect_left(ts, t_ns)
    if i == 0:
        return frames[0]
    if i == len(frames):
        return frames[-1]
    before, after = frames[i - 1], frames[i]
    return before if (t_ns - before.t_ns) <= (after.t_ns - t_ns) else after


class LatestQueue:
    """A bounded queue that drops the OLDEST item when full (consumers never block producers)."""

    def __init__(self, maxsize: int = 1):
        self._q: queue.Queue = queue.Queue(maxsize=max(1, maxsize))
        self.dropped = 0

    def put(self, item: Any) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def get(self, timeout: float | None = None) -> Any:
        """Blocks up to ``timeout`` seconds; raises queue.Empty on timeout."""
        return self._q.get(timeout=timeout)

    def get_nowait(self) -> Any:
        return self._q.get_nowait()

    def qsize(self) -> int:
        return self._q.qsize()


# -------------------------------------------------------------------------------------------- sources

class FrameSource(Protocol):
    def open(self) -> dict[str, Any]: ...            # returns negotiated settings for stats
    def read(self) -> tuple[np.ndarray | None, int | None]: ...   # (BGR image | None on failure, driver ts ns | None)
    def close(self) -> None: ...


class OpenCVSource:
    """V4L2 device, laptop webcam, GStreamer pipeline or video file through cv2.VideoCapture."""

    def __init__(self, cfg: CameraConfig):
        self.cfg = cfg
        self.cap: cv2.VideoCapture | None = None
        self.device: str | int | None = None
        self.backend = ""

    def open(self) -> dict[str, Any]:
        c = self.cfg
        if c.source == "gstreamer":
            if not c.pipeline:
                raise ValueError(f"camera {c.name}: source gstreamer needs `pipeline`")
            self.cap = cv2.VideoCapture(c.pipeline, cv2.CAP_GSTREAMER)
            self.device = c.pipeline
        elif c.source == "file":
            self.device = str(c.device)
            self.cap = cv2.VideoCapture(self.device)
        else:
            self.device = camera_v4l2.resolve_device(c.device if c.device is not None else 0)
            if camera_v4l2.IS_LINUX:
                self.cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            elif sys.platform == "darwin" and isinstance(self.device, int):
                self.cap = cv2.VideoCapture(self.device, cv2.CAP_AVFOUNDATION)
            else:
                self.cap = cv2.VideoCapture(self.device)
        if not self.cap or not self.cap.isOpened():
            raise RuntimeError(f"camera {c.name}: could not open {c.source} {self.device!r}")
        self.backend = self.cap.getBackendName()
        if c.source in ("v4l2", "webcam"):
            # Order matters for the V4L2 backend: pixel format, then size, then rate.
            if c.fourcc:
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*c.fourcc))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, c.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c.height)
            self.cap.set(cv2.CAP_PROP_FPS, c.fps)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)  # we read continuously; small queue = low latency
        problems: list[str] = []
        if c.source == "v4l2" and c.v4l2_controls and isinstance(self.device, str):
            problems = camera_v4l2.apply_controls(self.device, c.v4l2_controls)
            for p in problems:
                log.warning("camera %s: %s", c.name, p)
        got_fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        info = {
            "device": str(self.device), "backend": self.backend,
            "width": int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps_negotiated": round(float(self.cap.get(cv2.CAP_PROP_FPS)), 2),
            "fourcc": "".join(chr((got_fourcc >> 8 * i) & 0xFF) for i in range(4)) if got_fourcc else "",
            "control_problems": problems,
        }
        if (info["width"], info["height"]) != (c.width, c.height) and c.source != "file":
            log.warning("camera %s: asked %dx%d, got %dx%d", c.name, c.width, c.height, info["width"], info["height"])
        return info

    def read(self) -> tuple[np.ndarray | None, int | None]:
        assert self.cap is not None
        if not self.cap.grab():
            return None, None
        driver_ns = None
        if self.backend == "V4L2":
            ms = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            if ms and ms > 0:
                driver_ns = int(ms * 1e6)
        ok, img = self.cap.retrieve()
        if not ok or img is None:
            return None, None
        if self.cfg.source == "file":  # pace files like a live camera
            time.sleep(1.0 / self.cfg.fps)
        return img, driver_ns

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class SyntheticSource:
    """Fake camera: frames on a fixed 1/fps schedule with optional phase, jitter and drops.

    The picture shows the camera name, frame number, a moving bar and an "UP" arrow plus a red
    top-left corner, so ``cameras-check`` output can be checked for orientation handling too.
    The schedule timestamp is reported as the driver timestamp, like a V4L2 buffer stamp.
    """

    def __init__(self, cfg: CameraConfig, seed: int | None = None):
        self.cfg = cfg
        self.rng = np.random.default_rng(seed if seed is not None else abs(hash(cfg.name)) % 2**32)
        self.k = 0
        self.t0 = 0
        self._bg: np.ndarray | None = None

    def open(self) -> dict[str, Any]:
        c = self.cfg
        self.t0 = time.monotonic_ns() + int(c.synthetic_phase_ms * 1e6)
        self.k = 0
        h, w = c.height, c.width
        yy, xx = np.mgrid[0:h, 0:w]
        bg = np.zeros((h, w, 3), np.uint8)
        hue = (sum(map(ord, c.name)) * 37) % 180
        bg[..., 0] = hue
        bg[..., 1] = 90
        bg[..., 2] = (60 + 120 * yy / max(1, h - 1)).astype(np.uint8)
        bg = cv2.cvtColor(bg, cv2.COLOR_HSV2BGR)
        cv2.rectangle(bg, (0, 0), (w // 8, h // 8), (0, 0, 255), -1)  # top-left marker
        cx = w // 2
        cv2.arrowedLine(bg, (cx, h // 3), (cx, h // 10), (255, 255, 255), 6, tipLength=0.4)
        cv2.putText(bg, "UP", (cx - 28, h // 3 + 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
        cv2.putText(bg, f"SYNTHETIC {c.name}", (10, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        self._bg = bg
        return {"device": "synthetic", "backend": "synthetic", "width": w, "height": h,
                "fps_negotiated": c.fps, "fourcc": "", "control_problems": []}

    def read(self) -> tuple[np.ndarray | None, int | None]:
        c = self.cfg
        period = c.period_ns
        while True:
            t_sched = self.t0 + self.k * period
            self.k += 1
            if c.synthetic_drop_prob and self.rng.random() < c.synthetic_drop_prob:
                continue  # this frame is "lost on the bus": leaves a gap in timestamps
            break
        jitter = int(self.rng.normal(0, c.synthetic_jitter_ms * 1e6)) if c.synthetic_jitter_ms else 0
        t_frame = t_sched + jitter
        delay = (t_frame - time.monotonic_ns()) / 1e9
        if delay > 0:
            time.sleep(delay)
        img = self._bg.copy()
        h, w = img.shape[:2]
        x = int((self.k * 6) % w)
        cv2.rectangle(img, (x, h // 2), (min(w - 1, x + 20), h // 2 + 60), (0, 255, 255), -1)
        cv2.putText(img, f"#{self.k - 1}", (w - 160, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        return img, t_frame

    def close(self) -> None:
        self._bg = None


def make_source(cfg: CameraConfig) -> FrameSource:
    if cfg.source == "mujoco":  # [SIM] offscreen render of the MuJoCo arm (adapters/mujoco_sim.py)
        from .adapters.mujoco_sim import MujocoCameraSource

        return MujocoCameraSource(cfg)
    return SyntheticSource(cfg) if cfg.source == "synthetic" else OpenCVSource(cfg)


def _orient(img: np.ndarray, cfg: CameraConfig) -> np.ndarray:
    if cfg.rotate == 90:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif cfg.rotate == 180:
        img = cv2.rotate(img, cv2.ROTATE_180)
    elif cfg.rotate == 270:
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if cfg.flip == "horizontal":
        img = cv2.flip(img, 1)
    elif cfg.flip == "vertical":
        img = cv2.flip(img, 0)
    return img


# ------------------------------------------------------------------------------------- capture thread

class _CameraWorker:
    def __init__(self, cfg: CameraConfig, buffer_len: int, on_frame: Callable[[Frame], None],
                 source_factory: Callable[[CameraConfig], FrameSource] = make_source):
        self.cfg = cfg
        self._factory = source_factory
        self._on_frame = on_frame
        self.buffer: deque[Frame] = deque(maxlen=buffer_len)
        self.cond = threading.Condition()
        self.latest: Frame | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.info: dict[str, Any] = {}
        self.frames = 0
        self.dropped = 0
        self.errors = 0
        self.reopens = 0
        self.last_error = ""
        self._recent: deque[int] = deque(maxlen=int(cfg.fps * 2) + 1)
        self.ts_source = "host"

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.cfg.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _choose_ts(self, host_ns: int, driver_ns: int | None) -> tuple[int, str]:
        mode = self.cfg.timestamps
        if self.cfg.source in ("synthetic", "mujoco") and driver_ns is not None:
            return driver_ns, self.cfg.source
        if driver_ns is not None and mode in ("auto", "driver"):
            lag = host_ns - driver_ns
            if -1_000_000 <= lag <= 500_000_000:  # driver stamp is CLOCK_MONOTONIC and recent
                return driver_ns, "driver"
            if mode == "driver" and self.frames == 0:
                log.warning("camera %s: driver timestamp implausible (lag %.1f ms); using host clock",
                            self.cfg.name, lag / 1e6)
        return host_ns, "host"

    def _run(self) -> None:
        period = self.cfg.period_ns
        backoff = 0.5
        while not self._stop.is_set():
            src = self._factory(self.cfg)
            try:
                self.info = src.open()
                log.info("camera %s open: %s", self.cfg.name, self.info)
                backoff = 0.5
                fails = 0
                last_t: int | None = None
                while not self._stop.is_set():
                    img, driver_ns = src.read()
                    host_ns = time.monotonic_ns()
                    if img is None:
                        fails += 1
                        self.errors += 1
                        if fails >= 15:
                            raise RuntimeError("15 consecutive failed reads")
                        continue
                    fails = 0
                    t_ns, self.ts_source = self._choose_ts(host_ns, driver_ns)
                    if last_t is not None:
                        gap = t_ns - last_t
                        if gap > 1.5 * period:
                            self.dropped += max(0, round(gap / period) - 1)
                    last_t = t_ns
                    img = _orient(img, self.cfg)
                    img.flags.writeable = False
                    frame = Frame(self.cfg.name, self.frames, t_ns, time.time(), img, self.ts_source)
                    with self.cond:
                        self.buffer.append(frame)
                        self.latest = frame
                        self.frames += 1
                        self._recent.append(t_ns)
                        self.cond.notify_all()
                    self._on_frame(frame)
            except Exception as e:  # camera unplugged, device busy, ...: keep retrying
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("camera %s: %s (reopening in %.1fs)", self.cfg.name, self.last_error, backoff)
            finally:
                try:
                    src.close()
                except Exception:
                    pass
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 5.0)
            self.reopens += 1

    def snapshot(self) -> list[Frame]:
        with self.cond:
            return list(self.buffer)

    def wait_newer(self, seq: int, timeout: float) -> Frame | None:
        """Block until a frame with seq > ``seq`` exists; returns the latest frame (or None)."""
        with self.cond:
            self.cond.wait_for(lambda: self.latest is not None and self.latest.seq > seq, timeout)
            return self.latest

    def wait_until_t(self, t_ns: int, timeout: float) -> None:
        with self.cond:
            self.cond.wait_for(lambda: self.latest is not None and self.latest.t_ns >= t_ns, timeout)

    def stats(self) -> dict[str, Any]:
        now = time.monotonic_ns()
        with self.cond:
            recent = list(self._recent)
            latest = self.latest
        fps = (len(recent) - 1) / ((recent[-1] - recent[0]) / 1e9) if len(recent) >= 2 and recent[-1] > recent[0] else 0.0
        total = self.frames + self.dropped
        return {
            "source": self.cfg.source, **self.info,
            "frames": self.frames, "dropped": self.dropped,
            "drop_pct": round(100.0 * self.dropped / total, 3) if total else 0.0,
            "fps_measured": round(fps, 2), "fps_target": self.cfg.fps,
            "errors": self.errors, "reopens": self.reopens, "last_error": self.last_error,
            "ts_source": self.ts_source,
            "last_frame_age_ms": round((now - latest.t_ns) / 1e6, 1) if latest else None,
            "rotate": self.cfg.rotate, "flip": self.cfg.flip,
        }


# ------------------------------------------------------------------------------------------------ rig

class CameraRig:
    """All cameras of the robot, captured in parallel and paired in time. See module docstring."""

    def __init__(self, config: RigConfig,
                 source_factory: Callable[[CameraConfig], FrameSource] = make_source):
        self.config = config
        self._tol_ns = int(config.pair_tolerance_ms * 1e6)
        self._frame_cbs: list[Callable[[Frame], None]] = []
        self._synced_cbs: list[Callable[[SyncedFrames], None]] = []
        self._cb_lock = threading.Lock()
        self.workers: dict[str, _CameraWorker] = {}
        for c in config.cameras:
            n = max(4, int(math.ceil(config.buffer_seconds * c.fps)))
            self.workers[c.name] = _CameraWorker(c, n, self._dispatch_frame, source_factory)
        self._sync_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._synced_cond = threading.Condition()
        self._latest_synced: SyncedFrames | None = None
        self._synced_count = 0
        self._unsynced_count = 0
        self._skews: deque[int] = deque(maxlen=600)
        self._policy_keys = {c.name: c.policy_key or c.name for c in config.cameras}
        self._started_ns = 0
        self.running = False

    # -- construction
    @classmethod
    def from_config(cls, path: str | os.PathLike | None = None, *, synthetic: bool = False,
                    webcam: int | None = None) -> "CameraRig":
        cfg = load_config(path)
        if synthetic or webcam is not None:
            cfg = synthetic_config(cfg, webcam=webcam)
        return cls(cfg)

    # -- lifecycle
    def start(self) -> "CameraRig":
        if self.running:
            return self
        self._stop.clear()
        self._started_ns = time.monotonic_ns()
        for w in self.workers.values():
            w.start()
        self._sync_thread = threading.Thread(target=self._sync_loop, name="cam-sync", daemon=True)
        self._sync_thread.start()
        self.running = True
        return self

    def stop(self) -> None:
        self._stop.set()
        for w in self.workers.values():
            w.stop()
        if self._sync_thread:
            self._sync_thread.join(3.0)
        self.running = False

    def __enter__(self) -> "CameraRig":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def names(self) -> list[str]:
        return list(self.workers)

    @property
    def reference(self) -> str:
        return self.config.reference

    # -- reading
    def latest(self, camera: str) -> Frame | None:
        return self.workers[camera].latest

    def latest_synced(self) -> SyncedFrames | None:
        return self._latest_synced

    def wait_synced(self, timeout: float = 1.0, after_seq: int | None = None) -> SyncedFrames | None:
        """Block for the next synced sample (seq > ``after_seq``, default: newer than the current one)."""
        with self._synced_cond:
            cur = self._latest_synced
            target = after_seq if after_seq is not None else (cur.seq if cur else -1)
            self._synced_cond.wait_for(
                lambda: self._latest_synced is not None and self._latest_synced.seq > target, timeout)
            s = self._latest_synced
            return s if s is not None and s.seq > target else None

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """True once every camera has delivered a frame and one synced sample exists."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._latest_synced is not None and all(w.latest for w in self.workers.values()):
                return True
            time.sleep(0.02)
        return False

    def frames_between(self, camera: str, t0_ns: int, t1_ns: int) -> list[Frame]:
        """Frames of one camera still in the ring buffer with t0 <= t < t1 (recorder backfill)."""
        return [f for f in self.workers[camera].snapshot() if t0_ns <= f.t_ns < t1_ns]

    # -- hooks
    def add_frame_callback(self, fn: Callable[[Frame], None]) -> Callable[[], None]:
        """``fn(frame)`` on the capture thread for every frame of every camera. Returns an unsubscribe."""
        with self._cb_lock:
            self._frame_cbs = [*self._frame_cbs, fn]
        return lambda: self._remove(fn, "_frame_cbs")

    def add_synced_callback(self, fn: Callable[[SyncedFrames], None]) -> Callable[[], None]:
        """``fn(synced)`` on the sync thread once per reference frame. Returns an unsubscribe."""
        with self._cb_lock:
            self._synced_cbs = [*self._synced_cbs, fn]
        return lambda: self._remove(fn, "_synced_cbs")

    def subscribe_synced(self, maxsize: int = 300) -> LatestQueue:
        """A queue receiving every SyncedFrames (drop-oldest when full). ``q.close()`` unsubscribes."""
        q = LatestQueue(maxsize)
        q.close = self.add_synced_callback(q.put)  # type: ignore[attr-defined]
        return q

    def subscribe_frames(self, camera: str, maxsize: int = 1) -> LatestQueue:
        """A latest-wins queue of one camera's Frames (e.g. for a WebRTC VideoStreamTrack.recv)."""
        q = LatestQueue(maxsize)
        q.close = self.add_frame_callback(lambda f: q.put(f) if f.camera == camera else None)  # type: ignore[attr-defined]
        return q

    def _remove(self, fn: Callable, attr: str) -> None:
        with self._cb_lock:
            setattr(self, attr, [f for f in getattr(self, attr) if f is not fn])

    def _dispatch_frame(self, frame: Frame) -> None:
        for fn in self._frame_cbs:
            try:
                fn(frame)
            except Exception:
                log.exception("frame callback %r failed", fn)

    # -- pairing
    def pair(self, ref: Frame) -> SyncedFrames:
        """Build the synced sample for one reference frame from what is in the buffers now."""
        frames = {ref.camera: ref}
        skew = 0
        for name, w in self.workers.items():
            if name == ref.camera:
                continue
            f = nearest_frame(w.snapshot(), ref.t_ns)
            if f is not None:
                frames[name] = f
                skew = max(skew, abs(f.t_ns - ref.t_ns))
        in_sync = len(frames) == len(self.workers) and skew <= self._tol_ns
        return SyncedFrames(-1, ref.t_ns, frames, skew, in_sync, ref.camera, self._policy_keys)

    def _sync_loop(self) -> None:
        ref_w = self.workers[self.config.reference]
        others = [w for n, w in self.workers.items() if n != self.config.reference]
        last_seq = -1
        while not self._stop.is_set():
            latest = ref_w.wait_newer(last_seq, timeout=0.5)
            if latest is None or latest.seq <= last_seq:
                continue
            pending = [f for f in ref_w.snapshot() if f.seq > last_seq]
            for ref in pending:
                for w in others:  # wait (bounded) until the partner's bracketing frame has arrived
                    w.wait_until_t(ref.t_ns, timeout=2 * w.cfg.period_ns / 1e9)
                s = self.pair(ref)
                last_seq = ref.seq
                self._publish(s)

    def _publish(self, s: SyncedFrames) -> None:
        with self._synced_cond:
            s.seq = self._synced_count
            self._synced_count += 1
            if not s.in_sync:
                self._unsynced_count += 1
            if len(s.frames) == len(self.workers):
                self._skews.append(s.skew_ns)
            self._latest_synced = s
            self._synced_cond.notify_all()
        for fn in self._synced_cbs:
            try:
                fn(s)
            except Exception:
                log.exception("synced callback %r failed", fn)

    # -- health
    def stats(self) -> dict[str, Any]:
        skews = sorted(self._skews)

        def pct(p: float) -> float | None:
            return round(skews[min(len(skews) - 1, int(p * len(skews)))] / 1e6, 2) if skews else None

        return {
            "cameras": {n: w.stats() for n, w in self.workers.items()},
            "sync": {"reference": self.config.reference, "tolerance_ms": self.config.pair_tolerance_ms,
                     "samples": self._synced_count, "out_of_sync": self._unsynced_count,
                     "skew_ms_p50": pct(0.5), "skew_ms_p95": pct(0.95),
                     "skew_ms_max": round(skews[-1] / 1e6, 2) if skews else None},
            "running": self.running,
            "uptime_s": round((time.monotonic_ns() - self._started_ns) / 1e9, 1) if self.running else 0.0,
            "healthy": self.healthy(),
        }

    def healthy(self, max_age_ms: float = 500.0) -> bool:
        now = time.monotonic_ns()
        return self.running and all(
            w.latest is not None and (now - w.latest.t_ns) / 1e6 < max_age_ms for w in self.workers.values())


# ------------------------------------------------------------------------------------- check / report

def stitch(synced: SyncedFrames, rig_stats: dict[str, Any] | None = None, height: int = 480) -> np.ndarray:
    """Side-by-side image of one synced sample with a label strip per camera (images are untouched)."""
    tiles = []
    for name in sorted(synced.frames, key=lambda n: (n != synced.reference, n)):
        f = synced.frames[name]
        img = f.image
        w = round(img.shape[1] * height / img.shape[0])
        img = cv2.resize(img, (w, height), interpolation=cv2.INTER_AREA)
        strip = np.full((56, w, 3), 32, np.uint8)
        dt = (f.t_ns - synced.t_ns) / 1e6
        cam = (rig_stats or {}).get("cameras", {}).get(name, {})
        cv2.putText(strip, f"{name}  {f.image.shape[1]}x{f.image.shape[0]}  seq {f.seq}  dt {dt:+.1f} ms",
                    (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        cv2.putText(strip, f"{cam.get('device', '')}  rot {cam.get('rotate', 0)} flip {cam.get('flip', 'none')}"
                    f"  ts {f.ts_source}", (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        tiles.append(np.vstack([strip, img]))
    gap = np.full((tiles[0].shape[0], 6, 3), 0, np.uint8)
    row = tiles[0]
    for t in tiles[1:]:
        row = np.hstack([row, gap, t])
    footer = np.full((30, row.shape[1], 3), 32, np.uint8)
    cv2.putText(footer, f"synced sample {synced.seq}  skew {synced.skew_ms:.1f} ms  in_sync {synced.in_sync}"
                f"  (top of each tile = top of the camera's image as recorded)",
                (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return np.vstack([row, footer])


def run_capture_check(rig: CameraRig, seconds: float = 10.0, warmup: float = 1.0,
                      max_drop_pct: float = 1.0, max_skew_ms: float = 20.0) -> dict[str, Any]:
    """Run the rig for ``seconds`` and measure per-camera drops and inter-camera skew.

    Pass criteria (TDM-47 done-when): every camera < ``max_drop_pct`` % dropped frames and the
    p95 pairing skew < ``max_skew_ms``. Starts the rig if needed; leaves it running.
    """
    if not rig.running:
        rig.start()
    if not rig.wait_ready(timeout=max(5.0, warmup + 4)):
        st = rig.stats()
        return {"pass": False, "reason": "cameras did not deliver frames", "stats": st}
    time.sleep(warmup)
    base = {n: (w.frames, w.dropped) for n, w in rig.workers.items()}
    skews: list[int] = []
    unsynced = 0
    samples = 0

    def on_synced(s: SyncedFrames) -> None:
        nonlocal unsynced, samples
        samples += 1
        if len(s.frames) == len(rig.workers):
            skews.append(s.skew_ns)
        if not s.in_sync:
            unsynced += 1

    off = rig.add_synced_callback(on_synced)
    t0 = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - t0
    off()
    st = rig.stats()
    per_cam = {}
    ok = True
    for n, w in rig.workers.items():
        frames, dropped = w.frames - base[n][0], w.dropped - base[n][1]
        total = frames + dropped
        drop_pct = 100.0 * dropped / total if total else 100.0
        cam_ok = frames > 0 and drop_pct < max_drop_pct
        ok &= cam_ok
        per_cam[n] = {"frames": frames, "dropped": dropped, "drop_pct": round(drop_pct, 3),
                      "fps": round(frames / elapsed, 2), "pass": cam_ok}
    skews.sort()
    p = lambda q: round(skews[min(len(skews) - 1, int(q * len(skews)))] / 1e6, 2) if skews else None  # noqa: E731
    skew_ok = bool(skews) and p(0.95) is not None and p(0.95) < max_skew_ms
    ok &= skew_ok
    return {
        "pass": bool(ok), "seconds": round(elapsed, 2), "per_camera": per_cam,
        "sync": {"samples": samples, "out_of_sync": unsynced, "skew_ms_p50": p(0.5), "skew_ms_p95": p(0.95),
                 "skew_ms_max": round(skews[-1] / 1e6, 2) if skews else None, "pass": skew_ok,
                 "tolerance_ms": rig.config.pair_tolerance_ms},
        "thresholds": {"max_drop_pct": max_drop_pct, "max_skew_ms": max_skew_ms},
        "stats": st,
    }


# ------------------------------------------------------------------------------------------------ CLI

def _rig_from_args(a: argparse.Namespace) -> CameraRig:
    return CameraRig.from_config(a.config, synthetic=a.synthetic, webcam=a.webcam)


def _cmd_check(a: argparse.Namespace) -> int:
    out = Path(a.out or Path(tempfile.gettempdir()) / "rdf-cameras-check")
    out.mkdir(parents=True, exist_ok=True)
    rig = _rig_from_args(a)
    print(f"[cameras-check] cameras {rig.names}, reference {rig.reference}, capturing {a.seconds:.0f}s ...")
    try:
        report = run_capture_check(rig, seconds=a.seconds)
        s = rig.latest_synced()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if s is not None:
            img_path = out / f"cameras-check-{stamp}.jpg"
            cv2.imwrite(str(img_path), stitch(s, rig.stats()))
            report["image"] = str(img_path)
            for name, f in s.frames.items():  # also the raw full-res frames and the policy view
                cv2.imwrite(str(out / f"cameras-check-{stamp}-{name}.png"), f.image)
                cv2.imwrite(str(out / f"cameras-check-{stamp}-{name}-policy224.png"),
                            cv2.cvtColor(f.policy_rgb(rig.config.policy_size), cv2.COLOR_RGB2BGR))
    finally:
        rig.stop()
    rep_path = out / f"cameras-check-{time.strftime('%Y%m%d-%H%M%S')}.json"
    rep_path.write_text(json.dumps(report, indent=2, default=str))
    for n, c in report.get("per_camera", {}).items():
        print(f"  {n:<6} {c['frames']:>4} frames  {c['fps']:>6.2f} fps  dropped {c['dropped']} "
              f"({c['drop_pct']:.2f}%)  {'OK' if c['pass'] else 'FAIL'}")
    sy = report.get("sync", {})
    if sy:
        print(f"  sync   {sy['samples']} samples  skew p50 {sy['skew_ms_p50']} ms  p95 {sy['skew_ms_p95']} ms  "
              f"max {sy['skew_ms_max']} ms  out_of_sync {sy['out_of_sync']}  {'OK' if sy['pass'] else 'FAIL'}")
    if report.get("reason"):
        print(f"  {report['reason']}: {json.dumps(report['stats']['cameras'], default=str)}")
    print(f"  image  {report.get('image', '(none)')}  <- check orientation by eye")
    print(f"  report {rep_path}")
    print(f"[cameras-check] {'PASS' if report['pass'] else 'FAIL'}")
    return 0 if report["pass"] else 1


def _cmd_list(a: argparse.Namespace) -> int:
    devs = camera_v4l2.list_devices()
    if not camera_v4l2.IS_LINUX:
        print("not Linux: no V4L2 discovery. On macOS use `--webcam 0` (index) with check/serve.")
        return 0
    if not devs:
        print("no /dev/video* devices found")
        return 1
    for d in devs:
        tag = "capture" if d.is_capture_node else "metadata"
        print(f"{d.path:<13} [{tag}] {d.name}")
        for link in d.by_id + d.by_path:
            print(f"    {link}")
        if a.formats and d.is_capture_node:
            print("    " + camera_v4l2.describe_device(d.path).replace("\n", "\n    "))
    return 0


def _cmd_serve(a: argparse.Namespace) -> int:
    from .camera_stream import MjpegFeed, serve_mjpeg

    rig = _rig_from_args(a).start()
    feed = MjpegFeed(rig)
    srv = serve_mjpeg(feed, host=a.host, port=a.port)
    print(f"[cameras] MJPEG on http://{a.host}:{a.port}/  streams: "
          + ", ".join(f"/stream/{n}.mjpg" for n in [*rig.names, "both"]) + "  (Ctrl-C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        feed.close()
        rig.stop()
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rdf_robot_client.cameras", description="TDM-47 camera rig tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", help="camera YAML (default $RDF_CAMERAS_CONFIG or config/cameras.yaml)")
        sp.add_argument("--synthetic", action="store_true", help="fake cameras with the configured size/fps")
        sp.add_argument("--webcam", type=int, default=None,
                        help="laptop: use webcam N as the reference camera, synthetic for the others")
        sp.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("check", help="10 s capture test + stitched image (cameras-check)")
    common(c)
    c.add_argument("--seconds", type=float, default=10.0)
    c.add_argument("--out", help="output dir (default $TMPDIR/rdf-cameras-check)")
    c.set_defaults(fn=_cmd_check)
    ls = sub.add_parser("list", help="list V4L2 devices with stable /dev/v4l/by-id paths")
    ls.add_argument("--formats", action="store_true", help="also print formats/controls (v4l2-ctl)")
    ls.add_argument("-v", "--verbose", action="store_true")
    ls.set_defaults(fn=_cmd_list)
    s = sub.add_parser("serve", help="MJPEG server for the browser (TDM-55 feed source)")
    common(s)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8090)
    s.set_defaults(fn=_cmd_serve)

    a = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
