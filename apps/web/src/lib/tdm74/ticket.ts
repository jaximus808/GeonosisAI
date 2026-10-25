// TDM-74: ticket-creation logic for /tickets/new. Talks to POST /tickets/parse (preview, creates
// nothing) and POST /tickets (TDM-19) through the /api/tdm74 proxy.
//
// The Advanced section reuses the TDM-79 run-settings form and validation: the form is diffed
// against the module defaults (DEFAULT_RUN_SETTINGS in spacetime enums.ts), so only the settings a
// person actually changed are sent as settings_overrides.

import { buildPatch, toForm, type Tdm79Field, type Tdm79Form, type Tdm79Settings } from '@/lib/tdm79/settings';

/** DEFAULT_RUN_SETTINGS (spacetime/spacetimedb/src/enums.ts) as a TDM-79 settings object. */
export const TDM74_DEFAULTS: Tdm79Settings = {
  ticket_id: 0,
  eval_trials: 10,
  success_threshold: 0.8,
  require_ci_low: false,
  budget_cents: 0,
  target_episodes: 5,
  max_iterations: 3,
  data_type_override: null,
  hyperparams: { lr: 2.5e-5, steps: 2000, batch_size: 32, lora_rank: 16, prompt: '' },
  updated_at_micros: 0,
  ticket: { status: 'created', iteration: 0, budget_cents: 0, approved_cents: 0, spent_cents: 0 },
};

export const TDM74_ROBOT_ONLINE = ['online', 'busy', 'paused'];

export interface Tdm74Parsed {
  task_description: string;
  target_success: number | null;
  budget_usd: number | null;
  policy_prompt: string;
  eval_trials: number | null;
}

export interface Tdm74Robot {
  id: number;
  name: string;
  status: string;
  embodiment?: string | null;
  robot_mode?: 'sim' | 'real' | null; // TDM-107
}

export interface Tdm74Preview {
  parsed: Tdm74Parsed;
  task_description: string;
  target_success: number;
  budget_cents: number | null;
  settings_overrides: Record<string, unknown>;
  defaults_applied: string[];
  missing: string[];
  robot: Tdm74Robot | null;
  robot_mode?: 'sim' | 'real' | null; // TDM-107: the mode the ticket would run in
  robot_selection: string | null;
  robot_error: { code: string; message: string; robots?: Tdm74Robot[] } | null;
  collect_robot?: Tdm74Robot | null; // TDM-125 (also set by the server's auto split)
  note?: string | null; // e.g. a Real ticket on a teleop-only arm became a split (evals on the sim robot)
}

export interface Tdm74Created {
  ticket_id: number;
  robot: Tdm74Robot;
  collect_robot?: Tdm74Robot | null;
  note?: string | null; // the server converted the ticket (e.g. to a split); shown before the redirect
  ticket: Record<string, unknown>;
}

export interface Tdm74Inputs {
  prompt: string;
  robotId: string; // '' = let the orchestrator pick the only online robot
  robotMode?: 'sim' | 'real' | ''; // TDM-107: '' = any robot; else only robots of that mode
  budgetUsd: string; // '' = use the prompt's budget
  targetPct: string; // '' = use the prompt's target (or the 80% default)
  advanced: Tdm79Form;
  /** TDM-125: collect the data on this robot (e.g. the real yam-01) while evals run on robotId. '' = same robot. */
  collectRobotId?: string;
}

export type Tdm74Field = 'prompt' | 'robot' | 'budget' | 'target' | 'form' | Tdm79Field;
export type Tdm74Errors = Partial<Record<Tdm74Field, string>>;

// Demo-sized starting values for a new ticket: a 3-trial eval and one demo to buy per round, so one person can
// run the whole loop in a few minutes. They differ from the module defaults, so they are sent as overrides.
export const TDM74_START: Partial<Tdm79Settings> = { eval_trials: 3, target_episodes: 1 };

export function tdm74EmptyAdvanced(): Tdm79Form {
  return toForm({ ...TDM74_DEFAULTS, ...TDM74_START });
}

/**
 * Validate the inputs and build the request body. `parsed` (from a preview) is sent back so the
 * orchestrator does not call Claude again. Returns { body, errors }; send only when errors is empty.
 */
export function tdm74Body(
  inp: Tdm74Inputs,
  parsed?: Tdm74Parsed,
): { body: Record<string, unknown>; errors: Tdm74Errors } {
  const errors: Tdm74Errors = {};
  const body: Record<string, unknown> = { prompt: inp.prompt.trim() };
  if (!body.prompt) errors.prompt = 'Describe the job first';
  else if ((body.prompt as string).length > 4000) errors.prompt = 'Keep the description under 4,000 characters';

  if (inp.robotId) body.robot_id = Number(inp.robotId);
  if (inp.robotMode) body.robot_mode = inp.robotMode;
  if (inp.collectRobotId && inp.collectRobotId !== inp.robotId) body.collect_robot_id = Number(inp.collectRobotId); // TDM-125

  const b = inp.budgetUsd.trim().replace(/^\$/, '').replace(/,/g, '');
  if (b) {
    if (!/^\d+(\.\d{0,2})?$/.test(b)) errors.budget = 'Enter dollars, like 50 or 49.99';
    else body.budget_cents = Math.round(Number(b) * 100);
  }
  const t = inp.targetPct.trim().replace(/%$/, '');
  if (t) {
    const n = Number(t);
    if (!Number.isFinite(n) || n <= 0 || n > 100) errors.target = 'Enter a percentage above 0 and up to 100';
    else body.target_success = Math.round(n * 10) / 1000;
  }

  // Budget and target are top-level create_ticket arguments, so keep them out of the overrides diff.
  const base = toForm(TDM74_DEFAULTS);
  const adv: Tdm79Form = { ...inp.advanced, budget_usd: base.budget_usd, success_pct: base.success_pct };
  const { patch, errors: advErrors } = buildPatch(adv, TDM74_DEFAULTS);
  Object.assign(errors, advErrors);
  if (Object.keys(patch).length) body.settings_overrides = patch;
  if (parsed) body.parsed = parsed;
  return { body, errors };
}

// ---------------------------------------------------------------- errors from the orchestrator

const TDM74_LOC: Record<string, Tdm74Field> = {
  prompt: 'prompt',
  robot_id: 'robot',
  robot_mode: 'robot',
  budget_cents: 'budget',
  target_success: 'target',
  eval_trials: 'eval_trials',
  success_threshold: 'target',
  require_ci_low: 'require_ci_low',
  target_episodes: 'target_episodes',
  max_iterations: 'max_iterations',
  data_type_override: 'data_type_override',
  lr: 'lr',
  steps: 'steps',
  batch_size: 'batch_size',
  lora_rank: 'lora_rank',
};

const TDM74_CODE: Record<string, Tdm74Field> = {
  budget_required: 'budget',
  bad_budget: 'budget',
  bad_target_success: 'target',
  task_required: 'prompt',
  prompt_refused: 'prompt',
  unknown_robot: 'robot',
  no_online_robot: 'robot',
  multiple_online_robots: 'robot',
  robot_mode_mismatch: 'robot',
  real_robot_cannot_eval: 'robot', // a Real ticket on a teleop-only arm with no sim robot online for its evals
};

export interface Tdm74Failure {
  errors: Tdm74Errors;
  code?: string;
  robots?: Tdm74Robot[];
}

/** Map {detail: {code, message, robots?}} or FastAPI's {detail: [{loc, msg}]} to inline errors. */
export function tdm74Failure(status: number, body: unknown): Tdm74Failure {
  const errors: Tdm74Errors = {};
  const detail = (body as { detail?: unknown })?.detail;
  const add = (f: Tdm74Field, msg: string) => {
    if (!errors[f]) errors[f] = msg;
    else if (f === 'form') errors.form = `${errors.form}; ${msg}`;
  };
  if (Array.isArray(detail)) {
    for (const d of detail as { loc?: unknown[]; msg?: string }[]) {
      const loc = (d.loc ?? []).map(String);
      const f = [...loc].reverse().map((k) => TDM74_LOC[k]).find(Boolean);
      add(f ?? 'form', String(d.msg ?? 'rejected').replace(/^Value error, /, ''));
    }
    return { errors };
  }
  if (detail && typeof detail === 'object') {
    const d = detail as { code?: string; message?: string; robots?: Tdm74Robot[] };
    const msg = d.message ?? d.code ?? `The orchestrator answered ${status}`;
    let f: Tdm74Field = (d.code && TDM74_CODE[d.code]) || 'form';
    if (d.code === 'create_ticket_rejected') {
      // Reducer messages start with the setting they reject, e.g. "hyperparams.lr must be ...".
      const key = /^(?:hyperparams\.)?([a-z_]+)/.exec(msg)?.[1];
      f = (key && TDM74_LOC[key]) || 'form';
    }
    add(f, msg);
    return { errors, code: d.code, robots: d.robots };
  }
  add('form', typeof detail === 'string' ? detail : `The orchestrator answered ${status}`);
  return { errors };
}

async function tdm74Post<T>(path: string, body: Record<string, unknown>): Promise<{ ok: true; data: T } | ({ ok: false } & Tdm74Failure)> {
  let res: Response;
  try {
    res = await fetch(`/api/tdm74/${path}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (e) {
    return { ok: false, errors: { form: `Couldn't reach the web server (${(e as Error).message})` } };
  }
  const data = await res.json().catch(() => ({}));
  if (res.ok) return { ok: true, data: data as T };
  return { ok: false, ...tdm74Failure(res.status, data) };
}

export const tdm74Preview = (body: Record<string, unknown>) => tdm74Post<Tdm74Preview>('tickets/parse', body);
export const tdm74Create = (body: Record<string, unknown>) => tdm74Post<Tdm74Created>('tickets', body);

export function tdm74Usd(cents: number): string {
  return `$${(cents / 100).toLocaleString(undefined, { minimumFractionDigits: cents % 100 ? 2 : 0, maximumFractionDigits: 2 })}`;
}
