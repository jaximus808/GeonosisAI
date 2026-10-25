"""TDM-119: real right-YAM bring-up, one small step at a time. THE USER RUNS EVERY STEP, hand on the e-stop.

Rules: docs/real-arm-safety.md. Runbook: docs/tdm119-teleop-runbook.md. On the Jetson, run it through
``scripts/jetson/remote/tdm119_rt.sh <cmd>`` (it starts it under the user systemd manager, which has
RTPRIO 20, so the fork's 250 Hz CAN thread gets SCHED_FIFO; over plain ssh it would warn and run at
normal priority).

    tdm119_bringup preflight             # no CAN: vrteleop stopped, no arm process, flocks free, CAN ERROR-ACTIVE, RTPRIO
    tdm119_bringup hold [--seconds 10]   # enable the right arm the LARP way, hold the measured pose, release
    tdm119_bringup jog [--ready] [--dx 0.02] [--speed 0.02] [--rest-at-end]
                                         # +dx in x at <= speed through command_ee (teleop's path) and back,
                                         # then the soft e-stop test (latch + hold < 300 ms), clear, release
    tdm119_bringup sign-off              # the USER flips verified: true (writes ~/.config/geonosis/safety.yaml)
    tdm119_bringup verified-check        # exit 0 iff the yam profile in use is verified (the teleop unit's guard)

Every motion step asks for ``go`` first (``--yes`` only with ``--adapter sim``). Every step that enables the arm
ends with the LARP release (damp until settled, then disable), and **the arm goes limp after it: support it**.
The arm is ``LarpSafeYamArm`` wrapped in the TDM-45 SafetyLayer, exactly as the client builds it.
Nothing here zeroes, calibrates the arm, writes motor registers or loops error clears.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

TDM119_LOCAL_SAFETY = Path.home() / ".config" / "geonosis" / "safety.yaml"
TDM119_REPO_SAFETY = Path(__file__).resolve().parents[2] / "config" / "safety.yaml"
TDM119_STOP_BUDGET_MS = 300.0
TDM119_EXIT_REFUSED = 3
SIGN_OFF_PHRASE = "I verified the yam profile at the arm"


def _say(step: str, ok: bool | None, **kv: Any) -> dict[str, Any]:
    tag = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    print(f"[{tag}] {step} {json.dumps(kv, default=str)}", flush=True)
    return {"step": step, "ok": ok, **kv}


def _confirm(msg: str, yes: bool) -> bool:
    print(f"\n>>> {msg}", flush=True)
    if yes:
        return True
    try:
        return input("    hand on the e-stop? type 'go' to run, anything else stops: ").strip().lower() == "go"
    except (EOFError, OSError):  # no terminal: never a yes
        return False


def tdm119_safety_path() -> Path:
    """The safety YAML the arm uses: $RDF_SAFETY_CONFIG if that file exists, else the repo default (unverified)."""
    p = os.environ.get("RDF_SAFETY_CONFIG")
    return Path(p) if p and Path(p).is_file() else TDM119_REPO_SAFETY


def tdm119_rt_threads() -> int:
    """How many threads of this process run SCHED_FIFO/RR (Linux /proc; 0 elsewhere)."""
    n = 0
    for st in Path("/proc/self/task").glob("*/stat"):
        try:
            fields = st.read_text().rsplit(")", 1)[1].split()
            if int(fields[38]) in (1, 2):  # field 41 = policy (SCHED_FIFO 1, SCHED_RR 2)
                n += 1
        except (OSError, IndexError, ValueError):
            continue
    return n


# ----------------------------------------------------------------------------------------------- preflight


def _can_state(ch: str) -> str:
    try:
        out = subprocess.run(["ip", "-details", "-json", "link", "show", ch], capture_output=True, text=True,
                             timeout=5, check=False).stdout
        info = json.loads(out)[0]
        return str(info.get("linkinfo", {}).get("info_data", {}).get("state") or info.get("operstate") or "?")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return "missing"


def _lock_free(ch: str) -> bool | None:
    import fcntl

    from .adapters.yam_larp import larp_lock_path

    path = larp_lock_path(ch)
    if not os.path.exists(path):
        return True
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except (BlockingIOError, PermissionError):
        return False
    finally:
        os.close(fd)


def preflight(*, run: Callable[..., Any] = subprocess.run) -> int:
    """No CAN traffic, no camera opened: only systemctl / pgrep / flock / ip / rlimit reads."""
    import resource

    rows = []
    vr = run(["systemctl", "is-active", "--quiet", "vrteleop-rh"], check=False).returncode == 0
    rows.append(_say("vrteleop-rh stopped", not vr, fix=None if not vr else "sudo systemctl stop vrteleop-rh"))
    pg = run(["pgrep", "-af", "vrteleop|yam_robot_relay|arm_server"], capture_output=True, text=True, check=False)
    procs = [ln for ln in (pg.stdout or "").splitlines() if ln.strip() and "pgrep" not in ln]
    rows.append(_say("no other arm process", not procs, procs=procs[:5]))
    for ch in ("can_right", "can_left"):
        rows.append(_say(f"flock {ch} free", bool(_lock_free(ch))))
    st = _can_state("can_right")
    rows.append(_say("can_right ERROR-ACTIVE", st == "ERROR-ACTIVE", state=st))
    ch = os.environ.get("YAM_CAN_CHANNEL")
    rows.append(_say("YAM_CAN_CHANNEL is can_right", ch == "can_right", value=ch))
    soft, hard = resource.getrlimit(resource.RLIMIT_RTPRIO) if hasattr(resource, "RLIMIT_RTPRIO") else (0, 0)
    _say("RTPRIO limit (>= 10 lets the CAN thread run SCHED_FIFO; else it warns and runs normal)", None,
         soft=soft, hard=hard)
    _say("safety profile in use", None, path=str(tdm119_safety_path()), verified=_verified(tdm119_safety_path()))
    return 0 if all(r["ok"] for r in rows) else 1


# ----------------------------------------------------------------------------------------------- the arm


def build_safe_arm(adapter: str = "yam", **kw: Any) -> Any:
    """LarpSafeYamArm (or SimArm for a dry run) inside the SafetyLayer with the safety YAML in use."""
    from .adapters import make_adapter
    from .safety import SafetyLayer

    inner = make_adapter(adapter, **kw)
    return SafetyLayer(inner, config_path=tdm119_safety_path())


def _inner(arm: Any) -> Any:
    while hasattr(arm, "inner"):
        arm = arm.inner
    return arm


def _report_connected(arm: Any) -> dict[str, Any]:
    inner = _inner(arm)
    robot = getattr(inner, "_robot", None)
    lim = getattr(robot, "_gripper_limits", None)
    st = arm.status()
    out = _say("enabled and holding the measured pose", st.fault is None and not st.estopped,
               fork=getattr(inner, "fork_version", None), joints=[round(v, 4) for v in arm.read_state().joints],
               gripper_limits=None if lim is None else [round(float(v), 4) for v in lim],
               rt_threads=tdm119_rt_threads(), fault=st.fault)
    if lim is not None:
        print(f"    to skip the gripper calibration next time: YAM_GRIPPER_LIMITS={float(lim[0]):.4f},{float(lim[1]):.4f}")
    return out


def _release(arm: Any, yes: bool) -> None:
    print("\n>>> RELEASE: the arm is damped until it settles, then disabled. It goes LIMP after that: support it.")
    if not yes:
        try:
            input("    hand under the arm? press Enter to release: ")
        except (EOFError, OSError):
            pass
    t0 = time.monotonic()
    arm.disconnect()
    _say("released (damp until settled, then disable; the fork prints one line per motor)", None,
         seconds=round(time.monotonic() - t0, 2))


def hold(seconds: float, *, yes: bool = False, arm_factory: Callable[[], Any] = build_safe_arm) -> int:
    if not _confirm(f"HOLD: enable the right arm (motors 1-7 in id order), hold the measured pose {seconds:.0f} s, "
                    "release. The arm should not move at all.", yes):
        return TDM119_EXIT_REFUSED
    arm = arm_factory()
    try:
        arm.connect()
    except Exception as e:  # noqa: BLE001 - refusals (fault, limits, gains, lock, stock i2rt) are one line
        _say("enable refused", False, error=str(e)[:400])
        return TDM119_EXIT_REFUSED
    ok = True
    try:
        _report_connected(arm)
        q0 = arm.read_state().joints
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            time.sleep(1.0)
            st, q = arm.status(), arm.read_state().joints
            drift = max(abs(a - b) for a, b in zip(q, q0))
            ok = ok and st.fault is None and drift < 0.02
            _say("holding", st.fault is None and drift < 0.02, drift_rad=round(drift, 4), fault=st.fault)
    finally:
        _release(arm, yes)
    _say("hold step", ok)
    return 0 if ok else 1


# ----------------------------------------------------------------------------------------------- jog + e-stop


def _stream_ee(arm: Any, dx: float, speed: float, *, stop_after_s: float | None = None) -> dict[str, Any]:
    """Straight +dx (m) in base x at <= speed (m/s) through arm.command_ee at 30 Hz: teleop's path."""
    from .adapters.base import ArmStopped, Pose

    period = 1.0 / 30.0
    p0 = arm.ee_pose()
    seed = arm.joint_positions()
    duration = max(abs(dx) / max(speed, 1e-4), period)
    t0 = time.monotonic()
    while True:
        a = min((time.monotonic() - t0) / duration, 1.0)
        target = Pose((p0.xyz[0] + dx * a, p0.xyz[1], p0.xyz[2]), p0.quat)
        if stop_after_s is not None and time.monotonic() - t0 >= stop_after_s:
            return {"stopped_at_s": round(time.monotonic() - t0, 3)}
        try:
            seed = arm.command_ee(target, seed=seed)
        except ArmStopped:
            return {"latched": True}
        if a >= 1.0:
            break
        time.sleep(period)
    time.sleep(0.5)
    p1 = arm.ee_pose()
    return {"moved_x_m": round(p1.xyz[0] - p0.xyz[0], 4), "off_axis_m": round(max(abs(p1.xyz[1] - p0.xyz[1]),
                                                                                  abs(p1.xyz[2] - p0.xyz[2])), 4)}


def estop_test(arm: Any, *, budget_ms: float = TDM119_STOP_BUDGET_MS) -> dict[str, Any]:
    """Slow +1 cm jog, soft stop 0.4 s in: stop() must latch and the measured joints must hold < budget_ms."""
    from .adapters.base import ArmStopped, Pose

    _stream_ee(arm, 0.01, 0.01, stop_after_s=0.4)
    t0 = time.monotonic()
    arm.stop()
    call_ms = (time.monotonic() - t0) * 1000
    prev, still, hold_ms = arm.read_state().joints, 0, None
    while (time.monotonic() - t0) * 1000 < 1000:
        time.sleep(0.01)
        q = arm.read_state().joints
        if max(abs(a - b) for a, b in zip(q, prev)) < 5e-4:
            still += 1
            if still >= 3 and hold_ms is None:
                hold_ms = (time.monotonic() - t0) * 1000 - 20
        else:
            still = 0
        prev = q
    q_stop = arm.read_state().joints
    time.sleep(0.5)
    drift = max(abs(a - b) for a, b in zip(arm.read_state().joints, q_stop))
    try:
        arm.command_ee(Pose(arm.ee_pose().xyz, arm.ee_pose().quat))
        latched = False
    except ArmStopped:
        latched = True
    ok = latched and hold_ms is not None and hold_ms < budget_ms and drift < 0.01 and call_ms < 50
    return _say("soft e-stop: latch + hold", ok, stop_call_ms=round(call_ms, 1),
                hold_ms=None if hold_ms is None else round(max(hold_ms, 0.0), 1), drift_after_rad=round(drift, 4),
                latched=latched, budget_ms=budget_ms)


def jog(*, dx: float = 0.02, speed: float = 0.02, ready: bool = False, rest_at_end: bool = False,
        yes: bool = False, arm_factory: Callable[[], Any] = build_safe_arm) -> int:
    from .adapters.yam import YAM_READY, YAM_REST

    if speed > 0.02 + 1e-9 or abs(dx) > 0.05 + 1e-9:
        _say("jog refused: this test is <= 5 cm at <= 2 cm/s", False, dx=dx, speed=speed)
        return TDM119_EXIT_REFUSED
    if not _confirm(f"JOG: enable, {'slow move to the ready pose, ' if ready else ''}+{dx * 100:.0f} cm in x at "
                    f"{speed * 100:.0f} cm/s and back, then the soft e-stop test.", yes):
        return TDM119_EXIT_REFUSED
    arm = arm_factory()
    try:
        arm.connect()
    except Exception as e:  # noqa: BLE001
        _say("enable refused", False, error=str(e)[:400])
        return TDM119_EXIT_REFUSED
    results: list[dict[str, Any]] = []
    try:
        results.append(_report_connected(arm))
        lim = arm.limits
        q = arm.joint_positions()
        inside = all(lo <= v <= hi for v, lo, hi in zip(q, lim.lower, lim.upper))
        if ready:
            if not _confirm(f"READY: joint-space move to {list(YAM_READY)} at the safety layer's speed "
                            "(about 0.2 rad/s, ~30 s from rest). Clear space in front of the arm.", yes):
                return TDM119_EXIT_REFUSED
            arrived = arm.move_joints(YAM_READY)
            results.append(_say("ready pose", arrived, joints=[round(v, 3) for v in arm.joint_positions()]))
            inside = arrived
        if not inside:
            results.append(_say("jog start pose is outside the safe joint range (e.g. folded rest: J2/J3 at 0); "
                                "rerun with --ready", False, joints=[round(v, 3) for v in q]))
            return 1
        out = _stream_ee(arm, dx, speed)
        results.append(_say(f"+{dx * 100:.0f} cm x", abs(out.get("moved_x_m", 0) - dx) < 0.005 and out["off_axis_m"] < 0.005,
                            **out))
        back = _stream_ee(arm, -dx, speed)
        results.append(_say(f"-{dx * 100:.0f} cm x (back)", abs(back.get("moved_x_m", 0) + dx) < 0.005, **back))
        if not _confirm("E-STOP TEST: slow +1 cm jog, then a soft stop 0.4 s in. It must latch and hold.", yes):
            return TDM119_EXIT_REFUSED
        results.append(estop_test(arm))
        if _confirm("CLEAR: unlatch the soft stop (the arm keeps holding).", yes):
            arm.clear_stop()
            results.append(_say("cleared, holding", not arm.status().estopped))
        if rest_at_end and _confirm("REST: slow joint-space move back toward the folded rest pose "
                                    "(J2/J3 stop 0.1 rad above their limits).", yes):
            arm.move_joints(YAM_REST)
            results.append(_say("near rest", None, joints=[round(v, 3) for v in arm.joint_positions()]))
    finally:
        _release(arm, yes)
    ok = all(r["ok"] is not False for r in results)
    _say("jog step", ok)
    return 0 if ok else 1


# ----------------------------------------------------------------------------------------------- sign-off


def _verified(path: Path) -> bool:
    import yaml

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(((data.get("profiles") or {}).get("yam") or {}).get("verified") is True)


def tdm119_signed_off() -> bool:
    p = tdm119_safety_path()
    return p != TDM119_REPO_SAFETY and _verified(p)


def verified_check() -> int:
    p = tdm119_safety_path()
    ok = p != TDM119_REPO_SAFETY and _verified(p)
    _say("yam profile verified (user sign-off)", ok, path=str(p),
         fix=None if ok else "complete the runbook steps, then: tdm119_rt.sh sign-off")
    return 0 if ok else TDM119_EXIT_REFUSED


def sign_off(*, dest: Path = TDM119_LOCAL_SAFETY, source: Path = TDM119_REPO_SAFETY,
             answer: Callable[[str], str] = input) -> int:
    """The USER flips verified: true for the yam profile, in a Jetson-local copy of safety.yaml."""
    print(f"This sets profiles.yam.verified: true in {dest} (copied from {source}).\n"
          "Only do it after the hold, jog and e-stop steps passed with you at the arm.")
    try:
        typed = answer(f'Type exactly "{SIGN_OFF_PHRASE}": ').strip()
    except EOFError:
        typed = ""
    if typed != SIGN_OFF_PHRASE:
        _say("sign-off", False, reason="phrase not typed; nothing written")
        return TDM119_EXIT_REFUSED
    text = (dest if dest.is_file() else source).read_text()
    m = re.search(r"(?ms)^  yam:\n(.*?)(?=^  \S|\Z)", text)
    if not m or not re.search(r"(?m)^    verified: (true|false)\b", m.group(1)):
        _say("sign-off", False, reason="could not find profiles.yam.verified")
        return 1
    block = re.sub(r"(?m)^    verified: (true|false)\b.*$",
                   f"    verified: true             # signed off by {os.environ.get('USER', '?')} at "
                   f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} (tdm119_bringup sign-off)", m.group(1), count=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        shutil.copy2(dest, dest.with_suffix(".yaml.bak"))
    dest.write_text(text[: m.start(1)] + block + text[m.end(1):])
    ok = _verified(dest)
    _say("sign-off", ok, path=str(dest))
    return 0 if ok else 1


# ----------------------------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tdm119_bringup", description=__doc__.split("\n")[0])
    p.add_argument("--adapter", default="yam", help="yam (real arm) | sim (laptop dry run)")
    p.add_argument("--yes", action="store_true", help="skip the 'go' prompts (only allowed with --adapter sim)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    h = sub.add_parser("hold")
    h.add_argument("--seconds", type=float, default=10.0)
    j = sub.add_parser("jog")
    j.add_argument("--dx", type=float, default=0.02)
    j.add_argument("--speed", type=float, default=0.02)
    j.add_argument("--ready", action="store_true")
    j.add_argument("--rest-at-end", action="store_true")
    sub.add_parser("sign-off")
    sub.add_parser("verified-check")
    a = p.parse_args(argv)
    if a.yes and a.adapter != "sim":
        p.error("--yes is only for --adapter sim: on the real arm every step asks for 'go'")

    def factory() -> Any:
        return build_safe_arm(a.adapter)

    if a.cmd == "preflight":
        return preflight()
    if a.cmd == "hold":
        return hold(a.seconds, yes=a.yes, arm_factory=factory)
    if a.cmd == "jog":
        return jog(dx=a.dx, speed=a.speed, ready=a.ready, rest_at_end=a.rest_at_end, yes=a.yes, arm_factory=factory)
    if a.cmd == "sign-off":
        return sign_off()
    return verified_check()


if __name__ == "__main__":
    sys.exit(main())
