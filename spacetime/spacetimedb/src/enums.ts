// Allowed values for the string "enum" columns in schema.ts, plus defaults.
// Reducers validate with oneOf(); keep spacetime/schema.md in sync.
import { SenderError } from 'spacetimedb/server';

export const SERVICE_ROLES = [
  'owner',
  'orchestrator',
  'reviewer',
  'payments',
  'robot_client',
  'factory',
  'mcp',
  'web',
] as const;

export const ROBOT_STATUSES = ['offline', 'online', 'busy', 'paused', 'estop'] as const;

export const TICKET_STATUSES = [
  'created',
  'onboarding',
  'baseline',
  'diagnose',
  'plan_data',
  'dispatch',
  'review',
  'train',
  'eval',
  'shipped',
  'failed',
  'cancelled',
] as const;

export const DATA_TYPES = ['clean_teleop', 'recovery_takeover', 'purchased'] as const;
export const DATA_PLAN_STATUSES = ['proposed', 'approved', 'executing', 'done', 'rejected'] as const;

export const TASK_TYPES = ['clean_teleop', 'recovery_takeover', 'reset'] as const;
export const TASK_STATUSES = [
  'open',
  'claimed',
  'submitted',
  'completed',
  'expired',
  'cancelled',
] as const;

export const CONTRIBUTOR_KINDS = ['contributor', 'dataset_seller'] as const;

export const EPISODE_KINDS = [
  'baseline',
  'eval',
  'clean_teleop',
  'recovery_takeover',
  'purchased',
] as const;
export const REVIEW_STATUSES = ['pending', 'accepted', 'rejected', 'not_reviewed'] as const;

export const PAYMENT_STATUSES = [
  'pending',
  'paid',
  'failed',
  'reversed',
  'blocked_budget',
  'awaiting_onboarding',
] as const;

export const DATASET_OFFER_STATUSES = [
  'proposed',
  'accepted',
  'ingesting',
  'qa',
  'done',
  'rejected',
  'cancelled',
] as const;

export const TRAINING_STATUSES = ['queued', 'running', 'done', 'failed', 'cancelled'] as const;
export const EVAL_KINDS = ['baseline', 'candidate', 'ab_live'] as const;
export const EVAL_STATUSES = ['queued', 'running', 'done', 'failed', 'cancelled'] as const;

export const AGENT_ROLES = [
  'planner',
  'reviewer',
  'dispatcher',
  'onboarding_droid',
  'data_droid',
  'training_droid',
] as const;
// TDM-13: starting (row created, Factory session not yet confirmed) and
// resuming (agent_resume claimed the wake-up; native resume or restart in flight).
export const AGENT_STATUSES = [
  'starting',
  'running',
  'paused',
  'waiting_for',
  'resuming',
  'done',
  'failed',
] as const;

export const ROBOT_COMMAND_TYPES = [
  'run_baseline',
  'run_eval',
  'start_teleop',
  'stop_teleop',
  'pause_for_takeover',
  'handback',
  'reset_request',
  'load_checkpoint',
  'stop',
  // TDM-13 additions
  'resume_policy',
  'home',
  'soft_estop', // always accepted; preempts every open command on the robot
  'trial_outcome', // TDM-51: operator verdict {success, note?, eval_run_id?, index?} for the open eval trial
] as const;
// preempted (TDM-13): closed by a soft_estop before it finished; never run it.
export const ROBOT_COMMAND_STATUSES = ['issued', 'acked', 'done', 'failed', 'preempted'] as const;

// Example prices from CONTEXT.md / docs/stripe-plan.md PRICES_CENTS.
export const DEFAULT_PRICES_CENTS = {
  reset: 200n,
  recovery_takeover: 500n,
  clean_teleop: 1000n,
} as const;

// run_settings defaults (Architecture note: eval_trials=10, threshold=0.8,
// point estimate by default because 10 trials cannot push ci_low >= 0.8).
export const DEFAULT_RUN_SETTINGS = {
  eval_trials: 10,
  success_threshold: 0.8,
  require_ci_low: false,
  target_episodes: 5,
  max_iterations: 3,
  hyperparams_json: JSON.stringify({
    lr: 2.5e-5,
    steps: 2000,
    batch_size: 32,
    lora_rank: 16,
    prompt: '',
  }),
} as const;

/** Throws a SenderError unless value is one of the allowed strings. */
export function oneOf<T extends readonly string[]>(
  field: string,
  value: string,
  allowed: T
): T[number] {
  if (!(allowed as readonly string[]).includes(value)) {
    throw new SenderError(`${field} must be one of ${allowed.join('|')}, got "${value}"`);
  }
  return value as T[number];
}
