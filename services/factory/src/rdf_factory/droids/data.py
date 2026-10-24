"""TDM-33 Data Droid: assemble the per-ticket LeRobot v2.1 dataset from the ticket's accepted episodes.

The droid is the engineer between review and training. Each time it is started or woken it:
  1. inspects the ticket's episodes in SpacetimeDB (only ``review_status == accepted`` count),
  2. validates them against the episode format (``rdf_common.episode_format``) and computes the
     takeover cut the converter will make,
  3. assembles the dataset with TDM-39's converter (``rdf_training.convert``): failure frames before
     ``takeover_ts`` are cut from recovery episodes; broken episodes are excluded with a reason,
  4. verifies the result (totals, parquet + video, norm stats, the cut, and openpi's LeRobot loader
     when available) and writes ``manifest.json`` (by the tool) and the dataset card ``README.md``
     (by the droid, from the tool outputs only),
  5. publishes ``dataset.lerobot_uri`` (``set_dataset_lerobot_uri``, which refuses non-accepted ids),
  6. parks: ``WAITING_FOR dataset:accepted_count>=N`` while collection is short, then
     ``WAITING_FOR data_plan:iteration>=<next>`` so the next iteration's plan wakes it to rebuild.
     The TDM-27 resume bridge evaluates those predicates and calls ``FactoryClient.resume``.

It never accepts, rejects or edits episodes and never bypasses review: candidates come only from
accepted rows, ``--exclude`` can only remove ids, and the reducer re-checks every id server side.

Bounds of a data-droid session (local mode):
  * ``write_roots``: its own workdir (always), its ticket dir ``work/sessions/data-t<ticket>`` and this
    ticket's dataset dir (for README.md) only;
  * ``allow_commands``: the tool prefix ``TOOL_PREFIX`` and the converter's pytest (``PYTEST_PREFIX``);
    nothing else runs, and shell metacharacters are refused by the client;
  * ``max_turns``: ``MAX_TURNS`` (passed to the client when it supports it, and checked here before
    every resume), plus the client's per-turn timeout.

Orchestrator wiring: ``rdf_orchestrator/handlers/data_droid.py`` (opt-in ``RDF_DATA_DROID=1``) starts
one droid per ticket when the ticket enters ``review``.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("rdf_factory.droids.data")

ROLE = "data_droid"
MAX_TURNS = 6  # start + wake at target + wake per later iteration + slack for a missed final line
TOOL_PREFIX = "uv run --frozen --project services/training python -m rdf_training.data_droid_tools"
PYTEST_PREFIX = ("uv run --frozen --project services/training pytest "
                 "services/training/tests/test_tdm39_convert.py services/training/tests/test_tdm33_data_droid_tools.py")
LIVE = ("starting", "running", "waiting_for", "resuming", "paused")


def datasets_root(work_root: Path) -> Path:
    """Local dataset root (``<root>/lerobot/<repo_id>``, ``<root>/assets/<repo_id>``); gitignored."""
    env = os.environ.get("RDF_DATA_DROID_DATASETS_ROOT", "").strip()
    return Path(env).resolve() if env else Path(work_root).resolve().parent / "datasets"


def repo_id_for(ticket_id: int) -> str:
    return f"rdf/ticket-{int(ticket_id)}"


def dataset_dir(root: Path, ticket_id: int) -> Path:
    return Path(root) / "lerobot" / repo_id_for(ticket_id)


def ticket_dir(work_root: Path, ticket_id: int) -> Path:
    """The droid's per-ticket scratch dir (episodes.json, validation.json, verify.json, notes.md)."""
    return Path(work_root).resolve() / f"data-t{int(ticket_id)}"


BRIEF = """You are the Data Droid for ticket {ticket_id} (iteration {iteration}). Your job: turn this ticket's
ACCEPTED episodes into its LeRobot v2.1 training dataset for openpi, check it, document it and record it.{fixture_note}

Your ticket dir (tool outputs and notes): {w}
Your only shell tool (run from the repo root, exactly this prefix, no pipes or other commands):
  {tool} inspect  --ticket {ticket_id} --target {target} --out {w}/episodes.json
  {tool} validate --episodes {w}/episodes.json --out {w}/validation.json{media}
  {tool} assemble --episodes {w}/episodes.json --out-root {out}{assemble_flags}{media} [--exclude <id>:"<reason>" ...]
  {tool} verify   --dataset {ds} --episodes {w}/episodes.json --out {w}/verify.json{media}
  {tool} publish  --ticket {ticket_id} --manifest {ds}/manifest.json
Also allowed, only if a conversion error looks like a converter bug: {pytest}
Each prints one JSON object. Files you may write: {w}/, your working directory (resume_payload.json) and
{ds}/README.md.

Rules: never edit episode files, never change review status, never add or invent episodes, numbers or
results. Only accepted episodes go in (the tools and the reducer enforce this). If an accepted episode
looks wrong, flag it in the card; exclude it only for a validate error, with the reason.

Steps (now, and again whenever a [resume-bridge] message wakes you):
1. `inspect`. If `ready` is false (accepted_count < {target}): run `validate`, write {w}/notes.md (what is
   accepted, any problems found so far), write resume_payload.json, and end with
   WAITING_FOR dataset:accepted_count>={target}
2. `validate`. Read every error and warning. Episodes in `flagged_ids` cannot be converted: pass each as
   --exclude <id>:"<the error>" to assemble. Warnings go in the card.
3. `assemble`. If it fails, read the error and fix what you can (exclude a broken episode with its reason)
   and rerun; at most 2 retries, then report the failure and end with DONE.
4. `verify`. It must say ok:true: every recovery episode in takeover_cuts has no_frames_before_takeover
   true, and lerobot_loader shows the load result. If not ok, fix and repeat 3-4 once, else report and DONE.
5. Write {ds}/README.md, the dataset card: title; a FIXTURE warning line at the top if the assemble output
   or manifest says fixture; ticket, task prompt, repo_id, LeRobot v2.1, fps; features and shapes; a table of
   episodes (episode_id, kind, source frames, kept frames, cut before takeover, cut after handback); excluded
   and flagged episodes with reasons; validate warnings; state/action mean and std per dim and the
   norm-stats dims from verify; the LeRobot loader result; how to load it (HF_LEROBOT_HOME={out}/lerobot,
   repo_id). Only numbers from the tool outputs.
6. `publish`. Then write resume_payload.json and end with
   WAITING_FOR data_plan:iteration>={next_iteration}
Be brief in your replies; do not explore the repo beyond what a step needs."""


def build_brief(*, ticket_id: int, iteration: int, target: int, workdir_hint: str, out_root: Path,
                allow_fixtures: bool = False, media_root: str = "", backend: str = "local") -> str:
    ds = dataset_dir(out_root, ticket_id)
    flags = (" --allow-fixtures" if allow_fixtures else "") + (f" --backend {backend}" if backend != "local" else "")
    note = ("\nThis run uses LABELLED SYNTHETIC FIXTURE episodes (a local test database, not robot data): pass "
            "--allow-fixtures as shown and say so at the top of the card.") if allow_fixtures else ""
    return BRIEF.format(ticket_id=ticket_id, iteration=iteration, target=target, w=workdir_hint, out=out_root, ds=ds,
                        tool=TOOL_PREFIX, pytest=PYTEST_PREFIX, assemble_flags=flags,
                        media=f" --media-root {media_root}" if media_root else "", fixture_note=note,
                        next_iteration=iteration + 1).strip()


def _supports(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def launch_data_droid(fc: Any, ticket_id: int, *, target: int, iteration: int = 0, allow_fixtures: bool = False,
                            media_root: str = "", backend: str = "local", max_turns: int = MAX_TURNS,
                            name: str = "") -> Any:
    """Start a data-droid session for ``ticket_id`` (``fc``: a ``FactoryClient``). Returns the first TurnResult.

    ``target``: the accepted-episode count the dataset should be built at (the droid parks below it).
    ``allow_fixtures``: only for labelled fixture tests on a local database.
    """
    if target < 1:
        raise ValueError("target must be >= 1")
    out_root = datasets_root(fc.cfg.work_root)
    ds = dataset_dir(out_root, ticket_id)
    tdir = ticket_dir(fc.cfg.work_root, ticket_id)
    tdir.mkdir(parents=True, exist_ok=True)
    brief = build_brief(ticket_id=ticket_id, iteration=iteration, target=target, workdir_hint=str(tdir),
                        out_root=out_root, allow_fixtures=allow_fixtures, media_root=media_root, backend=backend)
    kw: dict[str, Any] = dict(env={"ticket_id": ticket_id, "iteration": iteration, "target_episodes": target,
                                   "dataset_dir": str(ds), "fixtures": allow_fixtures},
                              ticket_id=ticket_id, name=name or f"data-droid-t{ticket_id}",
                              write_roots=[str(tdir), str(ds)], allow_commands=[TOOL_PREFIX, PYTEST_PREFIX])
    if _supports(fc.start_session, "max_turns"):
        kw["max_turns"] = max_turns
    return await fc.start_session(ROLE, brief, **kw)


async def resume_data_droid(fc: Any, agent_session_id: int, text: str, *, max_turns: int = MAX_TURNS, **kw: Any) -> Any:
    """``fc.resume`` with this droid's turn bound checked first (the bridge may call ``fc.resume`` directly;
    newer clients enforce ``max_turns`` themselves)."""
    from rdf_factory.backends import SessionSpec

    spec = SessionSpec.load(fc.cfg.state_dir, agent_session_id)
    if spec is not None and spec.turns >= max_turns:
        await fc.finish(agent_session_id, "failed", f"failed: data droid turn limit {max_turns} reached")
        raise RuntimeError(f"agent_session {agent_session_id}: turn limit {max_turns} reached")
    return await fc.resume(agent_session_id, text, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_factory.droids.data", description="TDM-33 Data Droid")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--ticket", type=int, required=True)
    p.add_argument("--target", type=int, required=True)
    p.add_argument("--iteration", type=int, default=0)
    p.add_argument("--allow-fixtures", action="store_true")
    p.add_argument("--media-root", default="")
    p.add_argument("--backend", choices=("local", "modal"), default="local")
    p = sub.add_parser("resume")
    p.add_argument("id", type=int)
    p.add_argument("text")
    a = ap.parse_args(argv)

    async def go() -> int:
        from rdf_factory import FactoryClient

        async with FactoryClient() as fc:
            if a.cmd == "start":
                r = await launch_data_droid(fc, a.ticket, target=a.target, iteration=a.iteration,
                                            allow_fixtures=a.allow_fixtures, media_root=a.media_root, backend=a.backend)
            else:
                r = await resume_data_droid(fc, a.id, a.text)
        print(json.dumps(r.to_dict(), indent=2, default=str))
        return 0 if r.ok else 1

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
