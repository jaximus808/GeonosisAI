#!/usr/bin/env python3
"""TDM-15: prove the SpacetimeDB event_log is append-only.

Two modes, stdlib only (runs with plain `python3`, no venv):

  live (default)  Read every event_log row, print the row count, id/ts range, id gaps
                  and the chain hash "rdf-evlog-chain-v1" over the rows in id order.
                  The hash is TDM-77's chain_summary() (spikes/tdm-77-events/
                  chain_hash_ref.py, imported here), so it equals the "untouched" seal
                  on the web /events page. Any edit, delete or reorder of an earlier
                  row changes the hash.

      python3 scripts/verify_event_log.py                       # maincloud (SPACETIME_* from env/.env)
      python3 scripts/verify_event_log.py --save cp.json        # store a checkpoint (count + hash)
      python3 scripts/verify_event_log.py --check cp.json       # later: first N rows must still hash the same
      python3 scripts/verify_event_log.py --expect 120:3fa2...  # same, inline
      python3 scripts/verify_event_log.py --host http://127.0.0.1:3015 --db tdm15local

  static          Audit the module source (spacetime/spacetimedb/src):
                  1. no event_log update/delete path anywhere;
                  2. ctx.db.eventLog.insert appears only in event_log.ts (logEvent);
                  3. every exported reducer calls logEvent, directly or through a
                     helper that does.

      python3 scripts/verify_event_log.py static

Exit code 0 = OK, 1 = invariant broken, 2 = could not read. Never prints the token.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE_SRC = REPO / "spacetime" / "spacetimedb" / "src"
CHAIN_REF = REPO / "spikes" / "tdm-77-events" / "chain_hash_ref.py"


def _chain_ref():
    """TDM-77's reference chain hash (one definition, shared with apps/web)."""
    spec = importlib.util.spec_from_file_location("rdf_tdm77_chain_hash_ref", CHAIN_REF)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- env


def _dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or _dotenv().get(key) or default


# --------------------------------------------------------------------------- live


def _ts_micros(v) -> int:
    if isinstance(v, dict):
        return int(v["__timestamp_micros_since_unix_epoch__"])
    if isinstance(v, list):
        return int(v[0])
    return int(v)


def fetch_rows(host: str, db: str, token: str | None) -> list[tuple[int, int, str, str, str]]:
    req = urllib.request.Request(
        f"{host.rstrip('/')}/v1/database/{db}/sql",
        data=b"SELECT * FROM event_log",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        method="POST",
    )
    ctx = None
    if host.lower().startswith("https://"):
        ctx = ssl.create_default_context()
        if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():  # python.org macOS build
            ctx.load_verify_locations("/etc/ssl/cert.pem")
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        res = json.loads(r.read())[-1]
    cols = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
    rows = []
    for raw in res["rows"]:
        d = dict(zip(cols, raw))
        rows.append((int(d["id"]), _ts_micros(d["ts"]), d["actor"], d["type"], d["payload_json"]))
    rows.sort(key=lambda r: r[0])
    return rows


def _iso(micros: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(micros / 1e6, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def live(a) -> int:
    host = a.host or _env("SPACETIME_HOST", "https://maincloud.spacetimedb.com")
    db = a.db or _env("SPACETIME_DB_NAME")
    if not db:
        print("no database: pass --db or set SPACETIME_DB_NAME", file=sys.stderr)
        return 2
    token = None if a.anonymous else (_env("SPACETIME_TOKEN") or None)
    try:
        rows = fetch_rows(host, db, token)
    except (urllib.error.URLError, KeyError, ValueError) as e:
        print(f"could not read event_log from {host} / {db}: {e}", file=sys.stderr)
        return 2
    ref = _chain_ref()
    summary = ref.chain_summary(rows)
    head = summary["hash"]
    print(f"event_log @ {host} / {db}")
    print(f"  rows        {len(rows)}")
    if rows:
        # auto-inc ids can skip (e.g. after a restart), so gaps are reported, not failed
        print(f"  ids         {rows[0][0]} .. {rows[-1][0]}  (gaps: {summary['gaps'] or 'none'})")
        print(f"  ts          {_iso(rows[0][1])} .. {_iso(rows[-1][1])}")
        # ts must not go backwards in id order (each row takes its reducer's timestamp).
        back = [rows[i][0] for i in range(1, len(rows)) if rows[i][1] < rows[i - 1][1]]
        if back:
            print(f"  note        {len(back)} row(s) with ts earlier than the previous id (first: id {back[0]})")
    print(f"  chain hash  {head}  ({summary['algorithm']})")
    print(f"  checkpoint  {len(rows)}:{head}")

    expect = a.expect
    if a.check:
        cp = json.loads(Path(a.check).read_text())
        expect = f"{cp['count']}:{cp['hash']}"
    if a.save:
        Path(a.save).write_text(json.dumps({
            "host": host, "db": db, "count": len(rows), "hash": head,
            "last_id": rows[-1][0] if rows else 0, "last_ts": _iso(rows[-1][1]) if rows else None,
        }, indent=2) + "\n")
        print(f"  saved       {a.save}")
    if expect:
        n_s, _, want = expect.partition(":")
        n = int(n_s)
        if n > len(rows):
            print(f"FAIL: checkpoint has {n} rows but the log now has {len(rows)}: rows were deleted")
            return 1
        got = ref.chain_summary(rows[:n])["hash"]
        if got != want:
            print(f"FAIL: the first {n} rows hash to {got}, checkpoint says {want}: earlier rows changed")
            return 1
        print(f"OK: the first {n} rows are unchanged; {len(rows) - n} row(s) appended since the checkpoint")
    return 0


# --------------------------------------------------------------------------- static

# A top-level declaration starts a new block (reducer or helper).
_TOP = re.compile(r"^(export\s+)?(const|function|let|class|type|interface)\s+([A-Za-z0-9_]+)", re.M)
_REDUCER = re.compile(r"=\s*spacetimedb\.(reducer|init|clientConnected|clientDisconnected)\(")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return re.sub(r"(^|[^:])//.*$", r"\1", src, flags=re.M)


def _blocks(src: str):
    """[(name, is_exported, text)] for each top-level declaration."""
    ms = list(_TOP.finditer(src))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(src)
        yield m.group(3), bool(m.group(1)), src[m.start():end]


def static(a) -> int:
    root = Path(a.module) if a.module else MODULE_SRC
    files = sorted(p for p in root.rglob("*.ts") if "node_modules" not in p.parts)
    problems: list[str] = []
    srcs = {p: _strip_comments(p.read_text()) for p in files}

    # 1 + 2: every eventLog access must be the single insert in event_log.ts.
    for p, src in srcs.items():
        rel = p.relative_to(root)
        for ln, line in enumerate(src.splitlines(), 1):
            if "eventLog" not in line and "event_log" not in line:
                continue
            if re.search(r"\beventLog\b\s*(\.\s*\w+\s*)?\.\s*(update|delete|clear|deleteAll)\b", line):
                problems.append(f"{rel}:{ln}: event_log update/delete path: {line.strip()}")
            elif re.search(r"\beventLog\s*\.\s*insert\b", line) and p.name != "event_log.ts":
                problems.append(f"{rel}:{ln}: direct ctx.db.eventLog.insert outside event_log.ts (use logEvent): {line.strip()}")
            elif re.search(r"\bdb\s*\[", line):
                problems.append(f"{rel}:{ln}: dynamic table access next to event_log: {line.strip()}")
    # any dynamic ctx.db[...] access could reach eventLog; flag it everywhere
    for p, src in srcs.items():
        for ln, line in enumerate(src.splitlines(), 1):
            if re.search(r"\bctx\.db\s*\[", line):
                problems.append(f"{p.relative_to(root)}:{ln}: dynamic ctx.db[...] access: {line.strip()}")

    # 3: every exported reducer reaches logEvent.
    blocks = [(p, n, exp, txt) for p, src in srcs.items() for n, exp, txt in _blocks(src)]
    loggers = {"logEvent"}
    changed = True
    while changed:  # helpers that call a logger are loggers too (fixpoint)
        changed = False
        for _p, name, _exp, txt in blocks:
            if name in loggers or _REDUCER.search(txt.split("\n", 1)[0] + txt[:200]):
                continue
            body = txt.split("\n", 1)[1] if "\n" in txt else ""
            if any(re.search(rf"\b{re.escape(l)}\s*\(", body) for l in loggers):
                loggers.add(name)
                changed = True
    reducers, silent = 0, []
    for p, name, exp, txt in blocks:
        if not (exp and _REDUCER.search(txt[:300])):
            continue
        reducers += 1
        if not any(re.search(rf"\b{re.escape(l)}\s*\(", txt) for l in loggers):
            silent.append(f"{p.relative_to(root)}: {name}")
    for s in silent:
        problems.append(f"reducer never logs: {s}")

    print(f"static audit of {root.relative_to(REPO) if root.is_relative_to(REPO) else root}")
    print(f"  files       {len(files)}")
    print(f"  reducers    {reducers} exported, {reducers - len(silent)} reach logEvent")
    print(f"  loggers     {', '.join(sorted(loggers))}")
    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for pr in problems:
            print(f"  - {pr}")
        return 1
    print("OK: event_log is insert-only, written only by logEvent, and every reducer logs")
    return 0


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="live", choices=["live", "static"])
    ap.add_argument("--host", help="SpacetimeDB host (default SPACETIME_HOST or maincloud)")
    ap.add_argument("--db", help="database name (default SPACETIME_DB_NAME)")
    ap.add_argument("--anonymous", action="store_true", help="read without SPACETIME_TOKEN (event_log is public)")
    ap.add_argument("--expect", metavar="N:HASH", help="the first N rows must hash to HASH")
    ap.add_argument("--save", metavar="FILE", help="write a checkpoint {count, hash} to FILE")
    ap.add_argument("--check", metavar="FILE", help="verify against a checkpoint written by --save")
    ap.add_argument("--module", help="module src dir for static mode (default spacetime/spacetimedb/src)")
    a = ap.parse_args(argv)
    return static(a) if a.mode == "static" else live(a)


if __name__ == "__main__":
    sys.exit(main())
