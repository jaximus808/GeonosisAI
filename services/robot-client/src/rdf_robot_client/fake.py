"""Fake robot (TDM-28): behaves like the Jetson robot-client on the SpacetimeDB bus, with no arm.

    rdf-robot-client --fake --host http://127.0.0.1:3028 --db tdm28loop --token-file <robot_client.token>

It registers a robot, heartbeats, subscribes to ``robot_command`` for that robot and answers every
command the way the real client will: ``ack_robot_command`` first, then ``complete_robot_command``
or ``fail_robot_command``. Eval commands produce synthetic ``episode`` + ``eval_trial`` rows with a
configurable success probability, and the robot asks for a scene reset after every trial.

EVERYTHING IT WRITES IS FAKE AND SAYS SO:
  * robot:       name ``fake-...``, embodiment ``fake_yam``, adapter_version ``fake-tdm28``,
                 camera_config_json ``{"fake": true, ...}``
  * episode:     a real-format episode (TDM-25 ``rdf_common.episode_format``) of SYNTHETIC data, in
                 ``<media-dir>/FAKE-ep-*/`` (storage_uri): ``actions.parquet`` written with
                 ``write_actions`` (metadata ``fixture: true``, ``fake: true``, note ``[FAKE] ...``) and
                 one small cartoon mp4 per camera (``exo.mp4``, ``wrist.mp4``) rendered by
                 ``services/orchestrator/tests/tdm25_fixtures.py``. cameras_json is
                 ``{"exo": {"uri": "file://...", "fake": true}, "wrist": {...}}``. URIs are local
                 ``file://`` paths: the reviewer reads them directly, nothing is uploaded.
                 Clips (rendered once per process, then hard-linked): ``clean`` (10 s pick-and-place,
                 human-driven; clean_teleop), ``takeover`` (11.5 s: policy misses the grasp, human takes
                 over at 4.0 s and hands back at 9.8 s; recovery_takeover, takeover_ts/handback_ts and
                 control_source match), ``missed_grasp`` (6 s, policy; failed eval trials). Successful
                 eval trials use ``clean`` driven by the policy. The episode's duration_ms is the clip's
                 length; --trial-ms / --teleop-ms only set how long the simulated job takes.
                 The production reviewer rejects ``fixture: true``; the harness review config
                 (tests/integration/review_fixtures.yaml) allows it.
  * eval_trial:  failure_note always starts with ``[FAKE]`` (successes too)
  * robot_command.result_json and every event it appends: ``"fake": true``
  * it refuses any non-local SpacetimeDB host unless --allow-remote is passed, and refuses
    maincloud outright: fake eval numbers must never sit next to real ones.

Command payload contract (what the orchestrator puts in ``robot_command.payload_json``; the real
client should accept the same keys):
  run_eval / run_baseline  {"eval_run_id": int, "checkpoint_uri": str?, "trials": int?}
                           eval_run_id missing -> the robot's newest ``running`` eval_run for the
                           command's ticket; none -> trials are run and reported only in result_json.
                           Each trial: episode (kind eval|baseline) + record_eval_trial + reset.
  start_teleop             {"task_id": int?, "kind": "clean_teleop"|"recovery_takeover"?,
                            "contributor_id": int?} -> one fake teleop episode, then done.
  reset_request            {} -> simulated human reset, then done.
  load_checkpoint / home   {"checkpoint_uri": str?} -> short delay, done.
  stop                     aborts the running job (it completes with aborted=true), then done.
  soft_estop               the module already preempted open commands; we heartbeat ``estop``,
                           complete it, and clear back to ``online`` after --estop-clear-seconds
                           (0 = stay latched, like the real arm until a human clears it).
  stop_teleop / pause_for_takeover / resume_policy / handback   ack + done immediately.
Fake-only payload keys (ignored by the real client): ``fake_success_prob`` (float),
``fake_fail_command`` (bool: fail the command instead of running it), ``fake_time_scale`` (float:
override --time-scale for this command, e.g. 5 to make an eval slow enough to e-stop mid-run).

Reset requests appear in event_log as ``robot.reset_requested`` / ``robot.reset_done`` with
actor ``fake-robot:<id>`` and ``"fake": true`` in the payload.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FAKE_TAG = "[FAKE]"
JOB_TYPES = {"run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint"}
FAILURE_MODES = ["missed grasp", "dropped object", "collision with bin rim", "timeout before place"]
FAKE_CAMERAS = ("exo", "wrist")
# The TDM-25 synthetic-episode generator (cartoon video + min-jerk joints with encoder noise). It is
# test code in the orchestrator package; the fake robot only ever runs from this repo.
_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "orchestrator" / "tests"
# policy closes beside the cup (a missed grasp), opens and returns home; cup untouched
MISSED_GRASP_PLAN = [
    (0.5, "home", 1.0, False), (2.5, "above_cup", 1.0, False), (3.2, "beside_cup", 1.0, False),
    (3.6, "beside_cup", 0.0, False), (4.0, "beside_cup", 0.0, False), (6.0, "home", 1.0, False),
]


def _fixtures():
    try:
        import tdm25_fixtures
    except ImportError:
        if str(_FIXTURES_DIR) not in sys.path:
            sys.path.append(str(_FIXTURES_DIR))
        try:
            import tdm25_fixtures
        except ImportError as e:
            raise RuntimeError(f"fake robot episodes need {_FIXTURES_DIR / 'tdm25_fixtures.py'} "
                               f"(or pass --no-episodes): {e}") from e
    return tdm25_fixtures


@dataclass
class _Clip:
    """One synthetic recording, rendered once: sampled plan + one mp4 per camera."""
    name: str
    t: Any  # (N,) seconds from the start
    joints: Any  # (N, 6) commanded joints, rad
    pix: Any
    grip: Any
    takeover_s: float | None
    handback_s: float | None
    videos: dict[str, tuple[Path, int]]  # camera -> (mp4, frames)


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "fake-robot", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


@dataclass
class FakeConfig:
    host: str
    db: str
    token: str
    name: str = "fake-yam-1"
    success_prob: float = 0.3  # baseline / unknown checkpoints
    candidate_success_prob: float = 0.9  # eval_run.kind == candidate (a fine-tuned checkpoint)
    # TDM-70: per-ticket schedule for candidate evals: the k-th distinct candidate checkpoint of a
    # ticket succeeds with probability schedule[k] (the last value repeats). Empty: candidate_success_prob.
    candidate_success_schedule: tuple[float, ...] = ()
    trial_ms: tuple[int, int] = (15_000, 40_000)  # simulated trial length reported in duration_ms
    reset_ms: tuple[int, int] = (5_000, 15_000)
    time_scale: float = 40.0  # wall clock = simulated / time_scale
    teleop_ms: tuple[int, int] = (20_000, 60_000)
    estop_clear_seconds: float = 3.0
    command_fail_prob: float = 0.0
    heartbeat_seconds: float = 5.0
    media_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "tdm-28" / "media")
    seed: int | None = None
    episodes: bool = True  # create episode rows (needs the TDM-11 create_episode reducer)
    sim: bool = False  # TDM-107: register as a sim-mode robot (camera_config_json.sim = true), still [FAKE]


class Aborted(Exception):
    pass


class FakeRobot:
    def __init__(self, cfg: FakeConfig) -> None:
        from rdf_common.spacetime_client import SpacetimeClient, SpacetimeConfig

        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.stdb_cfg = SpacetimeConfig.resolve(cfg.host, cfg.db, cfg.token)
        self.http = SpacetimeClient(config=self.stdb_cfg)
        self.robot_id: int | None = None
        self.status = "online"
        self.sub = None
        self.jobs: queue.Queue = queue.Queue()
        self.controls: queue.Queue = queue.Queue()
        self.seen: set[int] = set()
        self.seen_lock = threading.Lock()
        self.current_job: int | None = None
        self.abort = threading.Event()
        self.stopping = threading.Event()
        self.has_create_episode = False
        self._clips: dict[str, _Clip] = {}
        self._clips_lock = threading.Lock()
        self._estop_timer: threading.Timer | None = None
        self._scale = cfg.time_scale

    # ------------------------------------------------------------------ reducers
    def call(self, reducer: str, *args: Any) -> None:
        self.http.call_reducer(reducer, *args)

    def event(self, type_: str, **payload: Any) -> None:
        payload = {"fake": True, "robot_id": self.robot_id, **payload}
        try:
            self.call("append_event", f"fake-robot:{self.robot_id}", type_, json.dumps(payload, default=str))
        except Exception as e:  # noqa: BLE001 - evidence is best effort, never kills the robot
            _log("append_event failed", type=type_, error=str(e))

    def heartbeat(self, status: str | None = None) -> None:
        if status:
            self.status = status
        try:
            self.call("robot_heartbeat", self.robot_id, self.status)
        except Exception as e:  # noqa: BLE001
            _log("heartbeat failed", error=str(e))

    # ------------------------------------------------------------------ boot
    def boot(self) -> None:
        schema = self.http.schema()
        self.has_create_episode = "create_episode" in schema.reducers
        if self.cfg.episodes and not self.has_create_episode:
            # TODO(TDM-11): create_episode is not in the published module yet; trials are
            # recorded with episode_id 0 until it lands.
            _log("create_episode reducer missing: trials will have no episode rows (TODO TDM-11)")
        if self.cfg.episodes:
            _fixtures()  # fail at boot, not on the first episode
            if not shutil.which("ffmpeg"):
                raise RuntimeError("fake robot episodes need ffmpeg to render the camera mp4s (or pass --no-episodes)")
        cams = {"fake": True, "tag": FAKE_TAG, "cameras": {c: f"fake://{c}" for c in FAKE_CAMERAS},
                **({"sim": True} if self.cfg.sim else {})}  # TDM-107: stands in for the MuJoCo sim robot
        self.call("register_robot", self.cfg.name, "fake_yam", "fake-tdm28", json.dumps(cams))
        rows = self.http.sql(f"SELECT * FROM robot WHERE name = '{self.cfg.name}'")
        if not rows:
            raise RuntimeError(f"register_robot did not create {self.cfg.name}")
        self.robot_id = int(rows[0]["id"])
        if rows[0].get("status") == "estop":
            _log("robot was left in estop; clearing (fake robot boot)")
        self.heartbeat("online")
        _log("registered", robot_id=self.robot_id, name=self.cfg.name, host=self.cfg.host, db=self.cfg.db)
        self.event("robot.fake_client_started", name=self.cfg.name,
                   success_prob=self.cfg.success_prob, candidate_success_prob=self.cfg.candidate_success_prob)

    def start(self) -> None:
        from rdf_common.spacetime_client import Subscription

        self.boot()
        rid = self.robot_id
        self.sub = Subscription(
            [f"SELECT * FROM robot_command WHERE robot_id = {rid}",
             f"SELECT * FROM eval_run WHERE robot_id = {rid}"],
            on_insert=self._on_insert,
            on_update=self._on_update,
            on_applied=self._on_applied,
            on_error=lambda e: _log("subscription error", error=str(e)),
            config=self.stdb_cfg,
            max_backoff=5.0,
        ).start_thread()
        for target, name in ((self._job_loop, "fake-jobs"), (self._control_loop, "fake-controls"),
                             (self._heartbeat_loop, "fake-heartbeat")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()
        self.abort.set()
        try:
            self.heartbeat("offline")
        finally:
            if self.sub:
                self.sub.stop()
            self.http.close()

    # ------------------------------------------------------------------ subscription callbacks
    def _offer(self, row: dict, snapshot: bool) -> None:
        cid = int(row["id"])
        with self.seen_lock:
            if cid in self.seen:
                return
            self.seen.add(cid)
        if row["status"] == "issued":
            (self.jobs if row["type"] in JOB_TYPES else self.controls).put(row)
        elif row["status"] == "acked" and snapshot:
            # Acked before this process started: the real client lost its state, so it fails it.
            threading.Thread(target=self._safe_fail, args=(cid, "fake robot restarted mid-command"),
                             daemon=True).start()

    def _on_applied(self, snapshot: dict) -> None:
        for row in sorted(snapshot.get("robot_command", []), key=lambda r: int(r["seq"])):
            self._offer(row, snapshot=True)

    def _on_insert(self, table: str, row: dict, meta: dict) -> None:
        if table == "robot_command":
            self._offer(row, snapshot=False)

    def _on_update(self, table: str, old: dict, new: dict, meta: dict) -> None:
        if table == "robot_command" and new["status"] == "preempted" and int(new["id"]) == self.current_job:
            _log("current job preempted", command_id=new["id"], by=new.get("result_json"))
            self.abort.set()

    # ------------------------------------------------------------------ loops
    def _heartbeat_loop(self) -> None:
        while not self.stopping.wait(self.cfg.heartbeat_seconds):
            self.heartbeat()

    def _control_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                row = self.controls.get(timeout=0.5)
            except queue.Empty:
                continue
            self._handle(row)

    def _job_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                row = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._handle(row)

    # ------------------------------------------------------------------ command handling
    def _command_status(self, cid: int) -> str | None:
        row = self.sub.get("robot_command", cid) if self.sub else None
        return row["status"] if row else None

    def _safe_fail(self, cid: int, error: str) -> None:
        try:
            self.call("fail_robot_command", cid, f"{FAKE_TAG} {error}")
        except Exception as e:  # noqa: BLE001
            _log("fail_robot_command refused", command_id=cid, error=str(e))

    def _handle(self, row: dict) -> None:
        cid, ctype = int(row["id"]), row["type"]
        if self._command_status(cid) not in (None, "issued"):
            _log("skip: command no longer issued", command_id=cid, status=self._command_status(cid))
            return
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        try:
            self.call("ack_robot_command", cid)
        except Exception as e:  # noqa: BLE001 - preempted between insert and ack: do not run it
            _log("ack refused; not running", command_id=cid, type=ctype, error=str(e))
            return
        _log("acked", command_id=cid, type=ctype, seq=row.get("seq"), payload=payload)
        is_job = ctype in JOB_TYPES
        if is_job:
            self._scale = float(payload.get("fake_time_scale") or self.cfg.time_scale)
            self.current_job = cid
            self.abort.clear()
            self.heartbeat("busy")
        try:
            if payload.get("fake_fail_command") or self.rng.random() < self.cfg.command_fail_prob:
                raise RuntimeError("injected failure (fake_fail_command / --command-fail-prob)")
            handler = getattr(self, f"_do_{ctype}", self._do_noop)
            result = handler(row, payload) or {}
            result = {"fake": True, **result}
            if self._command_status(cid) == "preempted":
                _log("finished after preemption; leaving it preempted", command_id=cid)
            else:
                self.call("complete_robot_command", cid, json.dumps(result, default=str))
                _log("done", command_id=cid, type=ctype, result=result)
        except Aborted as e:
            if self._command_status(cid) != "preempted":
                self.call("complete_robot_command", cid, json.dumps({"fake": True, "aborted": True, "reason": str(e)}))
            _log("aborted", command_id=cid, type=ctype, reason=str(e))
        except Exception as e:  # noqa: BLE001
            _log("command failed", command_id=cid, type=ctype, error=str(e))
            self._safe_fail(cid, str(e))
        finally:
            if is_job:
                self.current_job = None
                if self.status == "busy":
                    self.heartbeat("online")

    def _sleep(self, sim_ms: float) -> None:
        """Wait sim_ms of simulated time, in slices, so a preempt/stop interrupts it."""
        end = time.monotonic() + sim_ms / 1000.0 / max(self._scale, 1e-6)
        while True:
            if self.abort.is_set() or self.stopping.is_set():
                raise Aborted("preempted or stopped")
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))

    def _rand_ms(self, rng: tuple[int, int]) -> int:
        lo, hi = rng
        return int(self.rng.uniform(lo, hi))

    # ---- media + episodes
    def _clip(self, name: str) -> _Clip:
        """Render a synthetic clip once per process (``<media-dir>/FAKE-clips/<name>-<camera>.mp4``)."""
        with self._clips_lock:
            if name in self._clips:
                return self._clips[name]
            fx = _fixtures()
            plan, tk, hb = {
                "clean": (fx.CLEAN_PLAN, None, None),
                "takeover": (fx.TAKEOVER_PLAN, fx.TAKEOVER_AT_S, fx.HANDBACK_AT_S),
                "missed_grasp": (MISSED_GRASP_PLAN, None, None),
            }[name]
            t, joints, pix, grip, attached = fx.sample_plan(plan)
            d = self.cfg.media_dir / "FAKE-clips"
            d.mkdir(parents=True, exist_ok=True)
            videos = {}
            for i, cam in enumerate(FAKE_CAMERAS):
                mp4 = d / f"{name}-{cam}.mp4"
                # same cartoon for both cameras; the "wrist" copy gets a little pixel jitter so they differ
                frames = fx.render_video(mp4, t, pix, grip, attached, seed=i, jitter_px=0.5 * i)
                videos[cam] = (mp4, frames)
            clip = _Clip(name, t, joints, pix, grip, tk, hb, videos)
            self._clips[name] = clip
            _log("rendered fake clip", clip=name, seconds=round(float(t[-1]), 2), frames=len(t))
            return clip

    def _write_episode(self, d: Path, clip: _Clip, control_source: str, seed: int) -> dict:
        """actions.parquet (rdf_common.episode_format) + per-camera mp4 links for one episode, on the
        wall clock (the recording "ended" now). Returns the create_episode fields."""
        import numpy as np
        from rdf_common.episode_format import make_metadata, write_actions

        fx = _fixtures()
        t, n = clip.t, len(clip.t)
        t0_us = int(time.time() * 1_000_000) - int(round(float(t[-1]) * 1e6))
        t_us = (t0_us + np.rint(t * 1e6)).astype(np.int64)
        src = np.array([control_source] * n, dtype=object)
        if clip.takeover_s is not None:
            src[t < clip.takeover_s] = "policy"
            src[t >= clip.takeover_s] = "human"
            if clip.handback_s is not None:
                src[t >= clip.handback_s] = "policy"
        cams, meta_cams = {}, {}
        for cam, (mp4, frames) in clip.videos.items():
            dst = d / f"{cam}.mp4"
            try:
                os.link(mp4, dst)
            except OSError:
                shutil.copyfile(mp4, dst)
            cams[cam] = {"uri": dst.as_uri(), "fake": True}
            meta_cams[cam] = {"frames": frames, "fps": fx.FPS, "width": fx.W, "height": fx.H, "fake": True}
        rows = {
            "frame_index": np.arange(n, dtype=np.int32),
            "timestamp_us": t_us,
            "joint_pos": fx.measured(clip.joints, seed).astype(np.float32),
            "gripper_pos": clip.grip.astype(np.float32),
            "ik_target_pos": np.column_stack([clip.pix / 1000.0, np.full(n, 0.1)]).astype(np.float32),
            "ik_target_quat": np.tile([1.0, 0.0, 0.0, 0.0], (n, 1)).astype(np.float32),
            "gripper_cmd": clip.grip.astype(np.float32),
            "control_source": list(src),
            "joint_cmd": clip.joints.astype(np.float32),
        }
        meta = make_metadata(fps=fx.FPS, joint_names=fx.JOINTS, embodiment="fake_yam", cameras=meta_cams,
                             fixture=True, note=f"{FAKE_TAG} TDM-28 fake robot, synthetic '{clip.name}' clip "
                                                "(tdm25_fixtures cartoon), not real robot data")
        meta["fake"] = True
        pq = d / "actions.parquet"
        write_actions(pq, rows, meta)
        return {
            "actions_uri": pq.as_uri(), "cameras": cams, "frames": n,
            "duration_ms": int(round((int(t_us[-1]) - int(t_us[0])) / 1000)),
            "takeover_ts": None if clip.takeover_s is None else t0_us + int(round(clip.takeover_s * 1e6)),
            "handback_ts": None if clip.handback_s is None else t0_us + int(round(clip.handback_s * 1e6)),
        }

    def _make_episode(self, row: dict, kind: str, *, success: bool | None,
                      checkpoint_uri: str | None, task_id: int = 0, contributor_id: int = 0) -> tuple[int, int]:
        """Write a synthetic real-format episode + its episode row; returns (episode id, duration_ms),
        id 0 if not possible.

        Clip by kind: recovery_takeover -> ``takeover``; other teleop -> ``clean`` (human);
        eval/baseline -> ``clean`` (policy) on success, ``missed_grasp`` (policy) on failure.
        TODO(TDM-4/TDM-48): upload through the orchestrator's signed-URL media API like the real
        recorder, so storage_uri is a ``modal-vol://``/``s3://`` URI other machines can fetch.
        """
        if not (self.cfg.episodes and self.has_create_episode):
            return 0, 0
        tid = int(row.get("ticket_id") or 0)
        if not tid:
            return 0, 0
        ep_hex = uuid.uuid4().hex[:12]
        ep_key = f"FAKE-ep-{ep_hex}"
        d = self.cfg.media_dir / ep_key
        d.mkdir(parents=True, exist_ok=True)
        if kind == "recovery_takeover":
            clip, source = self._clip("takeover"), "human"
        elif kind in ("eval", "baseline"):
            clip, source = self._clip("clean" if success else "missed_grasp"), "policy"
        else:
            clip, source = self._clip("clean"), "human"
        # per-episode encoder noise from the uuid, not self.rng (keeps the seeded trial outcomes unchanged)
        ep = self._write_episode(d, clip, source, seed=int(ep_hex, 16) % (2 ** 31))
        self.call(
            "create_episode", tid, task_id, contributor_id, kind,
            d.as_uri(), ep["actions_uri"],
            json.dumps(ep["cameras"]),
            ep["frames"], ep["duration_ms"], ep["takeover_ts"], ep["handback_ts"], success, checkpoint_uri,
        )
        found = self.http.sql(f"SELECT * FROM episode WHERE storage_uri = '{d.as_uri()}'")
        return (int(found[0]["id"]) if found else 0), ep["duration_ms"]

    def _request_reset(self, row: dict, **ctx: Any) -> None:
        self.event("robot.reset_requested", ticket_id=row.get("ticket_id"), command_id=row["id"], **ctx)
        self._sleep(self._rand_ms(self.cfg.reset_ms))
        self.event("robot.reset_done", ticket_id=row.get("ticket_id"), command_id=row["id"],
                   reset_by="fake-auto", **ctx)

    # ---- per-type handlers
    def _tdm70_scheduled_prob(self, run: dict | None, ckpt: str | None) -> float:
        """candidate_success_schedule[k], k = distinct candidate checkpoints of this ticket evaluated
        before this one (eval_run rows with a lower id), so iteration 0 can fail and iteration 1 pass."""
        sched = self.cfg.candidate_success_schedule
        tid, rid = int((run or {}).get("ticket_id") or 0), int((run or {}).get("id") or 0)
        rows = self.sub.rows("eval_run") if self.sub else self.http.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")
        seen = {r.get("checkpoint_uri") for r in rows if int(r.get("ticket_id") or 0) == tid
                and r.get("kind") == "candidate" and int(r["id"]) < rid and r.get("checkpoint_uri") != ckpt}
        return float(sched[min(len(seen), len(sched) - 1)])

    def _find_eval_run(self, row: dict, payload: dict) -> dict | None:
        rid = payload.get("eval_run_id")
        runs = self.sub.rows("eval_run") if self.sub else []
        if rid:
            for r in runs:
                if int(r["id"]) == int(rid):
                    return r
            got = self.http.sql(f"SELECT * FROM eval_run WHERE id = {int(rid)}")
            return got[0] if got else None
        tid = int(row.get("ticket_id") or 0)
        cands = [r for r in runs if r["status"] == "running" and (not tid or int(r["ticket_id"]) == tid)]
        return max(cands, key=lambda r: int(r["id"])) if cands else None

    def _do_run_eval(self, row: dict, payload: dict) -> dict:
        run = self._find_eval_run(row, payload)
        ckpt = payload.get("checkpoint_uri") or (run or {}).get("checkpoint_uri")
        kind = (run or {}).get("kind") or ("baseline" if row["type"] == "run_baseline" else "candidate")
        trials = int(payload.get("trials") or (run or {}).get("trials") or 10)
        p = payload.get("fake_success_prob")
        if p is None and kind == "candidate" and self.cfg.candidate_success_schedule:
            p = self._tdm70_scheduled_prob(run, ckpt)
        if p is None:
            p = self.cfg.candidate_success_prob if kind == "candidate" else self.cfg.success_prob
        p = float(p)
        ep_kind = "baseline" if kind == "baseline" else "eval"
        done_idx: set[int] = set()
        if run is not None:
            existing = self.http.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {int(run['id'])}")
            done_idx = {int(t["trial_index"]) for t in existing}
        outcomes, episode_ids = [], []
        for i in range(trials):
            if i in done_idx:
                continue  # idempotent re-run of a half-finished eval
            dur = self._rand_ms(self.cfg.trial_ms)
            self._sleep(dur)
            ok = self.rng.random() < p
            note = f"{FAKE_TAG} success" if ok else f"{FAKE_TAG} {self.rng.choice(FAILURE_MODES)}"
            ep, _ = self._make_episode(row, ep_kind, success=ok, checkpoint_uri=ckpt)
            if run is not None:
                self.call("record_eval_trial", int(run["id"]), i, ok, dur, ep, note)
            outcomes.append({"index": i, "success": ok, "note": note, "episode_id": ep})
            episode_ids.append(ep)
            _log("trial", command_id=row["id"], eval_run_id=(run or {}).get("id"), index=i, success=ok, note=note)
            if i < trials - 1:
                self._request_reset(row, eval_run_id=(run or {}).get("id"), after_trial=i)
        res = {
            "eval_run_id": (run or {}).get("id"), "checkpoint_uri": ckpt, "trials": len(outcomes),
            "successes": sum(o["success"] for o in outcomes), "success_prob": p,
            "episode_ids": [e for e in episode_ids if e],
        }
        if run is None:
            res["outcomes"] = outcomes  # no eval_run to write to: report here only
        return res

    _do_run_baseline = _do_run_eval

    def _do_start_teleop(self, row: dict, payload: dict) -> dict:
        kind = payload.get("kind") or "clean_teleop"
        dur = self._rand_ms(self.cfg.teleop_ms)
        self._sleep(dur)
        ep, ep_ms = self._make_episode(row, kind, success=True, checkpoint_uri=None,
                                       task_id=int(payload.get("task_id") or 0),
                                       contributor_id=int(payload.get("contributor_id") or 0))
        return {"episode_id": ep, "kind": kind, "duration_ms": ep_ms or dur, "session_ms": dur}

    def _do_reset_request(self, row: dict, payload: dict) -> dict:
        self._request_reset(row, requested_by=row.get("issued_by"))
        return {"reset": "fake-auto"}

    def _do_load_checkpoint(self, row: dict, payload: dict) -> dict:
        self._sleep(2_000)
        return {"checkpoint_uri": payload.get("checkpoint_uri"), "loaded": True}

    def _do_home(self, row: dict, payload: dict) -> dict:
        self._sleep(1_500)
        return {"homed": True}

    def _do_stop(self, row: dict, payload: dict) -> dict:
        was = self.current_job
        self.abort.set()
        return {"stopped_job": was}

    def _do_soft_estop(self, row: dict, payload: dict) -> dict:
        self.abort.set()
        self.heartbeat("estop")
        if self.cfg.estop_clear_seconds > 0:
            if self._estop_timer:
                self._estop_timer.cancel()
            self._estop_timer = threading.Timer(self.cfg.estop_clear_seconds, self._clear_estop)
            self._estop_timer.daemon = True
            self._estop_timer.start()
        return {"estop": True, "auto_clear_s": self.cfg.estop_clear_seconds}

    def _clear_estop(self) -> None:
        self.heartbeat("online")
        self.event("robot.fake_estop_cleared")

    def _do_noop(self, row: dict, payload: dict) -> dict:
        return {}


# ---------------------------------------------------------------------- CLI

def _range(text: str) -> tuple[int, int]:
    lo, _, hi = text.partition(":")
    a, b = int(float(lo)), int(float(hi or lo))
    return (min(a, b), max(a, b))


def _is_local(host: str) -> bool:
    h = (urlparse(host).hostname or "").lower()
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or h.endswith(".local")


def parse_args(argv: list[str] | None = None) -> FakeConfig:
    p = argparse.ArgumentParser(prog="rdf-robot-client --fake", description=__doc__.split("\n\n")[0])
    p.add_argument("--fake", action="store_true", help="(accepted for the rdf-robot-client entry point)")
    p.add_argument("--host", default=os.environ.get("RDF_FAKE_SPACETIME_HOST", "http://127.0.0.1:3028"))
    p.add_argument("--db", default=os.environ.get("RDF_FAKE_SPACETIME_DB", "tdm28loop"))
    p.add_argument("--token-file", default=os.environ.get("RDF_FAKE_ROBOT_TOKEN_FILE"),
                   help="file holding a robot_client identity token (the harness writes one)")
    p.add_argument("--name", default="fake-yam-1")
    p.add_argument("--success-prob", type=float, default=0.3, help="baseline / unknown checkpoints")
    p.add_argument("--candidate-success-prob", type=float, default=0.9, help="eval_run.kind == candidate")
    p.add_argument("--candidate-success-schedule", default="",
                   help="TDM-70: comma list, k-th distinct candidate checkpoint of a ticket uses value k "
                        "(last repeats), e.g. 0,1 = first fine-tune fails, second passes")
    p.add_argument("--trial-ms", type=_range, default=(15_000, 40_000), help="simulated trial length lo:hi")
    p.add_argument("--reset-ms", type=_range, default=(5_000, 15_000))
    p.add_argument("--teleop-ms", type=_range, default=(20_000, 60_000))
    p.add_argument("--time-scale", type=float, default=40.0, help="simulated ms / wall ms (40 => 30 s trial in 0.75 s)")
    p.add_argument("--estop-clear-seconds", type=float, default=3.0, help="0 = stay latched in estop")
    p.add_argument("--command-fail-prob", type=float, default=0.0)
    p.add_argument("--heartbeat-seconds", type=float, default=5.0)
    p.add_argument("--media-dir", type=Path, default=Path(tempfile.gettempdir()) / "tdm-28" / "media")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--no-episodes", action="store_true", help="record trials without episode rows")
    p.add_argument("--sim-mode", action="store_true",
                   help="TDM-107: register as a sim-mode robot (camera_config_json.sim = true) for robot-mode tests")
    p.add_argument("--allow-remote", action="store_true",
                   help="allow a non-local SpacetimeDB (never maincloud: fake evals must not mix with real ones)")
    a = p.parse_args(argv)
    if "maincloud" in a.host.lower():
        p.error("refusing maincloud: the fake robot writes fake eval/episode rows")
    if not _is_local(a.host) and not a.allow_remote:
        p.error(f"refusing non-local host {a.host} without --allow-remote")
    token = ""
    if a.token_file:
        token = Path(a.token_file).read_text().strip()
    token = token or os.environ.get("RDF_FAKE_ROBOT_TOKEN", "")
    if not token:
        p.error("need a robot_client token: --token-file or RDF_FAKE_ROBOT_TOKEN (tests/integration/harness.py makes one)")
    return FakeConfig(
        host=a.host, db=a.db, token=token, name=a.name, success_prob=a.success_prob,
        candidate_success_prob=a.candidate_success_prob,
        candidate_success_schedule=tuple(float(x) for x in a.candidate_success_schedule.split(",") if x.strip()),
        trial_ms=a.trial_ms, reset_ms=a.reset_ms,
        teleop_ms=a.teleop_ms, time_scale=a.time_scale, estop_clear_seconds=a.estop_clear_seconds,
        command_fail_prob=a.command_fail_prob, heartbeat_seconds=a.heartbeat_seconds,
        media_dir=a.media_dir, seed=a.seed, episodes=not a.no_episodes, sim=a.sim_mode,
    )


def main(argv: list[str] | None = None) -> None:
    cfg = parse_args(argv)
    robot = FakeRobot(cfg)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    robot.start()
    _log("running; Ctrl-C to stop", robot_id=robot.robot_id)
    done.wait()
    robot.stop()
    _log("stopped")


if __name__ == "__main__":
    main()
