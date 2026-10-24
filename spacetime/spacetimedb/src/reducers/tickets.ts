// TDM-9: ticket, run-settings and robot reducers.
//
//   register_robot(name, embodiment, adapter_version, camera_config_json)
//   robot_heartbeat(robot_id, status)
//   create_ticket(robot_id, task_description, target_success, budget_cents, settings_overrides_json)
//   update_run_settings(ticket_id, patch_json)
//   set_ticket_status(ticket_id, status, note)
//
// Every state change appends an event_log row (actor = caller's service role).
// Ticket statuses are the TDM-8 enum (enums.ts TICKET_STATUSES); the TDM-9
// brief's names map as diagnosing=diagnose, planning=plan_data,
// collecting=dispatch, reviewing=review, training=train, evaluating=eval.
import { t, SenderError } from 'spacetimedb/server';
import spacetimedb, { type RdfCtx } from '../schema';
import { requireRole, requireService } from '../auth';
import { logEvent } from '../event_log'; // TDM-15
import { DEFAULT_RUN_SETTINGS, ROBOT_STATUSES, TICKET_STATUSES, oneOf } from '../enums';

type TicketStatus = (typeof TICKET_STATUSES)[number];

const TDM9_TERMINAL: readonly TicketStatus[] = ['shipped', 'failed', 'cancelled'];

// Forward edges of the flywheel. Any non-terminal status may also go to
// failed or cancelled. eval -> diagnose|plan_data|dispatch is the "plan more
// data" loop and bumps ticket.iteration (capped by run_settings.max_iterations).
// created -> baseline skips onboarding for a robot that is already onboarded.
// review -> dispatch re-opens collection when rejections leave the set short.
const TDM9_TICKET_TRANSITIONS: Record<TicketStatus, readonly TicketStatus[]> = {
  created: ['onboarding', 'baseline'],
  onboarding: ['baseline'],
  baseline: ['diagnose'],
  diagnose: ['plan_data'],
  plan_data: ['dispatch'],
  dispatch: ['review'],
  review: ['train', 'dispatch'],
  train: ['eval'],
  eval: ['shipped', 'diagnose', 'plan_data', 'dispatch'],
  shipped: [],
  failed: [],
  cancelled: [],
};
const TDM9_LOOP_TARGETS: readonly TicketStatus[] = ['diagnose', 'plan_data', 'dispatch'];

function tdm9Log(ctx: RdfCtx, actor: string, type: string, payload: Record<string, unknown>) {
  logEvent(ctx, actor, type, JSON.stringify(payload, (_k, v) =>
    typeof v === 'bigint' ? (v <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(v) : v.toString()) : v)); // TDM-15: sole event_log writer
}

function tdm9ParseObject(field: string, text: string): Record<string, unknown> {
  if (text.trim() === '') return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new SenderError(`${field} is not valid JSON`);
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new SenderError(`${field} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

function tdm9Int(field: string, v: unknown, min: number, max: number): number {
  if (typeof v !== 'number' || !Number.isInteger(v) || v < min || v > max) {
    throw new SenderError(`${field} must be an integer in [${min}, ${max}], got ${JSON.stringify(v)}`);
  }
  return v;
}

function tdm9Fraction(field: string, v: unknown, minExclusive = false): number {
  if (typeof v !== 'number' || !Number.isFinite(v) || v > 1 || (minExclusive ? v <= 0 : v < 0)) {
    throw new SenderError(`${field} must be a number in ${minExclusive ? '(0' : '[0'}, 1], got ${JSON.stringify(v)}`);
  }
  return v;
}

type Tdm9Settings = {
  eval_trials: number;
  success_threshold: number;
  require_ci_low: boolean;
  budget_cents: bigint;
  target_episodes: number;
  max_iterations: number;
  hyperparams: Record<string, unknown>;
  data_type_override: string; // TDM-20: '' = planner decides
};

// TDM-20: data types a human may force the planner to pick ('' / null = no override).
const TDM20_DATA_TYPE_OVERRIDES = ['clean_teleop', 'recovery_takeover'] as const;

const TDM9_HYPERPARAM_CHECKS: Record<string, (v: unknown) => unknown> = {
  lr: (v) => {
    if (typeof v !== 'number' || !Number.isFinite(v) || v <= 0 || v > 1) {
      throw new SenderError(`hyperparams.lr must be a number in (0, 1], got ${JSON.stringify(v)}`);
    }
    return v;
  },
  steps: (v) => tdm9Int('hyperparams.steps', v, 1, 1_000_000),
  batch_size: (v) => tdm9Int('hyperparams.batch_size', v, 1, 4096),
  lora_rank: (v) => tdm9Int('hyperparams.lora_rank', v, 0, 1024),
  prompt: (v) => {
    if (typeof v !== 'string') throw new SenderError('hyperparams.prompt must be a string');
    return v;
  },
  // TDM-69: DAgger interventions. Unset = default (on while collecting a recovery plan, off for evals).
  interventions_enabled: (v) => {
    if (typeof v !== 'boolean') throw new SenderError('hyperparams.interventions_enabled must be true or false');
    return v;
  },
};

/**
 * Applies a validated patch to settings (mutates and returns it). Allowed keys:
 * eval_trials (1-50), success_threshold (0-1), require_ci_low (bool),
 * budget_cents (integer >= 0, only when allowBudget), target_episodes (1-10000),
 * max_iterations (1-20), hyperparams (object merged key-by-key into the current
 * hyperparams: lr, steps, batch_size, lora_rank, prompt), data_type_override
 * (TDM-20: null or '' = planner decides, else clean_teleop|recovery_takeover).
 */
function tdm9ApplyPatch(s: Tdm9Settings, patch: Record<string, unknown>, allowBudget: boolean): Tdm9Settings {
  for (const [key, v] of Object.entries(patch)) {
    switch (key) {
      case 'eval_trials':
        s.eval_trials = tdm9Int(key, v, 1, 50);
        break;
      case 'success_threshold':
        s.success_threshold = tdm9Fraction(key, v);
        break;
      case 'require_ci_low':
        if (typeof v !== 'boolean') throw new SenderError('require_ci_low must be a boolean');
        s.require_ci_low = v;
        break;
      case 'budget_cents':
        if (!allowBudget) {
          throw new SenderError('budget_cents is a create_ticket argument, not an override');
        }
        s.budget_cents = BigInt(tdm9Int(key, v, 0, Number.MAX_SAFE_INTEGER));
        break;
      case 'target_episodes':
        s.target_episodes = tdm9Int(key, v, 1, 10_000);
        break;
      case 'max_iterations':
        s.max_iterations = tdm9Int(key, v, 1, 20);
        break;
      case 'hyperparams': {
        if (v === null || typeof v !== 'object' || Array.isArray(v)) {
          throw new SenderError('hyperparams must be a JSON object');
        }
        const merged = { ...s.hyperparams };
        for (const [hk, hv] of Object.entries(v as Record<string, unknown>)) {
          const check = TDM9_HYPERPARAM_CHECKS[hk];
          if (!check) {
            throw new SenderError(
              `unknown hyperparam "${hk}" (allowed: ${Object.keys(TDM9_HYPERPARAM_CHECKS).join('|')})`
            );
          }
          merged[hk] = check(hv);
        }
        s.hyperparams = merged;
        break;
      }
      case 'data_type_override':
        if (v === null || v === '') {
          s.data_type_override = '';
        } else if (typeof v === 'string' && (TDM20_DATA_TYPE_OVERRIDES as readonly string[]).includes(v)) {
          s.data_type_override = v;
        } else {
          throw new SenderError(
            `data_type_override must be null or one of ${TDM20_DATA_TYPE_OVERRIDES.join('|')}, got ${JSON.stringify(v)}`
          );
        }
        break;
      default:
        throw new SenderError(
          `unknown run setting "${key}" (allowed: eval_trials|success_threshold|require_ci_low|` +
            `${allowBudget ? 'budget_cents|' : ''}target_episodes|max_iterations|hyperparams|data_type_override)`
        );
    }
  }
  return s;
}

// ---------------------------------------------------------------- robots

// Register a robot, or re-register (update adapter/camera config) one with the
// same name, so the Jetson can call this on every boot. A robot_client caller
// is bound as the robot's client_identity.
export const register_robot = spacetimedb.reducer(
  { name: t.string(), embodiment: t.string(), adapter_version: t.string(), camera_config_json: t.string() },
  (ctx, { name, embodiment, adapter_version, camera_config_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'robot_client', 'factory');
    if (name.trim() === '') throw new SenderError('name is required');
    if (embodiment.trim() === '') throw new SenderError('embodiment is required');
    const cameraConfig = camera_config_json.trim() === '' ? '{}' : camera_config_json;
    tdm9ParseObject('camera_config_json', cameraConfig);
    const clientIdentity = role === 'robot_client' ? ctx.sender : undefined;

    const existing = [...ctx.db.robot.iter()].find((r) => r.name === name);
    if (existing) {
      if (existing.client_identity && clientIdentity && !existing.client_identity.equals(clientIdentity)) {
        throw new SenderError(`robot "${name}" is bound to another robot_client identity`);
      }
      ctx.db.robot.id.update({
        ...existing,
        embodiment,
        adapter_version,
        camera_config_json: cameraConfig,
        client_identity: existing.client_identity ?? clientIdentity,
      });
      tdm9Log(ctx, role, 'robot.updated', {
        robot_id: existing.id,
        name,
        embodiment,
        adapter_version,
        previous_adapter_version: existing.adapter_version,
      });
      return;
    }
    const row = ctx.db.robot.insert({
      id: 0n,
      name,
      embodiment,
      status: 'offline',
      adapter_version,
      camera_config_json: cameraConfig,
      client_identity: clientIdentity,
      last_heartbeat: undefined,
      created_at: ctx.timestamp,
    });
    tdm9Log(ctx, role, 'robot.registered', { robot_id: row.id, name, embodiment, adapter_version });
  }
);

// Liveness + status from the robot. A robot_client may only heartbeat its own
// robot (the first robot_client to heartbeat an unbound robot is bound to it).
// Logs to event_log only when the status changes or the client is bound, so a
// steady heartbeat does not flood the evidence log.
export const robot_heartbeat = spacetimedb.reducer(
  { robot_id: t.u64(), status: t.string() },
  (ctx, { robot_id, status }) => {
    const role = requireRole(ctx, 'robot_client', 'orchestrator');
    oneOf('status', status, ROBOT_STATUSES);
    const robot = ctx.db.robot.id.find(robot_id);
    if (!robot) throw new SenderError(`unknown robot ${robot_id}`);
    let clientIdentity = robot.client_identity;
    let bound = false;
    if (role === 'robot_client') {
      if (!clientIdentity) {
        clientIdentity = ctx.sender;
        bound = true;
      } else if (!clientIdentity.equals(ctx.sender)) {
        throw new SenderError(`robot ${robot_id} is bound to another robot_client identity`);
      }
    }
    ctx.db.robot.id.update({ ...robot, status, client_identity: clientIdentity, last_heartbeat: ctx.timestamp });
    if (bound || robot.status !== status) {
      tdm9Log(ctx, role, 'robot.status_changed', {
        robot_id,
        from: robot.status,
        to: status,
        ...(bound ? { bound_client: ctx.sender.toHexString() } : {}),
      });
    }
  }
);

// ---------------------------------------------------------------- tickets

// Creates the ticket (status created, iteration 0), its dataset row and its
// run_settings row. Settings start from DEFAULT_RUN_SETTINGS, except
// success_threshold which defaults to the ticket's target_success, then
// settings_overrides_json ("" or a JSON object, same keys as
// update_run_settings minus budget_cents) is applied on top.
export const create_ticket = spacetimedb.reducer(
  {
    robot_id: t.u64(),
    task_description: t.string(),
    target_success: t.f64(),
    budget_cents: t.u64(),
    settings_overrides_json: t.string(),
  },
  (ctx, { robot_id, task_description, target_success, budget_cents, settings_overrides_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'mcp', 'web');
    if (!ctx.db.robot.id.find(robot_id)) throw new SenderError(`unknown robot ${robot_id}`);
    if (task_description.trim() === '') throw new SenderError('task_description is required');
    tdm9Fraction('target_success', target_success, true);
    const overrides = tdm9ParseObject('settings_overrides_json', settings_overrides_json);

    const settings = tdm9ApplyPatch(
      {
        eval_trials: DEFAULT_RUN_SETTINGS.eval_trials,
        success_threshold: target_success,
        require_ci_low: DEFAULT_RUN_SETTINGS.require_ci_low,
        budget_cents,
        target_episodes: DEFAULT_RUN_SETTINGS.target_episodes,
        max_iterations: DEFAULT_RUN_SETTINGS.max_iterations,
        hyperparams: JSON.parse(DEFAULT_RUN_SETTINGS.hyperparams_json) as Record<string, unknown>,
        data_type_override: '',
      },
      overrides,
      false
    );

    const ticket = ctx.db.ticket.insert({
      id: 0n,
      robot_id,
      task_description,
      target_success,
      budget_cents,
      approved_cents: 0n,
      spent_cents: 0n,
      status: 'created',
      dataset_id: 0n,
      iteration: 0,
      deployed_checkpoint_uri: undefined,
      created_by: role,
      created_at: ctx.timestamp,
      updated_at: ctx.timestamp,
    });
    const dataset = ctx.db.dataset.insert({
      id: 0n,
      ticket_id: ticket.id,
      accepted_count: 0,
      total_count: 0,
      lerobot_uri: undefined,
      updated_at: ctx.timestamp,
    });
    ctx.db.ticket.id.update({ ...ticket, dataset_id: dataset.id });
    const hyperparams_json = JSON.stringify(settings.hyperparams);
    ctx.db.runSettings.insert({
      ticket_id: ticket.id,
      eval_trials: settings.eval_trials,
      success_threshold: settings.success_threshold,
      require_ci_low: settings.require_ci_low,
      budget_cents: settings.budget_cents,
      target_episodes: settings.target_episodes,
      max_iterations: settings.max_iterations,
      hyperparams_json,
      updated_at: ctx.timestamp,
      data_type_override: settings.data_type_override,
    });
    tdm9Log(ctx, role, 'ticket.created', {
      ticket_id: ticket.id,
      robot_id,
      task_description,
      target_success,
      budget_cents,
      dataset_id: dataset.id,
      overrides,
      run_settings: { ...settings, hyperparams: undefined, hyperparams_json },
    });
  }
);

// patch_json: JSON object with any of eval_trials (1-50), success_threshold
// (0-1), require_ci_low, budget_cents (>= 0 and >= what the ticket already
// approved/spent; mirrored onto ticket.budget_cents), target_episodes,
// max_iterations, hyperparams (merged), data_type_override (TDM-20: null|''
// clears it, else clean_teleop|recovery_takeover). Refused on shipped/failed/cancelled.
export const update_run_settings = spacetimedb.reducer(
  { ticket_id: t.u64(), patch_json: t.string() },
  (ctx, { ticket_id, patch_json }) => {
    const role = requireRole(ctx, 'orchestrator', 'mcp', 'web');
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`unknown ticket ${ticket_id}`);
    if (TDM9_TERMINAL.includes(ticket.status as TicketStatus)) {
      throw new SenderError(`ticket ${ticket_id} is ${ticket.status}; run settings are frozen`);
    }
    const current = ctx.db.runSettings.ticket_id.find(ticket_id);
    if (!current) throw new SenderError(`ticket ${ticket_id} has no run_settings row`);
    const patch = tdm9ParseObject('patch_json', patch_json);
    if (Object.keys(patch).length === 0) throw new SenderError('patch_json is empty');

    const before = {
      eval_trials: current.eval_trials,
      success_threshold: current.success_threshold,
      require_ci_low: current.require_ci_low,
      budget_cents: current.budget_cents,
      target_episodes: current.target_episodes,
      max_iterations: current.max_iterations,
      hyperparams: JSON.parse(current.hyperparams_json) as Record<string, unknown>,
      data_type_override: current.data_type_override,
    };
    const next = tdm9ApplyPatch({ ...before }, patch, true);
    const committed = ticket.approved_cents > ticket.spent_cents ? ticket.approved_cents : ticket.spent_cents;
    if (next.budget_cents < committed) {
      throw new SenderError(
        `budget_cents ${next.budget_cents} is below what ticket ${ticket_id} already committed (${committed})`
      );
    }
    if (next.max_iterations <= ticket.iteration) {
      throw new SenderError(
        `max_iterations ${next.max_iterations} must exceed the current iteration ${ticket.iteration}`
      );
    }
    const hyperparams_json = JSON.stringify(next.hyperparams);
    ctx.db.runSettings.ticket_id.update({
      ticket_id,
      eval_trials: next.eval_trials,
      success_threshold: next.success_threshold,
      require_ci_low: next.require_ci_low,
      budget_cents: next.budget_cents,
      target_episodes: next.target_episodes,
      max_iterations: next.max_iterations,
      hyperparams_json,
      updated_at: ctx.timestamp,
      data_type_override: next.data_type_override,
    });
    if (next.budget_cents !== ticket.budget_cents) {
      ctx.db.ticket.id.update({ ...ticket, budget_cents: next.budget_cents, updated_at: ctx.timestamp });
    }
    tdm9Log(ctx, role, 'ticket.run_settings_updated', {
      ticket_id,
      patch,
      before: { ...before, hyperparams: undefined, hyperparams_json: current.hyperparams_json },
      after: { ...next, hyperparams: undefined, hyperparams_json },
    });
  }
);

// Moves a ticket along TDM9_TICKET_TRANSITIONS. The orchestrator drives the
// flywheel; mcp/web callers may only cancel. Looping back from eval increments
// ticket.iteration and is refused once iteration + 1 >= max_iterations (the
// orchestrator should then mark the ticket failed).
export const set_ticket_status = spacetimedb.reducer(
  { ticket_id: t.u64(), status: t.string(), note: t.string() },
  (ctx, { ticket_id, status, note }) => {
    const role = requireService(ctx);
    const to = oneOf('status', status, TICKET_STATUSES);
    if (role !== 'owner' && role !== 'orchestrator') {
      if (!((role === 'mcp' || role === 'web') && to === 'cancelled')) {
        throw new SenderError(`unauthorized: role ${role} may not set ticket status ${to}`);
      }
    }
    const ticket = ctx.db.ticket.id.find(ticket_id);
    if (!ticket) throw new SenderError(`unknown ticket ${ticket_id}`);
    const from = ticket.status as TicketStatus;
    const isTerminal = TDM9_TERMINAL.includes(from);
    const allowed =
      TDM9_TICKET_TRANSITIONS[from]?.includes(to) ||
      (!isTerminal && (to === 'failed' || to === 'cancelled'));
    if (!allowed) {
      const options = isTerminal ? [] : [...(TDM9_TICKET_TRANSITIONS[from] ?? []), 'failed', 'cancelled'];
      throw new SenderError(
        `illegal ticket transition ${from} -> ${to} (allowed from ${from}: ${options.join('|') || 'none, terminal'})`
      );
    }
    let iteration = ticket.iteration;
    if (from === 'eval' && TDM9_LOOP_TARGETS.includes(to)) {
      const maxIterations = ctx.db.runSettings.ticket_id.find(ticket_id)?.max_iterations ?? DEFAULT_RUN_SETTINGS.max_iterations;
      if (iteration + 1 >= maxIterations) {
        throw new SenderError(
          `ticket ${ticket_id} reached max_iterations ${maxIterations}; ship it, mark it failed, or raise max_iterations`
        );
      }
      iteration += 1;
    }
    ctx.db.ticket.id.update({ ...ticket, status: to, iteration, updated_at: ctx.timestamp });
    tdm9Log(ctx, role, 'ticket.status_changed', {
      ticket_id,
      from,
      to,
      note,
      iteration,
      ...(iteration !== ticket.iteration ? { previous_iteration: ticket.iteration } : {}),
    });
  }
);
