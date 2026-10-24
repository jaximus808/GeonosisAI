"""``rdf-robot-client demo-square`` (TDM-46, CP-1): trace a square with the tool point, check the
state stream rate and cycle the gripper. Prints a JSON report; exit code 0 only if every check passes.

    # laptop, SDK MuJoCo stand-in (needs the i2rt SDK installed; NOT hardware):
    rdf-robot-client demo-square --yam-sim
    # laptop, no SDK: SimArm (TDM-49 kinematic sim):
    rdf-robot-client demo-square --sim
    # Jetson, real arm (human at the table, e-stop in hand; docs/yam-sdk-notes.md checklist):
    rdf-robot-client demo-square --side 0.10 --speed 0.02

The arm goes: current pose -> home (ready pose) -> square in the horizontal plane through the home
tool point (first corner = home, then +x, +y, -x, -y) -> gripper open/close/open -> home. With
``--rest-at-end`` it then folds to the rest pose (q = 0) so disconnecting does not drop the arm.
Every motion goes through ``main.build_arm`` (the TDM-45 safety layer when it exists).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from typing import Any

from .adapters import ArmAdapter, Pose, make_adapter
from .adapters.yam import move_ee_linear


def _log(msg: str) -> None:
    print(f"[demo-square] {msg}", file=sys.stderr, flush=True)


class _StateSampler(threading.Thread):
    """Reads ``arm.read_state()`` at the control rate in the background and measures the rate."""

    def __init__(self, arm: ArmAdapter) -> None:
        super().__init__(name="tdm46-state-sampler", daemon=True)
        self.arm = arm
        self.stop_evt = threading.Event()
        self.stamps: list[float] = []

    def run(self) -> None:
        period = 1.0 / self.arm.control_rate_hz
        nxt = time.monotonic()
        while not self.stop_evt.is_set():
            self.arm.read_state()
            self.stamps.append(time.monotonic())
            nxt += period
            self.stop_evt.wait(max(0.0, nxt - time.monotonic()))

    def rate(self) -> dict[str, float]:
        s = self.stamps
        if len(s) < 3:
            return {"samples": len(s)}
        pairs = list(zip(s, s[1:]))
        gaps = sorted(b - a for a, b in pairs)
        worst = max(pairs, key=lambda ab: ab[1] - ab[0])
        return {"samples": len(s), "hz": round((len(s) - 1) / (s[-1] - s[0]), 2),
                "gap_p50_ms": round(1000 * gaps[len(gaps) // 2], 2), "gap_max_ms": round(1000 * gaps[-1], 2),
                "gap_max_at_s": round(worst[0] - s[0], 2)}


def _build(args: argparse.Namespace) -> ArmAdapter:
    from .main import build_arm

    kwargs: dict[str, Any] = {}
    if args.adapter == "yam":
        kwargs = {"sim": args.yam_sim}
        if args.channel:
            kwargs["channel"] = args.channel
        if args.gripper:
            kwargs["gripper_type"] = args.gripper
        if args.no_smoothing:
            kwargs["smoothing"] = False
    if args.unsafe_no_safety_layer:
        return make_adapter(args.adapter, **kwargs)
    if args.adapter == "yam" and args.yam_sim:
        try:
            from .safety import SafetyLayer  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            _log("no safety layer yet (TDM-45); running the SDK sim arm unwrapped")
            return make_adapter("yam", **kwargs)
    return build_arm(args.adapter, safety=True, **kwargs)


def run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="rdf-robot-client demo-square", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="SimArm (TDM-49) instead of the YAM")
    p.add_argument("--yam-sim", action="store_true", help="YamArm on the I2RT SDK's MuJoCo SimRobot (not hardware)")
    p.add_argument("--channel", default=None, help="CAN channel (default $YAM_CAN_CHANNEL or can0)")
    p.add_argument("--gripper", default=None, help="gripper type (default $YAM_GRIPPER or linear_4310)")
    p.add_argument("--side", type=float, default=0.10, help="square side, metres (default 0.10)")
    p.add_argument("--speed", type=float, default=0.02, help="tool speed along an edge, m/s (default 0.02)")
    p.add_argument("--home-speed", type=float, default=0.15, help="joint-move speed fraction for homing")
    p.add_argument("--max-track-err", type=float, default=0.01, help="pass threshold: measured vs target, m")
    p.add_argument("--no-gripper", action="store_true", help="skip the gripper cycle")
    p.add_argument("--no-smoothing", action="store_true", help="YAM: send each command straight to the SDK")
    p.add_argument("--rest-at-end", action="store_true", help="fold to q = 0 before disconnecting")
    p.add_argument("--unsafe-no-safety-layer", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    args.adapter = "sim" if args.sim else "yam"

    arm = _build(args)
    desc = arm.describe()
    report: dict[str, Any] = {"adapter": desc, "simulated": bool(desc.get("sim")), "side_m": args.side,
                              "speed_mps": args.speed, "checks": {}}
    _log(f"adapter {desc.get('adapter')} sim={desc.get('sim')} wrapped_by={desc.get('wrapped_by')}")
    errs: list[float] = []
    sampler = _StateSampler(arm)
    arm.connect()
    try:
        st = arm.status()
        if not st.ok:
            raise SystemExit(f"arm not ok after connect: {st}")
        sampler.start()
        _log(f"homing to {arm.home_joints}")
        report["checks"]["home"] = arm.home(speed_fraction=args.home_speed)
        home = arm.ee_pose()
        report["home_pose"] = home.to_dict()

        def track(target: Pose, _q: tuple[float, ...]) -> None:
            errs.append(math.dist(target.xyz, arm.ee_pose().xyz))

        s = args.side
        corners = [home.translated(d) for d in ((s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, 0))]
        t_edge = s / max(args.speed, 1e-3)
        t0 = time.monotonic()
        prev = home
        for i, c in enumerate(corners):
            _log(f"edge {i + 1}/4 -> {c.to_dict()['xyz']} over {t_edge:.1f}s")
            move_ee_linear(arm, c, t_edge, start=prev, on_tick=track)
            prev = c
        time.sleep(0.5)
        report["square_s"] = round(time.monotonic() - t0, 2)
        closure = math.dist(arm.ee_pose().xyz, home.xyz)
        report["tracking_err_m"] = {"max": round(max(errs), 5), "mean": round(sum(errs) / len(errs), 5),
                                    "end_closure": round(closure, 5), "ticks": len(errs)}
        # tracking error is measured while moving (includes one control period of lag at speed)
        report["checks"]["square"] = max(errs) <= args.max_track_err and closure <= args.max_track_err / 2

        if not args.no_gripper:
            g_ok = True
            readings = []
            for g in (1.0, 0.0, 1.0):
                arm.command_gripper(g)
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and abs(arm.read_state().gripper - g) > 0.1:
                    time.sleep(0.05)
                got = round(arm.read_state().gripper, 3)
                readings.append({"cmd": g, "measured": got})
                g_ok = g_ok and abs(got - g) <= 0.1
            report["gripper_readings"] = readings
            report["checks"]["gripper"] = g_ok

        report["checks"]["home_again"] = arm.home(speed_fraction=args.home_speed)
        if args.rest_at_end and hasattr(arm, "rest_joints"):
            arm.move_joints(arm.rest_joints, speed_fraction=args.home_speed)  # type: ignore[attr-defined]
    except BaseException as e:
        report["error"] = f"{type(e).__name__}: {e}"
        try:
            arm.hold()
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(e, Exception):
            raise
    finally:
        sampler.stop_evt.set()
        if sampler.is_alive():
            sampler.join(timeout=1.0)
        report["state_stream"] = sampler.rate()
        report["status"] = {"ok": arm.status().ok, "detail": arm.status().detail}
        arm.disconnect()
    rate = report["state_stream"].get("hz", 0.0)
    report["checks"]["state_rate"] = rate >= 0.9 * arm.control_rate_hz
    report["passed"] = "error" not in report and all(report["checks"].values())
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(sys.argv[2:] if argv is None else argv))


__all__ = ["main", "run"]
