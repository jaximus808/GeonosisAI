"""Factory Droid session management (onboarding, data, training Droids). TDM-30.

Public API: ``FactoryClient`` (start_session / send / resume / restart_with_payload / finish /
stream_events), ``FactoryConfig``, ``TurnResult``, ``ResumeConflict``. See services/factory/README.md.
"""
from .client import FactoryClient, TurnResult
from .config import FactoryConfig
from .events import DroidEvent, parse_waiting_for
from .mirror import ResumeConflict

__all__ = ["FactoryClient", "FactoryConfig", "TurnResult", "ResumeConflict", "DroidEvent", "parse_waiting_for"]
