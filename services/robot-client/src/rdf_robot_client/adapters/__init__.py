"""Arm adapters. ``make_adapter(name)`` builds one by name; see ``base.py`` for the interface.

Registered names:
  * ``sim``  SimArm (TDM-49), kinematic simulation, no hardware.
  * ``mujoco`` [SIM] the YAM in a MuJoCo physics scene (cube + plates, rendered cameras), imported
             lazily from ``adapters/mujoco_sim.py`` (needs the ``sim`` extra: ``uv sync --extra sim``).
  * ``yam``  I2RT YAM (TDM-46), imported lazily from ``adapters/yam.py`` so laptops without the
             I2RT SDK can still import this package. TDM-46: define ``YamArm(ArmAdapter)`` there,
             accepting ``**kwargs`` from ``make_adapter("yam", **kwargs)``.
  * ``yam`` on hardware is ``LarpSafeYamArm`` (``adapters/yam_larp.py``): LARP's i2rt fork, LARP's flock,
             gains and limits, <= 0.3 rad/s rate limit, damp-until-settled release (docs/real-arm-safety.md).
             In sim it is plain YamArm on stock i2rt.
  * ``yam_readonly``  TDM-118: the real YAM read over CAN with every motion path refused
             (``adapters/tdm118_yam_readonly.py``; DM refresh-status query only).
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
    "yam": "rdf_robot_client.adapters.yam_larp:LarpSafeYamArm",  # YamArm + docs/real-arm-safety.md on hardware
    "mujoco": "rdf_robot_client.adapters.mujoco_sim:MujocoSimArm",
    "yam_readonly": "rdf_robot_client.adapters.tdm118_yam_readonly:Tdm118ReadOnlyYamArm",  # TDM-118: joint reads, no motion
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
