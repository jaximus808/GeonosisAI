# I2RT YAM SDK notes + YAM bring-up checklist (TDM-46, for the TDM-43 [HIL] spike)

Written on a laptop with **no arm and no Jetson**. Everything below was read from the SDK source or run against
the SDK's own MuJoCo simulator. Nothing has touched hardware. Items marked **TO VERIFY ON ARM** are for the human.

## 1. What the SDK is

| | |
|---|---|
| Repo | https://github.com/i2rt-robotics/i2rt (MIT). Docs: https://doc.i2rt.com |
| Tested ref | commit `120c3c8` (2026-09-17). `pyproject` version **1.1.2**, latest git tag **v1.3.6**: the two disagree, so pin the commit |
| PyPI | **not published** (the `i2rt` name 404s). Install from the git checkout |
| Python | `>=3.10` (we use 3.11). Pins `numpy==2.2.6`, `python-can==4.5.0`, `ruckig==0.15.3`, `mujoco>=3.3.5`, `mink`, `qpsolvers[quadprog]`, plus viser/rerun/pygame for its tools |
| Build gotcha | `ruckig 0.15.3` ships **only an sdist**, and its build fails under `scikit-build-core>=0.10`. The SDK pins the backend only through `[tool.uv] build-constraint-dependencies`, which uv ignores when you `uv pip install` from another project. Use a build constraint (below) and have cmake + a C++ compiler on the Jetson (`build-essential`) |

Install that worked (macOS arm64, uv 0.9, Python 3.11):

```bash
echo 'scikit-build-core<0.10' > /tmp/i2rt-build-constraints.txt
uv pip install --python services/robot-client/.venv/bin/python \
  --build-constraint /tmp/i2rt-build-constraints.txt -e /path/to/i2rt     # or UV_BUILD_CONSTRAINT=<file>
```

**`scripts/jetson/remote/deploy.sh` (TDM-44) runs `uv pip install -e "$I2RT_DIR"` without the constraint, so expect it
to fail on the ruckig build.** The fix is to add `--build-constraint <file>` (or export `UV_BUILD_CONSTRAINT`) on that
line and pin `I2RT_REF=120c3c8`. I left that file alone because it belongs to TDM-44 (see docs/morning-questions.md).
The script already passes `uv sync --inexact`, so the SDK survives later syncs. A plain `uv sync` would uninstall it.

## 2. Control interface

- Entry point: `i2rt.robots.get_robot.get_yam_robot(channel="can0", arm_type=ArmType.YAM, gripper_type=GripperType.LINEAR_4310, zero_gravity_mode=True, sim=False, gripper_limits_override=None, ...)`. It returns a `MotorChainRobot` (real arm) or a `SimRobot` (`sim=True`).
- **Mode: joint impedance (MIT mode) on DM motors over CAN.** Each motor gets `pos, vel, kp, kd, torque`. The torque is the
  MuJoCo gravity-compensation feed-forward (`× gravity_comp_factor`). YAM v1 gains are `kp = [80, 80, 80, 10, 10, 10]` and
  `kd = [5, 5, 5, 1.5, 1.5, 1.5]`. Motors: J1–J3 DM4340, J4–J6 DM4310, gripper DM4310 on CAN id 0x07.
- `command_joint_pos(q)` takes **7 values**: six arm joints in rad, plus the gripper **normalised 0 = closed .. 1 = open** (the same as
  our adapter convention). It only swaps the PD setpoint. A background thread streams it to the motors.
- **Rate:** the SDK's thread loops as fast as the bus allows. `dm_driver.CONTROL_FREQ = 250` Hz is used for the bus-bandwidth
  check, `EXPECTED_CONTROL_PERIOD = 7 ms`, and the robot server warns below 100 Hz. Our callers stream at **30 Hz** (policy rate).
  `YamArm` re-sends an interpolated setpoint at **200 Hz** in between (`smoothing`, on by default; `YAM_SMOOTHING=0` turns it off).
- `zero_gravity_mode=True` (the SDK default) makes the arm go **limp** (gravity comp only) until the first command. `YamArm` passes
  `False`, so it holds the boot pose with PD.
- **Motor timeout:** the factory default is **400 ms**. With no CAN command for that long, the motors drop into **damping mode**: the arm
  sinks slowly under gravity. That's what happens on `close()`, on a crashed process, or a pulled CAN cable. **Move the arm to rest (`q = 0`, folded)
  before disconnecting.** (`demo-square --rest-at-end`.)
- **SDK-side limits:** commands are clipped to the URDF ranges **± 0.15 rad**. If the **measured** pose leaves the URDF ranges by more than
  0.25 rad, the server thread raises and stops. That usually means a wrong zero offset. Its fix is to "move to zero and power-cycle or
  re-zero" (`motor_config_tool/set_zero.py`). `YamArm.status().fault` reports a dead chain or thread.
- Motor faults: `enable_auto_recovery=False` (the default, fail-fast) stops the chain on any motor error code.

## 3. Gripper

- Default `linear_4310`. Others: `linear_3507`, `crank_4310`, `flexible_4310`, `no_gripper`, `yam_teaching_handle` (the leader handle, passive).
- **Linear grippers need calibration.** Their motor travels more than 2π over the stroke. With no `gripper_limits_override`, `get_yam_robot` runs
  `detect_gripper_limits`, which **drives the gripper both ways at 0.5 Nm on connect**. Either start with it fully closed, or record the detected
  `[closed, open]` motor positions once and set `YAM_GRIPPER_LIMITS=closed,open` to skip it.
- A force limiter is built in (`limit_gripper_force=50` N for the arm+gripper chain).
- `YamArm.hold()` / `stop()` keep the gripper at its **last commanded** value. They don't use the measured opening, so a grasped object stays squeezed.

## 4. Kinematics

- The model ships in the SDK as `i2rt/robot_models/arm/yam/v1/yam.urdf` + `yam.xml` (MJCF), plus gripper MJCFs. `combine_arm_and_gripper_xml()` merges
  them. The model README publishes the DH table, the **PoE screw axes** and the home transform `M`.
- The SDK ships **FK + IK**: `i2rt.robots.kinematics.Kinematics(xml, "grasp_site")`, MuJoCo + `mink` differential IK (QP, quadprog). Its own
  examples call it **without joint limits** and let the driver clip.
- **Our approach** (`adapters/yam_kinematics.py`):
  - `sdk` backend (default when the SDK imports): the SDK's `Kinematics` + `mink.ConfigurationLimit`, so solutions respect the joint ranges. If mink
    stalls, it retries with the PoE solver and accepts only if the SDK's FK confirms the answer.
  - `poe` backend (no SDK): numpy PoE from the published screw axes + the `grasp_site` offset, with Levenberg–Marquardt damped least squares on an analytic
    Jacobian. It matches the SDK's MuJoCo model to **< 2.5e-6 m over random poses for all 5 grippers** (`tests/test_tdm46_yam.py`).
  - IK speed (laptop, 200 random poses with the seed within ~0.15 rad): sdk median **0.14–0.3 ms** (p95 < 0.9 ms), poe median **0.7–1.0 ms**. Both
    fit easily inside a 33 ms tick.
- **Tool point = `grasp_site`**: the midpoint of the finger tips, 0.1447 m past the mount for `linear_4310`. Its +Z is the approach axis. At `q = 0`
  the tool points along base +X, at (0.255, 0, 0.173) m.
- Joint ranges (rad, URDF): J1 [-2.618, 3.142], J2 [0, 3.665], J3 [0, 3.142], J4 [-1.693, 1.571], J5 [-1.571, 1.571], J6 [-2.094, 2.094].
  `YamArm.limits` = these. `YamArm.sdk_clip_limits` = these ± 0.15. The URDF `velocity="1"` (rad/s) on every joint is almost certainly a
  placeholder, but it's what `limits.max_velocity` reports. The safety layer's caps are lower anyway.
- **q = 0 is the folded rest pose, and J2/J3 sit on their lower limits there.** With TDM-45's `margin_rad: 0.10`, the first command from rest gets
  clamped up to 0.10 rad on J2/J3. It's harmless (speed-capped) but noisy in the logs.
- **Ready/home pose** `YAM_READY = (0, 0.98, 0.93, -0.73, 0, 0)`: tool pitched 45° down at (0.30, 0, 0.20) m, at least 0.8 rad from every limit over a
  ±5 cm square. A straight-down tool needs J4 near its −1.69 limit, so it's at the edge of the wrist range. **TO VERIFY ON ARM.**

## 5. Simulator

- `get_yam_robot(..., sim=True)` returns `SimRobot`, backed by MuJoCo. It has the same protocol and simulates gravity torques and temperatures, but
  `command_joint_pos` **teleports** (no dynamics or PD lag). So it checks API flow, kinematics, IK and timing, but **not** tracking dynamics, CAN
  behaviour or the gripper's force limiter.
- `YamArm(sim=True)` / `YAM_SIM=1` / `demo-square --yam-sim` use it. They register as embodiment `sim_yam` with `describe()["sim"] = True`.
- The SDK also has an interactive MuJoCo viewer (`examples/control_with_mujoco/control_with_mujoco.py --sim`; on macOS use `mjpython`) and a
  browser one (viser).

## 6. Our adapter (`adapters/yam.py`)

`make_adapter("yam", **kw)` creates `YamArm`. The I2RT import is lazy (in `connect()`), so laptops without the SDK still construct it and run
FK/IK through PoE. Config comes from kwargs, or else from the environment:

| env | default | |
|---|---|---|
| `YAM_CAN_CHANNEL` | `can0` | CAN interface the SDK opens |
| `YAM_ARM_TYPE` | `yam` | `yam`, `yam_pro`, `yam_ultra`, `yam_ultra_2`, `big_yam` (limits/home are YAM v1 values) |
| `YAM_GRIPPER` | `linear_4310` | see §3 |
| `YAM_GRIPPER_LIMITS` | unset | `closed,open` motor positions: skips the gripper calibration sweep |
| `YAM_SIM` | `0` | `1` = SDK MuJoCo SimRobot |
| `YAM_KINEMATICS` | `auto` | `sdk` / `poe` |
| `YAM_SMOOTHING` | `1` | 200 Hz interpolation between 30 Hz commands (adds one period ≈ 33 ms latency) |

Action space we record and train on (`yam.ACTION_SPACE`):
- `action = joint_pos_target[6] + gripper[1]` (7 floats, after safety).
- `ee_target` (xyz + wxyz) is stored alongside in `ik_target_pos/quat`.
- `observation.state = measured joints[6] + gripper[1]`.

Helpers that work on any (wrapped) arm:
- `yam.move_ee_linear(arm, pose, duration)`: straight-line Cartesian move, IK every tick.
- `yam.state_dict(arm)`: `{joint_pos, joint_vel, ee_pose, gripper, ts}`.

## 7. Simulated results (laptop, SDK MuJoCo SimRobot, NOT hardware)

`rdf-robot-client demo-square --yam-sim` (wrapped by the TDM-45 SafetyLayer, `yam` profile, unverified → speed scale 0.25) gave:
- **PASS**: home → 10 cm square at 2 cm/s (20.6 s) → gripper 1/0/1 → home.
- Max tracking error 1.8–3.9 mm while moving (this includes one control period of lag); closure 0.0 mm.
- State stream **30.0 Hz** (p50 gap 33.3 ms; one run had a 330 ms stall under heavy laptop load, and the next run's max gap was 53 ms).
- Servo thread ~186 Hz on average.

`demo-square --sim` (TDM-49 SimArm) also passes. Tests: `tests/test_tdm46_yam.py`: 16 pass without the SDK; with the SDK, 17 pass plus the
SDK-vs-PoE cross-check.

## 8. Bring-up checklist (human at the arm, physical e-stop in hand)

1. [ ] **Power & CAN.** Run `ls -l /sys/class/net/can*`, then `sudo ip link set can0 up type can bitrate 1000000` (or the `rdf-can` service). With the arm
   powered, `candump can0` shows traffic. Record which adapter it is (USB gs_usb vs Jetson mttcan) and the interface name → `YAM_CAN_CHANNEL`.
2. [ ] **SDK install.** Use the build constraint from §1 and pin commit `120c3c8`. Check with
   `.venv/bin/python -c "import i2rt, mink, mujoco; print('ok')"`.
3. [ ] **Which gripper?** Read the label or model → `YAM_GRIPPER`. If it's a linear gripper, start it **fully closed** for the first connect, or be ready for
   the calibration sweep.
4. [ ] **SDK alone first.** Run `python i2rt/robots/motor_chain_robot.py --channel can0 --arm yam --gripper linear_4310 --operation-mode stay_current_qpos`
   from the i2rt checkout (`python -m i2rt.robots.motor_chain_robot` works too). The arm should hold still. Ctrl-C makes it sink slowly (damping), so support it.
   If you get "Joint limit violation detected": the zero offsets are wrong → `set_zero.py` per motor (I2RT docs).
5. [ ] **Zero pose check.** With the arm folded at rest, `YamArm.read_state().joints` reads ≈ 0 on every joint (± a few hundredths).
6. [ ] **FK sanity.** Hand-guide or jog to a few poses. Measure the grasp point with a ruler from the base and compare it with `ee_pose`. Check the base frame
   is +x forward, +z up, and the origin is at the base bottom (so z(table) ≈ 0 if the base sits on the table).
7. [ ] **Ready pose.** Run `rdf-robot-client demo-square --side 0.0 --no-gripper` (homing only): the arm goes rest → `YAM_READY`. Check the clearance to the table
   and anything nearby. Adjust `home` if needed.
8. [ ] **Gripper.** Run `demo-square --side 0.0`: it goes open → closed → open, and `gripper_readings` matches within 0.1. Note the calibration result →
   `YAM_GRIPPER_LIMITS`.
9. [ ] **CP-1: `rdf-robot-client demo-square`** (10 cm, 2 cm/s, safety layer on). It should be smooth with no buzzing, report `"passed": true`, and give a
   state-stream `hz` ≈ 30. Then try `--no-smoothing` and compare by feel and sound: is the 30 Hz staircase audible? Keep whichever is smoother
   (`YAM_SMOOTHING`).
10. [ ] **E-stop paths.** Mid-square, the soft e-stop (`stop` on the command loop) halts within a tick. The physical e-stop cuts power. After it, power-cycle
    and restart the client.
11. [ ] Run `--rest-at-end` before you shut down, so the arm is folded when the motors time out.
12. [ ] Record in this file: the SDK commit on the Jetson, the gripper type, the CAN interface, `sdk_loop_hz` from `status().detail`, and anything that
    behaved differently from the notes above.

## 9. Open questions / unverified

- The real loop rate of the SDK on the Jetson (`status().detail["sdk_loop_hz"]`). It's 250 Hz nominal, and the SDK warns below 100.
- Whether 30 Hz setpoints with smoothing feel smooth on the real PD (kp 80). Dynamics are untested; the sim teleports.
- The real zero offsets / home clearance, and the gripper model and calibration.
- The velocity limits: the URDF's 1 rad/s is a placeholder. The motor `VMAX` registers can be read with `motor_config_tool/dm_motor_registers.py read-all`.
