"""Arm adapters. ``make_adapter(name)`` builds one by name; see ``base.py`` for the interface.

Registered names:
  * ``sim``  SimArm (TDM-49), kinematic simulation, no hardware.
  * ``yam``  I2RT YAM (TDM-46), imported lazily from ``adapters/yam.py`` so laptops without the
             I2RT SDK can still import this package. TDM-46: define ``YamArm(ArmAdapter)`` there,
             accepting ``**kwargs`` from ``make_adapter("yam", **kwargs)``.
"""
from __future__ import annotations

import importlib
from typing import Any

from .base import (
    ArmAdapter,
    ArmAdapterWrapper,
    ArmError,
    ArmNotConnected,
    ArmState,
    ArmStatus,
    ArmStopped,
    ControlTick,
    EETarget,
    IKError,
    JointLimits,
    JointTarget,
    Pose,
    Target,
)

# name -> "module:Class" (lazy, so a missing SDK only fails when that adapter is chosen)
ADAPTERS: dict[str, str] = {
    "sim": "rdf_robot_client.adapters.sim:SimArm",
    "yam": "rdf_robot_client.adapters.yam:YamArm",
}


def make_adapter(name: str, **kwargs: Any) -> ArmAdapter:
    try:
        spec = ADAPTERS[name]
    except KeyError:
        raise ArmError(f"unknown adapter {name!r}; known: {sorted(ADAPTERS)}") from None
    mod_name, cls_name = spec.split(":")
    try:
        cls = getattr(importlib.import_module(mod_name), cls_name)
    except (ImportError, AttributeError) as e:
        raise ArmError(f"adapter {name!r} is not available ({spec}): {e}") from e
    return cls(**kwargs)


__all__ = [
    "ADAPTERS", "ArmAdapter", "ArmAdapterWrapper", "ArmError", "ArmNotConnected", "ArmState", "ArmStatus",
    "ArmStopped", "ControlTick", "EETarget", "IKError", "JointLimits", "JointTarget", "Pose", "Target", "make_adapter",
]
