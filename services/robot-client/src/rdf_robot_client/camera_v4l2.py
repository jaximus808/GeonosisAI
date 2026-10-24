"""TDM-47: V4L2 helpers for the Jetson cameras (device discovery, controls).

Linux only. Everything here degrades to a no-op with a message on macOS, so the laptop can run
the same code with synthetic cameras or the built-in webcam.

    python -m rdf_robot_client.cameras list          # what is plugged in, with stable by-id paths

Stable device names: ``/dev/videoN`` numbers change across reboots and replugs, so the camera config
uses ``/dev/v4l/by-id/...`` (per camera serial) or ``/dev/v4l/by-path/...`` (per USB port), and may
use a glob such as ``/dev/v4l/by-id/*Arducam*-video-index0``. ``resolve_device`` turns that into a
concrete path. UVC cameras expose two nodes each: ``video-index0`` is the capture node,
``video-index1`` is metadata and cannot be opened for frames.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_LINUX = sys.platform.startswith("linux")

# Kernel 5.18+ renamed several UVC controls. Config may use either name; we try the other on failure.
CONTROL_ALIASES: dict[str, str] = {
    "auto_exposure": "exposure_auto",
    "exposure_time_absolute": "exposure_absolute",
    "exposure_dynamic_framerate": "exposure_auto_priority",
    "white_balance_automatic": "white_balance_temperature_auto",
    "focus_automatic_continuous": "focus_auto",
}
CONTROL_ALIASES.update({v: k for k, v in list(CONTROL_ALIASES.items())})


@dataclass
class VideoDevice:
    path: str                       # /dev/videoN
    name: str                       # driver-reported card name
    index: int | None               # 0 = capture node for UVC, 1 = metadata node
    by_id: list[str] = field(default_factory=list)
    by_path: list[str] = field(default_factory=list)

    @property
    def is_capture_node(self) -> bool:
        return self.index in (0, None)

    def as_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "index": self.index,
                "capture_node": self.is_capture_node, "by_id": self.by_id, "by_path": self.by_path}


def _links(directory: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for link in sorted(glob.glob(os.path.join(directory, "*"))):
        try:
            out.setdefault(os.path.realpath(link), []).append(link)
        except OSError:
            continue
    return out


def list_devices() -> list[VideoDevice]:
    """All /dev/video* nodes with their names and stable symlinks. Empty list off Linux."""
    if not IS_LINUX:
        return []
    by_id, by_path = _links("/dev/v4l/by-id"), _links("/dev/v4l/by-path")
    devices: list[VideoDevice] = []
    for sys_dir in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1] or 0)):
        node = "/dev/" + os.path.basename(sys_dir)
        name = _read(Path(sys_dir) / "name") or "?"
        idx_txt = _read(Path(sys_dir) / "index")
        devices.append(VideoDevice(path=node, name=name, index=int(idx_txt) if idx_txt and idx_txt.isdigit() else None,
                                   by_id=by_id.get(node, []), by_path=by_path.get(node, [])))
    return devices


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def resolve_device(spec: str | int) -> str | int:
    """Turn a config ``device`` into something cv2.VideoCapture can open.

    ints (and digit strings) pass through as indexes; globs resolve to the first sorted match;
    symlinks resolve to /dev/videoN (OpenCV's V4L2 backend wants the real node for some builds).
    """
    if isinstance(spec, int):
        return spec
    s = str(spec).strip()
    if s.isdigit():
        return int(s)
    if any(ch in s for ch in "*?["):
        matches = sorted(glob.glob(s))
        if not matches:
            raise FileNotFoundError(f"no video device matches {s!r} (run: python -m rdf_robot_client.cameras list)")
        s = matches[0]
    if not os.path.exists(s):
        raise FileNotFoundError(f"video device {s!r} does not exist (run: python -m rdf_robot_client.cameras list)")
    return os.path.realpath(s)


def have_v4l2_ctl() -> bool:
    return IS_LINUX and shutil.which("v4l2-ctl") is not None


def apply_controls(device: str, controls: dict[str, int | str]) -> list[str]:
    """Set UVC controls one by one with ``v4l2-ctl``; returns human-readable problems (empty = all set).

    Each control is tried under its config name, then under its old/new kernel alias.
    """
    if not controls:
        return []
    if not IS_LINUX:
        return [f"v4l2 controls skipped (not Linux): {sorted(controls)}"]
    if not have_v4l2_ctl():
        return ["v4l2-ctl not installed (sudo apt install v4l-utils); controls not applied"]
    problems: list[str] = []
    for name, value in controls.items():
        errs = []
        for candidate in (name, CONTROL_ALIASES.get(name)):
            if not candidate:
                continue
            r = subprocess.run(["v4l2-ctl", "-d", str(device), f"--set-ctrl={candidate}={value}"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and not r.stderr.strip():
                errs = []
                break
            errs.append(f"{candidate}: {(r.stderr or r.stdout).strip()}")
        if errs:
            problems.append(f"{device} {name}={value} failed ({'; '.join(errs)})")
    return problems


def describe_device(device: str) -> str:
    """``v4l2-ctl --all --list-formats-ext`` text for the report (empty if unavailable)."""
    if not have_v4l2_ctl():
        return ""
    r = subprocess.run(["v4l2-ctl", "-d", str(device), "--list-formats-ext", "--list-ctrls"],
                       capture_output=True, text=True, timeout=10)
    return r.stdout
