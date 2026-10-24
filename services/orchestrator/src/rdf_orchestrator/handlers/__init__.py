"""Orchestrator handlers. Importing this package registers every handler on ``bus.registry``.

Add a module per area (planner, training, resume bridge, ...) and import it below.
"""
from . import event_log_tap  # noqa: F401
from . import diagnose  # noqa: F401  (TDM-23 failure clustering)
