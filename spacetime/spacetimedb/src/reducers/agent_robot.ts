// TDM-13: agent_session (pause/resume signals for long-running agents) and
// robot_command (the command bus to the Jetson robot-client).
//
// Agent sessions. The resume bridge (orchestrator, TDM-27) subscribes to
// `SELECT * FROM agent_session WHERE status = 'waiting_for'`.
//   agent_wait_for   -> status waiting_for, waiting_for_json = predicate,
//                       resume_payload_json = state for an end-and-restart.
//   agent_resume     -> the bridge's atomic wake-up claim: only one caller can
//                       move a session out of waiting_for/paused. status becomes
//                       resuming. Two protocols:
//                         native:  new_factory_session_id = "" (or the current
//                                  id); the bridge messages the same session.
//                         restart: new_factory_session_id = the new Factory
//                                  session, appended to factory_session_ids_json
//                                  under the same agent_session.id; it is seeded
//                                  with resume_payload_json (kept on the row).
//                       A bridge that claims first and starts the new session
//                       afterwards reports the new id with upsert_agent_session,
//                       which also appends it.
//   agent_step       -> current_step/last_update; a step from a starting or
//                       resuming session flips it to running.
//   upsert_agent_session -> create (id = 0) or update. It cannot enter
//                       waiting_for or resuming (use the reducers above), and
//                       leaving waiting_for clears waiting_for_json.
//
// Robot commands.
//   Authorized callers: orchestrator / factory service identities (owner always),
//   or the contributor holding the robot's currently claimed, unexpired task
//   (teleop/takeover types only). Any service identity may issue soft_estop.
//   Every command gets a per-robot `seq` (1, 2, 3...). The Jetson acks
//   (issued -> acked) before it acts, then completes or fails it.
//   soft_estop is never blocked: no busy/latch/one-job checks, and inserting it
//   preempts every other open (issued/acked) command on that robot.
//   Other commands are refused while an e-stop is open or robot.status = estop,
//   and a "job" command is refused while another job is open on the robot.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole, requireService, serviceRole } from '../auth';
import { logEvent } from '../event_log'; // TDM-15
import {
  AGENT_ROLES,
  AGENT_STATUSES,
  ROBOT_COMMAND_TYPES,
  oneOf,
} from '../enums';

// ------------------------------------------------------------------ helpers

const TDM13_AGENT_WRITERS = ['orchestrator', 'factory'] as const;
const TDM13_TERMINAL_AGENT = ['done', 'failed'];
const TDM13_PREDICATE_OPS = ['==', '!=', '>', '>=', '<', '<='];

// Long-running commands: at most one open per robot.
const TDM13_JOB_TYPES = [
  'run_baseline',
  'run_eval',
  'start_teleop',
  'reset_request',
  'home',
  'load_checkpoint',
];
// What the current task claimant may send (web/phone teleop and takeover).
const TDM13_CLAIMANT_TYPES = [
  'start_teleop',
  'stop_teleop',
  'pause_for_takeover',
  'resume_policy',
  'handback',
  'soft_estop',
];
const TDM13_OPEN_COMMAND = ['issued', 'acked'];

function tdm13Log(ctx: RdfCtx, actor: string, type: string, payload: unknown) {
  logEvent(ctx, actor, type, JSON.stringify(payload, (_k, v) => (typeof v === 'bigint' ? v.toString() : v))); // TDM-15: sole event_log writer
}

function tdm13ParseJson(field: string, text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    throw new SenderError(`${field} is not valid JSON`);
  }
}

function tdm13IsObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

// A predicate is {table, ...} (e.g. {table:'dataset', ticket_id, field,
// op:'>=', value:5}) or {all:[...]} / {any:[...]} of predicates.
function tdm13ValidatePredicate(p: unknown, depth = 0): void {
  if (depth > 4) throw new SenderError('predicate nested too deeply');
  if (!tdm13IsObject(p)) throw new SenderError('predicate_json must be a JSON object');
  for (const key of ['all', 'any']) {
    if (key in p) {
      const list = p[key];
      if (!Array.isArray(list) || list.length === 0) {
        throw new SenderError(`predicate.${key} must be a non-empty array`);
      }
      for (const sub of list) tdm13ValidatePredicate(sub, depth + 1);
      return;
    }
  }
  if (typeof p.table !== 'string' || p.table === '') {
    throw new SenderError('predicate needs a "table" (or "all"/"any")');
  }
  if (p.op !== undefined && !TDM13_PREDICATE_OPS.includes(String(p.op))) {
    throw new SenderError(`predicate.op must be one of ${TDM13_PREDICATE_OPS.join(' ')}`);
  }
}

function tdm13FactoryIds(json: string): string[] {
  try {
    const v = JSON.parse(json);
    return Array.isArray(v) ? v.map(String) : [];
  } catch {
    return [];
  }
}

function tdm13GetSession(ctx: RdfCtx, id: bigint) {
  const row = ctx.db.agentSession.id.find(id);
  if (!row) throw new SenderError(`unknown agent_session ${id}`);
  return row;
}

// The contributor (by ctx.sender) holding a claimed, unexpired task on robotId.
function tdm13ClaimantTask(ctx: RdfCtx, robotId: bigint) {
  const me = [...ctx.db.contributor.iter()].find((c) => c.identity?.isEqual(ctx.sender) ?? false);
  if (!me) return undefined;
  const now = ctx.timestamp.microsSinceUnixEpoch;
  const task = [...ctx.db.task.assignee_id.filter(me.id)].find(
    (tk) =>
      tk.status === 'claimed' &&
      tk.robot_id === robotId &&
      (tk.lease_expires_at === undefined || tk.lease_expires_at.microsSinceUnixEpoch > now)
  );
  return task ? { contributor: me, task } : undefined;
}

// Ack/complete/fail: the robot's bound client identity, else a robot_client role.
function tdm13RequireRobotClient(ctx: RdfCtx, robotId: bigint): string {
  const role = requireService(ctx);
  if (role === 'owner') return role;
  const bound = ctx.db.robot.id.find(robotId)?.client_identity;
  if (bound !== undefined) {
    if (!bound.isEqual(ctx.sender)) {
      throw new SenderError(`unauthorized: not robot ${robotId}'s client identity`);
    }
    return role;
  }
  if (role !== 'robot_client') throw new SenderError('unauthorized: robot_client role required');
  return role;
}

function tdm13GetCommand(ctx: RdfCtx, id: bigint) {
  const row = ctx.db.robotCommand.id.find(id);
  if (!row) throw new SenderError(`unknown robot_command ${id}`);
  return row;
}

// ------------------------------------------------------------------ agent_session

export const upsert_agent_session = spacetimedb.reducer(
  {
    id: t.u64(), // 0 = create
    role: t.string(),
    factory_session_id: t.string(), // "" = none / unchanged
    status: t.string(),
    current_step: t.string(),
    name: t.string(), // "" = role (create) / unchanged (update)
    ticket_id: t.u64(), // 0 = none (create) / unchanged (update)
  },
  (ctx, { id, role, factory_session_id, status, current_step, name, ticket_id }) => {
    const actor = requireRole(ctx, ...TDM13_AGENT_WRITERS);
    oneOf('role', role, AGENT_ROLES);
    oneOf('status', status, AGENT_STATUSES);

    if (id === 0n) {
      if (status === 'waiting_for' || status === 'resuming') {
        throw new SenderError(`a new session cannot start as ${status}`);
      }
      const row = ctx.db.agentSession.insert({
        id: 0n,
        ticket_id,
        role,
        name: name || role,
        factory_session_id: factory_session_id || undefined,
        factory_session_ids_json: JSON.stringify(factory_session_id ? [factory_session_id] : []),
        status,
        current_step,
        waiting_for_json: undefined,
        resume_payload_json: undefined,
        created_at: ctx.timestamp,
        last_update: ctx.timestamp,
      });
      tdm13Log(ctx, actor, 'agent.session_created', {
        session_id: row.id,
        role,
        name: row.name,
        ticket_id,
        status,
        factory_session_id: row.factory_session_id ?? null,
      });
      return;
    }

    const old = tdm13GetSession(ctx, id);
    if (old.role !== role) throw new SenderError(`session ${id} has role ${old.role}, not ${role}`);
    if (status === 'waiting_for' && old.status !== 'waiting_for') {
      throw new SenderError('use agent_wait_for to enter waiting_for (it stores the predicate)');
    }
    if (status === 'resuming' && old.status !== 'resuming') {
      throw new SenderError('use agent_resume to enter resuming (it is the atomic wake-up claim)');
    }
    const ids = tdm13FactoryIds(old.factory_session_ids_json);
    if (factory_session_id && !ids.includes(factory_session_id)) ids.push(factory_session_id);
    const row = ctx.db.agentSession.id.update({
      ...old,
      name: name || old.name,
      ticket_id: ticket_id === 0n ? old.ticket_id : ticket_id,
      factory_session_id: factory_session_id || old.factory_session_id,
      factory_session_ids_json: JSON.stringify(ids),
      status,
      current_step,
      waiting_for_json: status === 'waiting_for' ? old.waiting_for_json : undefined,
      last_update: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'agent.session_updated', {
      session_id: id,
      role,
      from: old.status,
      to: status,
      factory_session_id: row.factory_session_id ?? null,
      new_factory_session: factory_session_id !== '' && factory_session_id !== old.factory_session_id,
      current_step,
    });
  }
);

export const agent_wait_for = spacetimedb.reducer(
  { session_id: t.u64(), predicate_json: t.string(), resume_payload_json: t.string() },
  (ctx, { session_id, predicate_json, resume_payload_json }) => {
    const actor = requireRole(ctx, ...TDM13_AGENT_WRITERS);
    const old = tdm13GetSession(ctx, session_id);
    if (TDM13_TERMINAL_AGENT.includes(old.status)) {
      throw new SenderError(`session ${session_id} is ${old.status}`);
    }
    const predicate = tdm13ParseJson('predicate_json', predicate_json);
    tdm13ValidatePredicate(predicate);
    const payload =
      resume_payload_json === '' ? undefined : tdm13ParseJson('resume_payload_json', resume_payload_json);
    ctx.db.agentSession.id.update({
      ...old,
      status: 'waiting_for',
      waiting_for_json: JSON.stringify(predicate),
      resume_payload_json: payload === undefined ? undefined : JSON.stringify(payload),
      last_update: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'agent.waiting_for', {
      session_id,
      role: old.role,
      ticket_id: old.ticket_id,
      from: old.status,
      predicate,
      factory_session_id: old.factory_session_id ?? null,
      has_resume_payload: payload !== undefined,
    });
  }
);

export const agent_resume = spacetimedb.reducer(
  { session_id: t.u64(), new_factory_session_id: t.string(), reason: t.string() },
  (ctx, { session_id, new_factory_session_id, reason }) => {
    const actor = requireRole(ctx, ...TDM13_AGENT_WRITERS);
    const old = tdm13GetSession(ctx, session_id);
    if (old.status !== 'waiting_for' && old.status !== 'paused') {
      throw new SenderError(`session ${session_id} is ${old.status}, not waiting_for/paused`);
    }
    const restart = new_factory_session_id !== '' && new_factory_session_id !== old.factory_session_id;
    const ids = tdm13FactoryIds(old.factory_session_ids_json);
    if (restart && !ids.includes(new_factory_session_id)) ids.push(new_factory_session_id);
    ctx.db.agentSession.id.update({
      ...old,
      status: 'resuming',
      factory_session_id: restart ? new_factory_session_id : old.factory_session_id,
      factory_session_ids_json: JSON.stringify(ids),
      waiting_for_json: undefined,
      last_update: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'agent.resumed', {
      session_id,
      role: old.role,
      ticket_id: old.ticket_id,
      from: old.status,
      mode: restart ? 'restart' : 'native',
      factory_session_id: restart ? new_factory_session_id : (old.factory_session_id ?? null),
      previous_factory_session_id: old.factory_session_id ?? null,
      reason,
      satisfied_predicate: old.waiting_for_json ? JSON.parse(old.waiting_for_json) : null,
    });
  }
);

export const agent_step = spacetimedb.reducer(
  { session_id: t.u64(), step_text: t.string() },
  (ctx, { session_id, step_text }) => {
    const actor = requireRole(ctx, ...TDM13_AGENT_WRITERS);
    const old = tdm13GetSession(ctx, session_id);
    if (TDM13_TERMINAL_AGENT.includes(old.status)) {
      throw new SenderError(`session ${session_id} is ${old.status}`);
    }
    const status = old.status === 'starting' || old.status === 'resuming' ? 'running' : old.status;
    ctx.db.agentSession.id.update({
      ...old,
      status,
      current_step: step_text,
      last_update: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'agent.step', {
      session_id,
      role: old.role,
      ticket_id: old.ticket_id,
      step: step_text,
      ...(status !== old.status ? { from: old.status, to: status } : {}),
    });
  }
);

// ------------------------------------------------------------------ robot_command

export const issue_robot_command = spacetimedb.reducer(
  { robot_id: t.u64(), type: t.string(), payload_json: t.string(), ticket_id: t.u64() },
  (ctx, { robot_id, type, payload_json, ticket_id }) => {
    oneOf('type', type, ROBOT_COMMAND_TYPES);
    const estop = type === 'soft_estop';

    // Who is asking.
    let issuedBy: string;
    let ticketId = ticket_id;
    const role = serviceRole(ctx);
    const claim = role === undefined ? tdm13ClaimantTask(ctx, robot_id) : undefined;
    if (role !== undefined && (estop || role === 'owner' || role === 'orchestrator' || role === 'factory')) {
      issuedBy = role;
    } else if (claim) {
      if (!TDM13_CLAIMANT_TYPES.includes(type)) {
        throw new SenderError(`unauthorized: task claimants may only send ${TDM13_CLAIMANT_TYPES.join('|')}`);
      }
      issuedBy = `contributor:${claim.contributor.id}:task:${claim.task.id}`;
      ticketId = claim.task.ticket_id;
    } else if (role !== undefined) {
      throw new SenderError(`unauthorized: role ${role} cannot issue ${type}`);
    } else {
      throw new SenderError('unauthorized: not a service identity or the claimant of a task on this robot');
    }

    const payload = payload_json === '' ? {} : tdm13ParseJson('payload_json', payload_json);
    const robot = ctx.db.robot.id.find(robot_id);
    const existing = [...ctx.db.robotCommand.robot_id.filter(robot_id)];
    const open = existing.filter((c) => TDM13_OPEN_COMMAND.includes(c.status));

    if (!estop) {
      // E-stop has priority: nothing else goes out until it is closed and the
      // robot-client reports it is no longer in estop.
      if (!robot) throw new SenderError(`unknown robot ${robot_id}`);
      if (robot.status === 'estop') throw new SenderError(`robot ${robot_id} is in estop`);
      const openEstop = open.find((c) => c.type === 'soft_estop');
      if (openEstop) throw new SenderError(`robot ${robot_id} has an open soft_estop (#${openEstop.id})`);
      if (TDM13_JOB_TYPES.includes(type)) {
        const job = open.find((c) => TDM13_JOB_TYPES.includes(c.type));
        if (job) {
          throw new SenderError(`robot ${robot_id} is busy with ${job.type} #${job.id} (${job.status})`);
        }
      }
    }

    const seq = existing.reduce((m, c) => (c.seq > m ? c.seq : m), 0n) + 1n;
    const row = ctx.db.robotCommand.insert({
      id: 0n,
      robot_id,
      ticket_id: ticketId,
      type,
      payload_json: JSON.stringify(payload),
      status: 'issued',
      issued_by: issuedBy,
      result_json: undefined,
      issued_at: ctx.timestamp,
      acked_at: undefined,
      finished_at: undefined,
      seq,
    });

    const preempted: { id: bigint; seq: bigint; type: string; status: string }[] = [];
    if (estop) {
      for (const c of open) {
        if (c.type === 'soft_estop') continue;
        ctx.db.robotCommand.id.update({
          ...c,
          status: 'preempted',
          result_json: JSON.stringify({ preempted_by: row.id.toString(), estop_seq: seq.toString() }),
          finished_at: ctx.timestamp,
        });
        preempted.push({ id: c.id, seq: c.seq, type: c.type, status: c.status });
      }
    }

    tdm13Log(ctx, issuedBy, estop ? 'robot.soft_estop' : 'robot.command_issued', {
      command_id: row.id,
      robot_id,
      ticket_id: ticketId,
      seq,
      type,
      payload,
      ...(estop ? { preempted, robot_known: robot !== undefined } : {}),
    });
  }
);

export const ack_robot_command = spacetimedb.reducer({ id: t.u64() }, (ctx, { id }) => {
  const cmd = tdm13GetCommand(ctx, id);
  const actor = tdm13RequireRobotClient(ctx, cmd.robot_id);
  if (cmd.status === 'acked') return; // idempotent retry
  if (cmd.status !== 'issued') throw new SenderError(`command ${id} is ${cmd.status}; do not run it`);
  ctx.db.robotCommand.id.update({ ...cmd, status: 'acked', acked_at: ctx.timestamp });
  tdm13Log(ctx, actor, 'robot.command_acked', {
    command_id: id,
    robot_id: cmd.robot_id,
    seq: cmd.seq,
    type: cmd.type,
  });
});

export const complete_robot_command = spacetimedb.reducer(
  { id: t.u64(), result_json: t.string() },
  (ctx, { id, result_json }) => {
    const cmd = tdm13GetCommand(ctx, id);
    const actor = tdm13RequireRobotClient(ctx, cmd.robot_id);
    if (cmd.status !== 'acked') {
      throw new SenderError(`command ${id} is ${cmd.status}; only an acked command can complete`);
    }
    const result = result_json === '' ? {} : tdm13ParseJson('result_json', result_json);
    ctx.db.robotCommand.id.update({
      ...cmd,
      status: 'done',
      result_json: JSON.stringify(result),
      finished_at: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'robot.command_done', {
      command_id: id,
      robot_id: cmd.robot_id,
      seq: cmd.seq,
      type: cmd.type,
      result,
    });
  }
);

export const fail_robot_command = spacetimedb.reducer(
  { id: t.u64(), error: t.string() },
  (ctx, { id, error }) => {
    const cmd = tdm13GetCommand(ctx, id);
    const actor = tdm13RequireRobotClient(ctx, cmd.robot_id);
    if (!TDM13_OPEN_COMMAND.includes(cmd.status)) {
      throw new SenderError(`command ${id} is already ${cmd.status}`);
    }
    ctx.db.robotCommand.id.update({
      ...cmd,
      status: 'failed',
      result_json: JSON.stringify({ error }),
      finished_at: ctx.timestamp,
    });
    tdm13Log(ctx, actor, 'robot.command_failed', {
      command_id: id,
      robot_id: cmd.robot_id,
      seq: cmd.seq,
      type: cmd.type,
      error,
    });
  }
);
