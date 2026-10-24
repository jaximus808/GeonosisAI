"""rdf-factory CLI (TDM-30). FACTORY_API_KEY is read from the env / repo .env and never printed.

  uv run rdf-factory start --role data_droid --brief "..." [--ticket 1] [--name x] [--mode local|cloud]
                           [--write-root DIR ...] [--allow-command "uv run pytest" ...]
  uv run rdf-factory send    <agent_session_id> "text"
  uv run rdf-factory resume  <agent_session_id> "what changed"   # what the TDM-27 bridge calls
  uv run rdf-factory restart <agent_session_id> "text"          # protocol B explicitly
  uv run rdf-factory finish  <agent_session_id> [done|failed]
  uv run rdf-factory show    <agent_session_id>
Events print one per line (kind, text) while a turn runs; the TurnResult prints as JSON at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .client import FactoryClient


def _print_event(ev: dict) -> None:
    if ev["kind"] in ("state", "usage", "thinking"):
        return
    print(f"  [{ev['seq']:>3} {ev['kind']:<17}] {ev['text']}", flush=True)


async def _amain(a: argparse.Namespace) -> int:
    async with FactoryClient(mode=a.mode, on_event=_print_event) as fc:
        if a.cmd == "start":
            r = await fc.start_session(a.role, a.brief, ticket_id=a.ticket, name=a.name or "",
                                       write_roots=a.write_root, allow_commands=a.allow_command)
        elif a.cmd == "send":
            r = await fc.send(a.id, a.text)
        elif a.cmd == "resume":
            r = await fc.resume(a.id, a.text)
        elif a.cmd == "restart":
            r = await fc.restart_with_payload(a.id, a.text)
        elif a.cmd == "finish":
            await fc.finish(a.id, a.status)
            print(json.dumps(await fc.mirror.get(a.id), default=str, indent=2))
            return 0
        else:
            print(json.dumps(await fc.mirror.get(a.id), default=str, indent=2))
            return 0
        print(json.dumps(r.to_dict(), default=str, indent=2))
        return 0 if r.status != "failed" else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="rdf-factory", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["local", "cloud"], default=None, help="default FACTORY_MODE or local")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("start")
    s.add_argument("--role", required=True)
    s.add_argument("--brief", required=True)
    s.add_argument("--ticket", type=int, default=0)
    s.add_argument("--name")
    s.add_argument("--write-root", action="append")
    s.add_argument("--allow-command", action="append")
    for c in ("send", "resume", "restart"):
        p = sp.add_parser(c)
        p.add_argument("id", type=int)
        p.add_argument("text", nargs="?", default="")
    f = sp.add_parser("finish")
    f.add_argument("id", type=int)
    f.add_argument("status", nargs="?", default="done", choices=["done", "failed"])
    sh = sp.add_parser("show")
    sh.add_argument("id", type=int)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(_amain(a)))


if __name__ == "__main__":
    main()
