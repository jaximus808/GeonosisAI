"""TDM-33: the Data Droid's tools. A Factory Droid (``rdf_factory.droids.data``) runs these, and only
these, through its ``allow_commands`` list, to turn a ticket's accepted episodes into its LeRobot
v2.1 training set with TDM-39's converter (``rdf_training.convert``).

    python -m rdf_training.data_droid_tools inspect  --ticket 12 --out W/episodes.json
    python -m rdf_training.data_droid_tools validate --episodes W/episodes.json --out W/validation.json
    python -m rdf_training.data_droid_tools assemble --episodes W/episodes.json --out-root D [--exclude 7:"reason"]
    python -m rdf_training.data_droid_tools verify   --dataset D/lerobot/rdf/ticket-12 --episodes W/episodes.json
    python -m rdf_training.data_droid_tools publish  --ticket 12 --manifest D/lerobot/rdf/ticket-12/manifest.json

* ``inspect`` reads the ticket, its ``dataset`` row, ``run_settings.target_episodes`` and every
  ``episode`` row from SpacetimeDB (read-only SQL). Only ``review_status == "accepted"`` episodes of a
  training kind are candidates; everything else is listed as excluded with the reason. Nothing here
  can accept an episode: review stays with the reviewer (TDM-25) and QA (TDM-92/93).
* ``validate`` checks each accepted episode against the episode format (``rdf_common.episode_format``):
  readable actions parquet with the required columns, increasing timestamps, the video for every
  camera with enough frames for every ``frame_index``, consistent joint count and camera set, and for
  ``recovery_takeover`` a usable takeover window. It also computes the exact takeover cut the
  converter will apply (``convert.plan_episode``). Errors make an episode unusable; warnings are
  for the dataset card.
* ``assemble`` builds the manifest from the accepted rows (``convert.manifest_from_episode_rows``),
  drops ``--exclude`` ids (each needs a reason, and must be an accepted id), converts locally
  (``convert.convert``) or on Modal (``--backend modal``: the ``rdf-training.convert`` function onto
  the ``rdf-datasets`` Volume), and writes ``<dataset>/manifest.json``: episode ids, per-episode frame
  counts and cuts, exclusions with reasons, openpi norm stats.
* ``verify`` re-reads the dataset: info.json totals, every parquet and video, the norm stats, that the
  manifest lists exactly the converted ids, and that no recovery frame comes from before
  ``takeover_ts`` (output frame 0's state equals the source state at the first tick >= takeover_ts).
  If a Python with openpi's LeRobot pin is available (``RDF_LEROBOT_PYTHON``, else the TDM-39 venv
  ``spikes/tdm-39-convert/tmp/lerobot-venv``) it also loads the dataset with ``LeRobotDataset`` and the
  S-05a action chunks. Prints the stats the dataset card needs.
* ``publish`` calls ``set_dataset_lerobot_uri`` (TDM-33 reducer): points ``dataset.lerobot_uri`` at the
  dataset. The reducer refuses any episode id that is not an accepted episode of the ticket. Fixture
  datasets are refused against maincloud.

Every command prints one JSON object on stdout (the droid reads it) and exits 1 on failure.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from rdf_common.episode_format import CONTROL_SOURCES, EpisodeFormatError, cameras_from_json, read_actions
from rdf_common.review_checks import ts_us

from . import convert as cv

TOOL = "rdf_training.data_droid_tools"
TRAINING_KINDS = ("clean_teleop", "recovery_takeover", "purchased")
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_LEROBOT_PY = REPO_ROOT / "spikes/tdm-39-convert/tmp/lerobot-venv/bin/python"


class ToolError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers

def _plain(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, _dt.datetime):
        return int(v.timestamp() * 1_000_000)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, bytes):
        return v.hex()
    return v


def _write(path: str | Path | None, obj: Any) -> None:
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, default=str))


def _load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def _mounts(media_root: str | None) -> dict[str, str] | None:
    return {"rdf-media": media_root} if media_root else None


def _ticket_task(ep_doc: Mapping[str, Any]) -> str:
    t = (ep_doc.get("ticket") or {}).get("task_description") or ""
    if not t.strip():
        raise ToolError("ticket has no task_description: pass --task")
    return t.strip()


def _client():
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient()


# --------------------------------------------------------------------------- inspect

def inspect_ticket(ticket_id: int, *, target: int | None = None, client: Any = None) -> dict:
    """Read-only snapshot of a ticket's episodes, split into candidates (accepted) and excluded."""
    c = client or _client()
    tid = int(ticket_id)
    tickets = c.sql(f"SELECT * FROM ticket WHERE id = {tid}")
    if not tickets:
        raise ToolError(f"ticket {tid} not found")
    ticket = _plain(dict(tickets[0]))
    ds = c.sql(f"SELECT * FROM dataset WHERE ticket_id = {tid}")
    rs = c.sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")
    rows = sorted((_plain(dict(r)) for r in c.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")),
                  key=lambda r: int(r["id"]))
    settings = _plain(dict(rs[0])) if rs else {}
    dataset = _plain(dict(ds[0])) if ds else None
    tgt = int(target if target is not None else settings.get("target_episodes") or 0)
    episodes, excluded = [], []
    for r in rows:
        r["takeover_ts"], r["handback_ts"] = ts_us(r.get("takeover_ts")), ts_us(r.get("handback_ts"))
        if r.get("review_status") != "accepted":
            excluded.append({"episode_id": r["id"], "kind": r.get("kind"),
                             "reason": f"review_status={r.get('review_status')} (only accepted episodes train)"})
        elif r.get("kind") not in TRAINING_KINDS:
            excluded.append({"episode_id": r["id"], "kind": r.get("kind"),
                             "reason": f"kind={r.get('kind')} is a policy rollout, not training data"})
        else:
            episodes.append(r)
    accepted_count = int((dataset or {}).get("accepted_count") or 0)
    return {
        "tool": TOOL, "ticket_id": tid, "ticket": {k: ticket.get(k) for k in
                                                   ("id", "status", "iteration", "task_description", "robot_id")},
        "dataset": dataset, "target_episodes": tgt, "accepted_count": accepted_count,
        "ready": tgt > 0 and accepted_count >= tgt,
        "candidate_ids": [r["id"] for r in episodes], "episodes": episodes, "excluded": excluded,
        "counts": {s: sum(1 for r in rows if r.get("review_status") == s)
                   for s in sorted({r.get("review_status") for r in rows})},
        "read_at_us": int(time.time() * 1e6),
    }


# --------------------------------------------------------------------------- validate

def validate_episode(row: Mapping[str, Any], *, mounts: Mapping[str, str] | None = None,
                     camera_map: Mapping[str, str] | None = None, fps: int = cv.DEFAULT_FPS) -> dict:
    eid = row.get("id", row.get("episode_id"))
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}
    cmap = dict(camera_map or cv.DEFAULT_CAMERA_MAP)
    try:
        src = cv.EpisodeSource.from_mapping({**row, "episode_id": eid})
    except Exception as e:  # noqa: BLE001
        return {"episode_id": eid, "ok": False, "errors": [f"row unusable: {type(e).__name__}: {e}"],
                "warnings": [], "stats": {}}
    try:
        act = read_actions(cv.resolve_uri(src.actions_uri, mounts=mounts))
    except (EpisodeFormatError, OSError, cv.ConvertError) as e:
        return {"episode_id": eid, "ok": False, "errors": [f"actions parquet: {e}"], "warnings": [], "stats": {}}
    n = act.n
    t = act.timestamp_us
    stats.update(rows=n, fps=act.fps, joints=int(act.joint_pos.shape[1]), fixture=act.is_fixture,
                 duration_s=round(float(t[-1] - t[0]) / 1e6, 3) if n > 1 else 0.0,
                 has_joint_cmd="joint_cmd" in act.columns,
                 control_source={s: act.control_source.count(s) for s in sorted(set(act.control_source))})
    if n < 2:
        errors.append(f"only {n} ticks")
    if n > 1 and np.any(np.diff(t) <= 0):
        errors.append("timestamp_us is not strictly increasing")
    if n > 1 and np.any(np.diff(act.frame_index) < 0):
        errors.append("frame_index decreases")
    bad_src = sorted(set(act.control_source) - set(CONTROL_SOURCES))
    if bad_src:
        errors.append(f"control_source has unknown values {bad_src}")
    if act.is_fixture:
        warnings.append("synthetic fixture episode (parquet metadata fixture=true): not robot data")
    fc = row.get("frame_count")
    if fc not in (None, 0) and int(fc) != n:
        warnings.append(f"episode.frame_count={fc} but the parquet has {n} rows")
    if abs(act.fps - fps) > 1e-6:
        warnings.append(f"recorded at {act.fps} fps; the converter resamples to {fps}")
    if np.any(~np.isfinite(act.joint_pos)):
        errors.append("joint_pos has NaN/inf")

    cams = {c: u for c, u in src.cameras.items() if c in cmap}
    stats["cameras"] = sorted(cams)
    if not cams:
        errors.append(f"no camera in cameras_json maps to a LeRobot image key ({sorted(src.cameras)})")
    need = int(act.frame_index.max()) + 1 if n else 0
    for cam, uri in sorted(cams.items()):
        try:
            p = cv.resolve_uri(uri, mounts=mounts)
            if not p.exists():
                errors.append(f"{cam} video missing: {uri}")
                continue
            frames = cv.count_video_frames(p)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cam} video unreadable: {type(e).__name__}: {e}")
            continue
        stats[f"{cam}_frames"] = frames
        if frames < need:
            errors.append(f"{cam} video has {frames} frames but frame_index reaches {need - 1}")
        elif frames > need + 1:
            warnings.append(f"{cam} video has {frames} frames, {frames - need} more than the actions use")

    if src.kind in cv.TAKEOVER_KINDS:
        if src.takeover_ts is None:
            human = [i for i, s in enumerate(act.control_source) if s == "human"]
            if human:
                warnings.append(f"no takeover_ts on the row: the converter cuts at the first human tick "
                                f"({human[0]}, t={int(t[human[0]])})")
            else:
                errors.append("recovery_takeover with no takeover_ts and no human ticks")
        else:
            if n and not (t[0] <= src.takeover_ts <= t[-1]):
                errors.append(f"takeover_ts {src.takeover_ts} is outside the recording [{int(t[0])}, {int(t[-1])}]")
            before = [i for i in range(n) if t[i] < src.takeover_ts and act.control_source[i] == "human"]
            if before:
                warnings.append(f"{len(before)} human ticks before takeover_ts (cut anyway)")
        if src.handback_ts is None:
            warnings.append("no handback_ts: keeps everything after takeover")
    if not errors:
        try:
            plan = cv.plan_episode(src, act, fps=fps, keep_after_handback=False)
            stats.update(kept_frames=plan.length, cut_before_takeover=plan.cut_before_takeover,
                         cut_after_handback=plan.cut_after_handback,
                         first_kept_ts=int(t[plan.rows[0]]) if plan.length else None)
        except cv.ConvertError as e:
            errors.append(str(e))
    return {"episode_id": eid, "kind": src.kind, "ok": not errors, "errors": errors, "warnings": warnings,
            "stats": stats}


def validate(ep_doc: Mapping[str, Any], *, mounts: Mapping[str, str] | None = None) -> dict:
    res = [validate_episode(r, mounts=mounts) for r in ep_doc.get("episodes", [])]
    ok = [r for r in res if r["ok"]]
    # Cross-episode consistency: one joint count and one camera set per dataset.
    joints = {r["stats"].get("joints") for r in ok}
    camsets = {tuple(r["stats"].get("cameras") or ()) for r in ok}
    cross = []
    if len(joints) > 1:
        cross.append(f"episodes disagree on joint count {sorted(joints)}")
    if len(camsets) > 1:
        cross.append(f"episodes disagree on cameras {sorted(camsets)}")
    return {"tool": TOOL, "ticket_id": ep_doc.get("ticket_id"), "episodes": res,
            "ok_ids": [r["episode_id"] for r in ok], "flagged_ids": [r["episode_id"] for r in res if not r["ok"]],
            "warnings_by_id": {str(r["episode_id"]): r["warnings"] for r in res if r["warnings"]},
            "cross_episode_errors": cross, "excluded_upstream": ep_doc.get("excluded", [])}


# --------------------------------------------------------------------------- assemble

def _parse_excludes(items: Sequence[str]) -> dict[str, str]:
    out = {}
    for it in items or ():
        eid, sep, reason = it.partition(":")
        if not sep or not reason.strip():
            raise ToolError(f"--exclude {it!r}: use <episode_id>:<reason>")
        out[eid.strip()] = reason.strip()
    return out


def build_manifest(ep_doc: Mapping[str, Any], *, repo_id: str | None = None, task: str | None = None,
                   exclude: Mapping[str, str] | None = None, allow_fixtures: bool = False,
                   keep_after_handback: bool = False) -> tuple[cv.ConvertManifest, list[dict]]:
    tid = ep_doc["ticket_id"]
    rows = list(ep_doc.get("episodes", []))
    accepted = {str(r["id"]) for r in rows if r.get("review_status") == "accepted"}
    exclude = dict(exclude or {})
    unknown = sorted(set(exclude) - accepted)
    if unknown:
        raise ToolError(f"--exclude ids {unknown} are not accepted candidates of ticket {tid}")
    keep = [r for r in rows if str(r["id"]) not in exclude]
    m = cv.manifest_from_episode_rows(keep, repo_id=repo_id or f"rdf/ticket-{tid}", task=task or _ticket_task(ep_doc),
                                      accepted_only=True, allow_fixtures=allow_fixtures,
                                      keep_after_handback=keep_after_handback)
    if not m.episodes:
        raise ToolError("no accepted episodes left to assemble")
    return m, [{"episode_id": int(k) if k.isdigit() else k, "reason": v} for k, v in exclude.items()]


def _manifest_doc(ep_doc: Mapping[str, Any], report: Mapping[str, Any], norm: Mapping[str, Any],
                  excluded: list[dict], dataset_uri: str, backend: str) -> dict:
    eps = [e for e in report["episodes"] if e.get("source", "ticket") == "ticket"]
    return {
        "tool": TOOL, "ticket_id": ep_doc["ticket_id"], "iteration": (ep_doc.get("ticket") or {}).get("iteration"),
        "repo_id": report["repo_id"], "lerobot_uri": dataset_uri, "backend": backend,
        "codebase_version": report["codebase_version"], "converter": report["converter"],
        "converter_version": report["converter_version"], "fps": report["fps"], "tasks": report["tasks"],
        "episode_ids": [int(e["episode_id"]) if str(e["episode_id"]).isdigit() else e["episode_id"] for e in eps],
        "total_episodes": report["total_episodes"], "total_frames": report["total_frames"],
        "episodes": [{k: e.get(k) for k in ("episode_index", "episode_id", "kind", "src_frames", "frames",
                                            "cut_before_takeover", "cut_after_handback", "takeover_ts",
                                            "handback_ts", "fixture")} for e in report["episodes"]],
        "excluded": excluded + list(ep_doc.get("excluded", [])),
        "fixture": any(e.get("fixture") for e in report["episodes"]),
        "norm_stats_path": report.get("norm_stats_path"), "norm_stats": norm.get("norm_stats", norm),
        "video_keys": report["video_keys"], "state_dim": report["state_dim"], "action_dim": report["action_dim"],
        "created_at_us": int(time.time() * 1e6),
    }


def assemble(ep_doc: Mapping[str, Any], *, out_root: str | Path, exclude: Mapping[str, str] | None = None,
             repo_id: str | None = None, task: str | None = None, allow_fixtures: bool = False,
             media_root: str | None = None, backend: str = "local") -> dict:
    m, excluded = build_manifest(ep_doc, repo_id=repo_id, task=task, exclude=exclude, allow_fixtures=allow_fixtures)
    if backend == "local":
        out_root = Path(out_root).resolve()
        report = cv.convert(m, lerobot_root=out_root / "lerobot", assets_root=out_root / "assets",
                            mounts=_mounts(media_root))
        root = Path(report["dataset_dir"])
        norm = json.loads(Path(report["norm_stats_path"]).read_text())
        uri = f"file://{root}"
        doc = _manifest_doc(ep_doc, report, norm, excluded, uri, backend)
        (root / "manifest.json").write_text(json.dumps(doc, indent=2))
    elif backend == "modal":
        doc = _assemble_modal(ep_doc, m, excluded)
        root = None
    else:
        raise ToolError(f"unknown backend {backend!r}")
    return {"tool": TOOL, "ok": True, "backend": backend, "dataset_dir": str(root) if root else None,
            "lerobot_uri": doc["lerobot_uri"], "manifest_path": str(root / "manifest.json") if root else doc.get("manifest_uri"),
            "episode_ids": doc["episode_ids"], "total_frames": doc["total_frames"], "excluded": doc["excluded"],
            "episodes": doc["episodes"]}


def _assemble_modal(ep_doc: Mapping[str, Any], m: cv.ConvertManifest, excluded: list[dict]) -> dict:
    """Modal path (production media on the rdf-media Volume): run ``rdf-training.convert`` remotely,
    then write manifest.json next to the dataset on ``rdf-datasets``. Not exercised by the TDM-33 proof."""
    import io

    import modal

    fn = modal.Function.from_name("rdf-training", "convert")
    uri = fn.remote(ticket_id=str(ep_doc["ticket_id"]), episode_manifest=m.to_dict())
    vol = modal.Volume.from_name(cv.DATASETS_VOLUME)
    key = f"lerobot/{m.repo_id}"
    report = json.loads(b"".join(vol.read_file(f"{key}/meta/rdf_conversion.json")))
    norm = json.loads(b"".join(vol.read_file(f"assets/{m.repo_id}/norm_stats.json")))
    doc = _manifest_doc(ep_doc, report, norm, excluded, uri, "modal")
    with vol.batch_upload(force=True) as b:
        b.put_file(io.BytesIO(json.dumps(doc, indent=2).encode()), f"{key}/manifest.json")
    doc["manifest_uri"] = f"{uri}/manifest.json"
    return doc


# --------------------------------------------------------------------------- verify

LEROBOT_SNIPPET = r"""
import json, sys
from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
repo_id, root, fps, horizon = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
ds = LeRobotDataset(repo_id, root=root, video_backend="pyav",
                    delta_timestamps={"action": [t / fps for t in range(horizon)]})
out = {"num_episodes": ds.num_episodes, "num_frames": ds.num_frames, "fps": ds.fps, "items": []}
for i in sorted({0, ds.num_frames // 2, ds.num_frames - 1}):
    it = ds[i]
    out["items"].append({"index": i, "task": it["task"],
                         **{k: list(v.shape) for k, v in it.items() if hasattr(v, "shape") and k.startswith(("observation", "action"))}})
print("LEROBOT_RESULT " + json.dumps(out))
"""


def lerobot_python() -> str | None:
    p = os.environ.get("RDF_LEROBOT_PYTHON", "").strip()
    if p:
        return p if Path(p).exists() else None
    return str(DEFAULT_LEROBOT_PY) if DEFAULT_LEROBOT_PY.exists() else None


def lerobot_load(dataset_dir: Path, repo_id: str, fps: int, horizon: int = cv.ACTION_HORIZON) -> dict:
    py = lerobot_python()
    if not py:
        return {"ran": False, "reason": "no Python with lerobot@0cf8648 (set RDF_LEROBOT_PYTHON)"}
    r = subprocess.run([py, "-c", LEROBOT_SNIPPET, repo_id, str(dataset_dir), str(fps), str(horizon)],
                       capture_output=True, text=True, timeout=600, env={**os.environ, "HF_HUB_OFFLINE": "1"})
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("LEROBOT_RESULT ")]
    if r.returncode != 0 or not line:
        return {"ran": True, "ok": False, "error": (r.stderr or r.stdout)[-1500:]}
    return {"ran": True, "ok": True, **json.loads(line[-1].split(" ", 1)[1])}


def verify(dataset_dir: str | Path, *, ep_doc: Mapping[str, Any] | None = None, media_root: str | None = None,
           loader: bool = True) -> dict:
    import pyarrow.parquet as pq

    root = Path(dataset_dir)
    errors: list[str] = []
    info = json.loads((root / "meta/info.json").read_text())
    conv = json.loads((root / "meta/rdf_conversion.json").read_text())
    man_p = root / "manifest.json"
    manifest = json.loads(man_p.read_text()) if man_p.exists() else None
    eps_meta = [json.loads(ln) for ln in (root / "meta/episodes.jsonl").read_text().splitlines() if ln.strip()]
    if info.get("codebase_version") != cv.CODEBASE_VERSION:
        errors.append(f"codebase_version {info.get('codebase_version')} != {cv.CODEBASE_VERSION}")
    if info["total_episodes"] != len(eps_meta):
        errors.append(f"info.total_episodes {info['total_episodes']} != {len(eps_meta)} in episodes.jsonl")
    if info["total_frames"] != sum(e["length"] for e in eps_meta):
        errors.append("info.total_frames != sum of episode lengths")
    video_keys = [k for k, f in info["features"].items() if f["dtype"] == "video"]
    per_ep = []
    next_index = 0
    state_all, action_all = [], []
    for e in eps_meta:
        i, n = e["episode_index"], e["length"]
        chunk = i // info["chunks_size"]
        t = pq.read_table(root / info["data_path"].format(episode_chunk=chunk, episode_index=i))
        if t.num_rows != n:
            errors.append(f"episode {i}: parquet {t.num_rows} rows != length {n}")
        fi = t.column("frame_index").to_pylist()
        idx = t.column("index").to_pylist()
        if fi != list(range(n)):
            errors.append(f"episode {i}: frame_index is not 0..{n - 1}")
        if idx != list(range(next_index, next_index + n)):
            errors.append(f"episode {i}: global index not contiguous")
        next_index += n
        st = np.asarray(t.column("observation.state").to_pylist(), dtype=np.float64)
        ac = np.asarray(t.column("action").to_pylist(), dtype=np.float64)
        state_all.append(st)
        action_all.append(ac)
        vids = {}
        for k in video_keys:
            vp = root / info["video_path"].format(episode_chunk=chunk, video_key=k, episode_index=i)
            vids[k] = cv.count_video_frames(vp) if vp.exists() else None
            if vids[k] != n:
                errors.append(f"episode {i}: {k} has {vids[k]} frames, expected {n}")
        per_ep.append({"episode_index": i, "frames": n, "video_frames": vids, "first_state": st[0].round(5).tolist()})

    conv_ids = [str(e["episode_id"]) for e in conv["episodes"] if e.get("source", "ticket") == "ticket"]
    if manifest is None:
        errors.append("manifest.json missing")
    elif [str(x) for x in manifest["episode_ids"]] != conv_ids:
        errors.append(f"manifest episode_ids {manifest['episode_ids']} != converted {conv_ids}")

    # Takeover cut, checked against the source episodes.
    cut_checks = []
    rows = {str(r["id"]): r for r in (ep_doc or {}).get("episodes", [])}
    for e, pe in zip(conv["episodes"], per_ep):
        if e["kind"] not in cv.TAKEOVER_KINDS:
            continue
        chk: dict[str, Any] = {"episode_id": e["episode_id"], "takeover_ts": e.get("takeover_ts"),
                               "cut_before_takeover": e["cut_before_takeover"]}
        row = rows.get(str(e["episode_id"]))
        if row is not None:
            src = cv.EpisodeSource.from_mapping({**row, "episode_id": row["id"]})
            act = read_actions(cv.resolve_uri(src.actions_uri, mounts=_mounts(media_root)))
            tk = src.takeover_ts if src.takeover_ts is not None else int(
                act.timestamp_us[act.control_source.index("human")])
            first = int(np.searchsorted(act.timestamp_us, tk, side="left"))
            src_state = np.concatenate([act.joint_pos[first], [act.extra["gripper_pos"][first]]])
            chk["first_source_row"] = first
            chk["first_frame_ts"] = int(act.timestamp_us[first])
            chk["matches_source"] = bool(np.allclose(src_state, pe["first_state"], atol=1e-4))
            chk["no_frames_before_takeover"] = bool(act.timestamp_us[first] >= tk and first == e["cut_before_takeover"])
            if not (chk["matches_source"] and chk["no_frames_before_takeover"]):
                errors.append(f"episode {e['episode_id']}: frames before takeover_ts survived the cut")
        cut_checks.append(chk)

    norm_p = Path(conv["norm_stats_path"])
    norm = json.loads(norm_p.read_text())["norm_stats"] if norm_p.exists() else None
    if norm is None:
        errors.append(f"norm stats missing at {norm_p}")
    else:
        for k in ("state", "actions"):
            for s in ("mean", "std", "q01", "q99"):
                if len(norm[k][s]) != conv["state_dim"]:
                    errors.append(f"norm_stats.{k}.{s} has {len(norm[k][s])} dims, expected {conv['state_dim']}")
    S, A = np.concatenate(state_all), np.concatenate(action_all)
    stats = {"state_mean": S.mean(0).round(4).tolist(), "state_std": S.std(0).round(4).tolist(),
             "action_mean": A.mean(0).round(4).tolist(), "action_std": A.std(0).round(4).tolist(),
             "state_min": S.min(0).round(4).tolist(), "state_max": S.max(0).round(4).tolist()}
    load = lerobot_load(root, conv["repo_id"], int(info["fps"])) if loader else {"ran": False, "reason": "--no-loader"}
    if load.get("ran") and not load.get("ok"):
        errors.append(f"LeRobotDataset failed to load: {load.get('error', '')[-300:]}")
    shapes = {k: info["features"][k]["shape"] for k in info["features"] if k.startswith(("observation", "action"))}
    return {"tool": TOOL, "ok": not errors, "errors": errors, "dataset_dir": str(root), "repo_id": conv["repo_id"],
            "codebase_version": info.get("codebase_version"), "fps": info["fps"],
            "total_episodes": info["total_episodes"], "total_frames": info["total_frames"], "feature_shapes": shapes,
            "episode_ids": manifest["episode_ids"] if manifest else None, "episodes": per_ep,
            "takeover_cuts": cut_checks, "stats": stats, "norm_stats_dims": conv["state_dim"],
            "lerobot_loader": load}


# --------------------------------------------------------------------------- publish

def publish(ticket_id: int, manifest_path: str | Path, *, client: Any = None) -> dict:
    man = _load(manifest_path)
    if int(man["ticket_id"]) != int(ticket_id):
        raise ToolError(f"manifest is for ticket {man['ticket_id']}, not {ticket_id}")
    host = os.environ.get("SPACETIME_HOST", "")
    if man.get("fixture") and "maincloud" in host:
        raise ToolError("refusing to publish a fixture dataset to maincloud")
    summary = {"repo_id": man["repo_id"], "total_frames": man["total_frames"], "fps": man["fps"],
               "fixture": man.get("fixture", False), "backend": man.get("backend"),
               "excluded": man.get("excluded", [])[:20],
               "episodes": [{k: e.get(k) for k in ("episode_id", "kind", "frames", "cut_before_takeover")}
                            for e in man["episodes"]][:200],
               "manifest_path": str(Path(manifest_path).resolve())}
    c = client or _client()
    c.call_reducer("set_dataset_lerobot_uri", int(ticket_id), man["lerobot_uri"], json.dumps(man["episode_ids"]),
                   json.dumps(summary, default=str))
    return {"tool": TOOL, "ok": True, "ticket_id": int(ticket_id), "lerobot_uri": man["lerobot_uri"],
            "episode_ids": man["episode_ids"]}


# --------------------------------------------------------------------------- CLI

def _short_validation(v: dict) -> dict:
    return {**v, "episodes": [{k: r[k] for k in ("episode_id", "kind", "ok", "errors", "warnings")} |
                              {"stats": {k: r["stats"].get(k) for k in ("rows", "kept_frames", "cut_before_takeover",
                                                                        "cut_after_handback", "cameras", "fixture")}}
                              for r in v["episodes"]]}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=f"python -m {TOOL}", description="TDM-33 Data Droid tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--target", type=int)
    p.add_argument("--out", required=True)
    p = sub.add_parser("validate")
    p.add_argument("--episodes", required=True)
    p.add_argument("--media-root")
    p.add_argument("--out", required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--episodes", required=True)
    p.add_argument("--out-root", default=os.environ.get("RDF_DATASETS_ROOT", ""))
    p.add_argument("--exclude", action="append", default=[], help="<episode_id>:<reason>")
    p.add_argument("--repo-id")
    p.add_argument("--task")
    p.add_argument("--allow-fixtures", action="store_true")
    p.add_argument("--media-root")
    p.add_argument("--backend", choices=("local", "modal"), default="local")
    p = sub.add_parser("verify")
    p.add_argument("--dataset", required=True)
    p.add_argument("--episodes")
    p.add_argument("--media-root")
    p.add_argument("--no-loader", action="store_true")
    p.add_argument("--out")
    p = sub.add_parser("publish")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--manifest", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "inspect":
            doc = inspect_ticket(a.ticket, target=a.target)
            _write(a.out, doc)
            out = {k: doc[k] for k in ("ticket_id", "ticket", "target_episodes", "accepted_count", "ready",
                                       "candidate_ids", "excluded", "counts")} | {"written": a.out}
        elif a.cmd == "validate":
            v = validate(_load(a.episodes), mounts=_mounts(a.media_root))
            _write(a.out, v)
            out = _short_validation(v) | {"written": a.out}
        elif a.cmd == "assemble":
            if a.backend == "local" and not a.out_root:
                raise ToolError("--out-root (or RDF_DATASETS_ROOT) is required for the local backend")
            out = assemble(_load(a.episodes), out_root=a.out_root, exclude=_parse_excludes(a.exclude),
                           repo_id=a.repo_id, task=a.task, allow_fixtures=a.allow_fixtures,
                           media_root=a.media_root, backend=a.backend)
        elif a.cmd == "verify":
            out = verify(a.dataset, ep_doc=_load(a.episodes) if a.episodes else None, media_root=a.media_root,
                         loader=not a.no_loader)
            _write(a.out, out)
        else:
            out = publish(a.ticket, a.manifest)
    except (ToolError, cv.ConvertError, EpisodeFormatError, FileNotFoundError) as e:
        print(json.dumps({"tool": TOOL, "ok": False, "cmd": a.cmd, "error": f"{type(e).__name__}: {e}"}))
        return 1
    except Exception as e:  # noqa: BLE001 - the droid needs the message, not a traceback dump
        print(json.dumps({"tool": TOOL, "ok": False, "cmd": a.cmd, "error": f"{type(e).__name__}: {e}"[:2000]}))
        return 1
    print(json.dumps(out, default=str, indent=1))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
