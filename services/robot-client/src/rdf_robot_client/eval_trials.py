"""TDM-51: eval trial runner. N policy trials with operator-confirmed outcomes and reset pauses.

Registers the ``run_eval`` job handler, and ``run_baseline`` when the payload names an eval run
(without one, ``run_baseline`` stays TDM-50's single policy episode, which records no trials).

TDM-67 contract: the orchestrator starts the eval_run and issues
``run_eval {eval_run_id, checkpoint_uri, trials, timeout_s, kind, solo_mode}``. Per trial:

  1. home (arm to the start pose, ``handlers.home``)
  2. start the TDM-48 recorder (kind ``baseline`` / ``eval``, with checkpoint_uri)
  3. ``rollout.run_episode(ctx, prompt, timeout_s=...)``. The prompt is payload.prompt, else
     run_settings.hyperparams.prompt, else ticket.task_description (same as the dispatcher)
  4. decide the outcome (see below)
  5. stop the recorder, upload, and wait for ``create_episode`` (the episode_id goes on the trial)
  6. ``record_eval_trial(eval_run_id, index, success, duration_ms, episode_id, note)``
  7. if more trials are left: ``ctx.wait_for_reset()``. The robot holds in ``waiting_reset``
     (status paused) until the dispatcher's ``resume_policy``. It never resets the scene itself.

Outcomes are confirmed by the operator. Nothing is marked a success without a human saying so:
  * The operator gives the verdict on the robot terminal (``pass`` / ``fail <note>``, see main.py)
    or with a ``trial_outcome`` robot command {success: bool, note?, eval_run_id?, index?}
    (the dashboard / robot page; delivered through ``loop._ctl_trial_outcome``). A verdict given
    while the policy is still running ends the rollout there (the arm holds). If eval_run_id or
    index is given and doesn't match the open trial, the command is refused, so a late click can
    never land on the next trial. A trial that is already recorded is never changed.
  * When the rollout ends without a verdict, the runner waits ``outcome_timeout_s`` (default 30 s,
    ``RDF_EVAL_OUTCOME_TIMEOUT_S``, payload ``outcome_timeout_s``; 0 = wait forever). Still nothing
    -> recorded as a FAILURE with the note "outcome not confirmed" (flagged in result_json).
  * Optional suggestion: ``RDF_EVAL_SUCCESS_CHECK=module:function`` (``fn(step, ArmState) -> bool``)
    can end the rollout early and is shown to the operator as a suggestion. It never decides.
  * Failure notes: the operator's text, else ``timeout: rollout hit the N s limit`` when the
    rollout ran out of time.

Failure handling:
  * soft e-stop / ``stop`` mid-trial: the rollout raises Cancelled, the recording is closed
    locally (kept on disk, not uploaded, never an eval outcome), NO trial row is written for the
    interrupted trial, and Cancelled goes up to the loop (the command stays ``preempted`` after an
    e-stop). The orchestrator aborts the eval run. Nothing restarts on its own.
  * policy error mid-trial (the TDM-50 executor gave up after its retries): a FAILED trial with
    the note ``policy_error: ...``. No operator wait, since the policy did not finish.
  * the policy never produced a single action (endpoint down, cold-start failure, no cameras):
    that is infrastructure, not the policy. No trial row is written and the command fails, so the
    orchestrator aborts the run as ``failed`` and retries. Two policy-error trials in a row fail
    the command the same way.
  * episode upload not finished within ``RDF_EVAL_UPLOAD_WAIT_S`` (default 120 s): the trial is
    recorded with episode_id 0 and the note says the episode key (the upload keeps retrying).

Every trial lands in ``ctx.result["outcomes"]`` as it is recorded, so a command that ends early
still reports exactly what was recorded.

TDM-69 collection rollouts (``run_baseline`` {purpose: "collect_recovery", eval_run_id: 0, trials: 1,
interventions: {enabled, trial_timeout_s, no_progress_s, verdict_wait_s}}, issued by the
orchestrator's InterventionManager while a recovery_takeover plan collects): the same per-trial
steps (home, record, policy episode) but NO eval_trial rows and no reset wait, and the TDM-69
``FailureDetector`` runs on every policy tick (``rdf_robot_client.interventions``):
  * detector fires (timeout; success_check when ``RDF_EVAL_SUCCESS_CHECK`` is set; no_progress needs
    a progress signal, which this runner does not have): the rollout ends, the arm holds (the
    recorder keeps rolling), ``intervention.failure_detected`` is appended, and the runner waits
    ``verdict_wait_s`` for ``pause_for_takeover`` {ref, task_id} or ``resume_policy`` {end_trial, ref}.
  * operator "Failing" (the orchestrator sends ``pause_for_takeover`` while the policy runs): the
    control loop holds in paused_for_takeover, exactly like a detector pause.
  * the recording is a ``recovery_takeover`` episode of the ticket from the start; on the pause it
    gets the routed task id + its claimant (so the TDM-58 control on the claimant's socket adopts it
    and stamps the takeover at the pause time). The runner waits for the handback (DB ``handback`` /
    ``resume_policy``, or ``{t:"handback", success}`` on /control, for which the TDM-52 hub appends
    ``intervention.handed_back``). Then the policy finishes the trial and the episode is uploaded
    with task_id + the task's assignee as contributor and ``success`` = the claimant's "Did you
    recover the task?" answer at handback (none = unknown: the reviewer rejects, nobody is paid).
  * ``resume_policy`` end_trial (nobody claimed / declined), no verdict in time, or no intervention
    at all: the trial ends there and the recording stays on the robot (not training data).
Eval and baseline runs (anything that is not purpose collect_recovery) never run the detector, even
if the payload asks for interventions, so eval numbers stay clean.
"""
from __future__ import annotations

import importlib
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import handlers as _default_handlers
from . import rollout as _rollout  # noqa: F401 - TDM-50 handlers first, so the ones below win
from .commands import Cancelled
from .interventions import COLLECT_PURPOSE, FAILURE_EVENT, FailureDetector, TrialIntervention
from .jobs import JobContext, job_handler
from .policy_client import BASE_CHECKPOINT
from .recorder import META_FILENAME, _write_json, recorder_for
from .rollout import RolloutResult, get_policy, resolve_prompt, rollout_episode_job, run_episode
from .state import RobotState

INBOX_SERVICE = "eval_outcomes"
DEFAULT_OUTCOME_TIMEOUT_S = 30.0
DEFAULT_TRIAL_TIMEOUT_S = 60.0
DEFAULT_UPLOAD_WAIT_S = 120.0
DEFAULT_MAX_PAUSE_S = 900.0  # collection: longest wait for a handback once paused (RDF_INTERVENTION_MAX_PAUSE_S)
# collection: how long the policy finishes the trial after a handback (RDF_INTERVENTION_POST_HANDBACK_S or
# payload interventions.post_handback_s). Short on purpose: the reviewer rejects episodes over 120 s.
DEFAULT_POST_HANDBACK_S = 10.0
MAX_CONSECUTIVE_POLICY_ERRORS = 2
NOT_CONFIRMED = "outcome not confirmed"


def _log(msg: str, **fields: Any) -> None:
    line = {"ts": time.strftime("%H:%M:%S"), "svc": "eval-trials", "msg": msg, **fields}
    print(json.dumps(line, default=str), file=sys.stderr, flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------- operator outcome inbox


@dataclass
class Verdict:
    success: bool
    note: str
    source: str  # "terminal" | "command #<id> by <issuer>" | ...
    at: float = field(default_factory=time.time)


class OutcomeInbox:
    """One open trial at a time. Verdicts come from the terminal or a ``trial_outcome`` command."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.eval_run_id: int | None = None
        self.index: int | None = None
        self.label = ""
        self.verdict: Verdict | None = None
        self._got = threading.Event()
        self._on_verdict: Callable[[Verdict], None] | None = None

    @property
    def is_open(self) -> bool:
        return self.index is not None

    def open(self, eval_run_id: int, index: int, label: str = "",
             on_verdict: Callable[[Verdict], None] | None = None) -> None:
        with self._lock:
            self.eval_run_id, self.index, self.label = eval_run_id, index, label
            self.verdict = None
            self._got.clear()
            self._on_verdict = on_verdict

    def close(self) -> None:
        with self._lock:
            self.eval_run_id = self.index = None
            self._on_verdict = None

    def submit(self, success: Any, note: str = "", source: str = "", *, eval_run_id: Any = None,
               index: Any = None) -> dict[str, Any]:
        """Deliver an operator verdict. Raises ValueError if no trial is open or it doesn't match."""
        if not isinstance(success, bool):
            raise ValueError("trial_outcome needs success: true|false")
        with self._lock:
            if self.index is None:
                raise ValueError("no eval trial is waiting for an outcome right now")
            if eval_run_id not in (None, "", 0) and int(eval_run_id) != self.eval_run_id:
                raise ValueError(f"outcome is for eval run {eval_run_id}, but the open trial is in eval run {self.eval_run_id}")
            if index is not None and index != "" and int(index) != self.index:
                raise ValueError(f"outcome is for trial {index}, but the open trial is {self.index} "
                                 "(recorded trials are never changed)")
            if self.verdict is not None:
                raise ValueError(f"trial {self.index} already has a verdict ({'pass' if self.verdict.success else 'fail'} "
                                 f"from {self.verdict.source})")
            v = Verdict(success, str(note or "").strip()[:500], source or "unknown")
            self.verdict = v
            cb = self._on_verdict
            out = {"eval_run_id": self.eval_run_id, "index": self.index, "success": success, "note": v.note}
            self._got.set()
        if cb is not None:
            try:
                cb(v)
            except Exception:  # noqa: BLE001 - the verdict itself is stored
                pass
        return out

    def wait(self, timeout: float | None, check: Callable[[], None]) -> Verdict | None:
        """Wait for a verdict (``timeout`` None/<=0 = forever), calling ``check`` every 50 ms."""
        end = None if not timeout or timeout <= 0 else time.monotonic() + timeout
        while not self._got.is_set():
            check()
            if end is not None and time.monotonic() >= end:
                break
            self._got.wait(0.05)
        return self.verdict


def inbox_for(client: Any) -> OutcomeInbox:
    services = client.services
    box = services.get(INBOX_SERVICE)
    if box is None:
        box = services[INBOX_SERVICE] = OutcomeInbox()
    return box


def deliver_outcome(client: Any, payload: dict[str, Any], source: str) -> dict[str, Any]:
    """``trial_outcome`` control command -> the open trial (loop.py). ValueError fails the command."""
    box = client.services.get(INBOX_SERVICE)
    if box is None:
        raise ValueError("no eval trial is waiting for an outcome right now")
    out = box.submit(payload.get("success"), str(payload.get("note") or ""), source,
                     eval_run_id=payload.get("eval_run_id"), index=payload.get("index"))
    return {"trial_outcome": out}


TERMINAL_PASS = ("pass", "success", "ok")
TERMINAL_FAIL = ("fail", "failure")


def terminal_outcome(client: Any, line: str) -> bool:
    """main.py stdin hook: ``pass [note]`` / ``fail [note]``. Returns False if not an outcome line."""
    words = line.strip().split(None, 1)
    if not words or words[0].lower() not in TERMINAL_PASS + TERMINAL_FAIL:
        return False
    note = words[1] if len(words) > 1 else ""
    try:
        out = inbox_for(client).submit(words[0].lower() in TERMINAL_PASS, note, "terminal")
        print(f"recorded verdict for trial {out['index']}: {'PASS' if out['success'] else 'FAIL'}"
              + (f" ({out['note']})" if out["note"] else ""), file=sys.stderr, flush=True)
    except ValueError as e:
        print(f"outcome not taken: {e}", file=sys.stderr, flush=True)
    return True


# --------------------------------------------------------------------------- helpers


SuccessCheck = Callable[[int, Any], bool]


def load_success_check(spec: str | None = None) -> SuccessCheck | None:
    """``RDF_EVAL_SUCCESS_CHECK=module:function`` (suggestion only, never the outcome)."""
    spec = (spec if spec is not None else os.environ.get("RDF_EVAL_SUCCESS_CHECK", "")).strip()
    if not spec:
        return None
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn or "success_check")


def policy_prompt(ctx: JobContext) -> str:
    """payload.prompt -> run_settings.hyperparams.prompt -> ticket.task_description."""
    p = str(ctx.payload.get("prompt") or "").strip()
    if p:
        return p
    tid = int(ctx.command.ticket_id or 0)
    if tid:
        try:
            rows = ctx.sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")
            hp = json.loads(rows[0].get("hyperparams_json") or "{}") if rows else {}
            if isinstance(hp, dict) and str(hp.get("prompt") or "").strip():
                return str(hp["prompt"]).strip()
        except Exception:  # noqa: BLE001 - fall back to the ticket description
            pass
    return resolve_prompt(ctx, None)


def _is_sim(ctx: JobContext) -> bool:
    d = ctx.arm.describe()
    return bool(d.get("sim")) or "sim" in str(d.get("adapter", "")) or "sim" in str(getattr(ctx.arm, "embodiment", ""))


def _keep_local(ep: Any, why: str) -> None:
    """Mark a local recording as never-to-upload (the uploader's boot rescan queues every
    ``status == "recorded"`` episode)."""
    if ep.meta.get("status") == "recorded":
        ep.meta.update(status="kept_local", kept_reason=why)
        _write_json(ep.dir / META_FILENAME, ep.meta)


def _go_home(ctx: JobContext) -> None:
    if ctx.state is not RobotState.IDLE:
        ctx.enter(RobotState.IDLE, "eval trial: to the start pose")
    _default_handlers.home(ctx)  # homing -> idle; raises if aborted or not arrived


@dataclass
class TrialOutcome:
    index: int
    success: bool
    note: str
    duration_ms: int
    episode_id: int
    outcome_source: str  # operator source | "unconfirmed" | "policy_error"
    confirmed: bool
    ended: str
    suggested: bool | None = None
    episode_key: str | None = None
    flags: list[str] = field(default_factory=list)
    intervention: dict[str, Any] | None = None  # collection rollouts: TrialIntervention.summary()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class InfraFailure(RuntimeError):
    """The policy never ran (endpoint / cameras): not an eval outcome. Fails the command."""


# --------------------------------------------------------------------------- the runner


class EvalTrialRunner:
    def __init__(self, ctx: JobContext) -> None:
        p = ctx.payload
        self.ctx = ctx
        self.collect = str(p.get("purpose") or "") == COLLECT_PURPOSE  # TDM-69 collection rollout
        self.eval_run_id = int(p.get("eval_run_id") or 0)
        if not self.eval_run_id and not self.collect:
            raise ValueError(f"{ctx.command.type} needs payload.eval_run_id (TDM-67 starts the eval run)")
        self.trials = int(p.get("trials") or (1 if self.collect else 0))
        if self.trials <= 0:
            raise ValueError("payload.trials must be > 0")
        default_kind = "baseline" if ctx.command.type == "run_baseline" else "eval"
        self.run_kind = str(p.get("kind") or default_kind)  # TDM-67: baseline | candidate; TDM-69: collect_recovery
        self.episode_kind = "baseline" if self.run_kind in ("baseline", COLLECT_PURPOSE) or self.collect else "eval"
        uri = p.get("checkpoint_uri") or (BASE_CHECKPOINT if self.episode_kind == "baseline" else None)
        if not uri:
            raise ValueError("payload.checkpoint_uri is required for a candidate eval")
        self.checkpoint_uri = str(uri)
        self.timeout_s = float(p.get("timeout_s") or DEFAULT_TRIAL_TIMEOUT_S)
        self.max_steps = int(p["max_steps"]) if p.get("max_steps") else None
        ot = p.get("outcome_timeout_s")
        self.outcome_timeout_s = float(ot) if ot is not None else _env_float("RDF_EVAL_OUTCOME_TIMEOUT_S",
                                                                             DEFAULT_OUTCOME_TIMEOUT_S)
        self.upload_wait_s = _env_float("RDF_EVAL_UPLOAD_WAIT_S", DEFAULT_UPLOAD_WAIT_S)
        self.mode = p.get("mode")
        self.sim = _is_sim(ctx)
        self.success_check = load_success_check()
        self.inbox = inbox_for(ctx.client)
        self.outcomes: list[TrialOutcome] = []
        self.policy_errors_in_row = 0
        iv = p.get("interventions")
        self.interventions_requested = bool(isinstance(iv, dict) and iv.get("enabled"))
        # the detector runs in collection rollouts only (eval / baseline numbers stay clean)
        self.iv_cfg: dict[str, Any] | None = iv if self.collect and self.interventions_requested else None
        self.max_pause_s = _env_float("RDF_INTERVENTION_MAX_PAUSE_S", DEFAULT_MAX_PAUSE_S)
        post = (self.iv_cfg or {}).get("post_handback_s")
        self.post_handback_s = float(post) if post else _env_float("RDF_INTERVENTION_POST_HANDBACK_S",
                                                                   DEFAULT_POST_HANDBACK_S)

    # ---- reducers
    def _recorded_indices(self) -> set[int]:
        try:
            rows = self.ctx.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {self.eval_run_id}")
        except Exception:  # noqa: BLE001
            return set()
        return {int(r.get("trial_index") or 0) for r in rows}

    def _record(self, o: TrialOutcome) -> None:
        last: Exception | None = None
        for attempt in range(4):
            try:
                self.ctx.call_reducer("record_eval_trial", self.eval_run_id, o.index, o.success, o.duration_ms,
                                      o.episode_id, o.note)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                if o.index in self._recorded_indices():  # our earlier attempt landed after all
                    return
                if "not running" in str(e) or "out of range" in str(e):
                    break  # deterministic refusal: the eval was aborted / finalized
                time.sleep(min(4.0, 0.5 * 2 ** attempt))
        raise RuntimeError(f"record_eval_trial({self.eval_run_id}, {o.index}) refused: {last}")

    # ---- one trial
    def _activate(self) -> dict[str, Any]:
        pc = get_policy(self.ctx)
        try:
            return pc.activate(self.checkpoint_uri)
        except Exception as e:  # noqa: BLE001 - e.g. the spike endpoint can't serve a candidate
            raise InfraFailure(f"policy activate {self.checkpoint_uri!r} failed: {e}") from e

    def _await_upload(self, rec: Any, ep: Any) -> tuple[int, str]:
        up = getattr(rec, "uploader", None)
        if up is None:
            return 0, f"episode {ep.key} kept on the robot (no uploader configured)"
        end = time.monotonic() + self.upload_wait_s
        while True:
            st = up.status(ep.dir)
            if st.get("state") == "done" and st.get("episode_id"):
                return int(st["episode_id"]), ""
            if st.get("state") == "failed":
                return 0, f"episode {ep.key} upload failed: {str(st.get('error'))[:200]}"
            if time.monotonic() >= end:
                return 0, f"episode {ep.key} upload still pending after {self.upload_wait_s:.0f}s"
            self.ctx.sleep(0.2)

    def run_trial(self, index: int, prompt: str) -> TrialOutcome:
        ctx = self.ctx
        label = f"eval {self.eval_run_id} trial {index + 1}/{self.trials}"
        rec = None
        try:
            ctx.finish_requested.clear()
            _go_home(ctx)  # 1. start pose
            rec = recorder_for(ctx)  # 2. recorder
            ep_key = None
            if rec is not None:
                ep_key = rec.start(ctx.command.ticket_id, self.episode_kind, checkpoint_uri=self.checkpoint_uri,
                                   note=f"{label}: {prompt[:80]}")

            def on_verdict(v: Verdict) -> None:
                ctx.finish_requested.set()  # a verdict during the rollout ends it; the arm holds
                _log("operator verdict", trial=index, success=v.success, note=v.note, source=v.source)

            self.inbox.open(self.eval_run_id, index, label, on_verdict)
            ctx.event("eval.trial_started", eval_run_id=self.eval_run_id, index=index, trials=self.trials,
                      checkpoint_uri=self.checkpoint_uri, kind=self.run_kind, episode_key=ep_key, sim=self.sim)
            # 3. the policy episode
            t_start = time.time()
            res = run_episode(ctx, prompt, max_steps=self.max_steps, success_check=self.success_check,
                              timeout_s=self.timeout_s, checkpoint_uri=self.checkpoint_uri, mode=self.mode, warm=True)
            duration_ms = int(round(res.seconds * 1000))
            if res.ended == "policy_error" and int(res.executor.get("chunks_used") or 0) == 0:
                raise InfraFailure(f"trial {index}: the policy never produced an action "
                                   f"({res.executor.get('aborted') or res.flags}); not recorded as an eval outcome")
            ep = rec.stop(success=None, note=f"{label} ended={res.ended}", upload=False) \
                if rec is not None and rec.recording else None

            # 4. the outcome
            suggested = res.success if self.success_check is not None else None
            intervened = any(tr.dst is RobotState.PAUSED_FOR_TAKEOVER and tr.at >= t_start
                             for tr in list(ctx.sm.history))
            if res.ended == "policy_error":
                success, confirmed, source = False, True, "policy_error"
                note = f"policy_error: {str(res.executor.get('aborted') or 'executor gave up')[:300]}"
            elif intervened:  # TDM-69 contract: a trial a human had to take over is a policy failure
                success, confirmed, source = False, True, "intervened"
                v = self.inbox.verdict
                note = "[intervened] the policy was paused for a takeover" + (f"; operator: {v.note}" if v and v.note else "")
            else:
                verdict = self.inbox.verdict
                if verdict is None:
                    self._prompt_operator(index, res, suggested)
                    verdict = self.inbox.wait(self.outcome_timeout_s, ctx.check)
                timeout_note = (f"timeout: rollout hit the {self.timeout_s:g}s limit"
                                if res.ended in ("max_seconds", "max_steps") else "")
                if verdict is None:
                    success, confirmed, source = False, False, "unconfirmed"
                    note = NOT_CONFIRMED + (f" within {self.outcome_timeout_s:.0f}s" if self.outcome_timeout_s > 0 else "")
                    if suggested is not None:
                        note += f"; success check suggested {'success' if suggested else 'failure'}"
                    if timeout_note:
                        note += f"; {timeout_note}"
                else:
                    success, confirmed, source = verdict.success, True, verdict.source
                    note = verdict.note or ("" if success else (timeout_note or "operator: failed"))
            self.inbox.close()

            # 5. the episode (uploaded only now, with the confirmed outcome on it)
            episode_id, ep_note = 0, ("no recorder (cameras off): no episode" if rec is None else "")
            if ep is not None:
                if ep.meta.get("status") == "recorded":
                    ep.meta["success"] = success if confirmed else None
                    ep.meta["eval"] = {"eval_run_id": self.eval_run_id, "index": index, "success": success,
                                       "confirmed": confirmed, "outcome_source": source}
                    _write_json(ep.dir / META_FILENAME, ep.meta)
                    if getattr(rec, "uploader", None) is not None:
                        rec.uploader.enqueue(ep.dir)
                    episode_id, ep_note = self._await_upload(rec, ep)
                else:
                    ep_note = f"episode {ep.key} not usable: {ep.meta.get('status')} {ep.meta.get('problems')}"
            if ep_note:
                note = f"{note}; {ep_note}" if note else ep_note
            if self.sim and not note.startswith("[SIM]"):
                note = f"[SIM] {note}".strip()
            return TrialOutcome(index=index, success=success, note=note[:1000], duration_ms=duration_ms,
                                episode_id=episode_id, outcome_source=source, confirmed=confirmed, ended=res.ended,
                                suggested=suggested, episode_key=ep_key, flags=list(res.flags))
        except Cancelled as e:  # soft e-stop / stop / preempted: no trial row, nothing restarts
            ctx.event("eval.trial_interrupted", eval_run_id=self.eval_run_id, index=index, reason=e.reason,
                      detail=e.detail, note="no trial row written for this trial; nothing restarts on its own")
            _log("trial interrupted; not recorded", index=index, reason=e.reason)
            raise
        finally:
            self.inbox.close()
            ctx.finish_requested.clear()
            if rec is not None and rec.recording:  # interrupted / error: keep the files locally, no outcome
                try:
                    ep = rec.stop(success=None, note=f"{label} interrupted; not an eval outcome", upload=False)
                    _keep_local(ep, "eval trial interrupted")  # else the boot rescan would upload it
                    _log("recording kept on disk (not uploaded)", dir=str(ep.dir))
                except Exception:  # noqa: BLE001
                    pass

    def _prompt_operator(self, index: int, res: RolloutResult, suggested: bool | None) -> None:
        wait = f"{self.outcome_timeout_s:.0f} s" if self.outcome_timeout_s > 0 else "no time limit"
        self.ctx.event("eval.trial_awaiting_outcome", eval_run_id=self.eval_run_id, index=index, ended=res.ended,
                       suggested=suggested, timeout_s=self.outcome_timeout_s, trials=self.trials,
                       how="trial_outcome command {success, note?, eval_run_id, index} or 'pass'/'fail <note>' on the robot terminal")
        hint = "" if suggested is None else f" (success check suggests {'PASS' if suggested else 'FAIL'})"
        print(f"\n>>> TRIAL {index + 1}/{self.trials} of eval {self.eval_run_id} ended ({res.ended}){hint}.\n"
              f">>> Type 'pass' or 'fail <note>' + Enter ({wait}; no answer = recorded as FAILURE: {NOT_CONFIRMED})\n",
              file=sys.stderr, flush=True)

    # ---- TDM-69 collection trial (purpose collect_recovery): failure detector + takeover
    def _watch_interventions(self, tiv: TrialIntervention, rec: Any, stop: threading.Event) -> None:
        """Side thread for one collection trial: feeds the job's control signals and the robot state
        into ``tiv`` (the job thread is parked in the control loop while paused) and applies the
        side effects at the right moment."""
        ctx = self.ctx
        busy_states = (RobotState.PAUSED_FOR_TAKEOVER, RobotState.TELEOP)
        while not stop.is_set():
            try:
                c = ctx.signals.get(timeout=0.05)
            except queue.Empty:
                c = None
            try:
                if c is not None:
                    what = tiv.on_signal(c.type, dict(c.payload or {}), int(c.id or 0))
                    if what:
                        _log("intervention signal", command=c.type, command_id=c.id, meaning=what, ref=tiv.ref)
                    if what == "end_trial":
                        ctx.finish_requested.set()  # paused inside the rollout: end it once back in rollout
                    # a /control handback: the TDM-52 hub already appended intervention.handed_back
                what = tiv.on_state(ctx.sm.state in busy_states)
                if what == "paused":
                    self._retag_recording(rec, tiv, self._assignee(tiv.task_id))
                    ctx.event("intervention.robot_paused", ref=tiv.ref, task_id=tiv.task_id, purpose=COLLECT_PURPOSE,
                              detector=tiv.failure.detector if tiv.failure else "operator", sim=self.sim)
                    _log("paused for takeover", ref=tiv.ref, task_id=tiv.task_id)
                elif what == "handed_back":
                    self._mark_handback(rec, tiv)
                    _log("handed back; the policy finishes the trial", ref=tiv.ref, via=tiv.handback_via)
                elif what == "ended":
                    _log("trial ended by the orchestrator while paused", ref=tiv.ref, reason=(tiv.end_trial or {}).get("reason"))
            except Exception as e:  # noqa: BLE001 - the watcher must keep running
                _log("intervention watcher error", error=repr(e)[:300])

    @staticmethod
    def _retag_recording(rec: Any, tiv: TrialIntervention, contributor_id: int = 0) -> None:
        """The running recovery_takeover recording gets the routed task (TDM-58's control on the
        claimant's socket adopts it by kind + task_id and stamps the takeover at the pause)."""
        s = getattr(rec, "_s", None) if rec is not None and rec.recording else None
        if s is not None:
            s.info.update(kind="recovery_takeover", task_id=tiv.task_id,
                          contributor_id=int(contributor_id or s.info.get("contributor_id") or 0))

    @staticmethod
    def _mark_handback(rec: Any, tiv: TrialIntervention) -> None:
        """Fallback marks when nobody stamped them (DB handback, no /control session): takeover at the
        pause, handback now. The TDM-52/58 socket path stamps its own and is left alone."""
        s = getattr(rec, "_s", None) if rec is not None and rec.recording else None
        if s is None:
            return
        if getattr(s, "takeover_ns", None) is None and tiv.paused_at_ns:
            rec.mark_takeover(tiv.paused_at_ns)
        if getattr(s, "handback_ns", None) is None:
            rec.mark_handback(tiv.handback_at_ns)

    def _assignee(self, task_id: int) -> int:
        if not task_id:
            return 0
        try:
            rows = self.ctx.sql(f"SELECT * FROM task WHERE id = {int(task_id)}")
            return int((rows[0] if rows else {}).get("assignee_id") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def run_collect_trial(self, index: int, prompt: str) -> TrialOutcome:
        ctx = self.ctx
        label = f"collection rollout #{ctx.command.id} trial {index + 1}/{self.trials}"
        det = FailureDetector.from_payload(self.iv_cfg)
        rid = int(getattr(ctx.client, "robot_id", None) or ctx.command.robot_id or 0)
        tiv = TrialIntervention(rid, ctx.command.id, index)
        rec = None
        stop_watch = threading.Event()
        watcher: threading.Thread | None = None
        base_check = self.success_check
        suggested: dict[str, bool | None] = {"v": None}

        def check(step: int, st: Any) -> bool:
            """Per-tick: the optional success suggestion, then the TDM-69 detector (fires once). After a
            handback the policy only finishes the trial for ``post_handback_s``."""
            if tiv.handback_at_ns is not None and (time.monotonic_ns() - tiv.handback_at_ns) / 1e9 >= self.post_handback_s:
                ctx.finish_requested.set()
                return False
            if tiv.intervened or tiv.failure is not None:
                return False
            if base_check is not None and base_check(step, st):
                suggested["v"] = True
                return True
            if det is not None:
                f = det.tick(time.monotonic(), None)
                if f is not None:
                    tiv.failure = f
                    ctx.finish_requested.set()  # stop stepping the policy; the arm holds
            return False

        try:
            ctx.finish_requested.clear()
            while True:  # signals from before this trial don't count
                try:
                    ctx.signals.get_nowait()
                except queue.Empty:
                    break
            _go_home(ctx)
            rec = recorder_for(ctx)
            ep_key = None
            if rec is not None:  # the task and its contributor are filled in when the rollout is paused
                ep_key = rec.start(ctx.command.ticket_id, "recovery_takeover", 0, 0, checkpoint_uri=self.checkpoint_uri,
                                   note=f"{label}: {prompt[:80]}")
            watcher = threading.Thread(target=self._watch_interventions, args=(tiv, rec, stop_watch),
                                       name="rdf-iv-watch", daemon=True)
            watcher.start()
            ctx.event("eval.trial_started", eval_run_id=0, index=index, trials=self.trials, purpose=COLLECT_PURPOSE,
                      checkpoint_uri=self.checkpoint_uri, kind=self.run_kind, episode_key=ep_key, sim=self.sim,
                      detector=det is not None)
            if det is not None:
                det.start(time.monotonic())
            # the detector's timeout must win over the episode's own limit
            ep_timeout = self.timeout_s if det is None else max(self.timeout_s, det.trial_timeout_s) + 2.0
            res = run_episode(ctx, prompt, max_steps=self.max_steps, success_check=check, timeout_s=ep_timeout,
                              checkpoint_uri=self.checkpoint_uri, mode=self.mode, warm=True)
            if res.ended == "policy_error" and int(res.executor.get("chunks_used") or 0) == 0:
                raise InfraFailure(f"collection trial {index}: the policy never produced an action "
                                   f"({res.executor.get('aborted') or res.flags})")
            seconds = res.seconds
            if (det is not None and base_check is not None and tiv.failure is None and not tiv.intervened
                    and res.ended not in ("success", "policy_error")):
                tiv.failure = det.success_check(False, "the success check never passed", time.monotonic())

            # ---- detector fired: report, hold, wait for the orchestrator's verdict
            no_verdict = False
            if tiv.failure is not None and not tiv.intervened:
                f = tiv.failure
                ctx.client.event(FAILURE_EVENT, **f.payload(rid, ctx.command.ticket_id, ctx.command.id, index, 0,
                                                            purpose=COLLECT_PURPOSE, sim=self.sim))
                _log("failure detected; holding for a verdict", detector=f.detector, reason=f.reason, ref=tiv.own_ref)
                ctx.arm.hold()
                got = tiv.wait(det.verdict_wait_s if det else 0.0, ctx.check,
                               lambda: tiv.paused or tiv.end_trial is not None)
                no_verdict = not got
            # ---- paused: wait for the handback (or the orchestrator ending the trial)
            if tiv.paused and tiv.handback_at_ns is None:
                if not tiv.wait(self.max_pause_s, ctx.check, lambda: tiv.handback_at_ns is not None):
                    tiv.end_trial = {"reason": f"no handback within {self.max_pause_s:.0f}s (robot side)"}
            # ---- handed back after a detector pause: the policy finishes the trial
            if tiv.handed_back and res.ended == "finish_requested" and tiv.failure is not None:
                ctx.finish_requested.clear()
                left = max(1.0, self.post_handback_s - (time.monotonic_ns() - tiv.handback_at_ns) / 1e9)
                res = run_episode(ctx, prompt, max_steps=self.max_steps,
                                  success_check=(lambda s, st: bool(base_check(s, st))) if base_check else None,
                                  timeout_s=left, checkpoint_uri=self.checkpoint_uri, mode=self.mode, warm=False)
                seconds += res.seconds
                if base_check is not None:
                    suggested["v"] = bool(res.success)
            ep = rec.stop(success=None, note=f"{label} ended={res.ended} intervention={tiv.phase}", upload=False) \
                if rec is not None and rec.recording else None
            stop_watch.set()

            # ---- outcome + episode
            summary = tiv.summary()
            duration_ms = int(round(seconds * 1000))
            episode_id, success, confirmed, source = 0, False, False, "not_intervened"
            if tiv.handed_back:
                # success = the claimant's "Did you recover the task?" at handback (docs/decisions.md);
                # no answer = unknown: the reviewer rejects it and nobody is paid
                answer = tiv.handback_success
                success, confirmed = bool(answer), answer is not None
                source = "handback" if confirmed else "unconfirmed"
                note = (f"[intervened] {summary['detector']}: {summary['reason'] or ''}; recovered by task "
                        f"#{tiv.task_id}; claimant at handback: "
                        + ({True: "recovered", False: "NOT recovered"}.get(answer, "no answer (success unknown)")))
                if ep is not None and ep.meta.get("status") == "recorded":
                    contributor = self._assignee(tiv.task_id)
                    ep.meta.update(kind="recovery_takeover", task_id=tiv.task_id, contributor_id=contributor,
                                   success=answer,
                                   intervention={**summary, "command_id": ctx.command.id, "trial_index": index})
                    _write_json(ep.dir / META_FILENAME, ep.meta)
                    if getattr(rec, "uploader", None) is not None:
                        rec.uploader.enqueue(ep.dir)
                    episode_id, ep_note = self._await_upload(rec, ep)
                else:
                    ep_note = ("no recorder (cameras off): no recovery episode" if ep is None else
                               f"episode {ep.key} not usable: {ep.meta.get('status')} {ep.meta.get('problems')}")
                if ep_note:
                    note = f"{note}; {ep_note}"
            else:
                if tiv.end_trial is not None:
                    source = "end_trial"
                    note = f"failure: {summary['detector']}: {summary['reason'] or ''} (orchestrator: {summary['end_trial']})"
                elif no_verdict:
                    source = "no_verdict"
                    note = f"failure: {tiv.failure.detector}: {tiv.failure.reason} (no verdict within the wait)"
                elif tiv.failure is not None:
                    source = "failure"
                    note = f"failure: {tiv.failure.detector}: {tiv.failure.reason}"
                else:
                    success = bool(suggested["v"]) if suggested["v"] is not None else False
                    note = f"no intervention (rollout ended {res.ended})"
                if ep is not None:
                    _keep_local(ep, "collection trial without a handed-back takeover: not a recovery episode")
                    note += "; recording kept on the robot (not a recovery episode)"
            if self.sim and not note.startswith("[SIM]"):
                note = f"[SIM] {note}"
            out = TrialOutcome(index=index, success=success, note=note[:1000], duration_ms=duration_ms,
                               episode_id=episode_id, outcome_source=source, confirmed=confirmed, ended=res.ended,
                               suggested=suggested["v"], episode_key=ep_key, flags=list(res.flags), intervention=summary)
            ctx.event("collect.trial_ended", index=index, purpose=COLLECT_PURPOSE, **{k: v for k, v in out.to_dict().items()
                                                                                       if k != "index"})
            return out
        except Cancelled as e:
            ctx.event("eval.trial_interrupted", eval_run_id=0, index=index, purpose=COLLECT_PURPOSE, reason=e.reason,
                      detail=e.detail, intervention=tiv.summary(), note="collection trial interrupted; nothing restarts")
            raise
        finally:
            stop_watch.set()
            if watcher is not None:
                watcher.join(1.0)
            self.inbox.close()
            ctx.finish_requested.clear()
            if rec is not None and rec.recording:
                try:
                    ep = rec.stop(success=None, note=f"{label} interrupted; not a recovery episode", upload=False)
                    _keep_local(ep, "collection trial interrupted")
                    _log("recording kept on disk (not uploaded)", dir=str(ep.dir))
                except Exception:  # noqa: BLE001
                    pass

    # ---- the job
    def run(self) -> dict[str, Any]:
        ctx = self.ctx
        prompt = policy_prompt(ctx)
        activate = self._activate()
        if self.collect:
            return self._run_collect(prompt, activate)
        done = self._recorded_indices()
        todo = [i for i in range(self.trials) if i not in done]
        base = {"eval_run_id": self.eval_run_id, "kind": self.run_kind, "checkpoint_uri": self.checkpoint_uri,
                "trials": self.trials, "prompt": prompt, "sim": self.sim, "outcome_timeout_s": self.outcome_timeout_s,
                "already_recorded": sorted(done), "activate": activate}
        if self.interventions_requested:  # TDM-69: the detector runs in collection rollouts only
            base["interventions_note"] = ("payload asked for interventions; eval/baseline runs never run the failure "
                                          "detector (eval numbers stay clean); an operator pause_for_takeover still "
                                          "records the trial as an [intervened] failure")
        ctx.result.update(base, outcomes=[])
        ctx.event("eval.trials_started", **{k: v for k, v in base.items() if k != "activate"})
        _log("eval started", **{k: v for k, v in base.items() if k != "activate"})
        for n, i in enumerate(todo):
            o = self.run_trial(i, prompt)
            self._record(o)
            self.outcomes.append(o)
            ctx.result["outcomes"] = [x.to_dict() for x in self.outcomes]
            ctx.event("eval.trial_recorded", eval_run_id=self.eval_run_id, **o.to_dict())
            _log("trial recorded", index=i, success=o.success, confirmed=o.confirmed, note=o.note, episode_id=o.episode_id)
            self.policy_errors_in_row = self.policy_errors_in_row + 1 if o.outcome_source == "policy_error" else 0
            if self.policy_errors_in_row >= MAX_CONSECUTIVE_POLICY_ERRORS:
                raise InfraFailure(f"{self.policy_errors_in_row} trials in a row ended in policy_error; "
                                   "stopping the eval (check the policy endpoint)")
            if n < len(todo) - 1:
                ctx.wait_for_reset(reason=f"eval {self.eval_run_id}: reset the scene for trial {i + 2}/{self.trials}")
        succ = sum(o.success for o in self.outcomes)
        out = {**base, "recorded": len(self.outcomes), "successes": succ,
               "unconfirmed": [o.index for o in self.outcomes if not o.confirmed],
               "outcomes": [o.to_dict() for o in self.outcomes],
               "note": ("[SIM] sim arm / synthetic cameras: NOT a real-robot eval. " if self.sim else "")
                       + "outcomes are operator-confirmed; unconfirmed trials are recorded as failures"}
        _log("eval trials done", eval_run_id=self.eval_run_id, recorded=len(self.outcomes), successes=succ)
        return out

    def _run_collect(self, prompt: str, activate: dict[str, Any]) -> dict[str, Any]:
        """TDM-69 collection rollout: no eval_trial rows; the orchestrator asks for the reset afterwards."""
        ctx = self.ctx
        base = {"purpose": COLLECT_PURPOSE, "eval_run_id": 0, "kind": self.run_kind,
                "data_plan_id": int(ctx.payload.get("data_plan_id") or 0), "checkpoint_uri": self.checkpoint_uri,
                "trials": self.trials, "prompt": prompt, "sim": self.sim, "activate": activate,
                "interventions": "on" if self.iv_cfg else "off (payload has no interventions.enabled)"}
        ctx.result.update(base, outcomes=[])
        ctx.event("eval.trials_started", **{k: v for k, v in base.items() if k != "activate"})
        _log("collection rollout started", **{k: v for k, v in base.items() if k != "activate"})
        for i in range(self.trials):
            o = self.run_collect_trial(i, prompt)
            self.outcomes.append(o)
            ctx.result["outcomes"] = [x.to_dict() for x in self.outcomes]
            _log("collection trial ended", index=i, intervention=o.intervention, episode_id=o.episode_id, note=o.note)
            if i < self.trials - 1:
                ctx.wait_for_reset(reason=f"collection rollout {ctx.command.id}: reset the scene for trial {i + 2}")
        recovered = [o for o in self.outcomes if (o.intervention or {}).get("phase") == "handed_back"]
        return {**base, "outcomes": [o.to_dict() for o in self.outcomes],
                "intervened": sum(1 for o in self.outcomes if (o.intervention or {}).get("ref")),
                "recovery_episode_ids": [o.episode_id for o in recovered if o.episode_id],
                "note": ("[SIM] sim arm / synthetic cameras: NOT real-robot data. " if self.sim else "")
                        + "TDM-69 collection rollout: no eval_trial rows; only a handed-back takeover uploads an "
                          "episode (kind recovery_takeover)"}


# --------------------------------------------------------------------------- job handlers


@job_handler("run_eval")
def run_eval(ctx: JobContext) -> dict[str, Any]:
    return EvalTrialRunner(ctx).run()


@job_handler("run_baseline")
def run_baseline(ctx: JobContext) -> dict[str, Any]:
    """With payload.eval_run_id: the trial runner (kind baseline, base checkpoint by default). With
    purpose collect_recovery: a TDM-69 collection rollout (failure detector + takeover, no eval rows).
    Otherwise: TDM-50's single policy episode (the robot page's demo button), which records no trials."""
    if ctx.payload.get("eval_run_id") or ctx.payload.get("purpose") == COLLECT_PURPOSE:
        return EvalTrialRunner(ctx).run()
    return rollout_episode_job(ctx)


__all__ = ["EvalTrialRunner", "InfraFailure", "OutcomeInbox", "TrialOutcome", "Verdict", "deliver_outcome",
           "inbox_for", "load_success_check", "policy_prompt", "run_baseline", "run_eval", "terminal_outcome"]
