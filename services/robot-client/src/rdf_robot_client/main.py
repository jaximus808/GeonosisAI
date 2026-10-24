"""Robot client entry point (TDM-49).

    rdf-robot-client                      # Jetson: YAM adapter (TDM-46), env from .env (RobotClientSettings)
    rdf-robot-client --sim                # laptop: real command loop + SimArm, no hardware
    rdf-robot-client --sim --host http://127.0.0.1:3049 --db tdm49loop --token-file robot_client.token
    rdf-robot-client --fake ...           # TDM-28 fake robot (synthetic episodes), see fake.py

Clearing a soft e-stop (robot ``locked`` / status ``estop``) is LOCAL ONLY, by a human at the
arm: type ``clear`` + Enter in this terminal, or ``pkill -USR1 -f rdf-robot-client``.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .adapters import ArmAdapter, make_adapter
from .handlers import sim_demo_rollout
from .loop import RobotClient, _log


def build_arm(adapter: str, *, safety: bool = True, **kwargs: Any) -> ArmAdapter:
    """The adapter, wrapped in the TDM-45 safety layer when it exists.

    TDM-45: provide ``rdf_robot_client.safety.SafetyLayer(arm: ArmAdapter) -> ArmAdapterWrapper``.
    """
    arm = make_adapter(adapter, **kwargs)
    if not safety:
        return arm
    try:
        from .safety import SafetyLayer  # type: ignore[import-not-found]
    except ImportError:
        if adapter != "sim":
            raise SystemExit("refusing to drive real hardware without rdf_robot_client.safety (TDM-45); "
                             "pass --unsafe-no-safety-layer only with a human on the physical e-stop")
        _log("no safety layer yet (TDM-45); running the sim arm unwrapped")
        return arm
    return SafetyLayer(arm)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rdf-robot-client", description="Robot client: SpacetimeDB command loop + arm")
    p.add_argument("--sim", action="store_true", help="use the simulated arm (same as --adapter sim)")
    p.add_argument("--adapter", default=None, help="arm adapter: yam (default on the Jetson) | sim")
    p.add_argument("--name", default=None, help="robot.name (default: $ROBOT_ID, or test.sim-yam-1 with --sim)")
    p.add_argument("--host", default=None, help="SpacetimeDB host (default $SPACETIME_HOST)")
    p.add_argument("--db", default=None, help="SpacetimeDB database (default $SPACETIME_DB_NAME)")
    p.add_argument("--token-file", default=None, help="robot_client identity token file (default $SPACETIME_TOKEN)")
    p.add_argument("--heartbeat-s", type=float, default=5.0)
    p.add_argument("--max-command-age-s", type=float, default=120.0,
                   help="fail issued commands older than this instead of running them (0 = off)")
    p.add_argument("--clear-estop-on-boot", action="store_true",
                   help="start unlocked even if robot.status is estop (default on with --sim)")
    p.add_argument("--estop-auto-clear-s", type=float, default=0.0, help="--sim only: auto-clear a soft e-stop")
    p.add_argument("--cameras", choices=["off", "synthetic", "config"], default=None,
                   help="TDM-47 CameraRig: off (default with --sim) | synthetic | config (default on the Jetson)")
    p.add_argument("--unsafe-no-safety-layer", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    a.adapter = "sim" if a.sim else (a.adapter or "yam")
    if a.adapter != "sim" and a.estop_auto_clear_s:
        p.error("--estop-auto-clear-s is only allowed with the sim arm")
    if a.cameras is None:
        a.cameras = "off" if a.adapter == "sim" else "config"
    return a


def _stdb_config(a: argparse.Namespace):
    from rdf_common.spacetime_client import SpacetimeConfig, load_dotenv

    if a.adapter == "sim":
        load_dotenv()
        token = Path(a.token_file).read_text().strip() if a.token_file else None
        return SpacetimeConfig.resolve(a.host, a.db, token), os.environ.get("ROBOT_ID")
    from rdf_common.settings import RobotClientSettings, load_settings

    s = load_settings(RobotClientSettings, "robot-client")  # exits non-zero naming any missing env var
    cfg = SpacetimeConfig.from_settings(s)
    if a.host or a.db or a.token_file:
        token = Path(a.token_file).read_text().strip() if a.token_file else cfg.token
        cfg = SpacetimeConfig.resolve(a.host or cfg.host, a.db or cfg.db, token or "")
    return cfg, s.robot_id


def _start_cameras(mode: str, services: dict[str, Any]) -> dict[str, Any]:
    if mode == "off":
        return {"cameras": "off"}
    from .cameras import CameraRig

    rig = CameraRig.from_config(synthetic=mode == "synthetic")
    rig.start()
    services["cameras"] = rig
    return {"cameras": mode, "synthetic": mode == "synthetic"}


def run(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    cfg, env_name = _stdb_config(a)
    sim = a.adapter == "sim"
    name = a.name or (("test.sim-yam-1" if sim else None) or env_name or "yam-1")
    arm = build_arm(a.adapter, safety=not a.unsafe_no_safety_layer)
    services: dict[str, Any] = {}
    cams = _start_cameras(a.cameras, services)
    handlers = {"run_eval": sim_demo_rollout, "run_baseline": sim_demo_rollout} if sim else {}
    client = RobotClient(
        arm, name=name, stdb=cfg, camera_config={**cams, **({"sim": True} if sim else {})},
        heartbeat_s=a.heartbeat_s, handlers=handlers, services=services,
        clear_estop_on_boot=a.clear_estop_on_boot or sim, max_command_age_s=a.max_command_age_s,
        estop_auto_clear_s=a.estop_auto_clear_s,
    )
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(target=client.clear_estop, kwargs={"by": "SIGUSR1"}, daemon=True).start())
    client.start()
    if sys.stdin and sys.stdin.isatty():
        def _stdin() -> None:
            for line in sys.stdin:
                cmd = line.strip().lower()
                if cmd == "clear":
                    if not client.clear_estop(by="terminal"):
                        print("not locked", file=sys.stderr)
                elif cmd in ("status", "s"):
                    print(json.dumps(client.snapshot(), default=str), file=sys.stderr)
                elif cmd in ("estop", "stop"):  # local soft stop: same latch as soft_estop
                    arm.stop()
                    client.sm.lock("local terminal e-stop")
        threading.Thread(target=_stdin, name="rdf-stdin", daemon=True).start()
    _log("running; 'status' / 'clear' / 'estop' on stdin, Ctrl-C to stop", robot_id=client.robot_id,
         adapter=a.adapter, state=str(client.sm.state))
    done.wait()
    client.stop()
    rig = services.get("cameras")
    if rig is not None:
        rig.stop()


__all__ = ["build_arm", "parse_args", "run"]
