"""Orchestrator handlers. Importing this package registers every handler on ``bus.registry``.

Add a module per area (planner, training, resume bridge, ...) and import it below.
"""
from . import event_log_tap  # noqa: F401
from . import diagnose  # noqa: F401  (TDM-23 failure clustering)
from . import planner  # noqa: F401  (TDM-24 data planner)
from . import payments  # noqa: F401  (TDM-63 Stripe test-mode payouts)
from . import dispatcher  # noqa: F401  (TDM-26 paid tasks + reset requests)
from . import paging  # noqa: F401  (TDM-65 web push: dispatcher page hook + manual open tasks)
from . import reviewer  # noqa: F401  (TDM-25 episode reviewer)
from . import dataset_qa  # noqa: F401  (TDM-92 deterministic QA for purchased episodes; writes deterministic_qa)
from . import llm_qa  # noqa: F401  (TDM-93 LLM QA for purchased episodes, after TDM-92 deterministic_qa)
from . import training  # noqa: F401  (TDM-21 Modal client: launch + poll training runs)
from . import offers  # noqa: F401  (TDM-91 dataset offers: finalize_offer -> reserve_dataset_payment)
from . import plan_offers  # noqa: F401  (TDM-95 planner offer suggestion -> offer after approve_spend; timeout fallback)
from . import flywheel  # noqa: F401  (TDM-22 flywheel state machine; keep last)
from . import evaluate  # noqa: F401  (TDM-67 eval orchestration; after flywheel: registers its steps)
from . import interventions  # noqa: F401  (TDM-69 DAgger interventions: pause on failure, takeover task, recovery episode)
from . import resume_bridge  # noqa: F401  (TDM-27 SpacetimeDB predicate -> Factory session resume)
from . import data_droid  # noqa: F401  (TDM-33 Data Droid: started when a ticket enters review; RDF_DATA_DROID=1)
