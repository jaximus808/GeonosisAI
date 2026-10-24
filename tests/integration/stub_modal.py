"""Stub Modal fine-tune for the TDM-28 harness: finishes training runs with a FAKE checkpoint.

Two modes:
  * ``queued``    (no orchestrator training handler yet) the stub owns the whole transition:
                  queued -> running (modal_call_id ``stub-...``) -> done.
  * ``stub-call`` (the orchestrator's Modal client runs in stub mode) the stub only finishes runs
                  that are ``running`` with a ``stub-`` modal_call_id.
                  TODO(TDM-21): when ``RDF_MODAL_STUB=1`` the Modal client should, instead of
                  ``modal.Function.spawn``, set status running with modal_call_id ``stub-<uuid>``
                  and leave the rest to this stub.

The checkpoint it writes is ``fake://tdm28/checkpoints/run-<id>`` and metrics_json carries
``"fake": true``, so a fake checkpoint can never be mistaken for a real one.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

FAKE_CKPT_PREFIX = "fake://tdm28/checkpoints/"


class StubTrainer:
    def __init__(self, client, *, mode: str = "queued", train_seconds: float = 1.0, poll: float = 0.3) -> None:
        assert mode in ("queued", "stub-call")
        self.client, self.mode, self.train_seconds, self.poll = client, mode, train_seconds, poll
        self._stop = threading.Event()
        self._started: dict[int, float] = {}
        self.finished: list[int] = []
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._loop, name="tdm28-stub-modal", daemon=True)

    def start(self) -> "StubTrainer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def _set(self, run_id: int, status: str, *, call_id=None, ckpt=None, metrics=None) -> None:
        self.client.call_reducer("set_training_status", run_id, status, call_id, ckpt,
                                 json.dumps(metrics) if metrics is not None else None, 0)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll):
            try:
                for r in self.client.sql("SELECT * FROM training_run"):
                    rid = int(r["id"])
                    if self.mode == "queued" and r["status"] == "queued":
                        self._set(rid, "running", call_id=f"stub-{uuid.uuid4().hex[:10]}",
                                  metrics={"fake": True, "stub": "tdm28 stub modal", "progress": 0})
                        self._started[rid] = time.monotonic()
                    elif r["status"] == "running" and str(r.get("modal_call_id") or "").startswith("stub-"):
                        t0 = self._started.setdefault(rid, time.monotonic())
                        if time.monotonic() - t0 >= self.train_seconds:
                            self._set(rid, "done", ckpt=f"{FAKE_CKPT_PREFIX}run-{rid}",
                                      metrics={"fake": True, "stub": "tdm28 stub modal", "loss": 0.0,
                                               "episodes": len(r.get("episode_ids") or [])})
                            self.finished.append(rid)
            except Exception as e:  # noqa: BLE001 - keep polling; the test asserts on outcomes
                self.errors.append(str(e))
