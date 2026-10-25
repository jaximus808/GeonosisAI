"""TDM-21: run training jobs on Modal (app ``rdf-training``) and track them in ``training_run``.

Flow (driven by ``handlers/training.py``; every write is ``set_training_status``):

    training_run queued  --launch-->  spawn convert(ticket_id, episode_manifest)
                                      set running  modal_call_id=<convert call>  metrics.phase=convert
    poll (every RDF_MODAL_POLL_S, default 15 s): FunctionCall.from_id(id).get(timeout=0)
        convert returned dataset_uri  -> spawn finetune(ticket_id, dataset_uri, hyperparams)
                                         set running  modal_call_id=<finetune call>  metrics.phase=finetune
        finetune returned {checkpoint_uri, metrics}
                                      -> set done  checkpoint_uri, metrics_json, gpu_cost_cents
        a call raised / timed out     -> set failed  metrics_json.error (copied to training_run.error)

``hyperparams.dataset_uri`` (a ``modal-vol://rdf-datasets/...`` URI) skips convert and fine-tunes on
that dataset directly.

Restart safety: everything needed to resume lives on the row, never in process memory. The current
Modal call id is ``training_run.modal_call_id`` and ``metrics_json`` carries ``phase``,
``convert_call_id`` / ``finetune_call_id``, ``dataset_uri`` and spawn times; ``set_training_status``
also writes each step to ``event_log`` (``training.status``). On boot the bus snapshot fires
``on_enter(status == running)`` for every in-flight run and polling resumes from the stored call id.
Modal keeps call results, so a convert that finished while we were down is picked up and the
finetune is spawned then. The one gap: a crash in the ~100 ms between ``spawn()`` returning and the
row write leaves an orphan Modal call (a queued run spawns convert again; a running convert spawns
finetune again).

Idempotency (the poller thread, direct ``poll_once`` calls and bus resyncs can all reach one run):
* Every side effect on a finished call (cancel the Modal call, write done/failed, spawn the finetune) is
  done only by the caller that takes the run out of ``_tracked`` (one atomic pop under the lock); any
  other caller sees ``untracked`` and does nothing. ``poll_once`` on an untracked run is a no-op.
* The bus mirror lags our own writes (one websocket round trip; a whole reconnect if the socket is
  down). The runner remembers the call id it last wrote per run (``_expect``) and skips a poll while
  the mirror still shows the older one, so a convert that already spawned a finetune is never polled
  again (no second finetune), and a run it moved out of ``queued`` is not launched twice. A run it set
  done/failed is never tracked again.

GPU cost (TDM-42, ``rdf_training.cost``): ``gpu_cost_cents`` = the run's Modal compute cost, runtime x
Modal's own rates (``services/training/config/modal_rates.json``, a snapshot of ``modal billing rates``;
``RDF_GPU_RATES_JSON`` e.g. ``{"H100": 395}`` still overrides GPU cents/h) for the GPU plus the finetune
container's reserved CPU and memory. The breakdown and its label (``source: estimate``,
``method: runtime_x_modal_rate``) go into ``metrics_json.cost``. Runtime is the finetune's reported
``gpu_seconds`` / ``metrics.wall_s`` if present, else wall clock since the finetune was spawned. The
GPU type is ``metrics.gpu`` from the finetune (nvidia-smi name; the TDM-38 stub reports ``null`` = no
GPU, cost 0), else ``RDF_TRAINING_GPU`` (default H100). Modal's billing report is per App per hour,
so it cannot be pinned to one run on the shared deployed App; ``rdf_training.cost.reconcile`` checks
the estimate against it.

``RDF_MODAL_STUB=1`` (TDM-28 harness, local SpacetimeDB only): nothing is spawned. A queued run is
set running with modal_call_id ``stub-<hex>`` and ``metrics.fake = true``; the harness's
``tests/integration/stub_modal.StubTrainer`` (mode ``stub-call``) finishes it with a ``fake://``
checkpoint. Polling ignores ``stub-`` call ids.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .log import get_logger

log = get_logger("modal_client")

APP_NAME = "rdf-training"
STUB_PREFIX = "stub-"
DEFAULT_POLL_S = 15.0
MAX_TRANSIENT_POLL_ERRORS = 8  # consecutive Modal client/network errors before a run is failed

# Modal list prices (USD/h -> cents/h), Oct 2026. Override with RDF_GPU_RATES_JSON.
GPU_CENTS_PER_HOUR: dict[str, float] = {
    "H100": 395, "H200": 454, "B200": 625, "A100-80GB": 250, "A100": 210, "A100-40GB": 210,
    "L40S": 195, "A10G": 110, "L4": 80, "T4": 59,
}


def stub_mode() -> bool:
    return os.environ.get("RDF_MODAL_STUB") == "1"


STUB_LABEL = "[STUB] no GPU"
_stub_notice_sent = False


def note_stub_on_shared_db(writer: Any = None) -> None:
    """``RDF_MODAL_STUB=1`` against a shared database (maincloud) is allowed when set explicitly (the demo
    saves Modal credit), but never silently: a warning in the log and ONE ``modal.stub_on_maincloud`` row in
    event_log per process (background thread, retried). Training rows it finishes are labelled STUB_LABEL."""
    global _stub_notice_sent
    if _stub_notice_sent:
        return
    _stub_notice_sent = True
    host = os.environ.get("SPACETIME_HOST", "")
    log.warning("modal.stub_on_maincloud", extra={"fields": {"host": host, "label": STUB_LABEL}})

    def go() -> None:
        import json as _json
        import time as _time

        for attempt in range(6):
            try:
                w = writer() if callable(writer) else writer
                if w is None:
                    from rdf_common.settings import OrchestratorSettings, load_settings
                    from rdf_common.spacetime_client import SpacetimeClient

                    w = SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)
                w.call_reducer("append_event", "orchestrator", "modal.stub_on_maincloud", _json.dumps({
                    "host": host, "db": os.environ.get("SPACETIME_DB_NAME", ""), "label": STUB_LABEL,
                    "note": "RDF_MODAL_STUB=1 on a shared database: training runs are STUBS (no GPU, no Modal credit); "
                            "their checkpoints are fake and labelled so"}))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("modal.stub_notice_retry", extra={"fields": {"attempt": attempt, "error": str(e)[:200]}})
                _time.sleep(min(30.0, 2.0 * 2 ** attempt))

    import threading as _threading

    _threading.Thread(target=go, name="modal-stub-notice", daemon=True).start()


def poll_interval() -> float:
    try:
        return max(0.5, float(os.environ.get("RDF_MODAL_POLL_S", DEFAULT_POLL_S)))
    except ValueError:
        return DEFAULT_POLL_S


def gpu_rates() -> dict[str, float]:
    rates = dict(GPU_CENTS_PER_HOUR)
    extra = os.environ.get("RDF_GPU_RATES_JSON", "").strip()
    if extra:
        rates.update({str(k): float(v) for k, v in json.loads(extra).items()})
    return rates


def gpu_cost_cents(seconds: float, gpu: Any, *, gpus: int = 1, rates: dict[str, float] | None = None) -> int:
    """Runtime x GPU list price, rounded to whole cents. gpu None/"" = CPU only (0)."""
    if not gpu or seconds <= 0:
        return 0
    rates = rates or gpu_rates()
    name = str(gpu).split(":")[0]
    rate = rates.get(name, rates.get(name.upper()))
    if rate is None:
        log.warning("training.unknown_gpu_rate", extra={"fields": {"gpu": gpu}})
        rate = rates["H100"]  # never under-report spend
    return int(round(seconds / 3600.0 * rate * max(1, gpus)))


# --------------------------------------------------------------------------- Modal backend

@dataclass
class PollResult:
    state: str  # pending | done | failed | transient
    value: Any = None
    error: str | None = None


class ModalBackend(Protocol):
    def spawn(self, function: str, **kwargs: Any) -> str: ...
    def poll(self, call_id: str) -> PollResult: ...
    def cancel(self, call_id: str) -> None: ...


class RealModal:
    """``modal.Function.from_name(APP_NAME, fn).spawn(...)`` / ``FunctionCall.from_id(id).get(timeout=0)``."""

    def __init__(self, app_name: str = APP_NAME) -> None:
        self.app_name = app_name
        self._fns: dict[str, Any] = {}

    def spawn(self, function: str, **kwargs: Any) -> str:
        import modal

        fn = self._fns.get(function)
        if fn is None:
            fn = self._fns[function] = modal.Function.from_name(self.app_name, function)
        return fn.spawn(**kwargs).object_id

    def poll(self, call_id: str) -> PollResult:
        import modal
        import modal.exception as mx

        try:
            value = modal.FunctionCall.from_id(call_id).get(timeout=0)
        except (mx.FunctionTimeoutError, mx.OutputExpiredError) as e:  # both subclass mx.TimeoutError
            return PollResult("failed", error=f"{type(e).__name__}: {e}")
        except (mx.TimeoutError, TimeoutError):
            return PollResult("pending")
        except (mx.ConnectionError, mx.ClientClosed, ConnectionError) as e:
            return PollResult("transient", error=f"{type(e).__name__}: {e}")
        except BaseException as e:  # noqa: BLE001 - remote exceptions are re-raised here; InputCancellation is a BaseException
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return PollResult("failed", error=f"{type(e).__name__}: {e}"[:2000])
        return PollResult("done", value=value)

    def cancel(self, call_id: str) -> None:
        import modal

        modal.FunctionCall.from_id(call_id).cancel()


# --------------------------------------------------------------------------- manifest

_pending_every = None


def _narrate_pending(run: dict, phase: str, m: dict) -> None:
    """AgentWorkbench heartbeat while Modal works: one ``agent.step`` per run every 20 s."""
    global _pending_every
    try:
        from . import agent_steps as ag

        if _pending_every is None:
            _pending_every = ag.Every(20.0)
        if not _pending_every.due(run.get("id")):
            return
        since = float(m.get(f"{phase}_spawned_at") or 0)
        took = f" · {ag.clock(time.time() - since)}" if since else ""
        words = "Converting episodes to a LeRobot dataset on Modal" if phase == "convert" else "Fine-tune running on Modal"
        ag.step(words + took, ticket_id=run.get("ticket_id"), station="train", training_run_id=run.get("id"),
                phase=phase)
    except Exception:  # noqa: BLE001 - narration never breaks the poller
        pass


def build_manifest(run: dict, ticket: dict | None, episodes: dict[int, dict], robots: dict[int, dict] | None = None) -> dict:
    """TDM-39 ``ConvertManifest`` (as a dict) for a run: its accepted episodes, the ticket's prompt.

    Only accepted episodes are included (``queue_training_run`` already refuses others). ``repo_id``
    is per run (``rdf/ticket-<t>-run-<id>``) so a retrain never overwrites a dataset being read.
    """
    from .robot_mode import row_mode  # TDM-107: each episode's sim | real label (convert ignores the key)

    hp = _json(run.get("hyperparams_json"))
    eps, missing = [], []
    for eid in run.get("episode_ids") or []:
        ep = episodes.get(int(eid))
        if ep is None:
            missing.append(int(eid))
            continue
        if ep.get("review_status") != "accepted":
            continue
        eps.append({
            "episode_id": str(ep["id"]), "kind": ep.get("kind") or "clean_teleop",
            "cameras_json": ep.get("cameras_json") or "{}", "actions_uri": ep.get("actions_uri") or "",
            "takeover_ts": ep.get("takeover_ts"), "handback_ts": ep.get("handback_ts"),
            "robot_mode": row_mode(ep, robots or {}),
        })
    if missing:
        raise ValueError(f"episodes {missing} of training run {run['id']} not found")
    task = str(hp.get("prompt") or (ticket or {}).get("task_description") or "").strip()
    manifest: dict[str, Any] = {"repo_id": f"rdf/ticket-{run['ticket_id']}-run-{run['id']}", "task": task,
                                "episodes": eps}
    for k in ("fps", "base_mix_ratio", "image_size", "keep_after_handback"):
        if k in hp:
            manifest[k] = hp[k]
    return manifest


def _json(text: Any) -> dict:
    if isinstance(text, dict):
        return dict(text)
    try:
        v = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _finetune_hyperparams(run: dict) -> dict:
    hp = _json(run.get("hyperparams_json"))
    hp.pop("dataset_uri", None)
    hp.setdefault("base_checkpoint_uri", run.get("base_checkpoint_uri"))
    hp.setdefault("training_run_id", int(run["id"]))
    return hp


# --------------------------------------------------------------------------- runner

class Writer(Protocol):
    def call_reducer(self, name: str, /, *args: Any) -> None: ...


class TrainingRunner:
    """Launch + poll training runs. ``rows`` reads the bus mirror: rows(table) -> list[dict]."""

    def __init__(self, *, writer: Callable[[], Writer], modal: Callable[[], ModalBackend] | None = None,
                 poll_s: float | None = None) -> None:
        self._writer_factory, self._modal_factory = writer, modal or RealModal
        self._writer: Writer | None = None
        self._modal: ModalBackend | None = None
        self.poll_s = poll_s
        self._lock = threading.Lock()
        self._launching: set[int] = set()
        self._tracked: dict[int, int] = {}  # run id -> consecutive transient errors
        self._busy: set[int] = set()
        self._expect: dict[int, str] = {}  # run id -> modal_call_id we last wrote (mirror may lag it)
        self._finished: set[int] = set()  # runs this runner set done/failed
        self._get_row: Callable[[int], dict | None] | None = None
        self._rows: Callable[[str], list] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- deps
    @property
    def writer(self) -> Writer:
        if self._writer is None:
            self._writer = self._writer_factory()
        return self._writer

    @property
    def modal(self) -> ModalBackend:
        if self._modal is None:
            self._modal = self._modal_factory()
        return self._modal

    def bind(self, bus) -> None:
        self._get_row = lambda rid: bus.get("training_run", rid)
        self._rows = bus.rows

    def _row(self, rid: int, fallback: dict | None = None) -> dict | None:
        r = self._get_row(rid) if self._get_row else None
        return dict(r) if r is not None else (dict(fallback) if fallback is not None else None)

    def _set(self, rid: int, status: str, *, call_id: str | None = None, ckpt: str | None = None,
             metrics: dict | None = None, cost: int | None = None) -> None:
        self.writer.call_reducer("set_training_status", rid, status, call_id, ckpt,
                                 json.dumps(metrics, default=str) if metrics is not None else None, cost)
        with self._lock:
            if status == "running" and call_id:
                self._expect[rid] = call_id
            elif status in ("done", "failed", "cancelled"):
                self._expect.pop(rid, None)
                self._finished.add(rid)

    # ---- queued -> running
    def launch(self, row: dict) -> None:
        rid = int(row["id"])
        with self._lock:
            if rid in self._launching or rid in self._expect or rid in self._finished:
                return  # already launching, or we already moved it out of queued (the mirror lags)
            self._launching.add(rid)
        try:
            cur = self._row(rid, row)
            if cur is None or cur.get("status") != "queued":
                return
            if stub_mode():
                self._launch_stub(cur)
                return
            self._launch_real(cur)
        finally:
            with self._lock:
                self._launching.discard(rid)

    def _launch_stub(self, run: dict) -> None:
        call_id = f"{STUB_PREFIX}{uuid.uuid4().hex[:10]}"
        log.warning("training.stub_running", extra={"fields": {"training_run_id": run["id"], "modal_call_id": call_id}})
        self._set(int(run["id"]), "running", call_id=call_id,
                  metrics={"fake": True, "stub": "RDF_MODAL_STUB", "phase": "stub", "label": STUB_LABEL})

    def _launch_real(self, run: dict) -> None:
        rid, tid = int(run["id"]), int(run["ticket_id"])
        hp = _json(run.get("hyperparams_json"))
        try:
            if run.get("modal_call_id"):  # someone stored a call while queued: adopt it as the convert
                self._set(rid, "running", call_id=run["modal_call_id"],
                          metrics={"phase": "convert", "convert_call_id": run["modal_call_id"],
                                   "convert_spawned_at": time.time()})
                return
            if str(hp.get("dataset_uri") or "").strip():
                self._spawn_finetune(run, str(hp["dataset_uri"]).strip(), {"convert": "skipped"})
                return
            rows = self._rows or (lambda _t: [])
            episodes = {int(e["id"]): dict(e) for e in rows("episode")}
            ticket = next((dict(t) for t in rows("ticket") if int(t["id"]) == tid), None)
            try:  # TDM-107 sim / real label; a mirror without the robot table just leaves it unknown
                robots = {int(r["id"]): dict(r) for r in rows("robot")}
            except (KeyError, TypeError, ValueError):
                robots = {}
            manifest = build_manifest(run, ticket, episodes, robots)
            call_id = self.modal.spawn("convert", ticket_id=str(tid), episode_manifest=manifest)
        except Exception as e:  # noqa: BLE001 - a launch error fails the run, visibly
            self._fail(run, f"launch failed: {type(e).__name__}: {e}", {"phase": "launch"})
            return
        log.info("training.convert_spawned", extra={"fields": {
            "training_run_id": rid, "modal_call_id": call_id, "episodes": len(manifest["episodes"]),
            "repo_id": manifest["repo_id"]}})
        self._set(rid, "running", call_id=call_id, metrics={
            "phase": "convert", "convert_call_id": call_id, "convert_spawned_at": time.time(),
            "repo_id": manifest["repo_id"], "episodes": len(manifest["episodes"])})

    def _spawn_finetune(self, run: dict, dataset_uri: str, prior: dict) -> None:
        rid = int(run["id"])
        call_id = self.modal.spawn("finetune", ticket_id=str(run["ticket_id"]), dataset_uri=dataset_uri,
                                   hyperparams=_finetune_hyperparams(run))
        log.info("training.finetune_spawned", extra={"fields": {
            "training_run_id": rid, "modal_call_id": call_id, "dataset_uri": dataset_uri}})
        self._set(rid, "running", call_id=call_id, metrics={
            **prior, "phase": "finetune", "dataset_uri": dataset_uri, "finetune_call_id": call_id,
            "finetune_spawned_at": time.time()})

    def _fail(self, run: dict, error: str, metrics: dict, cost: int | None = None) -> None:
        log.error("training.failed", extra={"fields": {"training_run_id": run["id"], "error": error[:500]}})
        self._set(int(run["id"]), "failed", metrics={**metrics, "error": error[:2000]}, cost=cost)

    # ---- running: poll
    def track(self, row: dict) -> None:
        call_id = str(row.get("modal_call_id") or "")
        if not call_id or call_id.startswith(STUB_PREFIX):
            return  # stub runs are finished by the TDM-28 StubTrainer
        with self._lock:
            if int(row["id"]) in self._finished:
                return  # a late/stale running event for a run we already finished
            new = int(row["id"]) not in self._tracked
            self._tracked.setdefault(int(row["id"]), 0)
            self._ensure_thread()
        if new:
            log.info("training.tracking", extra={"fields": {"training_run_id": row["id"], "modal_call_id": call_id}})

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="tdm21-modal-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s or poll_interval()):
            with self._lock:
                ids = list(self._tracked)
            for rid in ids:
                try:
                    self.poll_once(rid)
                except Exception:  # noqa: BLE001 - keep polling the others; the next tick retries
                    log.exception("training.poll_error", extra={"fields": {"training_run_id": rid}})

    def poll_once(self, rid: int) -> str:
        """One poll of one run. Returns what happened (for tests and logs). No-op for an untracked run."""
        with self._lock:
            if rid not in self._tracked:
                return "untracked"
            if rid in self._busy:
                return "busy"
            self._busy.add(rid)
        try:
            return self._poll(rid)
        finally:
            with self._lock:
                self._busy.discard(rid)

    def _untrack(self, rid: int) -> bool:
        """Stop tracking. True only for the one caller that actually removed it (it owns the side effects)."""
        with self._lock:
            return self._tracked.pop(rid, None) is not None

    def _poll(self, rid: int) -> str:
        run = self._row(rid)
        if run is None:
            self._untrack(rid)
            return "gone"
        call_id = str(run.get("modal_call_id") or "")
        m = _json(run.get("metrics_json"))
        if run.get("status") != "running" or not call_id or call_id.startswith(STUB_PREFIX):
            if not self._untrack(rid):
                return "untracked"  # someone else already handled it
            if run.get("status") == "cancelled" and call_id and not call_id.startswith(STUB_PREFIX):
                try:
                    self.modal.cancel(call_id)
                    log.info("training.cancelled_modal", extra={"fields": {"training_run_id": rid, "modal_call_id": call_id}})
                except Exception as e:  # noqa: BLE001
                    log.warning("training.cancel_failed", extra={"fields": {"training_run_id": rid, "error": str(e)}})
            return "untracked"
        with self._lock:
            expected = self._expect.get(rid)
            if expected is not None and expected == call_id:
                self._expect.pop(rid, None)  # the mirror has caught up with our write
        if expected is not None and expected != call_id:
            return "stale"  # the mirror still shows the call we already moved past; next tick re-reads
        phase = m.get("phase") or ("convert" if m.get("convert_call_id") == call_id else "finetune")
        res = self.modal.poll(call_id)
        if res.state == "pending":
            with self._lock:
                if rid in self._tracked:
                    self._tracked[rid] = 0
            _narrate_pending(run, phase, m)
            return "pending"
        if res.state == "transient":
            with self._lock:
                n = self._tracked.get(rid, 0) + 1
                self._tracked[rid] = n
            if n < MAX_TRANSIENT_POLL_ERRORS:
                log.warning("training.poll_transient", extra={"fields": {"training_run_id": rid, "n": n, "error": res.error}})
                return "transient"
            res = PollResult("failed", error=f"Modal unreachable for {n} polls: {res.error}")
        if not self._untrack(rid):
            return "untracked"  # another poller of this runner finished it while we waited on Modal
        cur = self._row(rid)  # re-read: a cancel, or another writer, may have landed during the Modal poll
        if cur is None or cur.get("status") != "running" or str(cur.get("modal_call_id") or "") != call_id:
            with self._lock:
                self._tracked.setdefault(rid, 0)  # the next tick handles whatever it is now (e.g. cancel)
            return "raced"
        if res.state == "failed":
            c = self._cost_breakdown(m, {}) if phase == "finetune" else None  # GPU time burnt before the failure
            self._fail(run, f"{phase} failed: {res.error}", {**m, "phase": phase, "failed_call_id": call_id,
                                                              **({"cost": c} if c else {})},
                       c["total_cents"] if c else None)
            return "failed"
        if phase == "convert":
            uri = res.value if isinstance(res.value, str) else (res.value or {}).get("dataset_uri")
            if not uri:
                self._fail(run, f"convert returned no dataset_uri: {res.value!r}"[:500], {**m, "phase": "convert"})
                return "failed"
            prior = {k: v for k, v in m.items() if k not in ("phase",)}
            prior["convert_s"] = round(time.time() - float(m.get("convert_spawned_at") or time.time()), 1)
            try:
                self._spawn_finetune(run, uri, prior)
            except Exception as e:  # noqa: BLE001
                self._fail(run, f"finetune spawn failed: {type(e).__name__}: {e}", {**m, "dataset_uri": uri})
                return "failed"
            with self._lock:
                self._tracked.setdefault(rid, 0)
            return "finetune_spawned"
        # finetune finished
        out = res.value if isinstance(res.value, dict) else {}
        ckpt = str(out.get("checkpoint_uri") or "").strip()
        fm = out.get("metrics") if isinstance(out.get("metrics"), dict) else {}
        if not ckpt:
            self._fail(run, f"finetune returned no checkpoint_uri: {res.value!r}"[:500], {**m, "phase": "finetune"})
            return "failed"
        c = self._cost_breakdown(m, fm, out)
        cost = int(c["total_cents"])
        metrics = {**fm, "modal": {k: v for k, v in m.items() if k != "phase"} | {"phase": "done"}, "cost": c}
        log.info("training.done", extra={"fields": {"training_run_id": rid, "checkpoint_uri": ckpt, "gpu_cost_cents": cost}})
        self._set(rid, "done", ckpt=ckpt, metrics=metrics, cost=cost)
        return "done"

    @staticmethod
    def _cost_breakdown(m: dict, fm: dict, out: dict | None = None) -> dict:
        """TDM-42: runtime x Modal rates (GPU + reserved CPU/memory), labelled ``source: estimate``."""
        from rdf_training import cost as tdm42_cost

        seconds = tdm42_cost.run_seconds(fm, out)
        measured = seconds is not None
        if seconds is None:
            seconds = time.time() - float(m.get("finetune_spawned_at") or time.time())
        gpu = fm["gpu"] if "gpu" in fm else os.environ.get("RDF_TRAINING_GPU", "H100")
        c = tdm42_cost.estimate(seconds, gpu, gpus=int(fm["gpu_count"]) if fm.get("gpu_count") else None)
        c["runtime_from"] = "finetune_reported" if measured else "orchestrator_wall_clock_since_spawn"
        return c

    @classmethod
    def _cost(cls, m: dict, fm: dict, out: dict | None = None) -> int:
        return int(cls._cost_breakdown(m, fm, out)["total_cents"])
