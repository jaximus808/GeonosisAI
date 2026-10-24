"""TDM-104: the sim robot loads the scene an operator deployed (``scene.deployed`` in event_log).

    from .scene_watch import start_scene_watch
    start_scene_watch(client)          # after client.start(); a no-op unless the arm is the MuJoCo sim

The orchestrator's ``POST /tickets/{id}/scene/deploy`` appends ``scene.deployed`` {robot_id, ticket_id,
revision, scene_id, deploy_id, spec}. This watcher subscribes to those rows (no new robot_command type, so no
reducer publish), and for the newest one naming this robot:

  * waits until no job runs (a scene never changes under a rollout, teleop session or reset; it retries every
    second), then calls the MuJoCo adapter's ``load_scene(spec)``, which rebuilds the MJCF and hot-swaps the
    model (the cameras and ``/ws/state`` follow on their next frame);
  * appends ``scene.loaded`` {robot_id, ticket_id, revision, scene_id, deploy_id, ok, error?, objects}.

On start it applies the newest deploy for this robot too, so a restarted sim comes back in the ticket's scene
(the "read the active scene on start" path). A deploy it already applied is not applied again.

An eval (``eval_trials``) calls ``settle()`` before trial 0: it waits (bounded) for the subscription's first
snapshot and applies a pending deploy right there, from the job's own thread. Without it a ``run_eval`` queued
before a restart could start trial 0 a second before the snapshot landed, and the scene swapped mid-trial. The
apply loop and ``settle`` share one lock around "is a job running? then apply", so a deploy never lands inside
a job that started after the check. The real arm
never runs this watcher (``find_sim`` is None there), and the orchestrator refuses to deploy to a real robot.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from rdf_common.spacetime_client import Subscription

from .loop import _log

EV_DEPLOYED = "scene.deployed"
EV_LOADED = "scene.loaded"


def _find_sim(arm: Any) -> Any:
    try:
        from .adapters.mujoco_sim import find_sim
    except Exception:  # noqa: BLE001 - mujoco not installed: not a sim robot
        return None
    return find_sim(arm)


class SceneWatcher:
    def __init__(self, client: Any, *, poll_s: float = 1.0) -> None:
        self.client = client
        self.poll_s = poll_s
        self.sim = _find_sim(client.arm)
        self.sub: Subscription | None = None
        self.applied: str | None = None  # deploy_id
        self.current: dict[str, Any] | None = None  # the scene.loaded payload of the scene in the sim
        self._pending: dict[str, Any] | None = None
        self._snapshot = threading.Event()  # the subscription's first snapshot arrived
        self._apply_lock = threading.Lock()  # "no job is running -> apply" is atomic against settle()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "SceneWatcher":
        self._thread = threading.Thread(target=self._apply_loop, name="rdf-scene-watch", daemon=True)
        self._thread.start()
        self.sub = Subscription([f"SELECT * FROM event_log WHERE type = '{EV_DEPLOYED}'"],
                                on_applied=self._on_applied, on_insert=self._on_insert,
                                on_error=lambda e: _log("scene watch subscription error", error=str(e)[:300]),
                                config=self.client.stdb, max_backoff=10.0).start_thread()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self.sub is not None:
            self.sub.stop()

    # ---------------------------------------------------------------- rows
    def _mine(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if row.get("type") != EV_DEPLOYED:
            return None
        try:
            p = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            return None
        if not isinstance(p, dict) or self.client.robot_id is None or int(p.get("robot_id") or 0) != int(self.client.robot_id):
            return None
        p["_event_id"] = int(row.get("id") or 0)
        return p

    def _on_applied(self, snapshot: dict[str, list[Any]]) -> None:
        mine = [p for p in (self._mine(dict(r)) for r in snapshot.get("event_log", [])) if p]
        if mine:
            self._offer(max(mine, key=lambda p: p["_event_id"]))
        self._snapshot.set()

    def _on_insert(self, table: str, row: dict[str, Any], meta: dict[str, Any]) -> None:
        p = self._mine(dict(row)) if table == "event_log" else None
        if p:
            self._offer(p)

    def _offer(self, p: dict[str, Any]) -> None:
        with self._cv:
            if p.get("deploy_id") == self.applied:
                return
            if self._pending is None or p["_event_id"] >= self._pending["_event_id"]:
                self._pending = p
            self._cv.notify_all()

    # ---------------------------------------------------------------- apply (own thread: never on the socket loop)
    def _busy(self) -> str | None:
        job = self.client.current_job() if callable(getattr(self.client, "current_job", None)) else None
        return job.command.label() if job is not None else None

    def _apply_loop(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while self._pending is None and not self._stop.is_set():
                    self._cv.wait(timeout=5.0)
                p = self._pending
            if p is None:
                continue
            with self._apply_lock:
                busy = self._busy()
                if not busy:
                    with self._cv:
                        if self._pending is not p:
                            continue  # a newer deploy arrived meanwhile (or settle() applied it)
                        self._pending = None
                    self.apply(p)
                    continue
            _log("scene deploy waiting for the current job", job=busy, scene_id=p.get("scene_id"))
            self._stop.wait(self.poll_s)

    def settle(self, timeout_s: float = 15.0) -> dict[str, Any] | None:
        """Called by a job (an eval) before it starts: wait up to ``timeout_s`` for the first subscription
        snapshot, then apply the newest pending deploy for this robot NOW (the caller is the running job, so
        the apply loop would defer it until the job ends). Returns the ``scene.loaded`` payload it wrote, or
        None when nothing was pending."""
        if not self._snapshot.wait(max(0.0, timeout_s)):
            _log("scene settle: no scene.deployed snapshot yet; trials start on the current scene",
                 waited_s=timeout_s, scene=getattr(getattr(self.sim, "scene", None), "name", None))
        with self._apply_lock:
            with self._cv:
                p, self._pending = self._pending, None
            if p is None or p.get("deploy_id") == self.applied:
                return None
            _log("scene settle: loading the pending deploy before the trials", scene_id=p.get("scene_id"),
                 deploy_id=p.get("deploy_id"))
            return self.apply(p)

    def apply(self, p: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {k: p.get(k) for k in ("ticket_id", "revision", "scene_id", "deploy_id")}
        if self.sim is None or not callable(getattr(self.sim, "load_scene", None)):
            out.update(ok=False, error="not a MuJoCo sim robot: scenes are only loaded into the simulator")
        else:
            t0 = time.monotonic()
            try:
                r = self.sim.load_scene(p.get("spec") or {})
                out.update(ok=True, objects=r.get("objects"), scene=r.get("scene"),
                           load_ms=round((time.monotonic() - t0) * 1000))
                self.current = out
            except Exception as e:  # noqa: BLE001 - a bad spec leaves the old scene running
                out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:400]}")
        self.applied = p.get("deploy_id")
        _log("scene deploy " + ("loaded" if out["ok"] else "FAILED"), **{k: v for k, v in out.items() if k != "objects"})
        self.client.event(EV_LOADED, **out)
        return out


def start_scene_watch(client: Any) -> SceneWatcher | None:
    """Start the watcher when the arm is the MuJoCo sim (else None: the real arm ignores scene deploys)."""
    w = SceneWatcher(client)
    if w.sim is None:
        return None
    client.services["scene_watch"] = w
    return w.start()


__all__ = ["EV_DEPLOYED", "EV_LOADED", "SceneWatcher", "start_scene_watch"]
