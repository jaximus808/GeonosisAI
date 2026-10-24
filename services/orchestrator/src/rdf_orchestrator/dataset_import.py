"""TDM-92: import a purchased dataset in a FOREIGN format into our episode format, before QA.

Bought data arrives as somebody else's dataset (today: a LeRobot dataset, local or on the HF Hub).
QA (``dataset_qa``) and training (TDM-39) read only our format (``rdf_common.episode_format``), so
every delivered episode is normalised here first:

    LeRobot (v2.0 / v2.1 per-episode files, or v3.0 concatenated files)
        meta/info.json                     fps, robot_type, features (names / shapes / video keys)
        meta/episodes.jsonl | meta/episodes/*.parquet     per-episode length, task, v3 file offsets
        data/...parquet                    timestamp, frame_index, observation.state, action[, next.success]
        videos/...mp4                      one per camera (v3: a [from, to] segment of a shared file)
  ->
    <out>/<prefix>ep<index>/actions.parquet   (``write_actions``; ``rdf.episode`` metadata keeps the source)
    <out>/<prefix>ep<index>/<role>.mp4         re-encoded H.264, one per camera (roles: exo, wrist, ...)

Mapping (recorded in the parquet metadata under ``source`` so QA can explain itself):
  * ``observation.state`` -> ``joint_pos`` (arm joints) + ``gripper_pos``; ``action`` -> ``joint_cmd`` +
    ``gripper_cmd``. The gripper is the channel whose name contains "gripper", else the last channel
    (LeRobot convention), marked ``assumed``. ``--arm left|right`` picks one arm of a bimanual dataset.
  * Values are copied, NOT converted: a dataset in degrees stays in degrees and fails ``action_range``.
  * ``ik_target_pos`` / ``ik_target_quat`` do not exist in joint-space datasets: written as a
    placeholder (0,0,0 / identity) and flagged ``ik_target: "absent"``. ``control_source`` = human.
  * ``timestamp_us`` = import time + the source's ``timestamp`` (our clock is the Unix epoch).
  * cameras: ``observation.images.<key>`` -> role via ``--camera-map key=role`` or the QA aliases
    (top/front/high -> exo, *wrist* -> wrist). A camera whose video is missing is left out (and
    QA's ``camera_set`` rejects the episode if it was a required one).
  * ``success`` = the last ``next.success`` when the dataset has it, else ``--assume-success`` (the
    seller's claim; the LLM stage, TDM-93, judges it anyway), else unknown.

CLI (local proof / manual ingest):

    uv run python -m rdf_orchestrator.dataset_import hf://lerobot/svla_so100_pickplace --out DIR --episodes 0,1
    uv run python -m rdf_orchestrator.dataset_import ./fixture_ds --out DIR --ingest OFFER_ID   # + ingest_purchased_episode

Needs numpy, pyarrow, httpx (HF downloads) and ffmpeg/ffprobe. HF videos are read by ffmpeg straight
from the public resolve URL (it seeks with range requests, so a v3 segment does not download the whole
shared file); gated datasets are not supported (the token is never put on a command line).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from rdf_common.episode_format import make_metadata, write_actions

IMPORTER = "rdf_orchestrator.dataset_import"
IMPORTER_VERSION = 1
HF_RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/{rev}/{path}"
DEFAULT_ALIASES = {"top": "exo", "front": "exo", "scene": "exo", "overhead": "exo", "high": "exo",
                   "cam_high": "exo", "env": "exo", "camera_front": "exo", "base": "exo"}


class DatasetImportError(RuntimeError):
    pass


# --------------------------------------------------------------------------- source access

class Source:
    """A LeRobot dataset root: a local directory or ``hf://<repo>[@<revision>]``."""

    def __init__(self, spec: str, cache_dir: Path):
        self.spec = spec
        self.cache = cache_dir
        if spec.startswith("hf://") or spec.startswith("https://huggingface.co/datasets/"):
            rest = spec.split("hf://", 1)[1] if spec.startswith("hf://") else spec.split("/datasets/", 1)[1]
            repo, _, rev = rest.partition("@")
            self.repo, self.rev, self.root = repo.strip("/"), rev or "main", None
        else:
            self.repo, self.rev, self.root = None, None, Path(spec).expanduser().resolve()
            if not (self.root / "meta" / "info.json").exists():
                raise DatasetImportError(f"{spec}: not a LeRobot dataset (no meta/info.json)")
        self._client = None

    @property
    def is_hf(self) -> bool:
        return self.repo is not None

    def url(self, rel: str) -> str:
        return HF_RESOLVE.format(repo=self.repo, rev=self.rev, path=rel)

    def exists(self, rel: str) -> bool:
        if not self.is_hf:
            return (self.root / rel).exists()
        try:
            self.local(rel)
            return True
        except DatasetImportError:
            return False

    def local(self, rel: str) -> Path:
        """A local copy of a (small) file: data parquet, meta files."""
        if not self.is_hf:
            p = self.root / rel
            if not p.exists():
                raise DatasetImportError(f"{self.spec}: missing {rel}")
            return p
        dest = self.cache / self.repo.replace("/", "__") / self.rev / rel
        if dest.exists():
            return dest
        import httpx

        if self._client is None:
            tok = os.environ.get("HF_TOKEN")
            self._client = httpx.Client(follow_redirects=True, timeout=120.0,
                                        headers={"Authorization": f"Bearer {tok}"} if tok else {})
        r = self._client.get(self.url(rel))
        if r.status_code == 404:
            raise DatasetImportError(f"{self.spec}: missing {rel}")
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return dest

    def video_input(self, rel: str) -> str | None:
        """What ffmpeg should open for a video: a local path, or the public resolve URL."""
        if not self.is_hf:
            p = self.root / rel
            return str(p) if p.exists() else None
        return self.url(rel)

    def info(self) -> dict:
        return json.loads(self.local("meta/info.json").read_text())


# --------------------------------------------------------------------------- dataset description

def _names(feature: Mapping[str, Any] | None, width: int) -> list[str]:
    raw = (feature or {}).get("names")
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    names = [str(x) for x in raw] if isinstance(raw, list) else []
    if len(names) != width:
        names = [f"ch{i}" for i in range(width)]
    return names


@dataclass
class Layout:
    version: str
    fps: float
    robot_type: str
    state_key: str
    action_key: str | None
    state_names: list[str]
    action_names: list[str]
    video_keys: list[str]
    image_keys: list[str]  # stored inside the parquet (not supported as video)
    info: dict = field(default_factory=dict)

    @property
    def is_v3(self) -> bool:
        return self.version.startswith("v3")


def describe(info: Mapping[str, Any]) -> Layout:
    feats = info.get("features") or {}
    state_key = next((k for k in ("observation.state", "state", "observation.joint_positions") if k in feats), None)
    if state_key is None:
        raise DatasetImportError("dataset has no observation.state feature")
    action_key = "action" if "action" in feats else None

    def width(k):
        sh = feats[k].get("shape") or [0]
        return int(sh[0]) if sh else 0

    return Layout(
        version=str(info.get("codebase_version") or "v2.1"), fps=float(info.get("fps") or 0),
        robot_type=str(info.get("robot_type") or "unknown"), state_key=state_key, action_key=action_key,
        state_names=_names(feats[state_key], width(state_key)),
        action_names=_names(feats.get(action_key), width(action_key)) if action_key else [],
        video_keys=[k for k, v in feats.items() if v.get("dtype") == "video"],
        image_keys=[k for k, v in feats.items() if v.get("dtype") == "image"], info=dict(info))


def select_channels(names: Sequence[str], arm: str | None) -> tuple[list[int], int | None, bool]:
    """(arm joint indices, gripper index or None, gripper_assumed)."""
    idx = list(range(len(names)))
    if arm:
        a = arm.lower()
        idx = [i for i in idx if names[i].lower().startswith(a + "_") or f"{a}_" in names[i].lower()
               or names[i].lower().startswith(a + ".")]
        if not idx:
            raise DatasetImportError(f"--arm {arm}: no state channel names start with '{arm}_' ({list(names)[:8]}...)")
    grip = [i for i in idx if "gripper" in names[i].lower() or "grip" in names[i].lower()]
    if grip:
        g, assumed = grip[0], False
    elif idx:
        g, assumed = idx[-1], True
    else:
        g, assumed = None, False
    return [i for i in idx if i != g], g, assumed


def camera_role(video_key: str, camera_map: Mapping[str, str], arm: str | None) -> str | None:
    short = video_key.split("observation.images.", 1)[-1]
    if short in camera_map:
        return camera_map[short] or None
    if video_key in camera_map:
        return camera_map[video_key] or None
    low = short.lower()
    if arm and low == arm.lower():  # bimanual rigs name the wrist cameras after the arm ("left")
        return "wrist"
    if low in DEFAULT_ALIASES:
        return DEFAULT_ALIASES[low]
    if "wrist" in low:
        if arm and any(o in low for o in ("left", "right") if o != arm.lower()):
            return low  # the other arm's wrist camera: kept under its own name
        return "wrist"
    return low


# --------------------------------------------------------------------------- per-episode reading

@dataclass
class EpisodeRef:
    index: int
    length: int
    task: str
    data_rel: str
    videos: dict[str, tuple[str, float | None, float | None]]  # key -> (rel path, from_s, to_s)


def episode_refs(src: Source, lay: Layout) -> list[EpisodeRef]:
    info = lay.info
    out: list[EpisodeRef] = []
    if lay.is_v3:
        import pyarrow.parquet as pq

        ep_files = sorted({f"meta/episodes/chunk-{c:03d}/file-{f:03d}.parquet" for c, f in _v3_episode_files(src)})
        for rel in ep_files:
            t = pq.read_table(src.local(rel))
            cols = [c for c in t.column_names if not c.startswith("stats/")]
            for row in t.select(cols).to_pylist():
                vids = {}
                for k in lay.video_keys:
                    ci, fi = row.get(f"videos/{k}/chunk_index"), row.get(f"videos/{k}/file_index")
                    if ci is None or fi is None:
                        continue
                    vids[k] = (info["video_path"].format(video_key=k, chunk_index=int(ci), file_index=int(fi)),
                               row.get(f"videos/{k}/from_timestamp"), row.get(f"videos/{k}/to_timestamp"))
                tasks = row.get("tasks") or []
                out.append(EpisodeRef(int(row["episode_index"]), int(row.get("length") or 0),
                                      str(tasks[0]) if tasks else "",
                                      info["data_path"].format(chunk_index=int(row["data/chunk_index"]),
                                                               file_index=int(row["data/file_index"])), vids))
        return sorted(out, key=lambda e: e.index)
    chunks = int(info.get("chunks_size") or 1000)
    lines = src.local("meta/episodes.jsonl").read_text().splitlines()
    for ln in lines:
        if not ln.strip():
            continue
        e = json.loads(ln)
        i = int(e["episode_index"])
        ch = i // chunks
        vids = {k: (info["video_path"].format(episode_chunk=ch, video_key=k, episode_index=i), None, None)
                for k in lay.video_keys}
        tasks = e.get("tasks") or []
        out.append(EpisodeRef(i, int(e.get("length") or 0), str(tasks[0]) if tasks else "",
                              info["data_path"].format(episode_chunk=ch, episode_index=i), vids))
    return sorted(out, key=lambda e: e.index)


def _v3_episode_files(src: Source) -> list[tuple[int, int]]:
    if not src.is_hf:
        return [(int(p.parent.name.split("-")[1]), int(p.stem.split("-")[1]))
                for p in sorted((src.root / "meta" / "episodes").glob("chunk-*/file-*.parquet"))]
    files, c = [], 0
    while True:  # probe chunk-000/file-000, file-001, ... (no listing API needed)
        f = 0
        while src.exists(f"meta/episodes/chunk-{c:03d}/file-{f:03d}.parquet"):
            files.append((c, f))
            f += 1
        if f == 0:
            break
        c += 1
    return files


def read_episode_rows(src: Source, lay: Layout, ref: EpisodeRef) -> dict[str, Any]:
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    t = pq.read_table(src.local(ref.data_rel))
    if "episode_index" in t.column_names:
        t = t.filter(pc.equal(t.column("episode_index"), ref.index))
    if t.num_rows == 0:
        raise DatasetImportError(f"episode {ref.index}: no rows in {ref.data_rel}")
    out = {"state": np.asarray(t.column(lay.state_key).to_pylist(), dtype=np.float64),
           "timestamp": np.asarray(t.column("timestamp").to_pylist(), dtype=np.float64).reshape(-1),
           "frame_index": np.asarray(t.column("frame_index").to_pylist(), dtype=np.int64).reshape(-1)}
    if lay.action_key and lay.action_key in t.column_names:
        out["action"] = np.asarray(t.column(lay.action_key).to_pylist(), dtype=np.float64)
    for k in ("next.success", "next.done"):
        if k in t.column_names:
            out[k] = np.asarray(t.column(k).to_pylist()).reshape(-1)
    return out


def cut_video(inp: str, dest: Path, from_s: float | None, to_s: float | None) -> int | None:
    """Re-encode one camera (segment) to H.264; returns the frame count, None when unreadable."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DatasetImportError("ffmpeg is required to import camera videos")
    cmd = [ffmpeg, "-v", "error", "-y"]
    if from_s:
        cmd += ["-ss", f"{float(from_s):.6f}"]
    cmd += ["-i", inp]
    if to_s is not None:
        cmd += ["-t", f"{float(to_s) - float(from_s or 0):.6f}"]
    cmd += ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-metadata", f"comment=imported by {IMPORTER}", str(dest)]
    r = subprocess.run(cmd, capture_output=True, timeout=900, check=False)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return None
    return probe_frames(dest)


def probe_frames(path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=nb_read_packets,width,height,r_frame_rate", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120, check=False)
    try:
        s = json.loads(r.stdout)["streams"][0]
        return int(s["nb_read_packets"])
    except (ValueError, KeyError, IndexError):
        return None


# --------------------------------------------------------------------------- import

@dataclass
class ImportedEpisode:
    source_index: int
    dir: Path
    actions_path: Path
    cameras: dict[str, Path]
    frame_count: int
    duration_ms: int
    success: bool | None
    task: str
    notes: list[str] = field(default_factory=list)

    def cameras_json(self, uri=lambda p: p.as_uri()) -> str:
        return json.dumps({k: uri(v) for k, v in self.cameras.items()})

    def ingest_args(self, offer_id: int, uri=lambda p: p.as_uri()) -> list:
        """Positional args for ``ingest_purchased_episode`` (TDM-14)."""
        return [int(offer_id), uri(self.dir), uri(self.actions_path), self.cameras_json(uri), int(self.frame_count),
                int(self.duration_ms), {"none": []} if self.success is None else {"some": bool(self.success)}]


def episode_columns(rows: Mapping[str, Any], lay: Layout, *, arm: str | None, dataset: str, episode_index: int,
                    task: str = "", cam_meta: Mapping[str, dict] | None = None, assume_success: bool | None = None,
                    t0_us: int | None = None, notes: list[str] | None = None) -> tuple[dict, dict, bool | None]:
    """LeRobot rows (``read_episode_rows``) -> (our action columns, ``rdf.episode`` metadata, success).
    The one mapping both the file importer and the in-place normaliser use."""
    import numpy as np

    notes = list(notes or [])
    joints_idx, grip_idx, grip_assumed = select_channels(lay.state_names, arm)
    n = len(rows["timestamp"])
    t0 = int(t0_us if t0_us is not None else time.time() * 1e6)
    ts = (t0 + np.rint((rows["timestamp"] - rows["timestamp"][0]) * 1e6)).astype(np.int64)
    st = rows["state"]
    act = rows.get("action")
    if act is None or act.shape != st.shape:
        notes.append("no usable action column: joint_cmd/gripper_cmd taken from the next state")
        act = np.vstack([st[1:], st[-1:]])
    if lay.image_keys:
        notes.append(f"parquet-embedded image features not imported: {lay.image_keys}")
    success = None
    if "next.success" in rows:
        success = bool(rows["next.success"][-1])
    elif assume_success is not None:
        success = bool(assume_success)
    fixture = bool(lay.info.get("rdf_fixture"))
    meta = make_metadata(
        fps=lay.fps or 30.0, joint_names=[lay.state_names[i] for i in joints_idx],
        embodiment=lay.robot_type.lower() + (f":{arm.lower()}" if arm else ""), cameras=dict(cam_meta or {}),
        fixture=fixture, note=(f"imported from LeRobot {lay.version} {dataset} episode {episode_index}"
                               + (" (TDM-92 synthetic fixture, not real robot data)" if fixture else "")))
    meta["source"] = {
        "format": "lerobot", "codebase_version": lay.version, "dataset": dataset, "episode_index": episode_index,
        "robot_type": lay.robot_type, "arm": arm, "task": task,
        "state_names": lay.state_names, "action_names": lay.action_names,
        "joint_channels": joints_idx, "gripper_channel": None if grip_idx is None else lay.state_names[grip_idx],
        "gripper_assumed": grip_assumed, "units": "as in source (not converted)",
        "ik_target": "absent", "success_source": ("next.success" if "next.success" in rows
                                                 else "seller_claim" if assume_success is not None else None),
        "importer": IMPORTER, "importer_version": IMPORTER_VERSION, "notes": notes}
    zeros = np.zeros(n)
    g_state = st[:, grip_idx] if grip_idx is not None else zeros
    g_act = act[:, grip_idx] if grip_idx is not None else zeros
    cols = {
        "frame_index": rows["frame_index"].astype(np.int32),
        "timestamp_us": ts,
        "joint_pos": st[:, joints_idx].astype(np.float32),
        "gripper_pos": g_state.astype(np.float32),
        "ik_target_pos": np.zeros((n, 3), dtype=np.float32),
        "ik_target_quat": np.tile([1.0, 0.0, 0.0, 0.0], (n, 1)).astype(np.float32),
        "gripper_cmd": g_act.astype(np.float32),
        "control_source": ["human"] * n,
        "joint_cmd": act[:, joints_idx].astype(np.float32),
    }
    return cols, meta, success


def import_lerobot(spec: str, out_dir: str | Path, *, episodes: Sequence[int] | None = None,
                   arm: str | None = None, camera_map: Mapping[str, str] | None = None,
                   assume_success: bool | None = None, max_episodes: int | None = None,
                   prefix: str = "", t0_us: int | None = None) -> list[ImportedEpisode]:
    """Import whole episodes (actions + re-encoded camera videos) into ``out_dir``."""
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = Source(spec, out / ".cache")
    lay = describe(src.info())
    refs = episode_refs(src, lay)
    if episodes is not None:
        want = set(int(e) for e in episodes)
        refs = [r for r in refs if r.index in want]
    if max_episodes is not None:
        refs = refs[:max_episodes]
    cam_map = dict(camera_map or {})
    imported = []
    for ref in refs:
        rows = read_episode_rows(src, lay, ref)
        d = out / f"{prefix}ep{ref.index:06d}"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        cams: dict[str, Path] = {}
        cam_meta: dict[str, dict] = {}
        missing: list[str] = []
        for key, (rel, f0, f1) in ref.videos.items():
            role = camera_role(key, cam_map, arm)
            if role is None or role in cams:
                continue
            inp = src.video_input(rel)
            frames = cut_video(inp, d / f"{role}.mp4", f0, f1) if inp else None
            if frames is None:
                missing.append(key)
                continue
            cams[role] = d / f"{role}.mp4"
            cam_meta[role] = {"frames": frames, "fps": lay.fps, "source_key": key}
        notes = [f"camera video missing/unreadable for {missing}"] if missing else []
        cols, meta, success = episode_columns(rows, lay, arm=arm, dataset=spec, episode_index=ref.index, task=ref.task,
                                              cam_meta=cam_meta, assume_success=assume_success, t0_us=t0_us,
                                              notes=notes)
        ap = d / "actions.parquet"
        write_actions(ap, cols, meta)
        ts = cols["timestamp_us"]
        imported.append(ImportedEpisode(ref.index, d, ap, cams, len(ts), int(round((ts[-1] - ts[0]) / 1000)), success,
                                        ref.task, meta["source"]["notes"]))
    return imported


def is_lerobot_parquet(path: str | Path) -> bool:
    import pyarrow.parquet as pq

    try:
        names = pq.read_schema(path).names
    except Exception:  # noqa: BLE001
        return False
    return "observation.state" in names and "timestamp" in names and "joint_pos" not in names


def normalize_lerobot_parquet(path: str | Path, info: Mapping[str, Any] | None, *, episode_index: int | None = None,
                              arm: str | None = None, dataset: str = "", t0_us: int | None = None):
    """A raw LeRobot episode parquet (as TDM-91's ``unpack_lerobot`` ingests it: ``actions_uri`` points at
    ``data/chunk-000/episode_000000.parquet``) -> ``EpisodeActions`` in our format, in memory. ``info`` is
    the dataset's ``meta/info.json`` (None: guessed from the parquet; robot_type 'unknown')."""
    import io

    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    from rdf_common.episode_format import read_actions

    t = pq.read_table(path)
    if episode_index is not None and "episode_index" in t.column_names:
        t = t.filter(pc.equal(t.column("episode_index"), int(episode_index)))
    if info is None:
        width = len(t.column("observation.state")[0].as_py())
        ts = np.asarray(t.column("timestamp").to_pylist(), dtype=np.float64)
        fps = float(round(1.0 / np.median(np.diff(ts)), 3)) if len(ts) > 1 else 30.0
        info = {"codebase_version": "unknown", "robot_type": "unknown", "fps": fps,
                "features": {"observation.state": {"dtype": "float32", "shape": [width]},
                             **({"action": {"dtype": "float32", "shape": [width]}} if "action" in t.column_names else {})}}
    lay = describe(info)
    rows = {"state": np.asarray(t.column(lay.state_key).to_pylist(), dtype=np.float64),
            "timestamp": np.asarray(t.column("timestamp").to_pylist(), dtype=np.float64).reshape(-1),
            "frame_index": np.asarray(t.column("frame_index").to_pylist(), dtype=np.int64).reshape(-1)}
    if lay.action_key and lay.action_key in t.column_names:
        rows["action"] = np.asarray(t.column(lay.action_key).to_pylist(), dtype=np.float64)
    if "next.success" in t.column_names:
        rows["next.success"] = np.asarray(t.column("next.success").to_pylist()).reshape(-1)
    idx = int(t.column("episode_index")[0].as_py()) if "episode_index" in t.column_names and t.num_rows else -1
    cols, meta, _ = episode_columns(rows, lay, arm=arm, dataset=dataset, episode_index=idx, t0_us=t0_us)
    buf = io.BytesIO()
    write_actions(buf, cols, meta)
    return read_actions(buf.getvalue())


# --------------------------------------------------------------------------- CLI

def _parse_map(s: str | None) -> dict[str, str]:
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("source", help="local LeRobot dataset dir or hf://<repo>[@rev]")
    ap.add_argument("--out", required=True)
    ap.add_argument("--episodes", help="comma-separated source episode indices")
    ap.add_argument("--max-episodes", type=int)
    ap.add_argument("--arm", help="left|right: take one arm of a bimanual dataset")
    ap.add_argument("--camera-map", help="key=role,... e.g. top=exo,left=wrist (role '' drops a camera)")
    ap.add_argument("--assume-success", action="store_true", help="the seller claims every episode succeeded")
    ap.add_argument("--ingest", type=int, metavar="OFFER_ID",
                    help="call ingest_purchased_episode for each episode (SPACETIME_* env; file:// uris)")
    a = ap.parse_args(argv)
    eps = [int(x) for x in a.episodes.split(",")] if a.episodes else None
    got = import_lerobot(a.source, a.out, episodes=eps, arm=a.arm, camera_map=_parse_map(a.camera_map),
                         assume_success=True if a.assume_success else None, max_episodes=a.max_episodes)
    for e in got:
        print(json.dumps({"source_index": e.source_index, "dir": str(e.dir), "frames": e.frame_count,
                          "duration_ms": e.duration_ms, "cameras": sorted(e.cameras), "success": e.success,
                          "notes": e.notes}))
    if a.ingest:
        from rdf_common.settings import OrchestratorSettings, load_settings
        from rdf_common.spacetime_client import SpacetimeClient

        with SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator")) as c:
            for e in got:
                c.call_reducer("ingest_purchased_episode", *e.ingest_args(a.ingest))
        print(f"ingested {len(got)} episode(s) into offer {a.ingest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
