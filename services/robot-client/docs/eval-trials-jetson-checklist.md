# TDM-51 eval trials: checklist for the real 10-trial baseline [HIL] (TDM-53)

Nobody has run the trial runner on the YAM yet. It was tested on the laptop only: SimArm behind the
TDM-45 safety layer, synthetic cameras, the TDM-50 fake policy server, and the TDM-67 orchestrator
side on a throwaway local SpacetimeDB (`spikes/tdm-51-eval/prove_eval_sim.py`). Every number from
those runs is SIM. **The real baseline needs a human at the arm with the physical e-stop in reach for
the whole eval. That person is also the one who judges each trial.**

## How a trial runs (robot side, `rdf_robot_client/eval_trials.py`)
1. The arm goes home (the start pose).
2. Recording starts (kind `baseline`, or `eval` for a candidate, with the checkpoint_uri).
3. The policy runs with the ticket's prompt (`run_settings.hyperparams.prompt`, else the task
   description) for at most `timeout_s` (the orchestrator sends `EVAL_TRIAL_TIMEOUT_S`, default 120 s).
4. **You give the outcome.** On the robot terminal type `pass` or `fail <what went wrong>` + Enter.
   The dashboard can do the same with a `trial_outcome` command
   `{success, note?, eval_run_id, index}`. If you answer while the policy is still moving, the
   trial ends right there and the arm holds. Otherwise, when the time limit is reached the terminal
   prints `>>> TRIAL n/10 ... Type 'pass' or 'fail <note>'`, and you have 30 s
   (`RDF_EVAL_OUTCOME_TIMEOUT_S`; `0` means wait forever). **No answer is recorded as a FAILURE
   with the note "outcome not confirmed".** Nothing is ever marked a success without you.
5. The episode uploads, then the trial is written with `record_eval_trial` and its episode_id.
6. The robot goes to `waiting_reset` (status paused). The dispatcher posts a paid reset task. Reset
   the scene, mark the reset task done (marketplace), and the dispatcher's `resume_policy` releases
   the robot for the next trial. **The robot never continues without that resume.**

## 0. Before you start
- [ ] TDM-50 rollout checklist (`rollout-jetson-checklist.md`) passed: ~30 Hz, p95 < 250 ms, holds work.
- [ ] TDM-48 recorder checklist passed: episodes upload and `create_episode` lands (`ORCHESTRATOR_URL` set).
- [ ] The maincloud module has the `trial_outcome` command type (TDM-51). If it doesn't, the
      dashboard verdict is refused. Use the terminal (`pass` / `fail`), which always works.
- [ ] The policy endpoint is warm (`spikes/tdm-37-latency/warm.py up`, or `make policy-up` for production).
- [ ] The robot client runs in a terminal you can type into (`rdf-robot-client`). The verdict
      prompt and the `pass` / `fail` input are on that terminal.
- [ ] The scene's start position is marked on the table (tape), so every reset is the same.

## 1. A 1-trial dry run (about 3 min)
Start a short eval from the orchestrator (or the flywheel) with `eval_trials = 1` on a test ticket.
- [ ] The arm homes, the policy runs, and the prompt appears at the time limit. Type `fail dry run`.
- [ ] `eval_trial` row 0 has `success = false`, the note `dry run`, and an `episode_id`. The
      episode plays on the ticket page.
- [ ] Type `pass` once more after the trial is recorded: the terminal says
      `outcome not taken: no eval trial is waiting`. A recorded trial is never changed.

## 2. E-stop check (about 3 min, test ticket)
- [ ] Start a 3-trial eval. During trial 2, press the dashboard soft e-stop (or the physical e-stop).
- [ ] The arm stops at once. There is no row for trial 2 (event_log has `eval.trial_interrupted` from the robot client).
      run_eval is `preempted`, the eval_run is `cancelled` with reason `estop: ...`, and the robot
      stays `estop`.
- [ ] Nothing restarts. Clear it at the arm (`clear` on the terminal). A re-run needs an
      `eval.rerun_requested` event with the ticket_id (TDM-67).

## 3. The real baseline: 10 trials (TDM-53)
- [ ] The ticket is in `baseline` and `run_settings.eval_trials = 10`. The orchestrator starts the
      eval_run and sends `run_eval` with the base checkpoint.
- [ ] For every trial, judge **only** what the arm did inside the time limit. Write a short note on
      every failure (`fail missed the grasp`, `fail knocked the cup over`). The failure notes feed
      the diagnose step.
- [ ] Reset the scene the same way every time, then complete the reset task.
- [ ] At the end the orchestrator finalizes the eval_run (successes / 10 + Wilson CI). Copy the
      numbers to TDM-53 with the trial count. **Don't edit trials or rerun single trials to improve
      the number.** If a trial was spoiled by something that wasn't the policy (someone bumped the
      table, the network dropped), e-stop and re-run the whole eval.

## Behaviour to know
- **Policy error mid-trial** (the endpoint fails after the policy has started moving): the trial
  is recorded as a failure with the note `policy_error: ...`. There is no prompt for that trial.
- **The policy never produced an action** (endpoint down, cold start failed, no camera frames):
  no trial row is written and run_eval fails. The orchestrator aborts the eval as `failed` and
  retries. Two policy-error trials in a row also fail the command.
- **Takeover during an eval** (pause_for_takeover, TDM-69): the trial is recorded as a failure with
  the note `[intervened] ...`, because the policy needed a human.
- **The upload is slow:** the runner waits up to `RDF_EVAL_UPLOAD_WAIT_S` (120 s). After that the
  trial is recorded with `episode_id = 0` and the episode key in the note. The upload keeps retrying.
- **Optional success-check suggestion:** `RDF_EVAL_SUCCESS_CHECK=module:function`
  (`fn(step, ArmState) -> bool`) can end a rollout early, and it is shown in the prompt as a
  suggestion. It never decides an outcome.
