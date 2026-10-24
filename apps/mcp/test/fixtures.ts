// A GET /tickets/{id} aggregate shaped like services/orchestrator tickets_api.TicketsService.aggregate.
export function aggregate(over: Record<string, any> = {}): Record<string, any> {
  const base = {
    ticket: { id: 7, robot_id: 1, task_description: "Put the cup in the bin", target_success: 0.8, budget_cents: 5000, approved_cents: 0, spent_cents: 0, status: "plan_data", iteration: 1, dataset_id: 0, deployed_checkpoint_uri: null },
    robot: { id: 1, name: "fake-yam-1", status: "online", embodiment: "i2rt_yam" },
    run_settings: { ticket_id: 7, eval_trials: 10, success_threshold: 0.8, require_ci_low: false, budget_cents: 5000, target_episodes: 5, max_iterations: 3, hyperparams: { prompt: "put the cup in the bin" } },
    dataset: { id: 1, ticket_id: 7, accepted_count: 0, total_count: 0 },
    latest_eval_run: null as any,
    eval_runs: [] as any[],
    data_plans: [] as any[],
    spend: { budget_cents: 5000, approved_cents: 0, spent_cents: 0, remaining_cents: 5000, payments_by_status_cents: {}, payment_count: 0 },
    agent_sessions: [] as any[],
  };
  return { ...base, ...over };
}

export const baselineEval = { id: 3, ticket_id: 7, kind: "baseline", status: "done", trials: 10, completed: 10, successes: 3, ci_low: 0.1078, ci_high: 0.6032 };
export const proposedPlan = { id: 4, ticket_id: 7, iteration: 1, data_type: "recovery_takeover", amount: 6, unit_price_cents: 500, est_cost_cents: 3000, status: "proposed", approved_cents: 0 };
