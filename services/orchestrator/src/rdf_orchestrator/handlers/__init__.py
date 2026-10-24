"""Orchestrator handlers. Importing this package registers every handler on ``bus.registry``.

Add a module per area (planner, training, resume bridge, ...) and import it below.
"""
from . import event_log_tap  # noqa: F401
from . import diagnose  # noqa: F401  (TDM-23 failure clustering)
from . import planner  # noqa: F401  (TDM-24 data planner)
from . import payments  # noqa: F401  (TDM-63 Stripe test-mode payouts)
from . import dispatcher  # noqa: F401  (TDM-26 paid tasks + reset requests)
from . import reviewer  # noqa: F401  (TDM-25 episode reviewer)
