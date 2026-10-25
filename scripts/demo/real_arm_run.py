"""TDM-126: ONE supervised real-arm run of a ticket's policy, from the terminal (hands on the e-stop).

    cd services/orchestrator
    uv run python ../../scripts/demo/real_arm_run.py --ticket 14 --checkpoint before   # YAM-adapted pi0.5 (smoke-200)
    uv run python ../../scripts/demo/real_arm_run.py --ticket 14 --checkpoint after    # the ticket's fine-tune
    uv run python ../../scripts/demo/real_arm_run.py --ticket 14 --plan                # both URIs + the prompt (read-only)

It asks you to type HANDS_ON_ESTOP, then calls the orchestrator's ``POST /tickets/{id}/deployment/run``
``{real_arm: true, confirm: "HANDS_ON_ESTOP", checkpoint}``. The orchestrator resolves the checkpoint server side
(before = the YAM smoke checkpoint, after = the shipped ``deployed_checkpoint_uri``) and refuses unless the robot
advertises policy (TDM119_ALLOW_POLICY=1 on the Jetson). The robot then checks the first chunk (dry-run rules) and
runs at most 30 s at speed scale <= 0.25, and the arm holds at the end.

It then polls the command row (SpacetimeDB SQL, read-only) and prints the robot's result.
  Ctrl-C once:  ``stop`` (the job is cancelled at once, the arm holds, nothing latches)
  Ctrl-C twice: ``soft_estop`` (latched; clear it at the Jetson: systemctl --user kill -s USR1 geonosis-robot-teleop)
The hardware e-stop is always the first stop.

Env (.env, never printed): RDF_OPERATOR_KEY, SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN.
Orchestrator: --orchestrator, else $ORCHESTRATOR_URL, else http://127.0.0.1:8028.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

CONFIRM = "HANDS_ON_ESTOP"
TAG = "real-arm"


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def post(url: str, body: dict, key: str | None = None, timeout: float = 20.0) -> tuple[int, dict]:
    headers = {"content-type": "application/json", "x-human-id": "operator:real_arm_run"}
    if key:
        headers["x-rdf-operator-key"] = key
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def detail(body: dict) -> str:
    d = body.get("detail")
    if isinstance(d, dict):
        return f"{d.get('code')}: {d.get('message')}"
    return str(d or body)[:300]


def plan(ticket_id: int) -> int:
    """The same server-side resolution the orchestrator does, read-only (SpacetimeDB SQL)."""
    import asyncio

    from rdf_common.spacetime_client import AsyncSpacetimeClient
    from rdf_orchestrator.deployment_api import _opt, _training_prompt
    from rdf_orchestrator.robot_mode import REAL_YAM_BASELINE_CHECKPOINT, real_checkpoint_ok

    async def go() -> int:
        stdb = AsyncSpacetimeClient()
        rows = await stdb.sql(f"SELECT * FROM ticket WHERE id = {int(ticket_id)}")
        if not rows:
            log(f"no ticket {ticket_id}")
            return 2
        t = rows[0]
        after = str(_opt(t.get("deployed_checkpoint_uri")) or "")
        prompt = await _training_prompt(stdb, t, after)
        print(json.dumps({"ticket": ticket_id, "status": t.get("status"),
                          "before": REAL_YAM_BASELINE_CHECKPOINT, "after": after,
                          "after_is_real": real_checkpoint_ok(after), "prompt": prompt}, indent=1))
        return 0

    return asyncio.run(go())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ticket", type=int, required=True)
    ap.add_argument("--checkpoint", choices=("before", "after"), default=None)
    ap.add_argument("--plan", action="store_true",
                    help="print both checkpoint URIs and the training prompt (read-only), for the Jetson dry run")
    ap.add_argument("--robot-id", type=int, default=None, help="default: the ticket's real / collect robot")
    ap.add_argument("--orchestrator", default=None)
    ap.add_argument("--poll-s", type=float, default=0.5)
    a = ap.parse_args(argv)
    try:
        from rdf_common.spacetime_client import SpacetimeClient, load_dotenv

        load_dotenv()
    except ImportError:
        print("run it from services/orchestrator with uv run (needs rdf_common)", file=sys.stderr)
        return 2
    if a.plan:
        return plan(a.ticket)
    if not a.checkpoint:
        ap.error("--checkpoint before|after is required (or --plan)")
    orch = (a.orchestrator or os.environ.get("ORCHESTRATOR_URL") or "http://127.0.0.1:8028").rstrip("/")
    key = os.environ.get("RDF_OPERATOR_KEY") or ""
    if not key:
        print("RDF_OPERATOR_KEY is not set (.env)", file=sys.stderr)
        return 2
    log(f"ticket {a.ticket} · checkpoint {a.checkpoint} · orchestrator {orch}")
    log("The arm will move slowly for up to 30 s. Hands on the e-stop.")
    if input(f"Type {CONFIRM} to start: ").strip() != CONFIRM:
        log("not confirmed; nothing was sent")
        return 1
    body = {"real_arm": True, "confirm": CONFIRM, "checkpoint": a.checkpoint}
    if a.robot_id:
        body["robot_id"] = a.robot_id
    status, out = post(f"{orch}/tickets/{a.ticket}/deployment/run", body, key)
    if status != 200:
        log(f"REFUSED ({status}) {detail(out)}")
        return 3
    cmd_id, robot_id = out.get("command_id"), out.get("robot_id")
    log(f"started: command #{cmd_id} on {out.get('robot_name')} (robot {robot_id}) · {out.get('checkpoint_uri')}")
    log(f"prompt: {out.get('prompt')!r} · cap {out.get('time_cap_s')} s · speed scale {out.get('speed_scale')}")

    stops = {"n": 0}

    def on_int(*_a) -> None:
        stops["n"] += 1
        kind = "stop" if stops["n"] == 1 else "soft_estop"
        payload = {"reason": "real_arm_run.py Ctrl-C"} if kind == "soft_estop" else {}
        s, b = post(f"{orch}/robots/{robot_id}/commands", {"type": kind, "payload": payload, "ticket_id": a.ticket},
                    key, timeout=5.0)
        log(f"{kind.upper()} sent ({s})" + ("" if s == 200 else f": {detail(b)}")
            + (" · Ctrl-C again for the latched soft e-stop" if kind == "stop" else ""))

    signal.signal(signal.SIGINT, on_int)
    stdb = SpacetimeClient()
    last, t0 = None, time.monotonic()
    while True:
        try:
            rows = stdb.sql(f"SELECT * FROM robot_command WHERE id = {int(cmd_id)}") if cmd_id else []
        except Exception as e:  # noqa: BLE001 - keep polling; the robot doesn't need us
            rows = []
            log(f"poll failed: {str(e)[:120]}")
        row = rows[0] if rows else {}
        st = row.get("status")
        if st != last:
            log(f"{time.monotonic() - t0:5.1f}s  command {st}")
            last = st
        if st in ("completed", "failed", "preempted", "cancelled"):
            raw = row.get("result_json") or ""
            if isinstance(raw, dict):  # option column, undecoded
                raw = raw.get("some") or ""
            try:
                res = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {"result": raw}
            except ValueError:
                res = {"result": raw}
            keep = ("ok", "ended", "seconds", "steps", "first_chunk_verdict", "verdicts", "rejected", "refused", "error",
                    "speed_scale", "rtt_p50_ms", "rtt_p95_ms", "flags", "aborted", "reason", "result")
            log("result: " + json.dumps({k: res[k] for k in keep if k in res}, default=str))
            return 0 if st == "completed" else 4
        if time.monotonic() - t0 > 180:
            log("no end after 180 s (the robot caps motion at 30 s): check the Jetson logs")
            return 5
        time.sleep(a.poll_s)


if __name__ == "__main__":
    raise SystemExit(main())
