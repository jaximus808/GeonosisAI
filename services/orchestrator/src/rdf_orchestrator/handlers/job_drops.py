"""TDM-108: job drops wiring. Adds ``job_drops.page_listener`` to the dispatcher's page listeners, so
every paging of new paid tasks (plan tasks, resets, manual ``create_task`` rows paged by
``handlers/paging.py``) also builds a share card and, in ``RDF_DROP_MODE=live`` only, posts it to the
configured social channels. The listener returns at once; rendering and posting run on a worker thread.
"""
from __future__ import annotations

from .. import job_drops
from ..bus import Registry
from ..bus import registry as default_registry


def register(reg: Registry) -> Registry:
    job_drops.install()
    return reg


register(default_registry)
