// TDM-79: run-settings editor logic. Talks to GET/PATCH /tickets/{id}/settings (TDM-20) through the
// /api/tdm79 proxy. The checks here mirror run_settings_api.py so most mistakes are caught before
// the request; whatever the orchestrator still refuses comes back as field-level 422s and is shown
// next to the same field.

export type DataTypeOverride = 'clean_teleop' | 'recovery_takeover' | null;

export interface Tdm79Hyperparams {
  lr?: number;
  steps?: number;
  batch_size?: number;
  lora_rank?: number;
  prompt?: string;
  [k: string]: unknown;
}

export interface Tdm79Settings {
  ticket_id: number;
  eval_trials: number;
  success_threshold: number;
  require_ci_low: boolean;
  budget_cents: number;
  target_episodes: number;
  max_iterations: number;
  data_type_override: DataTypeOverride;
  hyperparams: Tdm79Hyperparams;
  updated_at_micros: number;
  ticket: { status: string; iteration: number; budget_cents: number; approved_cents: number; spent_cents: number };
  audit_logged?: boolean | null;
}

/** Form values, kept as strings while editing. Percent and dollars are what people type. */
export interface Tdm79Form {
  eval_trials: string;
  success_pct: string;
  require_ci_low: boolean;
  budget_usd: string;
  target_episodes: string;
  max_iterations: string;
  data_type_override: '' | 'clean_teleop' | 'recovery_takeover';
  lr: string;
  steps: string;
  batch_size: string;
  lora_rank: string;
  prompt: string;
}

export type Tdm79Field = keyof Tdm79Form;
export type Tdm79Errors = Partial<Record<Tdm79Field | 'form', string>>;

export const TDM79_TERMINAL = ['shipped', 'failed', 'cancelled'];

function str(v: unknown): string {
  return v === undefined || v === null ? '' : String(v);
}

export function toForm(s: Tdm79Settings): Tdm79Form {
  const hp = s.hyperparams ?? {};
  return {
    eval_trials: str(s.eval_trials),
    success_pct: str(Math.round(s.success_threshold * 1000) / 10),
    require_ci_low: s.require_ci_low,
    budget_usd: (s.budget_cents / 100).toFixed(2),
    target_episodes: str(s.target_episodes),
    max_iterations: str(s.max_iterations),
    data_type_override: s.data_type_override ?? '',
    lr: str(hp.lr),
    steps: str(hp.steps),
    batch_size: str(hp.batch_size),
    lora_rank: str(hp.lora_rank),
    prompt: str(hp.prompt),
  };
}

function intIn(raw: string, lo: number, hi: number, label: string): number | string {
  const t = raw.trim();
  if (!/^-?\d+$/.test(t)) return `${label} must be a whole number`;
  const n = Number(t);
  if (n < lo || n > hi) return `${label} must be between ${lo} and ${hi.toLocaleString()}`;
  return n;
}

/**
 * Validate the form against the loaded settings and build the PATCH body from the changed fields
 * only. Returns { patch } (possibly empty) or { errors }.
 */
export function buildPatch(
  form: Tdm79Form,
  base: Tdm79Settings,
): { patch: Record<string, unknown>; errors: Tdm79Errors } {
  const errors: Tdm79Errors = {};
  const patch: Record<string, unknown> = {};
  const hp: Record<string, unknown> = {};
  const was = toForm(base);
  const changed = (f: Tdm79Field) => form[f] !== was[f];

  if (changed('eval_trials')) {
    const v = intIn(form.eval_trials, 1, 50, 'Eval trials');
    if (typeof v === 'string') errors.eval_trials = v;
    else patch.eval_trials = v;
  }
  if (changed('success_pct')) {
    const n = Number(form.success_pct.trim());
    if (form.success_pct.trim() === '' || !Number.isFinite(n)) errors.success_pct = 'Enter a percentage';
    else if (n < 0 || n > 100) errors.success_pct = 'Threshold must be between 0% and 100%';
    else patch.success_threshold = Math.round(n * 10) / 1000;
  }
  if (changed('require_ci_low')) patch.require_ci_low = form.require_ci_low;
  if (changed('budget_usd')) {
    const t = form.budget_usd.trim().replace(/^\$/, '').replace(/,/g, '');
    const n = Number(t);
    const committed = Math.max(base.ticket.approved_cents, base.ticket.spent_cents);
    if (t === '' || !Number.isFinite(n) || !/^\d+(\.\d{0,2})?$/.test(t)) errors.budget_usd = 'Enter dollars, like 50 or 49.99';
    else if (Math.round(n * 100) < committed) {
      errors.budget_usd = `Budget can't go below the $${(committed / 100).toFixed(2)} already approved or spent`;
    } else patch.budget_cents = Math.round(n * 100);
  }
  if (changed('target_episodes')) {
    const v = intIn(form.target_episodes, 1, 10_000, 'Target episodes');
    if (typeof v === 'string') errors.target_episodes = v;
    else patch.target_episodes = v;
  }
  if (changed('max_iterations')) {
    const v = intIn(form.max_iterations, 1, 20, 'Max iterations');
    if (typeof v === 'string') errors.max_iterations = v;
    else if (v <= base.ticket.iteration) {
      errors.max_iterations = `Must be more than the current iteration (${base.ticket.iteration})`;
    } else patch.max_iterations = v;
  }
  if (changed('data_type_override')) patch.data_type_override = form.data_type_override || null;

  if (changed('lr')) {
    const n = Number(form.lr.trim());
    if (form.lr.trim() === '' || !Number.isFinite(n)) errors.lr = 'Enter a number, like 0.0001 or 1e-4';
    else if (n <= 0 || n > 1) errors.lr = 'Learning rate must be above 0 and at most 1';
    else hp.lr = n;
  }
  if (changed('steps')) {
    const v = intIn(form.steps, 1, 1_000_000, 'Steps');
    if (typeof v === 'string') errors.steps = v;
    else hp.steps = v;
  }
  if (changed('batch_size')) {
    const v = intIn(form.batch_size, 1, 4096, 'Batch size');
    if (typeof v === 'string') errors.batch_size = v;
    else hp.batch_size = v;
  }
  if (changed('lora_rank')) {
    const v = intIn(form.lora_rank, 0, 1024, 'LoRA rank');
    if (typeof v === 'string') errors.lora_rank = v;
    else hp.lora_rank = v;
  }
  if (changed('prompt')) hp.prompt = form.prompt;
  if (Object.keys(hp).length) patch.hyperparams = hp;
  return { patch, errors };
}

/** The API field a 422 `loc` points at, as a form field. */
const LOC_TO_FIELD: Record<string, Tdm79Field> = {
  eval_trials: 'eval_trials',
  success_threshold: 'success_pct',
  require_ci_low: 'require_ci_low',
  budget_cents: 'budget_usd',
  target_episodes: 'target_episodes',
  max_iterations: 'max_iterations',
  data_type_override: 'data_type_override',
  lr: 'lr',
  steps: 'steps',
  batch_size: 'batch_size',
  lora_rank: 'lora_rank',
  prompt: 'prompt',
};

/** Map the orchestrator's error body ({detail: [{loc, msg}]} or {detail: "..."}) to form errors. */
export function errorsFromResponse(status: number, body: unknown): Tdm79Errors {
  const out: Tdm79Errors = {};
  const detail = (body as { detail?: unknown })?.detail;
  if (Array.isArray(detail)) {
    for (const d of detail as { loc?: unknown[]; msg?: string }[]) {
      const loc = (d.loc ?? []).map(String);
      const field = [...loc].reverse().map((k) => LOC_TO_FIELD[k]).find(Boolean);
      const msg = String(d.msg ?? 'rejected').replace(/^Value error, /, '');
      if (field && !out[field]) out[field] = msg;
      else out.form = out.form ? `${out.form}; ${msg}` : msg;
    }
  } else if (typeof detail === 'string') {
    out.form = detail;
  }
  if (!Object.keys(out).length) out.form = `The orchestrator answered ${status}`;
  return out;
}

export async function fetchSettings(ticketId: string | number | bigint): Promise<Tdm79Settings> {
  const res = await fetch(`/api/tdm79/tickets/${ticketId}/settings`, { cache: 'no-store' });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = errorsFromResponse(res.status, body);
    throw new Error(Object.values(e).join('; '));
  }
  return body as Tdm79Settings;
}

export async function patchSettings(
  ticketId: string | number | bigint,
  patch: Record<string, unknown>,
  humanId: string,
): Promise<{ ok: true; settings: Tdm79Settings } | { ok: false; errors: Tdm79Errors }> {
  const res = await fetch(`/api/tdm79/tickets/${ticketId}/settings`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json', ...(humanId ? { 'x-human-id': humanId } : {}) },
    body: JSON.stringify(patch),
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) return { ok: true, settings: body as Tdm79Settings };
  return { ok: false, errors: errorsFromResponse(res.status, body) };
}

// ---------------------------------------------------------------- when a change is picked up

const TDM79_STAGES = ['created', 'onboard', 'baseline', 'diagnose', 'plan_data', 'dispatch', 'review', 'train', 'eval'];

export interface Tdm79LoopState {
  status: string;
  iteration: number;
  maxIterations: number;
  /** Iterations that already have a training_run row. */
  trainedIterations: Set<number>;
  hasBaselineEval: boolean;
  /** Iterations whose after-training eval has started. */
  evaluatedIterations: Set<number>;
  /** Iterations that already have a data_plan row. */
  plannedIterations: Set<number>;
}

function nextIter(s: Tdm79LoopState, what: string): string {
  const n = s.iteration + 1;
  if (n >= s.maxIterations) {
    return `Too late for iteration ${s.iteration}, and it is the last one (max iterations ${s.maxIterations}). Raise max iterations to use this.`;
  }
  return `Iteration ${n}: ${what}`;
}

/** One sentence per settings group saying which step of which iteration will read the new value. */
export function pickupNotes(s: Tdm79LoopState): Record<'eval' | 'verdict' | 'data' | 'budget' | 'training', string> {
  if (TDM79_TERMINAL.includes(s.status)) {
    const frozen = `The ticket is ${s.status}, so its settings are frozen.`;
    return { eval: frozen, verdict: frozen, data: frozen, budget: frozen, training: frozen };
  }
  const at = TDM79_STAGES.indexOf(s.status);
  const it = s.iteration;
  const before = (stage: string) => at < TDM79_STAGES.indexOf(stage);
  const evalNote = !s.hasBaselineEval
    ? 'The baseline eval, before any training.'
    : before('eval') || !s.evaluatedIterations.has(it)
      ? `Iteration ${it}: the after-training eval. Trials already running keep their count.`
      : nextIter(s, 'the after-training eval.');
  // Every open stage is at or before this iteration's eval verdict.
  const verdict = `Iteration ${it}: the ship-or-retry decision when its eval finishes.`;
  const data = before('plan_data') && !s.plannedIterations.has(it)
    ? `Iteration ${it}: the planner's data plan.`
    : s.status === 'plan_data' && !s.plannedIterations.has(it)
      ? `Iteration ${it}: the planner's data plan, if it has not started writing it.`
      : nextIter(s, "the planner's data plan.");
  const training = before('train') || (s.status === 'train' && !s.trainedIterations.has(it))
    ? `Iteration ${it}: the training run, when it is queued. The prompt also goes on new paid tasks right away.`
    : nextIter(s, 'the next training run. The prompt also goes on new paid tasks right away.');
  return {
    eval: evalNote,
    verdict,
    data,
    budget: 'Right away: the next payout and dispatch check use it.',
    training,
  };
}
