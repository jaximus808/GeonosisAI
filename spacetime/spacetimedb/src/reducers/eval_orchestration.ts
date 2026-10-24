// TDM-67: eval orchestration. One reducer: abort a running eval_run.
//
// The orchestrator (services/orchestrator/src/rdf_orchestrator/evaluate.py)
// calls this when an eval cannot finish: the robot went offline or stopped
// heartbeating, its run_eval command failed or was preempted (soft_estop), it
// completed the command with fewer trial rows than planned, or the eval timed
// out. The trial rows stay (evidence); the run just stops accepting trials and
// can never be finalized, so a fresh eval_run is started for the re-run.
//
//   status 'cancelled'  the robot was lost (offline / stale / estop / timeout):
//                       not the policy's fault, so the flywheel does not count
//                       it against MAX_STEP_FAILURES.
//   status 'failed'     the robot reported a failure (fail_robot_command):
//                       counts as a failed step, the flywheel retries it.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15

const TDM67_ABORT_STATUSES = ['failed', 'cancelled'];

export const abort_eval_run = spacetimedb.reducer(
  { eval_run_id: t.u64(), status: t.string(), reason: t.string() },
  (ctx, { eval_run_id, status, reason }) => {
    const role = requireRole(ctx, 'orchestrator');
    if (!TDM67_ABORT_STATUSES.includes(status)) {
      throw new SenderError(`status must be one of ${TDM67_ABORT_STATUSES.join('|')}, got ${JSON.stringify(status)}`);
    }
    const why = reason.trim();
    if (!why) throw new SenderError('reason must not be empty');
    const run = ctx.db.evalRun.id.find(eval_run_id);
    if (!run) throw new SenderError(`unknown eval run ${eval_run_id}`);
    if (run.status !== 'running' && run.status !== 'queued') {
      throw new SenderError(`eval run ${eval_run_id} is ${run.status}; only a queued or running eval can be aborted`);
    }
    ctx.db.evalRun.id.update({ ...run, status, finished_at: ctx.timestamp });
    logEvent(ctx, role, 'eval.aborted', {
      eval_run_id,
      ticket_id: run.ticket_id,
      robot_id: run.robot_id,
      kind: run.kind,
      checkpoint_uri: run.checkpoint_uri,
      from: run.status,
      to: status,
      completed: run.completed,
      successes: run.successes,
      trials: run.trials,
      reason: why.slice(0, 1000),
    });
  }
);
