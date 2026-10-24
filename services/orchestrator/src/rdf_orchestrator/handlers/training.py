"""TDM-21: the Modal client's bus handlers. The logic is in ``rdf_orchestrator.modal_client``.

* ``training_run`` entering ``queued`` (also existing rows at boot) -> ``TrainingRunner.launch``:
  spawn ``rdf-training.convert`` (or ``finetune`` when hyperparams carry ``dataset_uri``) and set the
  row ``running`` with the Modal call id. The TDM-22 flywheel's ``QueueTrainingStep`` queues the row
  and waits for ``done`` + ``checkpoint_uri`` (or ``failed``, which it retries up to its limit).
* ``training_run`` entering ``running`` (also existing rows at boot: the restart path) ->
  ``TrainingRunner.track``: the poller thread follows the stored call id until done/failed.
  A run set ``cancelled`` while tracked has its Modal call cancelled.

Idempotent: launch re-reads the row from the bus mirror and only acts on ``queued`` (an in-process
set stops two pool workers launching the same run, and ``set_training_status`` refuses illegal
transitions); tracking is a set keyed by run id.

``RDF_MODAL_STUB=1``: no Modal calls; queued runs get a ``stub-`` call id for the TDM-28 harness's
StubTrainer to finish. Refused against maincloud. With this module registered the TDM-22
``tdm22_modal_stub`` stand-in steps aside.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..bus import Event, on_enter, watch
from ..log import get_logger
from ..modal_client import TrainingRunner, stub_mode

log = get_logger("training")

for _t in ("ticket", "episode"):
    watch(_t)  # the manifest is built from the bus mirror


def _default_stdb():
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator"), timeout=20.0)


make_stdb: Callable[[], Any] = _default_stdb  # swappable for tests
RUNNER = TrainingRunner(writer=lambda: make_stdb())


def _check_stub_target() -> None:
    if stub_mode() and "maincloud" in os.environ.get("SPACETIME_HOST", ""):
        raise RuntimeError("RDF_MODAL_STUB=1 against maincloud: refusing to fake training runs")


@on_enter("training_run", where=lambda r: r.status == "queued", name="tdm21_training_launch")
def launch_training(ev: Event) -> None:
    _check_stub_target()
    RUNNER.bind(ev.bus)
    log.info("training.launch", extra={"fields": {"training_run_id": ev.row.id, "source": ev.source,
                                                  "stub": stub_mode()}})
    RUNNER.launch(dict(ev.row))


@on_enter("training_run", where=lambda r: r.status == "running", name="tdm21_training_track")
def track_training(ev: Event) -> None:
    RUNNER.bind(ev.bus)
    RUNNER.track(dict(ev.row))
