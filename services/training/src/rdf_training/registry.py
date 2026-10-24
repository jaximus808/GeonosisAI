"""TDM-42: checkpoint registry and Volume cleanup for ``rdf-checkpoints``.

One record per checkpoint, built from what is already written, so there is no second copy to
drift (and no new SpacetimeDB table):

* the Volume (ground truth for what exists): ``runs/ticket-<t>/<run>/<step>/`` (TDM-40), its
  ``rdf_run.json`` (dataset, settings, created_at), ``metrics/ticket-<t>/<run>.json`` and the
  step dir's file sizes;
* SpacetimeDB rows, joined by ``checkpoint_uri``: ``training_run`` (source run, hyperparams,
  gpu_cost_cents + metrics_json.cost), ``eval_run`` (eval results) and
  ``ticket.deployed_checkpoint_uri`` (TDM-70's ``mark_checkpoint_deployed``) -> ``deployed``;
* the policy server's ``rdf-policy-state`` Dict (TDM-41): ``active`` / ``previous`` URIs.

The base model is one record too (``kind: base``, ``gs://openpi-assets/checkpoints/pi05_base``,
cached under ``openpi-cache/``).

Resolve per ticket: ``latest`` (newest complete fine-tuned checkpoint), ``deployed``, ``base``.

Cleanup (:func:`plan_cleanup`) only ever considers whole run dirs under ``runs/`` and keeps, per
ticket: the deployed checkpoint, the latest ``keep_latest`` complete runs, anything the policy
server has active/previous, anything a queued/running training run or a non-final eval
references, and every incomplete run younger than ``grace_s`` (a fine-tune still writing).
``openpi-cache/`` (base weights) and ``metrics/`` are never touched. The plan refuses to delete
anything if the deployed/active state could not be read (``state_ok`` False). It is a dry run
unless :func:`apply_cleanup` is called with the plan.
"""
from __future__ import annotations

import calendar
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import cost as _cost

VOLUME = "rdf-checkpoints"
URI_PREFIX = f"modal-vol://{VOLUME}/"
BASE_URI = "gs://openpi-assets/checkpoints/pi05_base"
BASE_CACHE_PATH = "openpi-cache/openpi-assets/checkpoints/pi05_base"
POLICY_STATE_DICT = "rdf-policy-state"
DEFAULT_KEEP_LATEST = 2
DEFAULT_GRACE_S = 3 * 3600  # > finetune's 2 h timeout: an incomplete run younger than this may still be writing
FINAL_EVAL = ("done", "finalized", "failed", "cancelled", "aborted")


@dataclass(frozen=True)
class Entry:
    path: str
    is_dir: bool
    size: int
    mtime: float


class VolumeLike(Protocol):
    def listdir(self, path: str, recursive: bool = False) -> list[Entry]: ...
    def read_json(self, path: str) -> dict | None: ...
    def remove(self, path: str) -> None: ...


class ModalVolume:
    """``modal.Volume.from_name(VOLUME)`` behind :class:`VolumeLike` (reads; ``remove`` only via apply)."""

    def __init__(self, name: str = VOLUME) -> None:
        import modal

        self.name = name
        self._v = modal.Volume.from_name(name)

    def listdir(self, path: str, recursive: bool = False) -> list[Entry]:
        from modal.volume import FileEntryType

        try:
            es = self._v.listdir(path, recursive=recursive)
        except Exception as e:  # noqa: BLE001 - a missing dir is an empty dir
            if "not found" in str(e).lower() or "no such" in str(e).lower():
                return []
            raise
        return [Entry(e.path.strip("/"), e.type == FileEntryType.DIRECTORY, int(e.size or 0), float(e.mtime or 0))
                for e in es]

    def read_json(self, path: str) -> dict | None:
        try:
            v = json.loads(b"".join(self._v.read_file(path)))
        except Exception:  # noqa: BLE001 - missing / unreadable metadata is reported as absent
            return None
        return v if isinstance(v, dict) else None

    def remove(self, path: str) -> None:
        self._v.remove_file(path, recursive=True)


def read_policy_state() -> dict:
    import modal

    return dict(modal.Dict.from_name(POLICY_STATE_DICT).get("state") or {})


# --------------------------------------------------------------------------- scan


def uri_for(path: str) -> str:
    return URI_PREFIX + path.strip("/")


def path_for(uri: str) -> str | None:
    u = (uri or "").strip().rstrip("/")
    if u.startswith(URI_PREFIX):
        return u[len(URI_PREFIX):]
    if u.startswith(f"{VOLUME}:/"):
        return u[len(VOLUME) + 2:].strip("/")
    return None


def _opt(v: Any) -> Any:
    """SpacetimeDB option encodings ({"some": x} / {"none": []} / [0, x]) -> x or None."""
    if isinstance(v, dict):
        return v.get("some") if "some" in v else None if "none" in v else v
    return v


def _norm_uri(uri: Any) -> str:
    u = str(_opt(uri) or "").strip().rstrip("/")
    if u.endswith("/params"):
        u = u[: -len("/params")]
    if u in ("pi05_base", BASE_URI + "/params"):
        return BASE_URI
    p = path_for(u)
    return uri_for(p) if p is not None else u


def ticket_key(ticket_id: Any) -> str:
    """SpacetimeDB ticket 42 / "42" / "ticket-42" -> "42" (the Volume dir is ``ticket-<key>``)."""
    t = str(ticket_id).strip()
    return t[len("ticket-"):] if t.startswith("ticket-") else t


def scan(volume: VolumeLike) -> list[dict]:
    """Every fine-tuned checkpoint (one per step dir) on the Volume, plus the base record."""
    entries = volume.listdir("runs", recursive=True)
    runs: dict[tuple[str, str], dict] = {}
    for e in entries:
        parts = e.path.split("/")
        if len(parts) < 3 or parts[0] != "runs" or not parts[1].startswith("ticket-"):
            continue
        r = runs.setdefault((parts[1], parts[2]), {"files": [], "dirs": set(), "mtime": 0.0})
        if len(parts) == 3:
            r["mtime"] = max(r["mtime"], e.mtime)
            continue
        r["mtime"] = max(r["mtime"], e.mtime)
        if e.is_dir:
            r["dirs"].add("/".join(parts[3:]))
        else:
            r["files"].append(e)
    out = []
    for (tdir, run_id), r in sorted(runs.items()):
        run_path = f"runs/{tdir}/{run_id}"
        meta = volume.read_json(f"{run_path}/rdf_run.json") or {}
        metrics = volume.read_json(f"metrics/{tdir}/{run_id}.json") or {}
        steps = sorted({int(p.split("/")[0]) for p in r["dirs"] if p.split("/")[0].isdigit()}
                       | {int(f.path.split("/")[3]) for f in r["files"] if f.path.split("/")[3].isdigit()})
        run_bytes = sum(f.size for f in r["files"])
        base = {"ticket": ticket_key(tdir), "ticket_dir": tdir, "run_id": run_id, "run_path": run_path,
                "run_size_bytes": run_bytes, "created_at": meta.get("created_at") or _created_from_run_id(run_id) or r["mtime"],
                "dataset_uri": meta.get("dataset_uri") or metrics.get("dataset_uri"),
                "repo_id": metrics.get("repo_id") or (meta.get("dataset_spec") or {}).get("repo_id"),
                "dataset": metrics.get("dataset") and {k: metrics["dataset"].get(k) for k in ("episodes", "frames", "fps")},
                "hyperparams": meta.get("settings") or metrics.get("settings"),
                "smoke": bool(metrics.get("smoke") if "smoke" in metrics else run_id.startswith("smoke-")),
                "metrics": {k: metrics.get(k) for k in ("final_loss", "first_loss", "steps", "wall_s", "train_s",
                                                         "sec_per_step", "gpu", "peak_gpu_mem_gb", "error") if k in metrics},
                "has_run_json": bool(meta), "has_metrics": bool(metrics)}
        if not steps:  # a run that never got to save a step (failed / still running)
            out.append({**base, "kind": "incomplete", "uri": None, "step": None, "size_bytes": 0, "complete": False,
                        "files": []})
            continue
        for step in steps:
            sp = f"{run_path}/{step}"
            files = [f for f in r["files"] if f.path.startswith(sp + "/")]
            names = {f.path[len(sp) + 1:] for f in files}
            complete = any(n.startswith("params/") for n in names) and "_CHECKPOINT_METADATA" in names
            out.append({**base, "kind": "finetuned", "uri": uri_for(sp), "step": step,
                        "size_bytes": sum(f.size for f in files), "complete": complete,
                        "has_train_state": any(n.startswith("train_state/") for n in names),
                        "has_policy_sidecar": "rdf_policy.json" in names})
    out.append(base_record(volume))
    return out


def base_record(volume: VolumeLike | None = None) -> dict:
    size = None
    if volume is not None:
        try:
            size = sum(e.size for e in volume.listdir(BASE_CACHE_PATH, recursive=True) if not e.is_dir) or None
        except Exception:  # noqa: BLE001
            size = None
    return {"kind": "base", "ticket": None, "uri": BASE_URI, "run_id": None, "step": None,
            "cache_path": BASE_CACHE_PATH, "size_bytes": size, "complete": True, "created_at": None,
            "note": "openpi pi0.5 base weights (gs://openpi-assets), cached on the Volume; never deleted"}


def _created_from_run_id(run_id: str) -> float | None:
    m = re.search(r"(\d{8}-\d{6})", run_id)
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%d-%H%M%S")))  # run ids are UTC
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- join


@dataclass
class State:
    """SpacetimeDB + policy-server facts the registry joins in."""
    training_runs: list[dict] = field(default_factory=list)
    eval_runs: list[dict] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)
    policy_state: dict = field(default_factory=dict)
    ok: bool = True  # False: rows could not be read; cleanup must not delete anything
    errors: list[str] = field(default_factory=list)


def _jsonish(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    try:
        d = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def join(records: list[dict], state: State) -> list[dict]:
    by_uri_run = {_norm_uri(r.get("checkpoint_uri")): r for r in state.training_runs if _opt(r.get("checkpoint_uri"))}
    evals: dict[str, list[dict]] = {}
    for e in state.eval_runs:
        evals.setdefault(_norm_uri(e.get("checkpoint_uri")), []).append(e)
    deployed = {_norm_uri(t["deployed_checkpoint_uri"]): t for t in state.tickets if _opt(t.get("deployed_checkpoint_uri"))}
    active = _norm_uri(state.policy_state.get("active")) if state.policy_state.get("active") else None
    previous = _norm_uri(state.policy_state.get("previous")) if state.policy_state.get("previous") else None
    out = []
    for rec in records:
        r = dict(rec)
        uri = _norm_uri(r.get("uri")) if r.get("uri") else None
        tr = by_uri_run.get(uri) if uri else None
        if tr is not None:
            m = _jsonish(tr.get("metrics_json"))
            r["training_run"] = {"id": int(tr["id"]), "ticket_id": int(tr.get("ticket_id") or 0),
                                 "iteration": tr.get("iteration"), "status": tr.get("status"),
                                 "episode_ids": [int(x) for x in (tr.get("episode_ids") or [])],
                                 "base_checkpoint_uri": tr.get("base_checkpoint_uri"),
                                 "hyperparams": _jsonish(tr.get("hyperparams_json")),
                                 "gpu_cost_cents": int(tr.get("gpu_cost_cents") or 0), "cost": m.get("cost")}
        r["evals"] = [{"eval_run_id": int(e["id"]), "kind": e.get("kind"), "status": e.get("status"),
                       "trials": e.get("trials"), "completed": e.get("completed"), "successes": e.get("successes"),
                       "ci_low": e.get("ci_low"), "ci_high": e.get("ci_high")} for e in evals.get(uri, [])] if uri else []
        t = deployed.get(uri) if uri else None
        r["deployed"] = t is not None
        if t is not None:
            r["deployed_on_ticket"] = int(t["id"])
        r["policy_server"] = ("active" if uri and uri == active else "previous" if uri and uri == previous else None)
        out.append(r)
    return out


def build(volume: VolumeLike, state: State) -> list[dict]:
    return join(scan(volume), state)


def for_ticket(records: Iterable[dict], ticket_id: Any) -> list[dict]:
    k = ticket_key(ticket_id)
    return [r for r in records if r.get("ticket") == k
            or (r.get("training_run") or {}).get("ticket_id") == (int(k) if k.isdigit() else None)]


def resolve(records: list[dict], ticket_id: Any, which: str = "latest") -> dict | None:
    """``latest`` | ``deployed`` | ``base`` for a ticket (None if there is none)."""
    which = which.strip().lower()
    if which == "base":
        return next((r for r in records if r.get("kind") == "base"), base_record())
    mine = [r for r in for_ticket(records, ticket_id) if r.get("kind") == "finetuned"]
    if which == "deployed":
        return next((r for r in mine if r.get("deployed")), None)
    if which == "latest":
        done = [r for r in mine if r.get("complete")]
        return max(done, key=lambda r: (float(r.get("created_at") or 0), int(r.get("step") or 0)), default=None)
    raise ValueError(f"which must be latest|deployed|base, not {which!r}")


# --------------------------------------------------------------------------- cleanup


def plan_cleanup(records: list[dict], state: State, *, keep_latest: int = DEFAULT_KEEP_LATEST,
                 grace_s: float = DEFAULT_GRACE_S, now: float | None = None) -> dict:
    """Which run dirs to keep/delete. Always a dry run: nothing is touched here."""
    now = time.time() if now is None else now
    keep_latest = max(1, int(keep_latest))
    referenced: dict[str, str] = {}
    for tr in state.training_runs:
        if tr.get("status") in ("queued", "running") and tr.get("base_checkpoint_uri"):
            referenced[_norm_uri(tr["base_checkpoint_uri"])] = f"base of {tr.get('status')} training_run {tr['id']}"
    for e in state.eval_runs:
        if e.get("status") not in FINAL_EVAL and e.get("checkpoint_uri"):
            referenced[_norm_uri(e["checkpoint_uri"])] = f"{e.get('status')} eval_run {e['id']}"

    runs: dict[str, dict] = {}
    for r in records:
        if r.get("kind") not in ("finetuned", "incomplete"):
            continue
        g = runs.setdefault(r["run_path"], {"run_path": r["run_path"], "ticket": r["ticket"], "run_id": r["run_id"],
                                            "created_at": float(r.get("created_at") or 0),
                                            "size_bytes": int(r.get("run_size_bytes") or 0),
                                            "uris": [], "complete": False, "reasons": []})
        if r.get("uri"):
            g["uris"].append(r["uri"])
        g["complete"] = g["complete"] or bool(r.get("complete"))
        if r.get("deployed"):
            g["reasons"].append(f"deployed on ticket {r.get('deployed_on_ticket')}")
        if r.get("policy_server"):
            g["reasons"].append(f"policy server {r['policy_server']}")
        for u in [r.get("uri")] if r.get("uri") else []:
            if _norm_uri(u) in referenced:
                g["reasons"].append(referenced[_norm_uri(u)])

    by_ticket: dict[str, list[dict]] = {}
    for g in runs.values():
        by_ticket.setdefault(g["ticket"], []).append(g)
    for groups in by_ticket.values():
        done = sorted((g for g in groups if g["complete"]), key=lambda g: g["created_at"], reverse=True)
        for i, g in enumerate(done[:keep_latest]):
            g["reasons"].append(f"latest {i + 1} of {keep_latest}")
        for g in groups:
            if not g["complete"] and now - g["created_at"] < grace_s:
                g["reasons"].append(f"incomplete, younger than {grace_s / 3600:g} h (may still be writing)")

    keep = sorted((g for g in runs.values() if g["reasons"]), key=lambda g: (g["ticket"], -g["created_at"]))
    delete = sorted((g for g in runs.values() if not g["reasons"]), key=lambda g: (g["ticket"], -g["created_at"]))
    refused = None
    if not state.ok:
        refused = "deployed / policy-server state could not be read: " + "; ".join(state.errors or ["unknown"])
        for g in delete:
            g["reasons"].append("kept: state unavailable (fail closed)")
        keep, delete = keep + delete, []
    return {
        "dry_run": True, "keep_latest": keep_latest, "grace_s": grace_s, "refused": refused,
        "never_touched": [BASE_CACHE_PATH, "openpi-cache/", "metrics/"],
        "keep": [_g(g) for g in keep], "delete": [_g(g) for g in delete],
        "delete_bytes": sum(g["size_bytes"] for g in delete), "keep_bytes": sum(g["size_bytes"] for g in keep),
        "storage_cost_usd_per_month_freed": round(sum(g["size_bytes"] for g in delete) / 2**30 * _storage_rate(), 4),
    }


def _storage_rate() -> float:
    try:
        return float(json.loads(_cost.RATES_FILE.read_text()).get("volume_storage_gib_month_usd") or 0.09)
    except (OSError, ValueError):
        return 0.09


def _g(g: dict) -> dict:
    return {"run_path": g["run_path"], "ticket": g["ticket"], "run_id": g["run_id"], "uris": g["uris"],
            "complete": g["complete"], "size_bytes": g["size_bytes"], "created_at": g["created_at"],
            "reasons": g["reasons"]}


def apply_cleanup(volume: VolumeLike, plan: Mapping[str, Any], *, records: list[dict],
                  state: State, log: Callable[[str], None] = print) -> list[str]:
    """Delete what ``plan`` lists, re-checking every guard against fresh ``records``/``state`` first."""
    if plan.get("refused") or not state.ok:
        raise RuntimeError(f"cleanup refused: {plan.get('refused') or state.errors}")
    fresh = plan_cleanup(records, state, keep_latest=int(plan["keep_latest"]), grace_s=float(plan["grace_s"]))
    allowed = {g["run_path"] for g in fresh["delete"]}
    removed = []
    for g in plan["delete"]:
        p = g["run_path"]
        if p not in allowed or not re.fullmatch(r"runs/ticket-[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", p):
            log(f"skip {p}: no longer deletable")
            continue
        volume.remove(p)
        removed.append(p)
        log(f"removed {p}")
    return removed


# --------------------------------------------------------------------------- download


def download_paths(rec: Mapping[str, Any], *, with_train_state: bool = False) -> list[str]:
    """Volume paths to fetch for local serving: the step dir minus ``train_state/`` (optimizer
    state, not needed to serve) plus the run's ``rdf_run.json``."""
    p = path_for(rec.get("uri") or "")
    if p is None:
        raise ValueError(f"{rec.get('uri')!r} is not on {VOLUME}")
    paths = [f"{p}/params", f"{p}/assets", f"{p}/_CHECKPOINT_METADATA"]
    if rec.get("has_policy_sidecar", True):
        paths.append(f"{p}/rdf_policy.json")
    if with_train_state:
        paths.append(f"{p}/train_state")
    if rec.get("run_path"):
        paths.append(f"{rec['run_path']}/rdf_run.json")
    return paths


__all__ = ["Entry", "ModalVolume", "State", "VolumeLike", "apply_cleanup", "base_record", "build", "download_paths",
           "for_ticket", "join", "path_for", "plan_cleanup", "read_policy_state", "resolve", "scan", "ticket_key",
           "uri_for"]
