"""TDM-58 proof robot: SimArm + synthetic cameras + TDM-55 camera stream + a minimal /control socket.

[SIM] Stand-in for TDM-52's teleop executor (not built yet when TDM-58 was done) so the web
recording controls can be driven end to end on a laptop. It ONLY implements what TDM-58 needs:

  * /control (ws://127.0.0.1:<control-port>/control): first message {type:"auth", token} with a
    TDM-55 stream token (same secret/claims as the camera socket); then the robot sends
    {t:"state", control_source, recording} at 10 Hz and answers recording messages through
    rdf_robot_client.tdm58_recording.RecordingControl (the real mapper TDM-52 will call).
    {t:"cmd"} is acked and ignored (no clutch here: the "human" segment replays the sim motion),
    {t:"stop"} holds the arm.
  * Motion: LocalArmDriver streaming a scripted sine motion, labelled with whoever has authority
    per RecordingControl.control_source(): policy (recovery task before takeover / after handback),
    human (clean demo, takeover). A clean task that is not recording holds the arm (idle).
  * Episodes: TDM-48 Recorder + EpisodeUploader (orchestrator /uploads/sign-batch, create_episode as
    the robot_client identity). Ticket/task/contributor come from the task row (lookup_task).

    uv run --project services/robot-client python spikes/tdm-58-recording/sim_control_robot.py \
        --host http://127.0.0.1:3058 --db tdm58rec --token-file <robot_client.token> \
        --orchestrator-url http://127.0.0.1:8058 --robot-id 1 --stream-port 8158 --control-port 8159 \
        --episodes-dir $TMPDIR/tdm-58/episodes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--orchestrator-url", required=True)
    ap.add_argument("--robot-id", type=int, required=True)
    ap.add_argument("--stream-port", type=int, default=8158)
    ap.add_argument("--control-port", type=int, default=8159)
    ap.add_argument("--episodes-dir", required=True)
    a = ap.parse_args()
    if not a.host.startswith("http://127."):
        print("refusing: local SpacetimeDB only", file=sys.stderr)
        return 2

    from rdf_common.spacetime_client import SpacetimeClient
    from rdf_common.stream_token import StreamTokenError
    from rdf_robot_client.adapters.sim import SimArm
    from rdf_robot_client.cameras import CameraRig
    from rdf_robot_client.recorder import EpisodeUploader, LocalArmDriver, Recorder, sim_motion
    from rdf_robot_client.stream import CameraStreamServer, StreamConfig
    from rdf_robot_client.tdm58_recording import RECORDING_MESSAGES, RecordingControl, lookup_task

    token = Path(a.token_file).read_text().strip()
    stdb = SpacetimeClient(a.host, a.db, token)
    arm = SimArm()
    arm.connect()
    rig = CameraRig.from_config(synthetic=True).start()
    if not rig.wait_ready(10):
        print("cameras not ready", file=sys.stderr)
        return 2
    cams = CameraStreamServer(rig, StreamConfig(port=a.stream_port, host="127.0.0.1", robot_id=a.robot_id)).start()

    controls: dict[int, RecordingControl] = {}
    authority = {"rc": None, "stopped": False}

    def source(_t: float) -> str:
        rc = authority["rc"]
        if authority["stopped"]:
            return "idle"
        if rc is None:
            return "idle"
        src = rc.control_source()
        if src:
            return src
        return "policy" if (rc._target and rc._target.kind == "recovery_takeover") else "idle"

    drv = LocalArmDriver(arm, sim_motion(arm.home_joints), source).start()
    up = EpisodeUploader(a.orchestrator_url, stdb, root=a.episodes_dir).start()
    rec = Recorder(drv, rig, uploader=up, root=a.episodes_dir)

    def control_for(task_id: int) -> RecordingControl:
        rc = controls.get(task_id)
        if rc is None:
            rc = RecordingControl(rec, lambda: lookup_task(stdb.sql, task_id))
            rc.target()  # resolve now: the page shows the kind before the first press
            controls[task_id] = rc
        return rc

    async def handler(ws: ServerConnection) -> None:
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5))
            claims = cams.check_token(str(first.get("token", ""))) if first.get("type") == "auth" else None
            if claims is None:
                raise StreamTokenError("auth_required")
        except (asyncio.TimeoutError, ValueError, StreamTokenError) as e:
            await ws.send(json.dumps({"t": "error", "code": getattr(e, "code", "auth_required")}))
            await ws.close(4003, "forbidden")
            return
        except ConnectionClosed:
            return
        rc = await asyncio.to_thread(control_for, int(claims.task_id))
        authority["rc"] = rc
        await ws.send(json.dumps({"t": "hello", "task_id": claims.task_id, "robot_id": claims.robot_id,
                                  "sim": True, "note": "[SIM] tdm58 proof robot"}))

        async def pump_state() -> None:
            while True:
                st = arm.read_state()
                await ws.send(json.dumps({"t": "state", "sim": True, "control_source": source(0),
                                          "joints": [round(v, 4) for v in st.joints], "gripper": round(st.gripper, 3),
                                          "recording": rc.state()}, default=str))
                await asyncio.sleep(0.1)

        async def receive() -> None:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                t = m.get("t")
                if t in RECORDING_MESSAGES:
                    reply = await rc.handle_async(m)
                    print(json.dumps({"tdm58": "control", "op": t, "ok": reply["ok"], "error": reply.get("error"),
                                      "phase": reply["recording"]["phase"]}), file=sys.stderr, flush=True)
                    await ws.send(json.dumps(reply, default=str))
                elif t == "cmd":
                    await ws.send(json.dumps({"t": "ack", "seq": m.get("seq")}))
                elif t == "stop":
                    authority["stopped"] = True
                    arm.hold()

        tasks = [asyncio.create_task(pump_state()), asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except ConnectionClosed:
            pass
        finally:
            for t in tasks:
                t.cancel()

    async def run() -> None:
        async with serve(handler, "127.0.0.1", a.control_port, compression=None):
            print(json.dumps({"tdm58": "ready", "camera_ws": f"ws://127.0.0.1:{cams.port}/ws/cameras",
                              "control_ws": f"ws://127.0.0.1:{a.control_port}/control"}), flush=True)
            await asyncio.Future()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        drv.stop()
        cams.stop()
        up.stop()
        rig.stop()
        arm.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
