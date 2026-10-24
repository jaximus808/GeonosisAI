"""TDM-42: what a training run cost on Modal, and where that number came from.

Two sources, always labelled (``source`` in every breakdown):

``estimate`` (method ``runtime_x_modal_rate``)
    The finetune's runtime x Modal's own price list. The price list is
    ``services/training/config/modal_rates.json``, a snapshot of ``modal billing rates --json``
    (refresh with ``make modal-rates``); ``RDF_GPU_RATES_JSON`` (cents per GPU-hour, e.g.
    ``{"H100": 395}``, TDM-21's override) still wins for GPUs. Modal bills a container's
    reserved CPU and memory for as long as it runs, so the estimate is
    ``seconds x (gpu_rate x gpus + cpu_cores x cpu_rate + memory_gib x mem_rate)`` with the
    finetune function's reservation (16 cores, 96 GiB in ``modal_app.py``). The GPU part on its
    own is ``gpu_cents = gpu_seconds x gpu_rate``.

``modal_billing``
    ``Workspace.billing.report(resolution="h")``: what Modal actually billed. Its finest grain is
    one App per UTC hour (per resource: CPU, Memory, H100, ...), not one function call. So it is
    exact for a run that had its App to itself (an ephemeral ``modal run`` App, like the TDM-40
    smoke runs) and only an upper bound for runs spawned on the deployed ``rdf-training`` App,
    which also hosts the ``Policy`` server (same GPU type) and ``convert``. :func:`reconcile`
    says which (``attribution``), and only an exclusive, complete bill replaces the estimate.
    Billing lags: an hour shows up once it has closed (``complete`` says whether it has).

``training_run.gpu_cost_cents`` holds ``total_cents``: the run's whole Modal compute bill
(GPU + its reserved CPU and memory), because that is what counts against the ticket budget
(flywheel budget stop: ``ticket.spent_cents`` (payouts) + sum of ``gpu_cost_cents``). The
breakdown goes into ``training_run.metrics_json.cost``.

Pure stdlib: the orchestrator imports this (``modal_client``) and the Modal SDK is only touched
by :func:`billing_report` / :func:`refresh_rates_file`, lazily.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

RATES_FILE = pathlib.Path(__file__).resolve().parents[2] / "config" / "modal_rates.json"

# Fallback if the config file is missing (Modal list prices, Oct 2026, USD/h).
_FALLBACK = {
    "gpu_usd_per_hour": {"H100": 3.95, "H200": 4.54, "B200": 6.25, "B300": 7.10, "A100-80GB": 2.50,
                         "A100-40GB": 2.10, "L40S": 1.95, "A10G": 1.10, "L4": 0.80, "T4": 0.59, "RTX6000": 3.03},
    "cpu_core_usd_per_hour": 0.0473,
    "mem_gib_usd_per_hour": 0.008,
    "as_of": "builtin fallback (Oct 2026 list prices)",
}

# modal_app.finetune's reservation (gpu="H100", cpu=16, memory=98304 MiB). Keep in sync.
FINETUNE_RESOURCES = {"gpu": "H100", "cpu": 16.0, "memory_gib": 96.0}

GPU_NAMES = ("H100", "H200", "B200", "B300", "A100-80GB", "A100-40GB", "L40S", "A10G", "L4", "T4", "RTX6000")


# --------------------------------------------------------------------------- rates


@dataclass(frozen=True)
class Rates:
    gpu_cents_per_hour: dict[str, float]
    cpu_core_cents_per_hour: float
    mem_gib_cents_per_hour: float
    as_of: str
    source: str  # "config:<path>" | "builtin" (+ "+RDF_GPU_RATES_JSON")

    def gpu(self, name: str) -> float | None:
        return self.gpu_cents_per_hour.get(name)


def _rates_from_dict(d: Mapping[str, Any], source: str) -> Rates:
    return Rates(
        gpu_cents_per_hour={str(k): float(v) * 100 for k, v in (d.get("gpu_usd_per_hour") or {}).items()},
        cpu_core_cents_per_hour=float(d.get("cpu_core_usd_per_hour") or 0) * 100,
        mem_gib_cents_per_hour=float(d.get("mem_gib_usd_per_hour") or 0) * 100,
        as_of=str(d.get("as_of") or ""), source=source)


def load_rates(path: str | os.PathLike | None = None) -> Rates:
    """The config file (``RDF_MODAL_RATES_FILE`` or :data:`RATES_FILE`), else the builtin list,
    then ``RDF_GPU_RATES_JSON`` (cents/h per GPU) on top."""
    p = pathlib.Path(path or os.environ.get("RDF_MODAL_RATES_FILE") or RATES_FILE)
    try:
        rates = _rates_from_dict(json.loads(p.read_text()), f"config:{p.name}")
    except (OSError, ValueError):
        rates = _rates_from_dict(_FALLBACK, "builtin")
    extra = os.environ.get("RDF_GPU_RATES_JSON", "").strip()
    if extra:
        gpu = dict(rates.gpu_cents_per_hour)
        gpu.update({normalize_gpu(k)[0] or str(k): float(v) for k, v in json.loads(extra).items()})
        rates = Rates(gpu, rates.cpu_core_cents_per_hour, rates.mem_gib_cents_per_hour, rates.as_of,
                      rates.source + "+RDF_GPU_RATES_JSON")
    return rates


def rates_from_modal_cli(raw: Mapping[str, Any], *, fetched_at: str | None = None) -> dict:
    """``modal billing rates --json`` -> the config file's shape."""
    gpu = {}
    for k, v in raw.items():
        m = re.fullmatch(r"gpu_hour_cost_(.+)", k)
        if m:
            name = normalize_gpu(m.group(1))[0] or m.group(1).upper()
            gpu[name] = float(v)
    return {
        "source": "modal billing rates --json (Modal's published list prices)",
        "as_of": fetched_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gpu_usd_per_hour": dict(sorted(gpu.items())),
        "cpu_core_usd_per_hour": float(raw.get("cpu_hour_cost") or 0),
        "mem_gib_usd_per_hour": float(raw.get("mem_gib_hour_cost") or 0),
        "volume_storage_gib_month_usd": float(raw.get("volume_storage_gib_month_cost") or 0),
        "note": "Read by rdf_training.cost (TDM-42). Refresh: make modal-rates.",
    }


def refresh_rates_file(path: str | os.PathLike = RATES_FILE) -> dict:
    out = subprocess.run(["modal", "billing", "rates", "--json"], capture_output=True, text=True, timeout=60, check=True)
    cfg = rates_from_modal_cli(json.loads(out.stdout))
    pathlib.Path(path).write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


# --------------------------------------------------------------------------- GPU names


def normalize_gpu(raw: Any) -> tuple[str | None, int]:
    """Any GPU spelling -> (Modal billing name, count).

    ``"H100"``, ``"h100:2"``, ``"a100_80gb"``, nvidia-smi's ``"NVIDIA H100 80GB HBM3, 81559 MiB"`` or
    ``"NVIDIA A100-SXM4-40GB"``. None / "" -> (None, 0) = no GPU. Unknown -> (raw upper, n)."""
    if raw is None:
        return None, 0
    s = str(raw).strip()
    if not s or s.lower() in ("none", "null", "cpu"):
        return None, 0
    count = 1
    m = re.fullmatch(r"(.+?):(\d+)", s)
    if m:
        s, count = m.group(1), int(m.group(2))
    u = s.upper().replace("_", "-").replace("NVIDIA ", "")
    u = u.split(",")[0].strip()
    if "A100" in u:
        if "40" in u.replace("A100", ""):
            return "A100-40GB", count
        return ("A100-80GB" if "80" in u.replace("A100", "") else "A100-40GB"), count
    for name in ("H200", "H100", "B300", "B200", "L40S", "A10G", "L4", "T4"):
        if re.search(rf"(^|[^A-Z0-9]){name}([^A-Z0-9]|$)", u) or u.startswith(name):
            return name, count
    if "RTX" in u and "6000" in u:
        return "RTX6000", count
    return u, count


# --------------------------------------------------------------------------- estimate


def _c(x: float) -> float:
    return round(x, 4)


def estimate(seconds: float, gpu: Any, *, gpus: int | None = None, cpu: float | None = None,
             memory_gib: float | None = None, rates: Rates | None = None) -> dict:
    """Runtime x Modal rate. ``cpu`` / ``memory_gib`` None -> the finetune reservation when a GPU
    is set (0 for a CPU-only call, which TDM-38's stub reports as ``gpu: null``)."""
    rates = rates or load_rates()
    name, n = normalize_gpu(gpu)
    n = int(gpus or n or (1 if name else 0))
    seconds = max(0.0, float(seconds or 0))
    hours = seconds / 3600.0
    warn = None
    rate = rates.gpu(name) if name else 0.0
    if name and rate is None:
        warn = f"no rate for GPU {name!r}; charged at the H100 rate (never under-report spend)"
        rate = rates.gpu("H100") or _FALLBACK["gpu_usd_per_hour"]["H100"] * 100
    if cpu is None:
        cpu = FINETUNE_RESOURCES["cpu"] if name else 0.0
    if memory_gib is None:
        memory_gib = FINETUNE_RESOURCES["memory_gib"] if name else 0.0
    gpu_c = hours * float(rate or 0) * n
    cpu_c = hours * float(cpu) * rates.cpu_core_cents_per_hour
    mem_c = hours * float(memory_gib) * rates.mem_gib_cents_per_hour
    out = {
        "source": "estimate", "method": "runtime_x_modal_rate",
        "seconds": round(seconds, 1), "gpu": name, "gpu_count": n,
        "gpu_rate_cents_per_hour": rate, "cpu_cores": cpu, "memory_gib": memory_gib,
        "gpu_cents": _c(gpu_c), "cpu_cents": _c(cpu_c), "mem_cents": _c(mem_c),
        "gpu_cost_cents": int(round(gpu_c)),
        "total_cents": int(round(gpu_c + cpu_c + mem_c)),
        "rates": {"source": rates.source, "as_of": rates.as_of},
        "formula": (f"{seconds:.1f} s x ({rate:g}c/h x {n} GPU + {cpu:g} cores x {rates.cpu_core_cents_per_hour:g}c/h"
                    f" + {memory_gib:g} GiB x {rates.mem_gib_cents_per_hour:g}c/h) / 3600"),
    }
    if warn:
        out["warning"] = warn
    return out


def run_seconds(metrics: Mapping[str, Any], result: Mapping[str, Any] | None = None) -> float | None:
    """The finetune's own runtime: top-level ``gpu_seconds`` (TDM-40 result) > ``metrics.gpu_seconds``
    > ``metrics.wall_s``. None when the run reported none."""
    for src, key in ((result or {}, "gpu_seconds"), (metrics, "gpu_seconds"), (metrics, "wall_s")):
        v = src.get(key)
        if v not in (None, ""):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------- Modal billing


def _utc(ts: float | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def billing_report(start: datetime, end: datetime | None = None) -> list[dict]:
    """Hourly per-App rows from Modal (``Workspace.billing.report``), as plain dicts."""
    import modal

    rows = modal.Workspace.from_context().billing.report(start=start, end=end, resolution="h")
    return [{"object_id": r.object_id, "description": r.description, "interval_start": r.interval_start,
             "cost": str(r.cost), "cost_by_resource": {k: str(v) for k, v in r.cost_by_resource.items()}}
            for r in rows]


def billed(app_id: str, start_ts: float | datetime, end_ts: float | datetime, *,
           report: Callable[[datetime, datetime | None], list[dict]] = billing_report,
           now: float | None = None) -> dict:
    """What Modal billed App ``app_id`` in the UTC hours touching [start, end]."""
    start, end = _utc(start_ts), _utc(end_ts)
    h0, h1 = _hour(start), _hour(end)
    now_dt = _utc(now if now is not None else time.time())
    rows = report(h0, h1 + timedelta(hours=1))
    hours: dict[str, dict] = {}
    by_res: dict[str, float] = {}
    total = 0.0
    for r in rows:
        if r.get("object_id") != app_id:
            continue
        cents = float(r.get("cost") or 0) * 100
        total += cents
        res = {k: float(v) * 100 for k, v in (r.get("cost_by_resource") or {}).items()}
        for k, v in res.items():
            by_res[k] = by_res.get(k, 0.0) + v
        hours[_utc(r["interval_start"]).isoformat()] = {"cents": _c(cents),
                                                         "by_resource_cents": {k: _c(v) for k, v in res.items()}}
    gpu_c = sum(v for k, v in by_res.items() if normalize_gpu(k)[0] in GPU_NAMES)
    return {
        "source": "modal_billing", "app_id": app_id,
        "window": [start.isoformat(), end.isoformat()],
        "hours": hours, "by_resource_cents": {k: _c(v) for k, v in sorted(by_res.items())},
        "gpu_cents": _c(gpu_c), "total_cents": int(round(total)),
        # Modal reports full hours only; the last hour is in once it has closed.
        "complete": now_dt >= h1 + timedelta(hours=1),
        "granularity": "app x UTC hour",
    }


def reconcile(est: Mapping[str, Any], bill: Mapping[str, Any] | None, *, exclusive: bool) -> dict:
    """Pick the number that goes into ``gpu_cost_cents``.

    ``exclusive`` = the App ran only this training run in those hours (an ephemeral App). Only an
    exclusive, complete bill is used; otherwise the estimate stands and the bill is attached as a
    check (``billing_check``, an upper bound when the App was shared)."""
    out = dict(est)
    if not bill:
        return out
    check = {**bill, "attribution": "exclusive" if exclusive else "shared_app_upper_bound",
             "delta_vs_estimate_cents": int(bill.get("total_cents", 0)) - int(est.get("total_cents", 0))}
    if exclusive and bill.get("complete") and bill.get("hours"):
        return {**out, "source": "modal_billing", "method": "app_hour_report_exclusive",
                "total_cents": int(bill["total_cents"]), "gpu_cents": bill["gpu_cents"],
                "gpu_cost_cents": int(round(float(bill["gpu_cents"]))), "estimate": dict(est), "billing": check}
    return {**out, "billing_check": check}


def cost_label(c: Mapping[str, Any] | None) -> str:
    """One line for the report: ``$0.71 (estimate: 482.8 s x Modal rates; GPU $0.53)``."""
    if not c:
        return "no cost breakdown recorded"
    usd = lambda cents: f"${float(cents) / 100:.2f}"  # noqa: E731
    if c.get("source") == "modal_billing":
        return f"{usd(c.get('total_cents', 0))} (Modal billing, app {c.get('billing', {}).get('app_id')}; GPU {usd(c.get('gpu_cents', 0))})"
    return (f"{usd(c.get('total_cents', 0))} (estimate: {c.get('seconds')} s x Modal rates as of "
            f"{(c.get('rates') or {}).get('as_of', '?')}; GPU {c.get('gpu')} {usd(c.get('gpu_cents', 0))})")


def ticket_spend(training_runs: Iterable[Mapping[str, Any]]) -> dict:
    """Sum of ``gpu_cost_cents`` over a ticket's runs, split by cost source (the flywheel's GPU term)."""
    by_source: dict[str, int] = {}
    total = 0
    for r in training_runs:
        cents = int(r.get("gpu_cost_cents") or 0)
        total += cents
        try:
            m = json.loads(r.get("metrics_json") or "{}")
        except (TypeError, ValueError):
            m = {}
        src = ((m.get("cost") or {}).get("source") if isinstance(m, dict) else None) or (
            "unlabelled" if cents else "none")
        by_source[src] = by_source.get(src, 0) + cents
    return {"gpu_cost_cents": total, "by_source_cents": by_source}
