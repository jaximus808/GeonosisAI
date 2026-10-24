"""TDM-34 Training Droid: configure, launch (via TDM-21) and monitor the Modal fine-tune.

Division of labour (read with ``services/orchestrator/src/rdf_orchestrator/modal_client.py``):

* **TDM-21 (orchestrator code, unchanged)** is the mechanical launcher. ``training_run`` entering
  ``queued`` -> spawn ``rdf-training.convert`` then ``finetune(ticket_id, dataset_uri, hyperparams)``
  with ``hyperparams = training_run.hyperparams_json``; poll; set running/done/failed with the
  checkpoint, metrics (``loss_curve`` ...) and GPU cost.
* **The Training Droid (this module)** is the engineer in front of and behind that launch:
    1. sanity-checks the dataset before any GPU is spent (``check_dataset``),
    2. picks and justifies hyperparams from run_settings and the dataset (``recommend``) and writes
       the training config ``configs/train_<ticket>_<iter>.yaml`` in its ticket dir,
    3. hands off by calling ``queue_training_run(ticket, episode_ids, hyperparams_json)``: the ONLY
       handoff. TDM-21 then launches exactly as for any queued run,
    4. parks (``WAITING_FOR`` on that run reaching done/failed/cancelled); the TDM-27 resume bridge
       wakes it,
    5. on ``failed`` classifies the Modal error and re-queues ONCE with adjusted hyperparams
       (``retry_plan``: OOM -> half batch, sqrt-scaled lr, steps doubled to keep samples seen),
    6. on ``done`` analyses the loss curve (``analyze_loss``: divergence, no learning, plateau,
       memorisation risk), writes a short training note and logs ``training.report``.
  The droid acts through one allow-listed tool, ``python -m rdf_factory.droids.training_tool``
  (inspect / queue / status / report), so the guardrails (bounds, budget, one retry, DB target)
  live in code and the droid supplies the judgement and the written rationale.

Bounds of a training-droid session: ``write_roots`` = its own workdir + its ticket dir only,
``allow_commands`` = the tool prefix only (no other shell), ``max_turns`` = ``MAX_TURNS``, and
droid-sdk autonomy ``off`` so every edit and command really goes through that policy (at the
default ``low`` the droid runs commands it rates read-only, and edits files, without asking).

Flywheel wiring: ``TrainingDroidStep`` replaces TDM-22's ``QueueTrainingStep`` for the ``train``
stage when ``RDF_TRAINING_DROID=1`` (``handlers/flywheel.py``). If the droid cannot start (no
Factory key, droid error) it falls back to the plain ``QueueTrainingStep`` so the loop never stalls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

log = logging.getLogger("rdf_factory.droids.training")

ROLE = "training_droid"
ACTOR = "droid:training"  # event_log actor for the droid's own records (training.config / training.report)
MAX_TURNS = 6  # start + (wake per run) x (1 run + 1 retry) + slack for a missed final line
MAX_RETRIES = 1  # automatic re-queues per droid session
TOOL_MODULE = "rdf_factory.droids.training_tool"
TOOL_PREFIX = f"uv run --project services/factory python -m {TOOL_MODULE}"  # run from the repo root

# The S-05a / TDM-40 recipe (services/training/src/rdf_training/finetune.py; WORKER_GUIDE "Training recipe").
DEFAULT_HP: dict[str, Any] = {"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16, "prompt": ""}
ACTION_HORIZON = 50  # a sample needs 50 future actions: shorter episodes contribute nothing
FPS = 30
H100_SEC_PER_STEP = 1.31  # at batch 32, LoRA
FIXED_OVERHEAD_S = 300  # image pull, weights load, compile, checkpoint save
H100_CENTS_PER_HOUR = 395
FINETUNE_TIMEOUT_S = 2 * 3600  # modal_app.FINETUNE_TIMEOUT_S
MIN_STEPS, MAX_STEPS = 300, 4500  # MAX: fits the 2 h function timeout with margin
BATCH_CHOICES = (8, 16, 32, 64)
LORA_CHOICES = (8, 16, 32)  # 0 (full fine-tune) is not allowed for a droid: 9x the memory
LR_RANGE = (1e-6, 2e-4)
DATASET_KINDS = ("clean_teleop", "recovery_takeover", "purchased")  # flywheel.DATASET_KINDS
TERMINAL = ("done", "failed", "cancelled")


# =========================================================================== dataset sanity checks

def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def check_dataset(episodes: Iterable[Mapping[str, Any]], *, fps: int = FPS) -> dict:
    """Accepted dataset episodes -> {ok, episode_ids, excluded, errors, warnings, stats}.

    Hard errors block the GPU spend; an episode that cannot yield a single training sample (shorter
    than the action horizon, no actions file, no camera) is excluded rather than trained on.
    """
    eps = [dict(e) for e in episodes if e.get("review_status") == "accepted"]
    errors: list[str] = []
    warnings: list[str] = []
    excluded: list[dict] = []
    used: list[dict] = []
    seen: set[int] = set()
    for e in sorted(eps, key=lambda r: int(r["id"])):
        eid = int(e["id"])
        if eid in seen:
            excluded.append({"episode_id": eid, "reason": "duplicate id"})
            continue
        seen.add(eid)
        kind = str(e.get("kind") or "")
        if kind not in DATASET_KINDS:
            excluded.append({"episode_id": eid, "reason": f"kind {kind!r} is a policy rollout, not a demonstration"})
            continue
        frames = int(_num(e.get("frame_count")))
        if frames < ACTION_HORIZON:
            excluded.append({"episode_id": eid, "reason": f"{frames} frames < action horizon {ACTION_HORIZON}"})
            continue
        if not str(e.get("actions_uri") or "").strip():
            excluded.append({"episode_id": eid, "reason": "no actions_uri"})
            continue
        try:
            cams = json.loads(e.get("cameras_json") or "{}")
        except ValueError:
            cams = {}
        if not isinstance(cams, dict) or not any(str(v).strip() for v in cams.values()):
            excluded.append({"episode_id": eid, "reason": "no camera video in cameras_json"})
            continue
        used.append({**e, "_frames": frames, "_cams": sorted(cams)})

    stats: dict[str, Any] = {"accepted": len(eps), "used": len(used), "excluded": len(excluded)}
    if not used:
        errors.append("no usable accepted demonstration episodes: nothing to train on")
        return {"ok": False, "episode_ids": [], "excluded": excluded, "errors": errors, "warnings": warnings,
                "stats": stats}

    frames = [u["_frames"] for u in used]
    durs = [_num(u.get("duration_ms")) / 1000.0 for u in used]
    by_kind: dict[str, int] = {}
    for u in used:
        by_kind[str(u.get("kind"))] = by_kind.get(str(u.get("kind")), 0) + 1
    med = statistics.median(frames)
    stats.update({
        "by_kind": by_kind, "frames_total": sum(frames), "frames_min": min(frames), "frames_max": max(frames),
        "frames_median": med, "duration_s_total": round(sum(durs), 1),
        "samples_per_epoch": sum(max(0, f - ACTION_HORIZON + 1) for f in frames),
        "cameras": sorted({c for u in used for c in u["_cams"]}),
    })
    if len(used) < 3:
        warnings.append(f"only {len(used)} usable episode(s): expect memorisation; the S-05a recipe used 5-10")
    for u, d in zip(used, durs):
        if d > 0:
            rate = u["_frames"] / d
            if not (0.8 * fps <= rate <= 1.2 * fps):
                warnings.append(f"episode {u['id']}: {rate:.1f} frames/s vs {fps} expected (dropped frames or wrong fps)")
    outliers = [int(u["id"]) for u in used if u["_frames"] > 3 * med or u["_frames"] < med / 3]
    if outliers:
        warnings.append(f"episode length outliers (>3x or <1/3 of the median {med:.0f} frames): {outliers}")
    cam_sets = {tuple(u["_cams"]) for u in used}
    if len(cam_sets) > 1:
        warnings.append(f"episodes disagree on camera sets {sorted(cam_sets)}: conversion keeps the common ones")
    rec = [int(u["id"]) for u in used if u.get("kind") == "recovery_takeover" and not u.get("takeover_ts")]
    if rec:
        warnings.append(f"recovery_takeover episodes without takeover_ts (trained as whole episodes): {rec}")
    fake = [int(u["id"]) for u in used if re.match(r"^(fake|stub|sim)://", str(u.get("storage_uri") or u.get("actions_uri") or ""))]
    if fake:
        warnings.append(f"episodes {fake} point at fake/stub media: only valid for a stubbed Modal run")
    return {"ok": not errors, "episode_ids": [int(u["id"]) for u in used], "excluded": excluded, "errors": errors,
            "warnings": warnings, "stats": stats}


# =========================================================================== hyperparams

def sec_per_step(batch_size: int) -> float:
    """S-05a: 1.31 s/step at batch 32; roughly a quarter of a step is fixed cost per step."""
    return H100_SEC_PER_STEP * (0.25 + 0.75 * batch_size / 32)


def estimate(hp: Mapping[str, Any], *, samples_per_epoch: int = 0) -> dict:
    steps, bs = int(hp["steps"]), int(hp["batch_size"])
    wall = FIXED_OVERHEAD_S + steps * sec_per_step(bs)
    out = {"wall_min": round(wall / 60, 1), "gpu_cost_cents": int(round(wall / 3600 * H100_CENTS_PER_HOUR)),
           "fits_timeout": wall <= FINETUNE_TIMEOUT_S - 300}
    if samples_per_epoch:
        out["epochs"] = round(steps * bs / samples_per_epoch, 1)
    return out


def _round_steps(x: float) -> int:
    return int(max(MIN_STEPS, min(MAX_STEPS, round(x / 100.0) * 100)))


def _steps_for_budget(budget_cents: int, bs: int) -> int:
    seconds = budget_cents / H100_CENTS_PER_HOUR * 3600 - FIXED_OVERHEAD_S
    return int(math.floor(max(0.0, seconds) / sec_per_step(bs) / 100.0) * 100)


def target_epochs(samples_per_epoch: int) -> int:
    """Few demos: more passes (S-05a: 5 x ~20 s demos, 2000 x 32 samples = ~20 epochs). Larger sets need fewer."""
    if samples_per_epoch < 5_000:
        return 20
    if samples_per_epoch < 20_000:
        return 12
    return 8


def recommend(stats: Mapping[str, Any], run_hp: Mapping[str, Any] | None, *, budget_left_cents: int | None = None,
              prompt_fallback: str = "") -> dict:
    """Starting-point hyperparams + the reasons, from run_settings and the dataset stats.

    run_settings values that differ from the default are treated as the user's choice and kept;
    defaults are re-derived from the dataset. Returns {hyperparams, rationale, estimate, pinned, error?}.
    """
    hp_in = {**DEFAULT_HP, **{k: v for k, v in dict(run_hp or {}).items() if v not in (None, "")}}
    pinned = sorted(k for k in ("lr", "steps", "batch_size", "lora_rank")
                    if k in (run_hp or {}) and (run_hp or {})[k] != DEFAULT_HP[k])
    why: list[str] = []
    spe = int(stats.get("samples_per_epoch") or stats.get("frames_total") or 0)
    bs = int(hp_in["batch_size"])
    lora = int(hp_in["lora_rank"])
    if "batch_size" in pinned:
        why.append(f"batch_size {bs}: set in run_settings")
    else:
        why.append("batch_size 32: the S-05a recipe on 1x H100 (224 px, action horizon 50)")
    if "lora_rank" in pinned:
        why.append(f"lora_rank {lora}: set in run_settings")
    else:
        why.append("lora_rank 16: openpi gemma_2b_lora; a few demos do not need more adapter capacity, and full "
                   "fine-tune (0) would overfit them and need ~9x the memory")
    if "steps" in pinned:
        steps = int(hp_in["steps"])
        why.append(f"steps {steps}: set in run_settings")
    elif spe:
        ep = target_epochs(spe)
        steps = _round_steps(ep * spe / bs)
        why.append(f"steps {steps}: ~{ep} epochs over {spe} samples ({stats.get('used', '?')} episodes, "
                   f"{stats.get('frames_total', '?')} frames) at batch {bs}; clamped to [{MIN_STEPS}, {MAX_STEPS}]")
    else:
        steps = int(DEFAULT_HP["steps"])
        why.append(f"steps {steps}: recipe default (no dataset stats)")
    if "lr" in pinned:
        lr = float(hp_in["lr"])
        why.append(f"lr {lr:g}: set in run_settings")
    else:
        lr = float(f"{DEFAULT_HP['lr'] * math.sqrt(bs / 32):.3g}")
        why.append(f"lr {lr:g}: recipe 2.5e-5 at batch 32" + ("" if bs == 32 else f", sqrt-scaled to batch {bs}"))
    hp = {"lr": lr, "steps": steps, "batch_size": bs, "lora_rank": lora,
          "prompt": str(hp_in.get("prompt") or prompt_fallback or "").strip()}
    out: dict[str, Any] = {"hyperparams": hp, "rationale": why, "pinned": pinned}
    if budget_left_cents is not None:
        est = estimate(hp, samples_per_epoch=spe)
        if est["gpu_cost_cents"] > budget_left_cents:
            fit = _steps_for_budget(budget_left_cents, bs)
            if fit < MIN_STEPS:
                out["error"] = (f"budget left {budget_left_cents} c cannot buy a {MIN_STEPS}-step run "
                                f"(~{estimate({**hp, 'steps': MIN_STEPS})['gpu_cost_cents']} c)")
            else:
                why.append(f"steps cut {steps} -> {fit} to fit the ticket budget ({budget_left_cents} c left)")
                hp["steps"] = fit
    out["estimate"] = estimate(hp, samples_per_epoch=spe)
    return out


def validate(hp: Mapping[str, Any], *, budget_left_cents: int | None = None) -> list[str]:
    """Bounds a droid-written config must satisfy before anything is queued. [] = ok."""
    errs: list[str] = []
    try:
        lr, steps, bs, lora = float(hp["lr"]), int(hp["steps"]), int(hp["batch_size"]), int(hp["lora_rank"])
    except (KeyError, TypeError, ValueError) as e:
        return [f"hyperparams need numeric lr, steps, batch_size, lora_rank ({type(e).__name__}: {e})"]
    if not (LR_RANGE[0] <= lr <= LR_RANGE[1]):
        errs.append(f"lr {lr:g} outside {LR_RANGE[0]:g}..{LR_RANGE[1]:g} for LoRA fine-tuning")
    if not (MIN_STEPS <= steps <= MAX_STEPS):
        errs.append(f"steps {steps} outside {MIN_STEPS}..{MAX_STEPS} (the 2 h Modal timeout)")
    if bs not in BATCH_CHOICES:
        errs.append(f"batch_size {bs} not one of {BATCH_CHOICES}")
    if lora not in LORA_CHOICES:
        errs.append(f"lora_rank {lora} not one of {LORA_CHOICES}")
    if not errs:
        est = estimate(hp)
        if not est["fits_timeout"]:
            errs.append(f"~{est['wall_min']} min does not fit the {FINETUNE_TIMEOUT_S // 60} min timeout")
        if budget_left_cents is not None and est["gpu_cost_cents"] > budget_left_cents:
            errs.append(f"~{est['gpu_cost_cents']} c GPU is over the ticket's remaining budget ({budget_left_cents} c)")
    return errs


# =========================================================================== failures

_OOM = re.compile(r"out of memory|\bOOM\b|RESOURCE_EXHAUSTED|CUDA_ERROR_OUT_OF_MEMORY|failed to allocate|"
                  r"Allocator .* ran out|MemoryError|exit code 137|OOMKilled", re.I)
_TIMEOUT = re.compile(r"FunctionTimeoutError|timed out|timeout", re.I)
_TRANSIENT = re.compile(r"Modal unreachable|preempt|InputCancellation|ConnectionError|ClientClosed|worker lost", re.I)
_DATA = re.compile(r"convert failed|launch failed|no dataset_uri|episodes? .* not found|norm.?stats|KeyError|"
                   r"FinetuneError", re.I)


def classify_failure(error: str) -> str:
    """oom | timeout | transient | data | unknown (first match wins, in that order)."""
    e = error or ""
    for name, rx in (("oom", _OOM), ("timeout", _TIMEOUT), ("transient", _TRANSIENT), ("data", _DATA)):
        if rx.search(e):
            return name
    return "unknown"


def retry_plan(error: str, hp: Mapping[str, Any], *, budget_left_cents: int | None = None) -> dict:
    """What one automatic retry should change. {kind, retry, hyperparams?, rationale}."""
    kind = classify_failure(error)
    hp = dict(hp)
    if kind == "oom":
        bs = int(hp.get("batch_size") or 32)
        if bs <= BATCH_CHOICES[0]:
            return {"kind": kind, "retry": False, "rationale": [f"OOM at batch {bs}: already the smallest batch; "
                                                                "needs a bigger GPU or lower image size, not a retry"]}
        nb = bs // 2
        lr = float(f"{float(hp.get('lr') or DEFAULT_HP['lr']) * math.sqrt(nb / bs):.3g}")
        steps = _round_steps(int(hp.get("steps") or DEFAULT_HP["steps"]) * bs / nb)
        why = [f"OOM: batch_size {bs} -> {nb} halves activation memory",
               f"lr {hp.get('lr')} -> {lr:g}: sqrt-scaled with the batch to keep the update noise comparable",
               f"steps {hp.get('steps')} -> {steps}: keeps the number of samples seen (capped at {MAX_STEPS})"]
        new = {**hp, "batch_size": nb, "lr": lr, "steps": steps}
    elif kind == "timeout":
        budget_s = 0.8 * FINETUNE_TIMEOUT_S - FIXED_OVERHEAD_S
        steps = _round_steps(min(int(hp.get("steps") or 0) * 0.7, budget_s / sec_per_step(int(hp.get("batch_size") or 32))))
        why = [f"timeout: steps {hp.get('steps')} -> {steps} so the run ends at <=80% of the {FINETUNE_TIMEOUT_S // 60} min cap"]
        new = {**hp, "steps": steps}
    elif kind == "transient":
        why = ["transient Modal/infra failure: retry the same config"]
        new = hp
    else:
        return {"kind": kind, "retry": False,
                "rationale": [f"{kind} failure: hyperparams are not the cause; fix the data/launch and re-queue by hand"]}
    if budget_left_cents is not None:
        errs = validate(new, budget_left_cents=budget_left_cents)
        if errs:
            return {"kind": kind, "retry": False, "hyperparams": new, "rationale": why + errs}
    return {"kind": kind, "retry": True, "hyperparams": new, "rationale": why}


# =========================================================================== loss curve

def _slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope (loss per step)."""
    n = len(points)
    if n < 2:
        return 0.0
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    return 0.0 if den == 0 else sum((p[0] - mx) * (p[1] - my) for p in points) / den


def analyze_loss(metrics: Mapping[str, Any], *, samples_per_epoch: int = 0, batch_size: int = 32,
                 episodes: int = 0) -> dict:
    """Read the finetune's loss curve. {verdict ok|suspect|diverged|no_curve, flags, numbers, notes}.

    There is no validation split (a handful of demos), so overfitting is reported as a *risk* from
    epochs seen and how far the train loss collapsed; the eval run decides.
    """
    raw = metrics.get("loss_curve") or []
    pts: list[tuple[float, float]] = []
    bad = False
    for p in raw:
        try:
            s, v = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isnan(v) or math.isinf(v):
            bad = True
            continue
        pts.append((s, v))
    out: dict[str, Any] = {"fake": bool(metrics.get("fake")), "points": len(pts)}
    if bad or (metrics.get("final_loss") is not None and not math.isfinite(_num(metrics.get("final_loss"), float("nan")))):
        return {**out, "verdict": "diverged", "flags": ["nan_loss"], "notes": ["loss became NaN/inf: diverged"]}
    if len(pts) < 3:
        return {**out, "verdict": "no_curve", "flags": [], "notes": ["no loss curve reported; judge by the eval only"]}
    pts.sort()
    first, final = pts[0][1], pts[-1][1]
    lo = min(v for _, v in pts)
    span = pts[-1][0] - pts[0][0] or 1.0
    tail = [p for p in pts if p[0] >= pts[-1][0] - 0.2 * span] or pts[-3:]
    late = [p for p in pts if p[0] >= pts[-1][0] - 0.3 * span]
    slope_tail = _slope(tail)
    steps = int(metrics.get("steps") or pts[-1][0])
    out.update({"first_loss": round(first, 5), "final_loss": round(final, 5), "min_loss": round(lo, 5),
                "drop_pct": round(100 * (1 - final / first), 1) if first > 0 else None,
                "tail_slope_per_1k": round(slope_tail * 1000, 5), "steps": steps})
    if samples_per_epoch:
        out["epochs"] = round(steps * batch_size / samples_per_epoch, 1)
    flags, notes = [], []
    if final > 1.5 * lo and final > 0.9 * first:
        flags.append("diverged")
        notes.append(f"loss climbed back from its minimum {lo:.4f} to {final:.4f}: diverged (lower lr)")
    elif final > 1.25 * lo and slope_tail > 0:
        flags.append("rising_tail")
        notes.append(f"loss rising over the last 20% of steps ({final:.4f} vs min {lo:.4f}): instability, consider lower lr")
    if first > 0 and final > 0.85 * first:
        flags.append("not_learning")
        notes.append(f"loss only fell {100 * (1 - final / first):.0f}%: check lr / data / norm stats")
    if late and late[0][1] > 0 and abs(late[0][1] - final) / late[0][1] < 0.02 and "not_learning" not in flags:
        flags.append("plateau")
        notes.append("loss flat over the last 30% of steps: fewer steps would likely do as well")
    ep = out.get("epochs") or 0
    if ep > 30 or (first > 0 and final / first < 0.03 and 0 < episodes < 10):
        flags.append("overfit_risk")
        notes.append(f"{ep or '?'} epochs over {episodes or '?'} episodes and train loss down to "
                     f"{100 * final / first:.1f}% of its start: likely memorising demos; trust the eval, not the loss")
    verdict = "diverged" if "diverged" in flags else ("suspect" if set(flags) - {"plateau"} else "ok")
    if not notes:
        notes.append(f"healthy curve: {first:.4f} -> {final:.4f} ({out['drop_pct']}% drop), flat-to-falling tail")
    return {**out, "verdict": verdict, "flags": flags, "notes": notes}


# =========================================================================== brief + launcher

def ticket_dir(work_root: Path, ticket_id: int, iteration: int) -> Path:
    return Path(work_root) / f"training-t{ticket_id}-i{iteration}"


def waiting_line(run_id: int) -> str:
    """The exact WAITING_FOR the droid ends with: this run reaching any terminal status (TDM-27 predicate)."""
    leaves = [{"table": "training_run", "id": int(run_id), "field": "status", "op": "==", "value": s} for s in TERMINAL]
    return "WAITING_FOR " + json.dumps({"any": leaves}, separators=(",", ":"))


BRIEF = """You are the Training Droid for ticket {ticket_id} (iteration {iteration}): you configure, launch and
monitor a pi0.5 LoRA fine-tune on Modal. The orchestrator (TDM-21) does the mechanical Modal launch and
polling once you queue the run; your job is the engineering judgement around it. {stub_note}

Your ticket dir (configs and notes go here): {tdir}
Your only shell tool (run from the repo root, exactly this prefix; no cd, pipes, redirects like 2>&1, or other commands):
  {tool} <command> --ctx {ctx}
Commands: inspect | queue --config <yaml> | status --run <id> | report --run <id> --note <md>

Turn 1 (now):
1. Run `inspect`. It prints the dataset sanity checks, dataset stats, run_settings, budget and a starting
   recommendation with reasons. If `checks.ok` is false, do not queue: explain why and end with DONE.
2. Decide the hyperparams (lr, steps, batch_size, lora_rank, prompt). Start from the recommendation; change
   a value only for a reason you can state from the stats or warnings. Write {tdir}/configs/train_{ticket_id}_{iteration}.yaml:
     ticket_id: {ticket_id}
     iteration: {iteration}
     episode_ids: [...]        # checks.episode_ids
     hyperparams: {{lr: ..., steps: ..., batch_size: ..., lora_rank: ..., prompt: "..."}}
     rationale:                # 3-6 short lines: why each value, what the warnings mean for this run
       - "..."
3. Run `queue --config <that file>`. It validates bounds and budget, queues the run and prints `waiting_line`.
4. Write resume_payload.json, print the RESUME_PAYLOAD line, and end your reply with the printed waiting_line
   verbatim as the final line.
When woken ([resume-bridge] message): run `status --run <id>`.
- failed: if `retry_plan.retry` is true and `retries_left` > 0, write configs/train_{ticket_id}_{iteration}_retry1.yaml
  (same format plus `retry_of: <failed run id>`) using retry_plan.hyperparams, adjusted only with a stated reason,
  `queue` it, and park again with the new waiting_line. Otherwise report the failure (next step) and end with DONE.
- done (or a failure you will not retry): write {tdir}/training_note_{ticket_id}_{iteration}.md (<= 15 lines: what
  ran, config and why, loss verdict with numbers, divergence / overfitting flags, what to try next), run
  `report --run <id> --note <that file>`, and end with DONE.
Rules: create/edit files only with your file tools (Create/Edit), never through the shell; any other
command is refused and ends your turn. Keep each turn short: no exploring the repo, only these commands."""


def build_brief(*, ticket_id: int, iteration: int, tdir: Path, ctx_path: Path, stub_note: str = "") -> str:
    return BRIEF.format(ticket_id=ticket_id, iteration=iteration, tdir=tdir, ctx=ctx_path, tool=TOOL_PREFIX,
                        stub_note=stub_note).strip()


def write_context(tdir: Path, *, host: str, db: str, ticket_id: int, iteration: int, extra: dict | None = None) -> Path:
    """droid_context.json: which database the tool may talk to (no secrets: the token stays in env)."""
    (tdir / "configs").mkdir(parents=True, exist_ok=True)
    p = tdir / "droid_context.json"
    p.write_text(json.dumps({"host": host.rstrip("/"), "db": db, "ticket_id": int(ticket_id), "iteration": int(iteration),
                             "created_at": time.time(), **(extra or {})}, indent=2))
    return p


async def launch_training_droid(fc: Any, ticket_id: int, iteration: int, *, host: str | None = None,
                                db: str | None = None, stub_note: str = "", max_turns: int = MAX_TURNS,
                                name: str = "") -> Any:
    """Start a training-droid session for (ticket, iteration). Returns the first TurnResult.

    ``fc``: a ``FactoryClient``. host/db default to SPACETIME_HOST / SPACETIME_DB_NAME (what ``fc``'s
    mirror and the droid's tool both use).
    """
    host = host or os.environ.get("SPACETIME_HOST", "")
    db = db or os.environ.get("SPACETIME_DB_NAME", "")
    if not host or not db:
        raise RuntimeError("SPACETIME_HOST / SPACETIME_DB_NAME must be set for the training droid")
    tdir = ticket_dir(fc.cfg.work_root, ticket_id, iteration)
    ctx = write_context(tdir, host=host, db=db, ticket_id=ticket_id, iteration=iteration)
    brief = build_brief(ticket_id=ticket_id, iteration=iteration, tdir=tdir, ctx_path=ctx, stub_note=stub_note)
    return await fc.start_session(ROLE, brief, env={"ticket_id": ticket_id, "iteration": iteration},
                                  ticket_id=ticket_id, name=name or f"training-droid-t{ticket_id}-i{iteration}",
                                  write_roots=[str(tdir)], allow_commands=[TOOL_PREFIX], max_turns=max_turns,
                                  autonomy="off")


# =========================================================================== flywheel step

@dataclass
class TrainingDroidStep:
    """Flywheel ``train`` step (opt-in ``RDF_TRAINING_DROID=1``): start a Training Droid instead of
    queueing with run_settings directly. Idempotent: skips while a run is queued/running/done in this
    iteration or a training droid for this ticket is still live (it owns its own one retry)."""

    stage: str = "train"
    owner: str = "TDM-34 training droid (launch: TDM-21)"

    async def run(self, ctx: Any) -> None:
        v = ctx.view
        run = v.training_run
        if run is not None and run.get("status") in ("queued", "running", "done"):
            ctx.log("step.skip", reason=f"training run {run['id']} already {run['status']}")
            return
        live = [dict(s) for s in ctx.bus.rows("agent_session")
                if s.get("role") == ROLE and int(s.get("ticket_id") or 0) == ctx.ticket_id
                and s.get("status") not in ("done", "failed")]
        if live:
            ctx.log("step.skip", reason=f"training droid agent_session {live[-1]['id']} is {live[-1]['status']}")
            return
        if not v.accepted_episode_ids:
            ctx.log("step.skip", reason="no accepted episodes")
            return
        await ctx.event("training.droid_starting", {"iteration": v.iteration})
        # The first droid turn takes ~30-90 s: run it off the flywheel's await so other tickets keep moving.
        asyncio.get_running_loop().create_task(self._launch(ctx, v.iteration), name=f"tdm34-droid-t{ctx.ticket_id}")

    async def _launch(self, ctx: Any, iteration: int) -> None:
        try:
            from rdf_factory import FactoryClient

            async with FactoryClient() as fc:
                stub = "RDF_MODAL_STUB=1: Modal is stubbed, nothing runs on a GPU." if os.environ.get("RDF_MODAL_STUB") == "1" else ""
                r = await launch_training_droid(fc, ctx.ticket_id, iteration, stub_note=stub)
            ctx.log("step.training_droid", agent_session_id=r.agent_session_id, status=r.status, credits=r.credits)
            if r.status in ("failed",) and ctx.view.training_run is None:
                raise RuntimeError(f"training droid turn 1 failed: {r.error}")
        except Exception as e:  # noqa: BLE001 - never stall the loop on the droid: fall back to TDM-22's queue
            log.exception("training droid could not start; falling back to QueueTrainingStep")
            await ctx.event("training.droid_fallback", {"error": f"{type(e).__name__}: {e}"[:500]})
            from rdf_orchestrator.flywheel import QueueTrainingStep

            await QueueTrainingStep().run(ctx)


def main(argv: list[str] | None = None) -> int:
    """``python -m rdf_factory.droids.training <ticket_id> <iteration>``: start a droid by hand."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python -m rdf_factory.droids.training <ticket_id> <iteration>", file=sys.stderr)
        return 2

    async def go() -> int:
        from rdf_factory import FactoryClient

        async with FactoryClient() as fc:
            r = await launch_training_droid(fc, int(argv[0]), int(argv[1]))
        print(json.dumps(r.to_dict(), indent=2, default=str))
        return 0 if r.ok else 1

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
