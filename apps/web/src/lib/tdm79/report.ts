// TDM-79: the parts of the TDM-71 report JSON (rdf-report-v1, services/orchestrator/.../report.py)
// the report page reads. Every number and derived string (ci_text, verdict, headline) comes from the
// orchestrator; this file only formats what is there and never fills a gap.

export type Ref = { event_id: number; ts: number; actor: string | null; type: string | null } | null;

export interface EvalView {
  eval_run_id: number;
  kind: string;
  status: string;
  checkpoint_uri: string | null;
  training_run_id: number;
  trials: number;
  completed: number;
  successes: number;
  rate: number | null;
  ci: [number | null, number | null] | null;
  ci_text: string | null;
  finished_at: number | null;
  finalized_event: Ref;
  trial_outcomes: { trial_index: number; success: boolean; episode_id: number; failure_note: string | null }[];
  fake_trials: number;
  seeded_trials?: number; // RDF_SEED_EVALS: outcomes drawn, not run
  is_deployed_checkpoint?: boolean;
}

export interface PlanView {
  data_plan_id: number;
  data_type: string;
  amount: number;
  unit_price_cents: number;
  est_cost_cents: number;
  reasoning: string | null;
  status: string;
  approved_cents: number;
  approved_by: string | null;
  approvals: { event_id: number; approved_by: string | null; approved_cents: number | null; via: string | null }[];
  planner_event: Ref;
  /** backend: api | droid | claude_code (absent on plans recorded before the switchable backend). */
  model: { backend?: string | null; backend_model?: string | null; requested: string | null; served: string | null; effort: string | null } | null;
  guardrail: { rule?: string | null; required_type?: string | null; why?: string } | null;
  fake: boolean;
}

export interface TrainingView {
  training_run_id: number;
  status: string;
  episodes: number;
  checkpoint_uri: string | null;
  base_checkpoint_uri: string | null;
  modal_call_id: string | null;
  gpu_cost_cents: number;
  hyperparams: Record<string, unknown> | null;
  duration_s: number | null;
  error: string | null;
  fake: boolean;
  stub?: string | null; // "[STUB] no GPU" when RDF_MODAL_STUB trained it
}

export interface IterationBlock {
  iteration: number;
  diagnosis: {
    event: Ref;
    model: string | null;
    backend?: string | null;
    reasoning: string | null;
    source_eval_run_id: number | null;
    clusters: { failure_cluster_id: number; label: string; count: number; description: string | null; fake: boolean }[];
  };
  data_plan: PlanView | null;
  tasks: { count: number; by_status: Record<string, number>; by_type: Record<string, number> };
  episodes_bought: number;
  episodes_accepted: number;
  episodes_rejected: { episode_id: number; kind: string; reason: string | null; reviewer: string | null }[];
  dataset_offers: { dataset_offer_id: number; status: string; price_per_episode_cents: number; accepted_count: number }[];
  cost_cents: { data: number; data_paid: number; gpu: number; total: number };
  training_run: TrainingView | null;
  eval: EvalView | null;
}

export interface TimelineItem {
  event_id: number;
  ts: number;
  actor: string | null;
  type: string;
  at: string | null;
  from?: string;
  to?: string;
  note?: string;
  [k: string]: unknown;
}

export interface Report {
  report_version: string;
  ticket_id: number;
  generated_at: number;
  fake: { any: boolean; banner: string | null; components: { component: string; fake_rows: number; total_rows: number }[] };
  mocked: { component: string; detail: string }[];
  task: {
    description: string | null;
    policy_prompt: string | null;
    target_success: number | null;
    budget_cents: number;
    status: string;
    iteration: number;
    created_at: number;
    run_settings: Record<string, unknown> | null;
  };
  robot: { id: number; name: string; embodiment: string; adapter_version: string; status: string; fake: boolean } | null;
  outcome: {
    status: string;
    iteration: number;
    target_success: number | null;
    success_threshold: number | null;
    require_ci_low: boolean | null;
    met_target: boolean | null;
    verdict: string;
    headline?: string;
    delta_rate?: number;
  };
  baseline: EvalView | null;
  final: EvalView | null;
  iterations: IterationBlock[];
  total_cost: {
    data: { committed_cents: number; paid_cents: number; pending_cents: number; not_spent_cents: number; payment_count: number; providers: string[] };
    gpu: { cents: number; runs: number };
    total_cents: number;
    budget_cents: number;
    approved_cents: number;
    note: string;
  };
  deployed_checkpoint: {
    uri: string | null;
    source: string;
    training_run_id: number | null;
    eval_run_id: number | null;
    fake: boolean;
    note?: string;
  } | null;
  timeline: TimelineItem[];
  event_log: {
    algorithm: string;
    count: number;
    first_id: number | null;
    last_id: number | null;
    gaps: [number, number][];
    hash: string;
    scope: string;
    ticket_span: { count: number; first_event_id: number | null; last_event_id: number | null; first_at: string | null; last_at: string | null };
  };
  event_log_hash: string;
  saved_reports: { event_id: number; uri: string | null; event_log_hash: string | null; fake: boolean | null }[];
  missing: string[];
}

export const usd = (cents: number | null | undefined) =>
  cents === null || cents === undefined ? 'n/a' : `$${(cents / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const pct = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : `${Math.round(x * 100)}%`);

export const DATA_TYPE_LABEL: Record<string, string> = {
  clean_teleop: 'Clean teleop demos',
  recovery_takeover: 'Recovery takeovers',
  purchased: 'Purchased episodes',
};

/** "2026-10-24T07:47:35Z" from micros, or from the iso string the report already carries. */
export function utc(micros: number | null | undefined): string {
  if (!micros) return 'n/a';
  return new Date(micros / 1000).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC');
}

export function clock(micros: number): string {
  return new Date(micros / 1000).toISOString().slice(11, 19);
}

/** One line per timeline event, built from the row's own fields. */
export function timelineText(t: TimelineItem): string {
  switch (t.type) {
    case 'ticket.created':
      return 'Ticket created';
    case 'ticket.status_changed':
      return `${String(t.from ?? '?').replace(/_/g, ' ')} to ${String(t.to ?? '?').replace(/_/g, ' ')}`;
    case 'ticket.run_settings_updated':
      return 'Run settings changed';
    case 'eval.finalized':
      return `Eval run ${t.eval_run_id ?? '?'} finalized`;
    case 'diagnose.clusters':
      return 'Failures clustered';
    case 'diagnose.no_failures':
      return 'No failures to cluster';
    case 'planner.data_plan':
      return `Data plan ${t.data_plan_id ?? '?'} written`;
    case 'planner.blocked':
      return 'Planner blocked';
    case 'planner.error':
      return 'Planner error';
    case 'spend.approved':
      return `Spend approved${t.approved_cents != null ? `: ${usd(Number(t.approved_cents))}` : ''}${t.approved_by ? ` by ${t.approved_by}` : ''}`;
    case 'flywheel.shipped':
      return 'Shipped';
    case 'report.saved':
      return 'Report saved';
    default:
      if (t.type === 'training.queued') return `Training run ${t.training_run_id ?? '?'} queued`;
      if (t.type === 'training.status') {
        return `Training run ${t.training_run_id ?? '?'} ${t.checkpoint_uri ? 'wrote its checkpoint' : 'status update'}`;
      }
      return t.type.replace(/\./g, ' ').replace(/_/g, ' ');
  }
}
