"""Merge one person's duplicate contributor accounts (laptop + phone signed up separately). Dry run by default.

    cd services/orchestrator
    uv run python ../../scripts/demo/merge_contributors.py --target maincloud                    # list candidates
    uv run python ../../scripts/demo/merge_contributors.py --target maincloud --merge 1,4        # the plan for 1 + 4
    uv run python ../../scripts/demo/merge_contributors.py --target maincloud --merge 1,4 --keep 1 --yes

Candidates (listing only, nothing is merged without ``--merge``): contributors that share a job-text number in the
private text store (``RDF_TEXT_STORE``, default ``~/.local/state/rdf/tdm113_texts.sqlite3``: opt-ins and offer
history), and contributors with the same display name. Numbers are shown masked to the last 4 digits.

``--merge A,B[,C]`` merges the group into one survivor: ``--keep N``, else the one with the most history (tasks,
episodes, payments, paid cents; a tie keeps the lowest id). For every other member it moves

  * text store (written here, on this host): the opt-in, offer rows and remembered devices, plus a ``merged``
    row, so the orchestrator treats both ids as one person at once (Give back control, takeover, phone sign-in);
  * SpacetimeDB, through the owner-only ``merge_contributor(from_id, into_id, take_identity)`` reducer: tasks
    (assignee_id), episodes, payments, contact email, paid totals; the merged row is kept as kind ``merged``,
    renamed "[merged into #N] ...". ``--identity take`` (the ``auto`` default picks it when the merged account's
    browser was seen more recently) moves that browser identity onto the survivor, so the device in use keeps
    working, now as the survivor.

If the target's module has no ``merge_contributor`` reducer yet (not published), the text-store part still runs
and the script prints the publish command; run the same command again afterwards (each step is idempotent).

Auth: the owner SPACETIME_TOKEN from .env (never printed). Never prints a full phone number.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _target as T  # noqa: E402

TAG = "merge"
REDUCER = "merge_contributor"
DEFAULT_STORE = Path.home() / ".local" / "state" / "rdf" / "tdm113_texts.sqlite3"
PUBLISH = ("spacetime publish -s maincloud robo-factory-a706j --delete-data=never --yes=remote,migrate   "
           "(from spacetime/spacetimedb), then: pnpm gen:bindings")


def log(msg: str) -> None:
    print(f"[{TAG}] {msg}", flush=True)


def mask(phone: str | None) -> str:
    return f"***{phone[-4:]}" if phone else "-"


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _norm_id(v) -> str:
    if v is None or v == {} or v == []:
        return ""
    if isinstance(v, dict):  # an option / identity wrapper
        v = next(iter(v.values()), "")
        return _norm_id(v)
    if isinstance(v, (list, tuple)):
        return _norm_id(v[0]) if v else ""
    s = str(v).lower()
    return s[2:] if s.startswith("0x") else s


def _ts(v) -> float:
    from rdf_common.spacetime_client import ts_micros

    try:
        return ts_micros(v) / 1e6 if v not in (None, {}, []) else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def store_path(arg: str | None) -> Path:
    return Path(arg or os.environ.get("RDF_TEXT_STORE") or DEFAULT_STORE)


def read_store(path: Path) -> dict:
    """Read-only view of the private text store: phone links per contributor."""
    out: dict = {"optin": {}, "offers": {}, "devices": {}, "merged": {}, "phones": {}}
    if not path.exists():
        return out
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "optin" in names:
            for phone, cid, status in db.execute("SELECT phone, contributor_id, status FROM optin"):
                out["optin"][int(cid)] = (phone, status)
                out["phones"].setdefault(phone, set()).add(int(cid))
        if "offer" in names:
            for phone, cid, n in db.execute("SELECT phone, contributor_id, COUNT(*) FROM offer GROUP BY phone, contributor_id"):
                out["offers"][int(cid)] = out["offers"].get(int(cid), 0) + int(n)
                out["phones"].setdefault(phone, set()).add(int(cid))
        if "device" in names:
            for cid, n in db.execute("SELECT contributor_id, COUNT(*) FROM device GROUP BY contributor_id"):
                out["devices"][int(cid)] = int(n)
        if "merged" in names:
            out["merged"] = {int(a): int(b) for a, b in db.execute("SELECT from_id, into_id FROM merged")}
    finally:
        db.close()
    return out


def snapshot(c) -> dict:
    return {"contributors": c.sql("SELECT * FROM contributor"), "tasks": c.sql("SELECT * FROM task"),
            "episodes": c.sql("SELECT * FROM episode"), "payments": c.sql("SELECT * FROM payment"),
            "presence": c.sql("SELECT * FROM contributor_presence")}


def history(snap: dict, st: dict) -> dict[int, dict]:
    seen = {_i(p.get("contributor_id")): _ts(p.get("last_seen")) for p in snap["presence"]}
    out: dict[int, dict] = {}
    for c in snap["contributors"]:
        cid = _i(c.get("id"))
        tasks = [t for t in snap["tasks"] if _i(t.get("assignee_id")) == cid]
        out[cid] = {
            "id": cid, "name": str(c.get("name") or "")[:32], "kind": c.get("kind"),
            "tasks": len(tasks), "live_claims": sum(1 for t in tasks if t.get("status") == "claimed"),
            "episodes": sum(1 for e in snap["episodes"] if _i(e.get("contributor_id")) == cid),
            "payments": sum(1 for p in snap["payments"] if _i(p.get("contributor_id")) == cid),
            "paid": _i(c.get("total_paid_cents")),
            "phone": mask(st["optin"].get(cid, (None,))[0]), "offers": st["offers"].get(cid, 0),
            "devices": st["devices"].get(cid, 0), "identity": bool(_norm_id(c.get("identity"))),
            "last_seen": seen.get(cid, 0.0),
            "seen": time.strftime("%m-%d %H:%M", time.localtime(seen[cid])) if seen.get(cid) else "-",
        }
    return out


def candidates(snap: dict, st: dict) -> list[tuple[str, list[int]]]:
    live = {_i(c.get("id")): c for c in snap["contributors"] if c.get("kind") == "contributor"
            and not str(c.get("name") or "").startswith("[Sample]")}
    out: list[tuple[str, list[int]]] = []
    for phone, ids in sorted(st["phones"].items()):
        ids = sorted(i for i in ids if i in live)
        if len(ids) > 1:
            out.append((f"same job-text number {mask(phone)}", ids))
    names: dict[str, list[int]] = {}
    for cid, c in live.items():
        names.setdefault(" ".join(str(c.get("name") or "").lower().split()), []).append(cid)
    for name, ids in sorted(names.items()):
        if name and len(ids) > 1:
            out.append((f"same name {name!r}", sorted(ids)))
    return out


def score(h: dict) -> tuple:
    return (h["tasks"] + h["episodes"] + h["payments"], h["paid"])


def pick_survivor(group: list[int], hist: dict[int, dict], keep: int | None) -> tuple[int, str]:
    if keep:
        return keep, "--keep"
    best = max(group, key=lambda i: (score(hist[i]), -i))
    if all(score(hist[i]) == score(hist[best]) for i in group):
        return min(group), "same history (no tasks/episodes/payments difference): the lowest id"
    return best, "most history (tasks + episodes + payments, then paid cents)"


def take_identity(mode: str, from_h: dict, into_h: dict) -> bool:
    if mode == "take":
        return True
    if mode == "keep":
        return False
    if not from_h["identity"]:
        return False
    return (not into_h["identity"]) or from_h["last_seen"] > into_h["last_seen"]


COLS = ["id", "name", "kind", "tasks", "live_claims", "episodes", "payments", "paid", "phone", "offers", "devices",
        "identity", "seen"]


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    T.add_target_args(ap)
    ap.add_argument("--merge", help="contributor ids of one person, e.g. 1,4")
    ap.add_argument("--keep", type=int, help="the survivor (default: most history, tie: lowest id)")
    ap.add_argument("--identity", choices=("auto", "take", "keep"), default="auto",
                    help="move the merged account's browser identity onto the survivor (auto: if seen more recently)")
    ap.add_argument("--text-store", help="private text store (default RDF_TEXT_STORE or ~/.local/state/rdf/...)")
    args = ap.parse_args(argv)
    write = args.yes and not args.dry_run
    if client is None:
        host, db, token = T.resolve(args, TAG)
        log(f"target {args.target}: {host} / {db}  ({'WRITE' if write else 'dry run: pass --yes to write'})")
        client = T.client(host, db, token)
    sp = store_path(args.text_store)
    st = read_store(sp)
    log(f"text store: {sp}{'' if sp.exists() else ' (missing)'}")

    with client as c:
        snap = snapshot(c)
        hist = history(snap, st)
        if not args.merge:
            groups = candidates(snap, st)
            print("\nCONTRIBUTORS")
            print(T.table([hist[i] for i in sorted(hist)], COLS))
            print("\nCANDIDATE GROUPS (nothing is merged without --merge)")
            for why, ids in groups:
                print(f"  {why}: {', '.join(f'#{i}' for i in ids)}")
            if not groups:
                print("  (none)")
            if st["merged"]:
                print("\nALREADY MERGED IN THE TEXT STORE: " + ", ".join(f"#{a} -> #{b}" for a, b in st["merged"].items()))
            return 0

        group = sorted({int(x) for x in args.merge.replace(" ", ",").split(",") if x.strip()})
        if len(group) < 2:
            log("--merge needs at least two contributor ids")
            return 2
        missing = [i for i in group if i not in hist]
        if missing:
            log(f"no contributor {missing}")
            return 2
        if args.keep and args.keep not in group:
            log(f"--keep {args.keep} is not in --merge {group}")
            return 2
        bad = [i for i in group if hist[i]["kind"] != "contributor" and st["merged"].get(i) is None]
        if bad:
            log(f"only kind 'contributor' rows merge: {[(i, hist[i]['kind']) for i in bad]}")
            return 2
        into, why = pick_survivor(group, hist, args.keep)
        print("\nBEFORE")
        print(T.table([hist[i] for i in group], COLS))
        log(f"survivor #{into} ({why})")
        has_reducer = REDUCER in c.schema().reducers
        steps = []
        for f in [i for i in group if i != into]:
            h = hist[f]
            take = take_identity(args.identity, h, hist[into])
            already = st["merged"].get(f) == into and h["kind"] == "merged"
            steps.append((f, take, already))
            print(f"\n  #{f} -> #{into}{'  (already merged)' if already else ''}")
            print(f"    SpacetimeDB  {REDUCER}({f}, {into}, take_identity={str(take).lower()}): "
                  f"{h['tasks']} tasks ({h['live_claims']} live claims), {h['episodes']} episodes, "
                  f"{h['payments']} payments, {h['paid']} paid cents; #{f} becomes kind 'merged' "
                  f"\"[merged into #{into}] {h['name']}\"")
            print(f"    identity     " + (f"#{f}'s browser (seen {h['seen']}) becomes #{into}; #{into}'s old browser "
                                          f"(seen {hist[into]['seen']}) is signed out" if take else
                                          f"unchanged (#{into} keeps its browser; #{f}'s browser stays the hidden row)"))
            print(f"    text store   opt-in {h['phone']}"
                  + (f" moves to #{into}" if h["phone"] != "-" and hist[into]["phone"] in ("-", h["phone"]) else
                     " dropped (the survivor keeps its own number)" if h["phone"] != "-" else " (none)")
                  + f", {h['offers']} offer rows, {h['devices']} remembered devices "
                  + ("moved" if take else "forgotten") + f", merged row #{f} -> #{into}")
        if not has_reducer:
            log(f"the module on this target has no {REDUCER} reducer yet: the SpacetimeDB part waits for a publish:")
            log(f"  {PUBLISH}")
        if not write:
            log("dry run: nothing written")
            return 0

        from rdf_orchestrator.text_offers import TextStore

        store = TextStore(sp)
        failed = 0
        for f, take, already in steps:
            out = store.merge(f, into, time.time(), move_devices=take)
            log(f"text store #{f} -> #{into}: {out}")
            if already:
                continue
            if not has_reducer:
                continue
            try:
                c.call_reducer(REDUCER, f, into, take)
                log(f"{REDUCER}({f}, {into}, {str(take).lower()}) ok")
            except Exception as e:  # noqa: BLE001
                failed += 1
                log(f"{REDUCER}({f}, {into}) FAILED: {str(e)[:200]}")
        hist2 = history(snapshot(c), read_store(sp))
        print("\nAFTER")
        print(T.table([hist2[i] for i in group if i in hist2], COLS))
        if not has_reducer:
            log("text store merged; publish the module and run this command again to finish SpacetimeDB")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
