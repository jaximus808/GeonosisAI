"""Robot client entry point (TDM-49).

    rdf-robot-client                      # Jetson: YAM adapter (TDM-46), env from .env (RobotClientSettings)
    rdf-robot-client --sim                # laptop: real command loop + SimArm, no hardware
    rdf-robot-client --sim --host http://127.0.0.1:3049 --db tdm49loop --token-file robot_client.token
    rdf-robot-client --adapter mujoco     # [SIM] MuJoCo YAM: physics + rendered cameras + the real
                                          # rollout / trial runner (needs `uv sync --extra sim`)
    rdf-robot-client --fake ...           # TDM-28 fake robot (synthetic episodes), see fake.py

Clearing a soft e-stop (robot ``locked`` / status ``estop``) is LOCAL ONLY, by a human at the
arm: type ``clear`` (or ``unlock``) + Enter in this terminal, or ``pkill -USR1 -f rdf-robot-client``.
TDM-51 eval trials on stdin: ``pass`` / ``fail <note>`` gives the operator verdict for the open trial.
TDM-45 safety layer (safety.py) on stdin: ``speed <0..1>`` sets the speed scale (capped by
config/safety.yaml), ``slow on`` / ``slow off`` toggles slow mode, ``safety`` prints the counters, ``pose`` the EE pose.
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

from . import rollout as _rollout  # noqa: F401 - TDM-50: registers load_checkpoint + run_baseline (policy rollout)
from . import eval_trials as _eval_trials  # TDM-51: run_eval + run_baseline(eval_run_id) trial runner (after rollout)
from .adapters import ArmAdapter, make_adapter
from .handlers import load_checkpoint as pointer_load_checkpoint
from .handlers import sim_demo_rollout
from .loop import RobotClient, _log


SIM_ADAPTERS = ("sim", "mujoco")  # no hardware: laptop SpacetimeDB config, e-stop auto-clear allowed
MUJOCO_CAMERAS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "cameras.mujoco.yaml"


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
    p.add_argument("--adapter", default=None, help="arm adapter: yam (default on the Jetson) | sim | mujoco ([SIM] physics)")
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
    p.add_argument("--cameras", choices=["off", "synthetic", "config", "mujoco"], default=None,
                   help="TDM-47 CameraRig: off (default with --sim) | synthetic | config (default on the Jetson) | "
                        "mujoco ([SIM] rendered top + wrist, default with --adapter mujoco)")
    p.add_argument("--sim-policy", action="store_true",
                   help="with --sim: run_baseline/run_eval use the real TDM-50 rollout + TDM-51 trial runner "
                        "($POLICY_BACKEND) instead of the demo")
    p.add_argument("--unsafe-no-safety-layer", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    a.adapter = "sim" if a.sim else (a.adapter or "yam")
    if a.adapter not in SIM_ADAPTERS and a.estop_auto_clear_s:
        p.error("--estop-auto-clear-s is only allowed with the sim arm")
    if a.cameras == "mujoco" and a.adapter != "mujoco":
        p.error("--cameras mujoco renders the MuJoCo sim: it needs --adapter mujoco")
    if a.cameras is None:
        a.cameras = {"sim": "off", "mujoco": "mujoco"}.get(a.adapter, "config")
    return a


def _stdb_config(a: argparse.Namespace):
    from rdf_common.spacetime_client import SpacetimeConfig, load_dotenv

    if a.adapter in SIM_ADAPTERS:
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

    if mode == "mujoco":  # [SIM] offscreen renders of the MuJoCo arm (adapters/mujoco_sim.py)
        rig = CameraRig.from_config(os.environ.get("RDF_CAMERAS_CONFIG") or MUJOCO_CAMERAS_CONFIG)
    else:
        rig = CameraRig.from_config(synthetic=mode == "synthetic")
    rig.start()
    services["cameras"] = rig
    return {"cameras": mode, "synthetic": mode == "synthetic", **({"sim": True, "rendered": "mujoco"} if mode == "mujoco" else {})}


def run(argv: list[str] | None = None) -> None:
    a = parse_args(argv)
    cfg, env_name = _stdb_config(a)
    sim = a.adapter in SIM_ADAPTERS
    mujoco = a.adapter == "mujoco"
    name = a.name or (("test.sim-yam-1" if a.adapter == "sim" else "sim-yam-mujoco" if mujoco else None) or env_name or "yam-1")
    arm = build_arm(a.adapter, safety=not a.unsafe_no_safety_layer)
    if mujoco:  # physics + render threads before the cameras open (they render this sim)
        arm.connect()
    services: dict[str, Any] = {}
    cams = _start_cameras(a.cameras, services)
    # --sim: the TDM-49 demo motion; --adapter mujoco: the real TDM-50 rollout + TDM-51 trial runner (registry)
    handlers = {"run_eval": sim_demo_rollout, "run_baseline": sim_demo_rollout,
                "load_checkpoint": pointer_load_checkpoint} if a.adapter == "sim" else {}
    if a.adapter == "sim" and a.sim_policy:  # TDM-50: the real rollout + policy endpoint, on the sim arm
        handlers.pop("run_baseline")
        handlers.pop("load_checkpoint")
        handlers.pop("run_eval")  # TDM-51 trial runner
    client = RobotClient(
        arm, name=name, stdb=cfg, camera_config={**cams, **({"sim": True} if sim else {})},
        heartbeat_s=a.heartbeat_s, handlers=handlers, services=services,
        clear_estop_on_boot=a.clear_estop_on_boot or sim, max_command_age_s=a.max_command_age_s,
        estop_auto_clear_s=a.estop_auto_clear_s,
    )
    # TDM-69: advertise DAgger interventions (camera_config_json.capabilities) only when run_baseline is the
    # TDM-51 trial runner, which runs the failure detector in collection rollouts. RDF_INTERVENTIONS=0 opts out.
    from . import interventions as _iv

    _iv.advertise(client.camera_config, client.handlers.get("run_baseline") is _eval_trials.run_baseline
                  and _iv.robot_enabled())
    if hasattr(arm, "set_event_sink"):  # TDM-45: rate-limited robot.limit_hit event_log rows
        arm.set_event_sink(client.event)
    # TDM-52: TDM-55 video stream + teleop /ws/control (same server, tunnel and token). stream.info() is merged
    # into the registered camera_config (robot.camera_config_json["stream"], incl. control_url) and re-published
    # when the tunnel URL changes. Env: RDF_STREAM=cloudflared|none|off (default cloudflared; none = LAN, the
    # default with --sim), RDF_STREAM_PORT (8091), RDF_TELEOP_ALLOW_OPS=1 (ops tokens may drive: HIL tests only).
    import atexit

    from . import teleop as _teleop

    client.handlers["start_teleop"] = _teleop.start_teleop
    stream_mode = os.environ.get("RDF_STREAM") or ("none" if sim else "cloudflared")
    # [SIM] a sim robot without cameras still serves /ws/control + /ws/state (the browser renders it from state)
    stream_rig = services.get("cameras")
    if stream_rig is None and sim:
        from .stream import NoCameraRig

        stream_rig = NoCameraRig()
    if stream_rig is not None and stream_mode != "off":
        try:
            from .stream import start_stream

            stream = start_stream(stream_rig, robot_name=name, tunnel=stream_mode,
                                  port=int(os.environ.get("RDF_STREAM_PORT") or 8091))
        except (Exception, SystemExit) as e:  # e.g. RDF_STREAM_TOKEN_SECRET missing: run without video/teleop
            _log("stream + teleop NOT started (no video, no /ws/control)", error=repr(e)[:300])
        else:
            _teleop.install_control(stream, _teleop.hub_for(client, config=_teleop.TeleopConfig(
                allow_ops_tokens=os.environ.get("RDF_TELEOP_ALLOW_OPS") == "1",
                # [SIM] practice sessions (no lease, no recording, reset_scene): sim robots only
                allow_practice=sim and os.environ.get("RDF_TELEOP_PRACTICE", "1") != "0")))
            from .stream import install_state_feed  # TDM-96: /ws/state live robot state (RDF_STATE_HZ, default 30)

            install_state_feed(stream, client, sim=sim)
            client.camera_config.update(stream.info())

            def _republish(info: dict[str, Any]) -> None:
                client.camera_config.update(info)
                if client.robot_id is None:
                    return  # not registered yet: _register() sends the merged config
                try:
                    client.http.call_reducer("register_robot", name, arm.embodiment, arm.adapter_version,
                                             json.dumps({**client.camera_config, "arm": arm.describe()}, default=str))
                except Exception as e:  # noqa: BLE001
                    _log("stream url re-publish failed", error=str(e)[:200])

            stream.on_url(_republish)
            services["stream"] = stream
            atexit.register(stream.stop)
            _log("stream + teleop up", **stream.info()["stream"])
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
                if _eval_trials.terminal_outcome(client, line):  # TDM-51: 'pass' / 'fail <note>' for the open trial
                    continue
                if cmd in ("clear", "unlock"):
                    if not client.clear_estop(by="terminal"):
                        print("not locked", file=sys.stderr)
                elif cmd in ("status", "s"):
                    print(json.dumps(client.snapshot(), default=str), file=sys.stderr)
                elif cmd in ("estop", "stop"):  # local soft stop: same latch as soft_estop
                    arm.stop()
                    client.sm.lock("local terminal e-stop")
                elif cmd.startswith("speed ") and hasattr(arm, "set_speed_scale"):  # TDM-45
                    try:
                        print(f"speed scale {arm.set_speed_scale(float(cmd.split()[1]))}", file=sys.stderr)
                    except ValueError as e:
                        print(f"speed: {e}", file=sys.stderr)
                elif cmd in ("slow on", "slow off") and hasattr(arm, "set_slow_mode"):  # TDM-45
                    arm.set_slow_mode(cmd == "slow on")
                elif cmd == "safety" and hasattr(arm, "stats"):  # TDM-45
                    print(json.dumps(arm.stats(), default=str), file=sys.stderr)
                elif cmd == "pose":  # TDM-45: measure the workspace box / table floor by jogging
                    st = arm.read_state()
                    print(json.dumps({"joints": [round(v, 4) for v in st.joints], "gripper": round(st.gripper, 3),
                                      "ee_pose": st.ee_pose.to_dict()}), file=sys.stderr)
        threading.Thread(target=_stdin, name="rdf-stdin", daemon=True).start()
    _log("running; 'status' / 'clear' / 'estop' on stdin, Ctrl-C to stop", robot_id=client.robot_id,
         adapter=a.adapter, state=str(client.sm.state))
    done.wait()
    client.stop()
    rig = services.get("cameras")
    if rig is not None:
        rig.stop()
    if services.get("policy") is not None:  # TDM-50 PolicyClient (built on first rollout)
        services["policy"].close()


__all__ = ["build_arm", "parse_args", "run"]
