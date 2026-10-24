// TDM-107: per-ticket robot mode. One reducer: move a ticket to another robot row.
//
// A ticket runs on the MuJoCo **sim** robot (robot.camera_config_json.sim = true) or on the
// **real** YAM. Every row the flywheel creates for a ticket (task, eval_run, episode) copies
// ticket.robot_id, so switching the ticket's robot is the whole routing change: new tasks,
// evals and episodes land on the new robot, and the rows already written keep the robot they
// ran on (that is how sim data stays labelled sim forever).
//
// The orchestrator (services/orchestrator/src/rdf_orchestrator/robot_mode.py) is the only
// caller: its POST /tickets/{id}/robot_mode is operator-only (the web proxy requires an operator
// login) and checks the target robot's mode before calling. The mode logged here is read from
// the robot row itself, never from the caller.
//
// Refused while the ticket is terminal, while an eval_run of the ticket is queued/running, or
// while a job command (run_eval, run_baseline, start_teleop, ...) is open on the ticket.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15

const TDM107_TERMINAL = ['shipped', 'failed', 'cancelled'];
const TDM107_OPEN_COMMAND = ['issued', 'acked']; // = TDM13_OPEN_COMMAND
const TDM107_ESTOP = 'soft_estop';

function tdm107Mode(cameraConfigJson: string): 'sim' | 'real' {
  try {
    const cfg = JSON.parse(cameraConfigJson || '{}') as Record<string, unknown> | null;
    return cfg && typeof cfg === 'object' && cfg.sim === true ? 'sim' : 'real';
  } catch {
    return 'real';
  }
}

function tdm107Robot(ctx: RdfCtx, id: bigint) {
  const r = ctx.db.robot.id.find(id);
  if (!r) throw new SenderError(`unknown robot ${id}`);
  return r;
}

export const set_ticket_robot = spacetimedb.reducer(
  { ticket_id: t.u64(), robot_id: t.u64(), by: t.string(), reason: t.string() },
  (ctx, { ticket_id, robot_id, by, reason }) => {
    const role = requireRole(ctx, 'orchestrator');
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`unknown ticket ${ticket_id}`);
    if (TDM107_TERMINAL.includes(ticket.status)) {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; its robot is frozen`);
    }
    const to = tdm107Robot(ctx, robot_id);
    const from = ctx.db.robot.id.find(ticket.robot_id);
    if (ticket.robot_id === robot_id) throw new SenderError(`ticket ${ticket_id} already runs on robot ${robot_id}`);
    const live = [...ctx.db.evalRun.ticket_id.filter(ticket_id)].find((r) => r.status === 'queued' || r.status === 'running');
    if (live) {
      throw new SenderError(`ticket ${ticket_id} has eval run ${live.id} ${live.status}; switch robots after it finishes or is aborted`);
    }
    const job = [...ctx.db.robotCommand.robot_id.filter(ticket.robot_id)].find(
      (c) => c.ticket_id === ticket_id && c.type !== TDM107_ESTOP && TDM107_OPEN_COMMAND.includes(c.status)
    );
    if (job) {
      throw new SenderError(`ticket ${ticket_id} has ${job.type} #${job.id} ${job.status} on robot ${ticket.robot_id}`);
    }
    ctx.db.ticket.id.update({ ...ticket, robot_id, updated_at: ctx.timestamp });
    logEvent(ctx, role, 'ticket.robot_mode_changed', {
      ticket_id,
      from_robot_id: ticket.robot_id,
      from_robot_name: from?.name ?? null,
      from_mode: from ? tdm107Mode(from.camera_config_json) : null,
      to_robot_id: robot_id,
      to_robot_name: to.name,
      to_mode: tdm107Mode(to.camera_config_json),
      to_robot_status: to.status,
      status: ticket.status,
      iteration: ticket.iteration,
      by: by.trim().slice(0, 100) || role,
      reason: reason.trim().slice(0, 500),
    });
  }
);
