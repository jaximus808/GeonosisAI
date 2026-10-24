"""TDM-62: show or set a robot's scene-reset routine (steps + start-state photo) for the reset page.

    cd services/orchestrator
    uv run python scripts/tdm62_reset_routine.py show --robot-id 2
    uv run python scripts/tdm62_reset_routine.py set --robot-id 2 \
        --step "Put the cup on the taped X, handle facing the robot." \
        --step "Empty the bin and put it back in its outline." \
        --step "Step back out of the arm's reach, then tap Done." \
        --photo ~/Desktop/start.jpg            # a phone photo of the start state, or:
        # --from-camera http://127.0.0.1:8765  (the robot's TDM-55 stream server; needs RDF_STREAM_TOKEN_SECRET)

Writes ``robots/<id>/reset_routine.json`` (+ ``reset_reference.jpg``) to the media store configured
in .env (TDM-4). Pass --robot-name with --from-camera if the stream server checks robot names.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("--robot-id", type=int, required=True)
    s.add_argument("--task", default="", help="task description for the default steps")
    w = sub.add_parser("set")
    w.add_argument("--robot-id", type=int, required=True)
    w.add_argument("--step", action="append", required=True, help="one step (repeat, in order)")
    g = w.add_mutually_exclusive_group()
    g.add_argument("--photo", type=Path, help="JPEG/PNG of the start state")
    g.add_argument("--from-camera", metavar="STREAM_HTTP", help="grab the start photo from the robot stream server")
    w.add_argument("--cam", default="exo")
    w.add_argument("--robot-name", default="")
    a = ap.parse_args(argv)

    from rdf_orchestrator import reset_routine as rr

    if a.cmd == "show":
        print(json.dumps(rr.load(a.robot_id, a.task).to_dict(), indent=2))
        return 0
    photo = None
    if a.photo:
        photo = a.photo.expanduser().read_bytes()
    elif a.from_camera:
        import httpx
        from rdf_common.stream_token import StreamTokenSettings, mint

        token, _ = mint(StreamTokenSettings().rdf_stream_token_secret, robot_id=a.robot_id, robot_name=a.robot_name,
                        sub="operator:tdm62_reset_routine", ttl_s=20)
        r = httpx.get(f"{a.from_camera.rstrip('/')}/snapshot/{a.cam}.jpg", params={"token": token}, timeout=10)
        r.raise_for_status()
        photo = r.content
    out = rr.save(a.robot_id, a.step, photo, source="operator:tdm62_reset_routine")
    print(json.dumps(out.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
