// Pure formatting for chat: GET /tickets/{id} (+ /flywheel/tickets/{id}) -> a short status
// summary Claude can relay. Every number comes from the orchestrator's rows; nothing is estimated.

type Row = Record<string, any>;

export const TERMINAL_STATUSES = ["shipped", "failed", "cancelled"] as const;

const STAGE_ACTIVITY: Record<string, string> = {
  created: "the ticket was just created; onboarding starts next",
  onboarding: "onboarding the robot (adapter, cameras, success check, reset routine)",
  baseline: "running the baseline eval of the base policy on the robot",
  diagnose: "clustering the baseline failures (Claude)",
  plan_data: "the planner is choosing what data to buy",
  dispatch: "contributors are collecting episodes",
  review: "the reviewer is checking submitted episodes",
  train: "fine-tuning the policy on Modal",
  eval: "evaluating the new checkpoint on the robot",
};

export function usd(cents: unknown): string {
  const n = Number(cents ?? 0);
  if (!Number.isFinite(n)) return "$?";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${(Math.abs(n) / 100).toFixed(2)}`;
}

export function pct(fraction: unknown, digits = 0): string {
  const n = Number(fraction);
  return Number.isFinite(n) ? `${(n * 100).toFixed(digits)}%` : "?";
}

export interface EvalSummary {
  id: number;
  kind: string;
  status: string;
  trials: number;
  completed: number;
  successes: number;
  success_rate: number | null;
  ci_low: number | null;
  ci_high: number | null;
  text: string;
}

export function summarizeEval(e: Row | null | undefined): EvalSummary | null {
  if (!e) return null;
  const trials = Number(e.trials ?? 0);
  const completed = Number(e.completed ?? 0);
  const successes = Number(e.successes ?? 0);
  const rate = completed > 0 ? successes / completed : null;
  // ci_low/ci_high are 0/0 until the run is finalized; only show a CI that was actually computed.
  const hasCi = Number(e.ci_high ?? 0) > 0 || Number(e.ci_low ?? 0) > 0;
  const ciLow = hasCi ? Number(e.ci_low) : null;
  const ciHigh = hasCi ? Number(e.ci_high) : null;
  let text = `${e.kind} eval #${e.id} (${e.status}): `;
  if (completed === 0) text += `0 of ${trials} trials run yet`;
  else {
    text += `${successes}/${completed} succeeded = ${pct(rate)}`;
    if (completed < trials) text += ` (${completed} of ${trials} trials so far)`;
    if (hasCi) text += `, 95% CI ${pct(ciLow)}–${pct(ciHigh)}`;
  }
  return { id: Number(e.id), kind: String(e.kind), status: String(e.status), trials, completed, successes, success_rate: rate, ci_low: ciLow, ci_high: ciHigh, text };
}

/** {"table":"dataset","field":"accepted_count","op":">=","value":5} -> "dataset.accepted_count >= 5". */
export function describePredicate(p: unknown): string {
  if (p == null) return "an external event";
  if (typeof p === "string") {
    try {
      return describePredicate(JSON.parse(p));
    } catch {
      return p.slice(0, 160);
    }
  }
  if (typeof p !== "object") return String(p);
  const o = p as Row;
  if (Array.isArray(o.all)) return o.all.map(describePredicate).join(" and ");
  if (Array.isArray(o.any)) return o.any.map(describePredicate).join(" or ");
  if (o.table && o.field && o.op) return `${o.table}.${o.field} ${o.op} ${JSON.stringify(o.value)}`;
  return JSON.stringify(o).slice(0, 160);
}

function unwrapOption(v: unknown): unknown {
  // SpacetimeDB options may arrive as {"some": x} / {"none": []}; rdf_common usually unwraps them.
  if (v && typeof v === "object" && !Array.isArray(v)) {
    if ("some" in (v as Row)) return (v as Row).some;
    if ("none" in (v as Row)) return null;
  }
  return v;
}

export interface WaitingOn {
  who: "you" | "robot" | "contributors" | "agent" | "orchestrator" | "nobody";
  text: string;
}

export function waitingOn(agg: Row, flywheel?: Row | null): WaitingOn[] {
  const t: Row = agg.ticket ?? {};
  const status = String(t.status ?? "unknown");
  const out: WaitingOn[] = [];

  if (status === "shipped") {
    const ckpt = unwrapOption(t.deployed_checkpoint_uri);
    return [{ who: "nobody", text: `Shipped${ckpt ? ` (deployed checkpoint ${ckpt})` : ""}. Use get_report for the write-up.` }];
  }
  if (status === "failed" || status === "cancelled") return [{ who: "nobody", text: `The ticket is ${status}; nothing else will run.` }];

  for (const p of (agg.data_plans ?? []) as Row[]) {
    if (p.status === "proposed" && Number(p.approved_cents ?? 0) === 0) {
      out.push({
        who: "you",
        text:
          `Spend approval for data plan #${p.id}: ${p.amount} ${p.data_type} episodes at ${usd(p.unit_price_cents)} each ` +
          `(estimated ${usd(p.est_cost_cents)}). Approve with approve_spend(ticket_id=${t.id}, data_plan_id=${p.id}, approved_cents=<amount the user agrees to>).`,
      });
    }
  }

  const blocked = Number(agg.spend?.payments_by_status_cents?.blocked_budget ?? 0);
  if (blocked > 0) {
    out.push({ who: "you", text: `${usd(blocked)} of contributor payouts are blocked by the budget cap. Raising the budget (approve_spend with additional_budget_cents) unblocks them.` });
  }

  for (const s of (agg.agent_sessions ?? []) as Row[]) {
    if (s.status === "waiting_for") {
      out.push({ who: "agent", text: `${s.role} agent "${s.name}" is waiting for ${describePredicate(unwrapOption(s.waiting_for_json))}.` });
    }
  }

  const ev = agg.latest_eval_run as Row | null;
  if (ev && (ev.status === "running" || ev.status === "queued")) {
    out.push({ who: "robot", text: `The robot is running ${ev.kind} eval #${ev.id} (${ev.completed ?? 0}/${ev.trials ?? "?"} trials done).` });
  }

  const reason = flywheel?.decision?.reason;
  if (typeof reason === "string" && reason.trim() && !out.some((w) => w.who === "you" && reason.includes("approve_spend"))) {
    out.push({ who: "orchestrator", text: reason.trim() });
  }

  if (out.length === 0) {
    const activity = STAGE_ACTIVITY[status];
    out.push({ who: status === "dispatch" ? "contributors" : status === "baseline" || status === "eval" ? "robot" : "orchestrator", text: activity ? `Nothing from you: ${activity}.` : `Stage "${status}".` });
  }
  return out;
}

export interface TicketStatusSummary {
  text: string;
  data: Row;
}

export function summarizeTicket(agg: Row, flywheel?: Row | null): TicketStatusSummary {
  const t: Row = agg.ticket ?? {};
  const rs: Row = agg.run_settings ?? {};
  const spend: Row = agg.spend ?? {};
  const robot: Row | null = agg.robot ?? null;
  const evals: Row[] = agg.eval_runs ?? [];
  const latest = summarizeEval(agg.latest_eval_run);
  const baselineRow = evals.find((e) => e.kind === "baseline") ?? null;
  const baseline = baselineRow && baselineRow.id !== agg.latest_eval_run?.id ? summarizeEval(baselineRow) : null;
  const target = Number(rs.success_threshold ?? t.target_success);
  const maxIter = rs.max_iterations ?? flywheel?.max_iterations;
  const waits = waitingOn(agg, flywheel);

  const lines: string[] = [];
  lines.push(`Ticket #${t.id}: ${t.task_description}${robot ? ` (robot ${robot.name} #${robot.id}, ${robot.status})` : ""}`);
  lines.push(`Status: ${t.status}, iteration ${t.iteration ?? 0}${maxIter != null ? ` of max ${maxIter}` : ""}`);
  if (Number.isFinite(target)) {
    lines.push(`Target: ${pct(target)} success${rs.require_ci_low ? " (CI lower bound must clear it)" : ""} over ${rs.eval_trials ?? "?"} eval trials`);
  }
  lines.push(`Latest eval: ${latest ? latest.text : "none yet"}`);
  if (baseline) lines.push(`Baseline: ${baseline.text}`);
  const paid = spend.payments_by_status_cents ?? {};
  const payParts = Object.entries(paid).map(([k, v]) => `${k} ${usd(v)}`);
  lines.push(
    `Spend: budget ${usd(spend.budget_cents)}, approved ${usd(spend.approved_cents)}, spent ${usd(spend.spent_cents)}, remaining ${usd(spend.remaining_cents)}` +
      (payParts.length ? ` (payouts: ${payParts.join(", ")})` : ""),
  );
  if (agg.dataset) lines.push(`Data: ${agg.dataset.accepted_count} accepted of ${agg.dataset.total_count} episodes`);
  const openPlans = ((agg.data_plans ?? []) as Row[]).filter((p) => p.status !== "rejected" && p.status !== "done");
  for (const p of openPlans) {
    lines.push(`Data plan #${p.id} (${p.status}): ${p.amount} ${p.data_type} at ${usd(p.unit_price_cents)} each, est ${usd(p.est_cost_cents)}, approved ${usd(p.approved_cents)}`);
  }
  lines.push("Waiting on:");
  for (const w of waits) lines.push(`- [${w.who}] ${w.text}`);

  return {
    text: lines.join("\n"),
    data: {
      ticket_id: t.id,
      status: t.status,
      iteration: t.iteration ?? 0,
      max_iterations: maxIter ?? null,
      target_success: Number.isFinite(target) ? target : null,
      latest_eval: latest,
      baseline_eval: baseline,
      spend: {
        budget_cents: spend.budget_cents ?? null,
        approved_cents: spend.approved_cents ?? null,
        spent_cents: spend.spent_cents ?? null,
        remaining_cents: spend.remaining_cents ?? null,
        payments_by_status_cents: paid,
      },
      open_data_plans: openPlans.map((p) => ({ id: p.id, status: p.status, data_type: p.data_type, amount: p.amount, est_cost_cents: p.est_cost_cents, approved_cents: p.approved_cents })),
      waiting_on: waits,
    },
  };
}

/** Pull the markdown out of whatever GET /tickets/{id}/report returns (TDM-71 is in flight). */
export function reportMarkdown(body: unknown, contentType = ""): string | null {
  if (typeof body === "string") return contentType.includes("json") ? null : body.trim() || null;
  if (!body || typeof body !== "object") return null;
  const o = body as Row;
  for (const k of ["markdown", "report_markdown", "md"]) {
    if (typeof o[k] === "string" && o[k].trim()) return o[k];
  }
  if (o.report && typeof o.report === "object") return reportMarkdown(o.report, contentType);
  return null;
}
