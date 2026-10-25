"""TDM-119: the GeonosisAI robot client on the real right YAM in TELEOP mode (geonosis-robot-teleop.service).

    python -m rdf_robot_client.tdm119_teleop            # what the unit runs (refuses unless the user signed off)
    python -m rdf_robot_client.tdm119_teleop --check    # offline: config, speeds, sign-off state; opens nothing

What runs:
  * ``RobotClient`` with ``LarpSafeYamArm`` inside the TDM-45 SafetyLayer (``tdm119_bringup.build_safe_arm``),
    registered as ``$ROBOT_ID`` (``yam-01``, embodiment ``i2rt_yam``, ``camera_config_json.sim = false``).
  * the stream server with ``/ws/cameras`` (config/cameras.yaml colour nodes), ``/ws/state`` and **``/ws/control``**
    (TDM-52 teleop: relative EE deltas from the measured pose at clutch-down; ``{t:"stop"}`` always honoured),
    through a cloudflared quick tunnel on the Jetson, announced in ``robot.camera_config_json.stream``.
  * jobs: ``start_teleop`` (clean teleop and recovery takeovers, with TDM-58 recording), ``load_checkpoint``,
    ``reset_request``. **Policy rollouts (run_baseline / run_eval) and ``home`` are refused** on the real arm
    tonight, unless ``TDM119_ALLOW_POLICY=1`` (they then use the ramped TDM-50/122 executor).

Speeds (all of them apply, the slowest wins):
  * teleop EE speed = ``RDF_TELEOP_MAX_LIN_SPEED`` (default here 0.10 m/s) x the phone's speed_scale (0.5 by
    default): about 5 cm/s at the start, 10 cm/s with the slider at 100 %.
  * SafetyLayer: ``speed_scale`` 0.25 (``RDF_SAFETY_SPEED_SCALE``) x yam ``max_velocity`` 0.8-1.2 rad/s:
    0.2-0.3 rad/s per joint.
  * LarpSafeYamArm: every setpoint <= 0.3 rad/s in joint space.

Refuses (exit 3) unless the yam profile in ``$RDF_SAFETY_CONFIG`` is ``verified: true``. Only the user sets that,
with ``tdm119_bringup sign-off``. It also refuses while vrteleop-rh runs (and the LARP flock refuses a busy bus).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import Any

from .tdm118_readonly import TDM118_STOP_VRTELEOP, tdm118_refuse

TDM119_ALLOWED_JOBS = ("start_teleop", "load_checkpoint", "reset_request")
TDM119_DEFAULT_TELEOP_LIN = 0.10  # m/s at the phone's speed_scale 1.0


def tdm119_handlers(allow_policy: bool) -> dict[str, Any]:
    from . import main as _main  # noqa: F401 - registers the rollout / eval handlers
    from . import teleop as _teleop
    from .jobs import HANDLERS

    allowed = set(TDM119_ALLOWED_JOBS) | ({"run_baseline", "run_eval"} if allow_policy else set())
    names = set(HANDLERS) | {"home", "run_baseline", "run_eval", "start_teleop", "reset_request"}
    out: dict[str, Any] = {n: tdm118_refuse for n in sorted(names) if n not in allowed}
    out["start_teleop"] = _teleop.start_teleop
    return out


def tdm119_teleop_config() -> Any:
    from .teleop import TeleopConfig

    lin = float(os.environ.get("RDF_TELEOP_MAX_LIN_SPEED") or TDM119_DEFAULT_TELEOP_LIN)
    ang = float(os.environ.get("RDF_TELEOP_MAX_ANG_SPEED") or 0.5)
    return TeleopConfig(max_lin_speed=min(lin, 0.25), max_ang_speed=min(ang, 1.5),
                        allow_ops_tokens=False, allow_practice=False)


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tdm119_teleop", description="GeonosisAI real-YAM teleop client (TDM-119)")
    p.add_argument("--check", action="store_true", help="offline: print the config and sign-off state, open nothing")
    p.add_argument("--name", default=None)
    a = p.parse_args(argv)
    from .adapters.tdm118_yam_readonly import tdm118_vrteleop_active
    from .tdm119_bringup import (
        build_safe_arm,
        tdm119_safety_path,
        tdm119_signed_off,
        verified_check,
    )

    allow_policy = os.environ.get("TDM119_ALLOW_POLICY") == "1"
    handlers = tdm119_handlers(allow_policy)
    tcfg = tdm119_teleop_config()
    name = a.name or os.environ.get("ROBOT_ID") or "yam-01"
    if a.check:
        arm = build_safe_arm("yam")
        print(json.dumps({"robot": name, "safety_config": str(tdm119_safety_path()),
                          "verified": tdm119_signed_off(), "speed_scale": arm.speed_scale,
                          "teleop_max_lin_speed": tcfg.max_lin_speed, "teleop_max_ang_speed": tcfg.max_ang_speed,
                          "phone_default_speed_scale": tcfg.default_speed_scale,
                          "ee_speed_at_start_m_s": round(tcfg.max_lin_speed * tcfg.default_speed_scale, 3),
                          "joint_vmax_rad_s": [round(v, 3) for v in arm.limits.max_velocity],
                          "adapter_max_joint_speed": getattr(arm.inner, "max_joint_speed", None),
                          "refused_jobs": sorted(k for k, v in handlers.items() if v is tdm118_refuse),
                          "cameras_config": os.environ.get("RDF_CAMERAS_CONFIG")}, indent=1, default=str))
        return 0
    if verified_check() != 0:
        return 3
    if tdm118_vrteleop_active():
        print(f"vrteleop-rh is running and owns CAN + cameras. Stop it first: {TDM118_STOP_VRTELEOP}", file=sys.stderr)
        return 3

    from . import teleop as _teleop
    from .cameras import CameraRig
    from .loop import RobotClient, _log
    from .main import _stdb_config
    from .stream import install_state_feed, start_stream

    arm = build_safe_arm("yam")
    cfg, _ = _stdb_config(argparse.Namespace(adapter="yam", host=None, db=None, token_file=None))
    services: dict[str, Any] = {}
    cams: dict[str, Any] = {"cameras": "config"}
    try:
        services["cameras"] = CameraRig.from_config(os.environ.get("RDF_CAMERAS_CONFIG")).start()
    except Exception as e:  # noqa: BLE001
        _log("cameras NOT started", error=repr(e)[:300])
        cams = {"cameras": "off"}
    client = RobotClient(arm, name=name, stdb=cfg, camera_config={**cams, "sim": False, "teleop": True,
                                                                    "policy": allow_policy},
                         heartbeat_s=5.0, handlers=handlers, services=services, clear_estop_on_boot=False)
    if hasattr(arm, "set_event_sink"):
        arm.set_event_sink(client.event)
    rig = services.get("cameras")
    stream = None
    if rig is not None:
        try:
            stream = start_stream(rig, robot_name=name, tunnel=os.environ.get("RDF_STREAM") or "cloudflared",
                                  port=int(os.environ.get("RDF_STREAM_PORT") or 8091))
        except (Exception, SystemExit) as e:  # noqa: BLE001
            _log("stream + teleop NOT started (no video, no /ws/control)", error=repr(e)[:300])
    if stream is not None:
        _teleop.install_control(stream, _teleop.hub_for(client, config=tcfg))
        install_state_feed(stream, client, sim=False)
        client.camera_config.update(stream.info())

        def _republish(info: dict[str, Any]) -> None:
            client.camera_config.update(info)
            if client.robot_id is None:
                return
            try:
                client.http.call_reducer("register_robot", name, arm.embodiment, arm.adapter_version,
                                         json.dumps({**client.camera_config, "arm": arm.describe()}, default=str))
            except Exception as e:  # noqa: BLE001
                _log("stream url re-publish failed", error=str(e)[:200])

        stream.on_url(_republish)
        services["stream"] = stream
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    # clearing a soft e-stop is local only: systemctl --user kill -s USR1 geonosis-robot-teleop
    signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(target=client.clear_estop, kwargs={"by": "SIGUSR1"},
                                                              daemon=True).start())
    try:
        client.start()  # connects the arm: LARP enable, hold measured pose (refusals raise)
    except Exception as e:  # noqa: BLE001
        _log("TDM-119 teleop client did not start", error=str(e)[:400])
        if stream is not None:
            stream.stop()
        if rig is not None:
            rig.stop()
        return 3
    _log("TDM-119 teleop client running on the real arm", robot_id=client.robot_id, name=name,
         speed_scale=arm.speed_scale, teleop_max_lin_speed=tcfg.max_lin_speed)
    if rig is not None:  # teleop recordings upload: ORCHESTRATOR_URL, else TDM-110 discovery (RDF_EDGE_SECRET)
        from .orch_endpoint import start_orchestrator_discovery

        start_orchestrator_discovery(client)
    done.wait()
    client.stop()   # cancels the job and holds; it does not release the arm
    arm.disconnect()  # the LARP release: damp until settled, then disable (the arm goes limp after)
    if stream is not None:
        stream.stop()
    if rig is not None:
        rig.stop()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
