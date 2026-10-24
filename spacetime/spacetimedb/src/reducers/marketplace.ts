// TDM-10: task marketplace reducers and the atomic claim lock.
//
// The claim lock is what gives one contributor exclusive control of the arm
// (TDM-57 builds web teleop on top of it), so the rules here are strict:
//
// - Atomicity: a SpacetimeDB reducer is one serializable transaction, and
//   reducer calls on a database never interleave. claim_task reads the task,
//   checks it is claimable and writes the claim in the same transaction, so
//   two racing claims are ordered by the server. The first one wins and the
//   second sees status = claimed with a live lease and fails.
// - Lease: a claim is live only while status = 'claimed' AND now <
//   lease_expires_at. Arm-control tasks (clean_teleop, recovery_takeover) get
//   CONTROL_LEASE_MICROS (60 s), so a dead phone or tab loses control within a
//   minute (TDM-57 heartbeats every 30 s). Resets get TASK_LEASE_MICROS
//   (10 min). heartbeat_claim pushes the lease out by the same TTL.
// - Expiry is lazy, so it never depends on a sweeper running. Every reducer
//   treats a claimed row with a lapsed lease as open, and an expired lease can
//   never be revived (heartbeat_claim / complete_task fail and the holder must
//   claim again, which another contributor may win). expire_task_leases
//   writes the transition to the table and the event log so dashboards show
//   'open' again. Anyone may call it because it only applies the time rule.
// - One arm, one operator: at most one live arm-control lease per robot.
// - Errors throw SenderError, so a failed call rolls back and writes nothing.
//   Every call that succeeds appends one or more event_log rows.
//
// Consumers that gate robot control (the Jetson executor) must check all
// three: task.status === 'claimed', lease_expires_at > now, and that the
// sender is the assignee's contributor.identity. (task_id, claimed_at) names
// one claim generation. claimed_at changes on every new claim and stays the
// same across heartbeats.
import { t, SenderError } from 'spacetimedb/server';
import { Timestamp } from 'spacetimedb';
import spacetimedb, { type RdfCtx } from '../schema';
import { serviceRole, requireRole } from '../auth';
import { TASK_TYPES, DEFAULT_PRICES_CENTS, oneOf } from '../enums';

const TASK_LEASE_MICROS = 10n * 60n * 1_000_000n; // 10 min (resets)
const CONTROL_LEASE_MICROS = 60n * 1_000_000n; // 60 s (arm control)
const ARM_CONTROL_TYPES: readonly string[] = ['clean_teleop', 'recovery_takeover'];
// Services that may act for a contributor (claim on behalf, release, complete,
// cancel). The owner always passes.
const MARKET_ADMIN_ROLES = ['orchestrator'];
const MAX_RESULT_JSON = 16_384;

type TaskRow = NonNullable<ReturnType<RdfCtx['db']['task']['id']['find']>>;
type ContributorRow = NonNullable<ReturnType<RdfCtx['db']['contributor']['id']['find']>>;

// ---------------------------------------------------------------- helpers
// Not exported: index.ts re-exports this file with `export *`, and only
// registered reducers may be named exports of the module. The tdm10 prefix
// keeps the names unique in the shared module.

function tdm10LeaseMicros(type: string): bigint {
  return ARM_CONTROL_TYPES.includes(type) ? CONTROL_LEASE_MICROS : TASK_LEASE_MICROS;
}

/** The single source of truth for "who holds this task right now". */
function tdm10LeaseLive(task: TaskRow, now: Timestamp): boolean {
  return (
    task.status === 'claimed' &&
    task.lease_expires_at != null &&
    task.lease_expires_at.microsSinceUnixEpoch > now.microsSinceUnixEpoch
  );
}

function tdm10IsAdmin(ctx: RdfCtx): string | undefined {
  const role = serviceRole(ctx);
  return role === 'owner' || (role !== undefined && MARKET_ADMIN_ROLES.includes(role))
    ? role
    : undefined;
}

function tdm10IsContributorSender(ctx: RdfCtx, c: ContributorRow | null | undefined): boolean {
  return c != null && c.identity != null && c.identity.equals(ctx.sender);
}

// JSON with bigint ids as numbers (ids/cents stay far below 2^53) and
// timestamps as micros.
function tdm10Json(obj: Record<string, unknown>): string {
  return JSON.stringify(obj, (_k, v) => (typeof v === 'bigint' ? Number(v) : v));
}

function tdm10Log(ctx: RdfCtx, actor: string, type: string, payload: Record<string, unknown>): void {
  ctx.db.eventLog.insert({
    id: 0n,
    ts: ctx.timestamp,
    actor,
    type,
    payload_json: tdm10Json({ ...payload, sender: ctx.sender.toHexString() }),
  });
}

function tdm10GetTask(ctx: RdfCtx, task_id: bigint): TaskRow {
  const task = ctx.db.task.id.find(task_id);
  if (!task) throw new SenderError(`task ${task_id} not found`);
  return task;
}

/**
 * If the task is claimed but its lease has lapsed, write it back to 'open' and
 * log task.lease_expired. Returns the current row. When the calling reducer
 * then throws, this write rolls back too. That is safe because tdm10LeaseLive
 * treats the row as expired either way.
 */
function tdm10ExpireIfLapsed(ctx: RdfCtx, task: TaskRow): TaskRow {
  if (task.status !== 'claimed' || tdm10LeaseLive(task, ctx.timestamp)) return task;
  const reopened: TaskRow = {
    ...task,
    status: 'open',
    assignee_id: 0n,
    claimed_at: undefined,
    lease_expires_at: undefined,
  };
  ctx.db.task.id.update(reopened);
  tdm10Log(ctx, 'spacetime', 'task.lease_expired', {
    task_id: task.id,
    ticket_id: task.ticket_id,
    robot_id: task.robot_id,
    previous_assignee_id: task.assignee_id,
    claimed_at: task.claimed_at?.microsSinceUnixEpoch,
    lease_expires_at: task.lease_expires_at?.microsSinceUnixEpoch,
  });
  return reopened;
}

/**
 * The caller must be the current lease holder (by contributor.identity) or a
 * market admin service. Returns the actor string for the event log.
 */
function tdm10RequireHolderOrAdmin(ctx: RdfCtx, task: TaskRow, what: string): string {
  const assignee = task.assignee_id === 0n ? undefined : ctx.db.contributor.id.find(task.assignee_id);
  if (tdm10IsContributorSender(ctx, assignee)) return `contributor:${task.assignee_id}`;
  const role = tdm10IsAdmin(ctx);
  if (role !== undefined) return role;
  throw new SenderError(`unauthorized: only the holder of task ${task.id} or a service may ${what}`);
}

// ---------------------------------------------------------------- reducers

// Orchestrator (dispatcher) creates a paid task for a ticket. payload_json is a
// JSON object: { instructions?: string, data_plan_id?: number }. price_cents 0
// means the default price for the type (enums.ts DEFAULT_PRICES_CENTS). The
// new row is found by subscription (reducers return nothing).
export const create_task = spacetimedb.reducer(
  { ticket_id: t.u64(), type: t.string(), price_cents: t.u64(), payload_json: t.string() },
  (ctx, { ticket_id, type, price_cents, payload_json }) => {
    const role = requireRole(ctx, ...MARKET_ADMIN_ROLES);
    oneOf('type', type, TASK_TYPES);
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`ticket ${ticket_id} not found`);
    if (['shipped', 'failed', 'cancelled'].includes(ticket.status)) {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; no new tasks`);
    }

    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(payload_json || '{}');
    } catch {
      throw new SenderError('payload_json is not valid JSON');
    }
    if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new SenderError('payload_json must be a JSON object');
    }
    const instructions = payload.instructions ?? '';
    if (typeof instructions !== 'string') throw new SenderError('payload.instructions must be a string');
    let data_plan_id = 0n;
    if (payload.data_plan_id !== undefined && payload.data_plan_id !== null) {
      const raw = payload.data_plan_id;
      if (typeof raw !== 'number' && typeof raw !== 'string') {
        throw new SenderError('payload.data_plan_id must be an integer');
      }
      try {
        data_plan_id = BigInt(raw);
      } catch {
        throw new SenderError('payload.data_plan_id must be an integer');
      }
      if (data_plan_id < 0n) throw new SenderError('payload.data_plan_id must be >= 0');
      if (data_plan_id !== 0n) {
        const plan = ctx.db.dataPlan.id.find(data_plan_id);
        if (!plan || plan.ticket_id !== ticket_id) {
          throw new SenderError(`data_plan ${data_plan_id} not found on ticket ${ticket_id}`);
        }
      }
    }
    const price =
      price_cents === 0n ? DEFAULT_PRICES_CENTS[type as keyof typeof DEFAULT_PRICES_CENTS] : price_cents;

    const row = ctx.db.task.insert({
      id: 0n,
      ticket_id,
      robot_id: ticket.robot_id,
      data_plan_id,
      type,
      status: 'open',
      instructions,
      price_cents: price,
      assignee_id: 0n,
      claimed_at: undefined,
      lease_expires_at: undefined,
      completed_at: undefined,
      created_at: ctx.timestamp,
    });
    tdm10Log(ctx, role, 'task.created', {
      task_id: row.id,
      ticket_id,
      robot_id: ticket.robot_id,
      data_plan_id,
      type,
      price_cents: price,
      payload,
    });
  }
);

// The lock. Succeeds only if the task is open, or claimed with a lapsed lease.
// The caller must be the contributor (contributor.identity == ctx.sender) or a
// market admin service claiming on the contributor's behalf. If the holder
// calls again while the lease is live, the lease is refreshed (page reload).
// Any other claim on a live lease fails.
export const claim_task = spacetimedb.reducer(
  { task_id: t.u64(), contributor_id: t.u64() },
  (ctx, { task_id, contributor_id }) => {
    const contributor = ctx.db.contributor.id.find(contributor_id);
    if (!contributor) throw new SenderError(`contributor ${contributor_id} not found`);
    let actor: string;
    if (tdm10IsContributorSender(ctx, contributor)) {
      actor = `contributor:${contributor_id}`;
    } else {
      const role = tdm10IsAdmin(ctx);
      if (role === undefined) {
        throw new SenderError(`unauthorized: caller is not contributor ${contributor_id}`);
      }
      actor = role;
    }
    if (contributor.kind !== 'contributor') {
      throw new SenderError(`contributor ${contributor_id} is a ${contributor.kind}; cannot claim tasks`);
    }

    const now = ctx.timestamp;
    let task = tdm10GetTask(ctx, task_id);
    const ttl = tdm10LeaseMicros(task.type);
    const lease_expires_at = new Timestamp(now.microsSinceUnixEpoch + ttl);

    // Holder re-claiming its own live lease: refresh, keep claim generation.
    if (tdm10LeaseLive(task, now) && task.assignee_id === contributor_id) {
      ctx.db.task.id.update({ ...task, lease_expires_at });
      tdm10Log(ctx, actor, 'task.claim_refreshed', {
        task_id,
        contributor_id,
        robot_id: task.robot_id,
        claimed_at: task.claimed_at?.microsSinceUnixEpoch,
        lease_expires_at: lease_expires_at.microsSinceUnixEpoch,
      });
      return;
    }

    task = tdm10ExpireIfLapsed(ctx, task);
    if (task.status !== 'open') {
      if (task.status === 'claimed') {
        const holder = ctx.db.contributor.id.find(task.assignee_id);
        throw new SenderError(
          `task ${task_id} is already claimed by ${holder?.name ?? `contributor ${task.assignee_id}`} ` +
            `(contributor ${task.assignee_id})`
        );
      }
      throw new SenderError(`task ${task_id} is ${task.status}, not open`);
    }

    if (ARM_CONTROL_TYPES.includes(task.type)) {
      const robot = ctx.db.robot.id.find(task.robot_id);
      if (robot && robot.status === 'estop') {
        throw new SenderError(`robot ${task.robot_id} is in estop; control tasks cannot be claimed`);
      }
      // One arm, one operator: refuse if another arm-control task on this robot holds a live lease.
      for (const other of ctx.db.task.robot_id.filter(task.robot_id)) {
        if (other.id !== task.id && ARM_CONTROL_TYPES.includes(other.type) && tdm10LeaseLive(other, now)) {
          throw new SenderError(
            `robot ${task.robot_id} is already under control via task ${other.id} ` +
              `(contributor ${other.assignee_id})`
          );
        }
      }
    }

    ctx.db.task.id.update({
      ...task,
      status: 'claimed',
      assignee_id: contributor_id,
      claimed_at: now,
      lease_expires_at,
    });
    tdm10Log(ctx, actor, 'task.claimed', {
      task_id,
      contributor_id,
      ticket_id: task.ticket_id,
      robot_id: task.robot_id,
      type: task.type,
      claimed_at: now.microsSinceUnixEpoch,
      lease_expires_at: lease_expires_at.microsSinceUnixEpoch,
      lease_ttl_ms: ttl / 1000n,
    });
  }
);

// Extends a live lease by the type's TTL. A lapsed lease cannot be revived: the
// holder must claim_task again, and someone else may win that claim.
export const heartbeat_claim = spacetimedb.reducer({ task_id: t.u64() }, (ctx, { task_id }) => {
  const task = tdm10GetTask(ctx, task_id);
  if (task.status !== 'claimed') throw new SenderError(`task ${task_id} is ${task.status}, not claimed`);
  const actor = tdm10RequireHolderOrAdmin(ctx, task, 'heartbeat it');
  if (!tdm10LeaseLive(task, ctx.timestamp)) {
    throw new SenderError(`lease on task ${task_id} has expired; claim it again`);
  }
  const lease_expires_at = new Timestamp(ctx.timestamp.microsSinceUnixEpoch + tdm10LeaseMicros(task.type));
  ctx.db.task.id.update({ ...task, lease_expires_at });
  tdm10Log(ctx, actor, 'task.heartbeat', {
    task_id,
    contributor_id: task.assignee_id,
    robot_id: task.robot_id,
    claimed_at: task.claimed_at?.microsSinceUnixEpoch,
    lease_expires_at: lease_expires_at.microsSinceUnixEpoch,
  });
});

// The holder (or a market admin) gives the task back to the market.
export const release_task = spacetimedb.reducer({ task_id: t.u64() }, (ctx, { task_id }) => {
  let task = tdm10GetTask(ctx, task_id);
  if (task.status !== 'claimed') throw new SenderError(`task ${task_id} is ${task.status}, not claimed`);
  const actor = tdm10RequireHolderOrAdmin(ctx, task, 'release it');
  task = tdm10ExpireIfLapsed(ctx, task);
  if (task.status !== 'claimed') return; // lease had already lapsed; now open and logged
  ctx.db.task.id.update({
    ...task,
    status: 'open',
    assignee_id: 0n,
    claimed_at: undefined,
    lease_expires_at: undefined,
  });
  tdm10Log(ctx, actor, 'task.released', {
    task_id,
    previous_assignee_id: task.assignee_id,
    robot_id: task.robot_id,
    claimed_at: task.claimed_at?.microsSinceUnixEpoch,
  });
});

// The holder (live lease required) or a market admin marks the task done. This
// ends control of the arm (lease cleared). assignee_id and claimed_at stay for
// the record. Review and payment of any episode are on the episode (TDM-11).
export const complete_task = spacetimedb.reducer(
  { task_id: t.u64(), result_json: t.string() },
  (ctx, { task_id, result_json }) => {
    const task = tdm10GetTask(ctx, task_id);
    if (task.status !== 'claimed') throw new SenderError(`task ${task_id} is ${task.status}, not claimed`);
    const actor = tdm10RequireHolderOrAdmin(ctx, task, 'complete it');
    if (actor.startsWith('contributor:') && !tdm10LeaseLive(task, ctx.timestamp)) {
      throw new SenderError(`lease on task ${task_id} has expired; claim it again`);
    }
    if (result_json.length > MAX_RESULT_JSON) throw new SenderError('result_json too large');
    let result: unknown;
    try {
      result = JSON.parse(result_json || '{}');
    } catch {
      throw new SenderError('result_json is not valid JSON');
    }
    ctx.db.task.id.update({
      ...task,
      status: 'completed',
      lease_expires_at: undefined,
      completed_at: ctx.timestamp,
    });
    tdm10Log(ctx, actor, 'task.completed', {
      task_id,
      contributor_id: task.assignee_id,
      ticket_id: task.ticket_id,
      robot_id: task.robot_id,
      type: task.type,
      price_cents: task.price_cents,
      claimed_at: task.claimed_at?.microsSinceUnixEpoch,
      result,
    });
  }
);

// A market admin withdraws an open or claimed task. This also revokes a live claim.
export const cancel_task = spacetimedb.reducer(
  { task_id: t.u64(), reason: t.string() },
  (ctx, { task_id, reason }) => {
    const role = requireRole(ctx, ...MARKET_ADMIN_ROLES);
    const task = tdm10GetTask(ctx, task_id);
    if (task.status !== 'open' && task.status !== 'claimed') {
      throw new SenderError(`task ${task_id} is ${task.status}; only open or claimed tasks can be cancelled`);
    }
    ctx.db.task.id.update({ ...task, status: 'cancelled', lease_expires_at: undefined });
    tdm10Log(ctx, role, 'task.cancelled', {
      task_id,
      previous_status: task.status,
      assignee_id: task.assignee_id,
      lease_was_live: tdm10LeaseLive(task, ctx.timestamp),
      robot_id: task.robot_id,
      reason,
    });
  }
);

// Sweeper: writes every lapsed claim back to 'open' (with task.lease_expired
// events). Open to any caller because it only applies the time rule. The lock
// does not depend on it running.
export const expire_task_leases = spacetimedb.reducer((ctx) => {
  for (const task of [...ctx.db.task.status.filter('claimed')]) {
    tdm10ExpireIfLapsed(ctx, task);
  }
});
