"""TDM-42 laptop CLI: checkpoint registry, Volume cleanup, local download, Modal cost.

    uv run python scripts/tdm42_checkpoints.py list [--ticket T] [--json]
    uv run python scripts/tdm42_checkpoints.py resolve --ticket T [--which latest|deployed|base]
    uv run python scripts/tdm42_checkpoints.py cleanup [--keep 2]            # dry run (default)
    uv run python scripts/tdm42_checkpoints.py cleanup --keep 2 --apply      # deletes; re-checks every guard
    uv run python scripts/tdm42_checkpoints.py download --ticket T [--which latest] [--uri U] [--dest DIR]
    uv run python scripts/tdm42_checkpoints.py rates [--refresh]
    uv run python scripts/tdm42_checkpoints.py cost --ticket T [--billing]   # estimate vs Modal billing

Make targets: ``make checkpoints``, ``make checkpoint-cleanup`` (``APPLY=1`` to delete),
``make download-checkpoint TICKET=42 [WHICH=latest|deployed] [URI=...] [DEST=...]``, ``make modal-rates``.

SpacetimeDB rows (deployed flags, training/eval runs) are read with the owner token from ``.env``;
if they cannot be read, ``cleanup`` keeps everything (fail closed). Read-only except
``cleanup --apply`` and ``rates --refresh`` (rewrites config/modal_rates.json).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

from rdf_training import cost, registry

REPO = pathlib.Path(__file__).resolve().parents[3]


def _load_dotenv() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state(*, need_rows: bool = True) -> registry.State:
    st = registry.State()
    if need_rows:
        try:
            from rdf_common.spacetime_client import SpacetimeClient

            with SpacetimeClient() as c:
                st.training_runs = c.sql("SELECT * FROM training_run")
                st.eval_runs = c.sql("SELECT * FROM eval_run")
                st.tickets = c.sql("SELECT * FROM ticket")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            st.ok = False
            st.errors.append(f"SpacetimeDB: {type(e).__name__}: {e}")
    try:
        st.policy_state = registry.read_policy_state()
    except Exception as e:  # noqa: BLE001
        st.ok = False
        st.errors.append(f"policy state: {type(e).__name__}: {e}")
    return st


def gb(n) -> str:
    return "?" if n is None else f"{n / 1e9:.2f} GB"


def when(ts) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ") if ts else "-"


def cmd_list(a) -> int:
    st = load_state()
    recs = registry.build(registry.ModalVolume(), st)
    if a.ticket:
        recs = registry.for_ticket(recs, a.ticket) + [r for r in recs if r["kind"] == "base"]
    if a.json:
        print(json.dumps({"state_ok": st.ok, "state_errors": st.errors, "checkpoints": recs}, indent=2, default=str))
        return 0
    if not st.ok:
        print("WARNING: " + "; ".join(st.errors), file=sys.stderr)
    for r in recs:
        flags = [f for f, on in (("DEPLOYED", r.get("deployed")), (f"policy:{r.get('policy_server')}", r.get("policy_server")),
                                 ("smoke", r.get("smoke")), ("INCOMPLETE", r["kind"] == "incomplete")) if on]
        tr = r.get("training_run") or {}
        ev = "; ".join(f"{e['kind']} {e['successes']}/{e['trials']}" for e in r.get("evals") or []) or "-"
        print(f"{r['kind']:<10} ticket={r.get('ticket')} run={r.get('run_id')} step={r.get('step')} "
              f"{gb(r.get('size_bytes'))} created={when(r.get('created_at'))} "
              f"loss={(r.get('metrics') or {}).get('final_loss')} training_run={tr.get('id')} "
              f"gpu_cost={tr.get('gpu_cost_cents')}c evals={ev} {' '.join(flags)}\n    {r.get('uri')}")
    return 0


def cmd_resolve(a) -> int:
    recs = registry.build(registry.ModalVolume(), load_state())
    r = registry.resolve(recs, a.ticket, a.which)
    if r is None:
        print(f"no {a.which} checkpoint for ticket {a.ticket}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2, default=str) if a.json else r["uri"])
    return 0


def cmd_cleanup(a) -> int:
    vol = registry.ModalVolume()
    st = load_state()
    recs = registry.build(vol, st)
    plan = registry.plan_cleanup(recs, st, keep_latest=a.keep)
    print(f"cleanup plan (keep base + deployed + policy active/previous + latest {plan['keep_latest']} per ticket)"
          + ("" if a.apply else "  [DRY RUN]"))
    if plan["refused"]:
        print(f"REFUSED: {plan['refused']}")
    for g in plan["keep"]:
        print(f"  keep   {g['run_path']}  {gb(g['size_bytes'])}  ({'; '.join(g['reasons'])})")
    for g in plan["delete"]:
        print(f"  DELETE {g['run_path']}  {gb(g['size_bytes'])}  created {when(g['created_at'])}")
    print(f"  never touched: {', '.join(plan['never_touched'])}")
    print(f"  would free {gb(plan['delete_bytes'])} (~${plan['storage_cost_usd_per_month_freed']}/month)")
    if a.json:
        print(json.dumps(plan, indent=2, default=str))
    if not a.apply or not plan["delete"]:
        return 0
    removed = registry.apply_cleanup(vol, plan, records=registry.build(vol, load_state()), state=load_state())
    print(f"removed {len(removed)} run dir(s)")
    return 0


def cmd_download(a) -> int:
    if a.uri:
        rec = next((r for r in registry.build(registry.ModalVolume(), load_state(need_rows=False))
                    if r.get("uri") == a.uri.rstrip("/")), None) or {"uri": a.uri.rstrip("/")}
    else:
        if not a.ticket:
            print("--ticket or --uri is required", file=sys.stderr)
            return 2
        rec = registry.resolve(registry.build(registry.ModalVolume(), load_state()), a.ticket, a.which)
        if rec is None:
            print(f"no {a.which} checkpoint for ticket {a.ticket}", file=sys.stderr)
            return 1
    if rec.get("kind") == "base":
        print(f"the base model is {registry.BASE_URI}; openpi downloads it itself", file=sys.stderr)
        return 1
    rel = registry.path_for(rec["uri"])
    # Default outside the repo (9+ GB): ~/.cache/rdf-checkpoints/<volume path>
    dest = pathlib.Path(a.dest or (pathlib.Path.home() / ".cache" / "rdf-checkpoints" / rel)).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for p in registry.download_paths(rec, with_train_state=a.with_train_state):
        # `modal volume get <vol> <dir-or-file> <existing local dir>` lands it as <local dir>/<basename>
        print(f"modal volume get {registry.VOLUME} {p} -> {dest}/{pathlib.PurePosixPath(p).name}")
        if a.dry_run:
            continue
        subprocess.run(["modal", "volume", "get", "--force", registry.VOLUME, p, str(dest)], check=True)
    print(f"checkpoint {rec['uri']} -> {dest}")
    print("serve locally: rdf_training.finetune_openpi.load_policy(<dest>/rdf_run.json) with checkpoint_step_dir=<dest>")
    return 0


def cmd_rates(a) -> int:
    cfg = cost.refresh_rates_file() if a.refresh else json.loads(cost.RATES_FILE.read_text())
    print(json.dumps(cfg, indent=2))
    return 0


def _apps() -> list[dict]:
    out = subprocess.run(["modal", "app", "list", "--json"], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _ts(s: str | None) -> float | None:
    return datetime.fromisoformat(s).timestamp() if s else None


def run_window(rec: dict) -> tuple[float, float] | None:
    """[started, finished] of a fine-tune from the Volume metadata: created_at (rdf_run.json, written
    at the end) minus wall_s, else the run id stamp (written at the start) plus wall_s."""
    wall = (rec.get("metrics") or {}).get("wall_s")
    if not wall:
        return None
    start = registry._created_from_run_id(rec["run_id"] or "")
    if start is None:
        return None
    return start, start + float(wall)


def cmd_cost(a) -> int:
    st = load_state()
    recs = [r for r in registry.for_ticket(registry.build(registry.ModalVolume(), st), a.ticket) if r["kind"] == "finetuned"]
    apps = [x for x in _apps() if x.get("description") == "rdf-training"] if a.billing else []
    rows = []
    for r in recs:
        m = r.get("metrics") or {}
        est = cost.estimate(float(m.get("wall_s") or 0), m.get("gpu"))
        row = {"uri": r["uri"], "training_run_id": (r.get("training_run") or {}).get("id"),
               "recorded_gpu_cost_cents": (r.get("training_run") or {}).get("gpu_cost_cents"), "estimate": est}
        win = run_window(r)
        if a.billing and win:
            # The App whose lifetime contains the run. Ephemeral (`modal run`) Apps that held exactly
            # this run are billed exclusively; the deployed App is shared (Policy, convert, other runs).
            cands = [x for x in apps if _ts(x["created_at"]) <= win[0] + 5
                     and (x.get("stopped_at") is None or _ts(x["stopped_at"]) >= win[1] - 5)]
            ephemeral = [x for x in cands if x.get("state") != "deployed"]
            app = ephemeral[0] if len(ephemeral) == 1 else next((x for x in cands if x.get("state") == "deployed"), None)
            if app:
                exclusive = app.get("state") != "deployed" and sum(
                    1 for o in recs if (w := run_window(o)) and _ts(app["created_at"]) <= w[0] + 5
                    and (app.get("stopped_at") is None or _ts(app["stopped_at"]) >= w[1] - 5)) == 1
                end = _ts(app.get("stopped_at")) or win[1]
                bill = cost.billed(app["app_id"], _ts(app["created_at"]), end)
                row["app"] = {k: app.get(k) for k in ("app_id", "state", "created_at", "stopped_at")}
                row["reconciled"] = cost.reconcile(est, bill, exclusive=exclusive)
        rows.append(row)
    if a.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for row in rows:
        e = row["estimate"]
        print(f"{row['uri']}\n  estimate: {cost.cost_label(e)}\n    {e['formula']}\n"
              f"    GPU only: {e['seconds']} s x {e['gpu_rate_cents_per_hour']}c/h = {e['gpu_cents']:.2f}c")
        if row.get("recorded_gpu_cost_cents") is not None:
            print(f"  training_run {row['training_run_id']}.gpu_cost_cents = {row['recorded_gpu_cost_cents']}")
        rc = row.get("reconciled")
        if rc:
            b = rc.get("billing") or rc.get("billing_check")
            print(f"  Modal billing (app {b['app_id']} {row['app']['state']}, {b['attribution']}, "
                  f"{'complete' if b['complete'] else 'INCOMPLETE: hour not closed yet'}): "
                  f"${b['total_cents'] / 100:.2f} {b['by_resource_cents']}  "
                  f"delta vs estimate {b['delta_vs_estimate_cents']:+d}c")
            print(f"  -> gpu_cost_cents would be {rc['total_cents']} (source {rc['source']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--ticket"); p.add_argument("--json", action="store_true")  # noqa: E702
    p = sub.add_parser("resolve"); p.add_argument("--ticket", required=True)  # noqa: E702
    p.add_argument("--which", default="latest", choices=["latest", "deployed", "base"]); p.add_argument("--json", action="store_true")  # noqa: E702
    p = sub.add_parser("cleanup"); p.add_argument("--keep", type=int, default=registry.DEFAULT_KEEP_LATEST)  # noqa: E702
    p.add_argument("--apply", action="store_true"); p.add_argument("--json", action="store_true")  # noqa: E702
    p = sub.add_parser("download"); p.add_argument("--ticket"); p.add_argument("--uri"); p.add_argument("--dest")  # noqa: E702
    p.add_argument("--which", default="latest", choices=["latest", "deployed"])
    p.add_argument("--with-train-state", action="store_true"); p.add_argument("--dry-run", action="store_true")  # noqa: E702
    p = sub.add_parser("rates"); p.add_argument("--refresh", action="store_true")  # noqa: E702
    p = sub.add_parser("cost"); p.add_argument("--ticket", required=True); p.add_argument("--billing", action="store_true")  # noqa: E702
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return {"list": cmd_list, "resolve": cmd_resolve, "cleanup": cmd_cleanup, "download": cmd_download,
            "rates": cmd_rates, "cost": cmd_cost}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
