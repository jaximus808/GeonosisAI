"""TDM-118: the GeonosisAI robot client on the real YAM in READ-ONLY mode (joint reads, no motion).

    python -m rdf_robot_client.tdm118_readonly            # what geonosis-robot.service runs on the Jetson
    python -m rdf_robot_client.tdm118_readonly --check    # config check only: no CAN, no SpacetimeDB

What runs:
  * the real ``RobotClient`` command loop: registers ``$ROBOT_ID`` (``yam-01``) with embodiment
    ``i2rt_yam`` and ``camera_config_json.sim = false``, heartbeats, and handles soft e-stops.
  * the arm is ``adapters/tdm118_yam_readonly.py``: CAN refresh-status queries only. Every job type that
    could move the arm (home, run_baseline, run_eval, start_teleop, reset_request, ...) is replaced by a
    handler that fails the command, so a motion command fails before it reaches the arm. The adapter
    refuses every ``command_*`` call too.
  * the TDM-55 stream server with ``/ws/state`` only (TDM-96 live state for RobotView). **No ``/ws/control``**
    is mounted, so there is no teleop input path. No cameras yet (TDM-120 adds them).

Refuses to start (exit 3) while ``vrteleop-rh`` is active: it owns the CAN buses and the cameras.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import Any

TDM118_EXIT_REFUSED = 3
TDM118_ALLOWED_JOBS = ("load_checkpoint",)  # stores a pointer, never moves the arm
TDM118_STOP_VRTELEOP = "sudo systemctl stop vrteleop-rh"


def _log(msg: str, **kv: Any) -> None:
    from .loop import _log as loop_log

    loop_log(msg, **kv)


def tdm118_refuse(ctx: Any) -> dict[str, Any]:
    raise RuntimeError(f"{ctx.command.type}: robot runs the TDM-118 read-only client (joint reads only); "
                       "motion is enabled by TDM-119 with a human at the e-stop")


def tdm118_handlers() -> dict[str, Any]:
    """Every registered job type -> refuse, except the pointer-only ones."""
    from . import main as _main  # noqa: F401 - imports rollout + eval_trials, which register their handlers
    from .jobs import HANDLERS

    names = set(HANDLERS) | {"home", "run_baseline", "run_eval", "start_teleop", "reset_request"}
    return {n: tdm118_refuse for n in sorted(names) if n not in TDM118_ALLOWED_JOBS}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tdm118_readonly", description="GeonosisAI read-only YAM client (TDM-118)")
    p.add_argument("--name", default=None, help="robot.name (default $ROBOT_ID, else yam-01)")
    p.add_argument("--heartbeat-s", type=float, default=5.0)
    p.add_argument("--stream", default=None, help="cloudflared | none | off (default $RDF_STREAM or cloudflared)")
    p.add_argument("--port", type=int, default=None, help="stream server port (default $RDF_STREAM_PORT or 8091)")
    p.add_argument("--check", action="store_true", help="offline config check: build the adapter, open nothing")
    p.add_argument("--no-vrteleop-check", action="store_true", help=argparse.SUPPRESS)  # tests only
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    from .adapters import make_adapter
    from .adapters.tdm118_yam_readonly import tdm118_vrteleop_active

    vr_check = None if a.no_vrteleop_check else tdm118_vrteleop_active
    arm = make_adapter("yam_readonly", vrteleop_check=vr_check)
    name = a.name or os.environ.get("ROBOT_ID") or "yam-01"
    handlers = tdm118_handlers()
    if a.check:
        print(json.dumps({"ok": True, "robot": name, "arm": arm.describe(), "refused_jobs": sorted(handlers),
                          "vrteleop_active": tdm118_vrteleop_active()}, default=str, indent=1))
        return 0
    if vr_check is not None and vr_check():
        print(f"vrteleop-rh is running and owns the CAN buses and cameras. Stop it first: {TDM118_STOP_VRTELEOP}",
              file=sys.stderr)
        return TDM118_EXIT_REFUSED

    from .loop import RobotClient
    from .main import _stdb_config

    cfg, _env_name = _stdb_config(argparse.Namespace(adapter="yam_readonly", host=None, db=None, token_file=None))
    client = RobotClient(
        arm, name=name, stdb=cfg, heartbeat_s=a.heartbeat_s, handlers=handlers, services={},
        camera_config={"cameras": "off", "sim": False, "read_only": True,
                       "motion": "disabled (TDM-118 read-only; TDM-119 enables it)"},
        clear_estop_on_boot=False,
    )
    stream_mode = a.stream or os.environ.get("RDF_STREAM") or "cloudflared"
    stream = None
    if stream_mode != "off":
        try:
            from .stream import NoCameraRig, install_state_feed, start_stream

            stream = start_stream(NoCameraRig(), robot_name=name, tunnel=stream_mode,
                                  port=a.port or int(os.environ.get("RDF_STREAM_PORT") or 8091))
        except (Exception, SystemExit) as e:  # noqa: BLE001 - run without /ws/state rather than not at all
            _log("stream NOT started (no /ws/state)", error=repr(e)[:300])
        else:
            install_state_feed(stream, client, sim=False)  # /ws/state only: no install_control, no teleop
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
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(
            target=client.clear_estop, kwargs={"by": "SIGUSR1"}, daemon=True).start())
    try:
        client.start()
    except Exception as e:  # noqa: BLE001 - e.g. CAN busy / vrteleop raced us: a clear refusal, not a traceback loop
        _log("TDM-118 client did not start", error=str(e)[:400])
        if stream is not None:
            stream.stop()
        arm.disconnect()
        return TDM118_EXIT_REFUSED
    _log("TDM-118 read-only client running: joint reads only, every motion command is refused",
         robot_id=client.robot_id, name=name, can=getattr(arm, "channel", None))
    done.wait()
    client.stop()
    if stream is not None:
        stream.stop()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
