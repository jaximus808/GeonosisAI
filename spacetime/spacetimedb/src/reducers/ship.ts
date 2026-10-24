// TDM-70: ship step. One reducer: record the checkpoint a shipped ticket deploys.
//
//   mark_checkpoint_deployed(ticket_id, training_run_id, eval_run_id, note)
//
// The orchestrator's flywheel moves the ticket eval -> shipped with
// set_ticket_status (TDM-9), then its ShipStep calls this. The URI is taken
// from the training run, never from the caller, and the eval run must be a
// finished candidate eval of that same run, so ticket.deployed_checkpoint_uri
// always points at a checkpoint whose success rate is on record. The policy
// server's active checkpoint (TDM-41 POST /policy/activate) follows this
// column; the report (TDM-71) reads it.
//
// Idempotent: calling it again with the same run is a no-op (no event).
// Calling it with a different run re-points the deployment (e.g. a rollback)
// and logs the previous URI.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15

export const mark_checkpoint_deployed = spacetimedb.reducer(
  { ticket_id: t.u64(), training_run_id: t.u64(), eval_run_id: t.u64(), note: t.string() },
  (ctx, { ticket_id, training_run_id, eval_run_id, note }) => {
    const role = requireRole(ctx, 'orchestrator');
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`unknown ticket ${ticket_id}`);
    if (ticket.status !== 'shipped') {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; only a shipped ticket deploys a checkpoint`);
    }
    const run = ctx.db.trainingRun.id.find(training_run_id);
    if (!run || run.ticket_id !== ticket_id) {
      throw new SenderError(`training run ${training_run_id} is not a run of ticket ${ticket_id}`);
    }
    const uri = (run.checkpoint_uri ?? '').trim();
    if (run.status !== 'done' || uri === '') {
      throw new SenderError(`training run ${training_run_id} is ${run.status} with no checkpoint; nothing to deploy`);
    }
    const ev = ctx.db.evalRun.id.find(eval_run_id);
    if (!ev || ev.ticket_id !== ticket_id) {
      throw new SenderError(`eval run ${eval_run_id} is not an eval of ticket ${ticket_id}`);
    }
    if (ev.kind === 'baseline' || ev.training_run_id !== training_run_id || ev.status !== 'done') {
      throw new SenderError(
        `eval run ${eval_run_id} (${ev.kind}, ${ev.status}, training run ${ev.training_run_id}) is not a finished ` +
          `candidate eval of training run ${training_run_id}`
      );
    }
    const previous = ticket.deployed_checkpoint_uri ?? null;
    if (previous === uri) return; // already deployed; nothing changes
    ctx.db.ticket.id.update({ ...ticket, deployed_checkpoint_uri: uri, updated_at: ctx.timestamp });
    logEvent(ctx, role, 'ticket.checkpoint_deployed', {
      ticket_id,
      checkpoint_uri: uri,
      training_run_id,
      eval_run_id,
      iteration: ticket.iteration,
      successes: ev.successes,
      trials: ev.trials,
      ci_low: ev.ci_low,
      ci_high: ev.ci_high,
      previous_checkpoint_uri: previous,
      note: note.slice(0, 500),
    });
  }
);
