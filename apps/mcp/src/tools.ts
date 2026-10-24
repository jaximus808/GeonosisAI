// The connector's MCP tools. Each delegates to the orchestrator (or, for approve_spend on a plan
// and list_robots, to SpacetimeDB as this server's own identity). Errors come back as tool
// results with isError so Claude can relay them or ask the user, never as protocol errors.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

import { BackendError, type OrchestratorClient, type SpacetimeHttp } from "./backend.ts";
import { reportMarkdown, summarizeTicket, usd } from "./summarize.ts";

/** Actor recorded for every human action taken through this connector. */
export const MCP_HUMAN_ID = "via_mcp";
export const MCP_ACTOR = `human:${MCP_HUMAN_ID}`;

export interface ToolDeps {
  orchestrator: OrchestratorClient;
  spacetime: SpacetimeHttp;
  maxSpendCents: number;
  log?: (event: string, fields: Record<string, unknown>) => void;
}

type Row = Record<string, any>;

const ticketId = z.number().int().positive().describe("Ticket id, as returned by create_ticket (e.g. 12).");

function text(t: string, extra?: unknown): CallToolResult {
  const content: CallToolResult["content"] = [{ type: "text", text: t }];
  if (extra !== undefined) content.push({ type: "text", text: "```json\n" + JSON.stringify(extra, null, 2) + "\n```" });
  return { content };
}

function fail(t: string, extra?: unknown): CallToolResult {
  return { ...text(t, extra), isError: true };
}

function backendFail(e: unknown, what: string): CallToolResult {
  if (e instanceof BackendError) {
    const d = e.detail && typeof e.detail === "object" && !Array.isArray(e.detail) ? (e.detail as Row) : null;
    const hints: Row = {};
    if (d?.robots) hints.robots = d.robots;
    if (d?.parsed) hints.parsed = d.parsed;
    let msg = `${what} failed (${e.code}): ${e.message}`;
    if (e.code === "budget_required") msg += "\nAsk the user how much the factory may spend (data plus training), then call create_ticket again with budget_cents.";
    if (e.code === "multiple_online_robots" || e.code === "no_online_robot" || e.code === "unknown_robot") msg += "\nAsk the user which robot to use, then pass robot_id.";
    return fail(msg, Object.keys(hints).length ? hints : undefined);
  }
  return fail(`${what} failed: ${(e as Error).message ?? String(e)}`);
}

export function buildServer(deps: ToolDeps): McpServer {
  const { orchestrator, spacetime, maxSpendCents } = deps;
  const log = deps.log ?? (() => {});
  const server = new McpServer(
    { name: "robot-deployment-factory", version: "0.1.0" },
    {
      instructions:
        "Robot Deployment Factory: give it a robot and a task, and it buys demonstration data, fine-tunes a policy and " +
        "evaluates it until the robot meets a reliability target within a budget. When the user says \"set up the harness " +
        "for <robot/task>\", call create_ticket. Use ticket_status to check progress, approve_spend only with an amount the " +
        "user explicitly stated, and get_report once a ticket has shipped. Money amounts are integer US cents (test mode).",
    },
  );

  // ------------------------------------------------------------------ create_ticket
  server.registerTool(
    "create_ticket",
    {
      title: "Create a robot deployment ticket",
      description:
        "Start a new job: set up the harness for a robot and task. The prompt is plain English, e.g. \"Put the cup in the " +
        "bin, 80% reliable, $50 budget\"; the orchestrator turns it into a ticket with run settings. A budget is required " +
        "(in the prompt or as budget_cents); if the user gave none, ask before calling. A missing reliability target " +
        "defaults to 80%. The robot defaults to the only online robot; if several are online the call fails and lists them. " +
        "Returns the new ticket id, the parsed task, budget and settings.",
      inputSchema: {
        prompt: z.string().min(1).max(4000).describe("The user's request in their words: task, reliability target, budget."),
        robot_id: z.number().int().positive().optional().describe("Robot to use (see list_robots). Omit to use the only online robot."),
        budget_cents: z.number().int().min(0).optional().describe("Spending cap in US cents (e.g. 5000 = $50). Overrides any budget in the prompt."),
        target_success: z.number().gt(0).lte(1).optional().describe("Required success rate as a fraction (0.8 = 80%). Overrides the prompt."),
        settings: z
          .record(z.unknown())
          .optional()
          .describe("Optional run-setting overrides: eval_trials, success_threshold, require_ci_low, target_episodes, max_iterations, hyperparams {lr, steps, batch_size, lora_rank, prompt}."),
      },
      annotations: { title: "Create ticket", readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    },
    async ({ prompt, robot_id, budget_cents, target_success, settings }) => {
      const body: Row = { prompt };
      if (robot_id !== undefined) body.robot_id = robot_id;
      if (budget_cents !== undefined) body.budget_cents = budget_cents;
      if (target_success !== undefined) body.target_success = target_success;
      if (settings !== undefined) body.settings_overrides = settings;
      try {
        const r = await orchestrator.createTicket(body);
        log("tool.create_ticket", { ticket_id: r.ticket_id, robot_id: r.robot?.id });
        const rs: Row = r.run_settings ?? {};
        const t: Row = r.ticket ?? {};
        const lines = [
          `Created ticket #${r.ticket_id}: ${t.task_description}`,
          `Robot: ${r.robot?.name} #${r.robot?.id} (${r.robot_selection === "only_online_robot" ? "the only online robot" : "as requested"})`,
          `Target: ${Math.round(Number(t.target_success) * 100)}% success over ${rs.eval_trials ?? "?"} eval trials`,
          `Budget: ${usd(t.budget_cents)} (nothing is spent until a data plan is approved)`,
          `Policy prompt: ${rs.hyperparams?.prompt ?? r.parsed?.policy_prompt ?? "?"}`,
        ];
        if (r.defaults_applied?.length) lines.push(`Defaults applied: ${r.defaults_applied.join(", ")}`);
        lines.push(`Status: ${t.status}. Check progress with ticket_status(ticket_id=${r.ticket_id}).`);
        return text(lines.join("\n"), { ticket_id: r.ticket_id, robot: r.robot, run_settings: r.run_settings, defaults_applied: r.defaults_applied });
      } catch (e) {
        return backendFail(e, "create_ticket");
      }
    },
  );

  // ------------------------------------------------------------------ ticket_status
  server.registerTool(
    "ticket_status",
    {
      title: "Ticket status",
      description:
        "Where a ticket stands: lifecycle stage and iteration, the latest eval (success rate with 95% confidence interval), " +
        "the baseline, budget/approved/spent money, open data plans, and what it is waiting on (often the user's spend approval).",
      inputSchema: { ticket_id: ticketId },
      annotations: { title: "Ticket status", readOnlyHint: true, openWorldHint: true },
    },
    async ({ ticket_id }) => {
      try {
        const [agg, fw] = await Promise.all([orchestrator.getTicket(ticket_id), orchestrator.getFlywheel(ticket_id).catch(() => null)]);
        const s = summarizeTicket(agg, fw);
        return text(s.text, s.data);
      } catch (e) {
        return backendFail(e, "ticket_status");
      }
    },
  );

  // ------------------------------------------------------------------ approve_spend
  server.registerTool(
    "approve_spend",
    {
      title: "Approve spend (test-mode money)",
      description:
        "Authorize money on a ticket. Two modes, exactly one per call:\n" +
        "1. Approve a data plan: data_plan_id + approved_cents. The factory may then spend up to approved_cents buying the " +
        "plan's episodes. Total approved spend on a ticket can never exceed its budget.\n" +
        "2. Raise the budget: additional_budget_cents, added to the ticket's current budget.\n" +
        "Only call this with an amount the user explicitly stated or confirmed in this conversation; never infer or round " +
        "up an amount, and confirm the plan id with ticket_status first. Amounts are integer US cents (2500 = $25). " +
        `A single call is capped at ${usd(maxSpendCents)}. Logged as ${MCP_ACTOR}.`,
      inputSchema: {
        ticket_id: ticketId,
        data_plan_id: z.number().int().positive().optional().describe("Mode 1: the data plan to approve (from ticket_status)."),
        approved_cents: z.number().int().positive().optional().describe("Mode 1: the exact amount the user approved for that plan, in cents."),
        additional_budget_cents: z.number().int().positive().optional().describe("Mode 2: how much to add to the ticket budget, in cents."),
      },
      annotations: { title: "Approve spend", readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: true },
    },
    async ({ ticket_id, data_plan_id, approved_cents, additional_budget_cents }) => {
      const planMode = data_plan_id !== undefined || approved_cents !== undefined;
      const budgetMode = additional_budget_cents !== undefined;
      if (planMode === budgetMode) {
        return fail("approve_spend needs exactly one mode: (data_plan_id and approved_cents) to approve a plan, or additional_budget_cents to raise the budget.");
      }
      if (planMode && (data_plan_id === undefined || approved_cents === undefined)) {
        return fail("Approving a data plan needs both data_plan_id and an explicit approved_cents. Ask the user for the exact amount.");
      }
      const amount = (planMode ? approved_cents : additional_budget_cents) as number;
      if (amount > maxSpendCents) {
        return fail(`Refused: ${usd(amount)} is above this connector's per-call cap of ${usd(maxSpendCents)}. Approve it on the dashboard, or split it with the user's explicit agreement.`);
      }

      if (budgetMode) {
        try {
          const before = await orchestrator.getSettings(ticket_id);
          const oldBudget = Number(before.budget_cents);
          const after = await orchestrator.patchSettings(ticket_id, { budget_cents: oldBudget + amount }, MCP_HUMAN_ID);
          log("tool.approve_spend.budget", { ticket_id, added_cents: amount, budget_cents: after.budget_cents });
          return text(
            `Raised ticket #${ticket_id}'s budget by ${usd(amount)}: ${usd(oldBudget)} -> ${usd(after.budget_cents)}. ` +
              `Approved so far ${usd(after.ticket?.approved_cents)}, spent ${usd(after.ticket?.spent_cents)}. Logged as ${MCP_ACTOR}` +
              (after.audit_logged === false ? " (warning: the audit row failed to write)." : "."),
            { ticket_id, mode: "budget_increase", added_cents: amount, previous_budget_cents: oldBudget, budget_cents: after.budget_cents, actor: MCP_ACTOR },
          );
        } catch (e) {
          return backendFail(e, "approve_spend (budget increase)");
        }
      }

      try {
        const agg = await orchestrator.getTicket(ticket_id);
        const plans: Row[] = agg.data_plans ?? [];
        const plan = plans.find((p) => Number(p.id) === data_plan_id);
        if (!plan) {
          return fail(`Ticket #${ticket_id} has no data plan #${data_plan_id}.`, { data_plans: plans.map((p) => ({ id: p.id, status: p.status, data_type: p.data_type, amount: p.amount, est_cost_cents: p.est_cost_cents })) });
        }
        await spacetime.callReducer("approve_spend", [ticket_id, data_plan_id, amount, MCP_ACTOR]);
        const after = await orchestrator.getTicket(ticket_id);
        const p2: Row = (after.data_plans ?? []).find((p: Row) => Number(p.id) === data_plan_id) ?? {};
        log("tool.approve_spend.plan", { ticket_id, data_plan_id, approved_cents: p2.approved_cents });
        const t: Row = after.ticket ?? {};
        return text(
          `Approved ${usd(p2.approved_cents)} for data plan #${data_plan_id} on ticket #${ticket_id} ` +
            `(${plan.amount} ${plan.data_type} episodes, estimated ${usd(plan.est_cost_cents)}; previously approved ${usd(plan.approved_cents)}). ` +
            `Plan status: ${p2.status}. Ticket approved spend is now ${usd(t.approved_cents)} of a ${usd(t.budget_cents)} budget (spent ${usd(t.spent_cents)}). ` +
            `Logged as ${MCP_ACTOR}. Stripe is in test mode.`,
          {
            ticket_id,
            mode: "plan_approval",
            data_plan_id,
            requested_cents: amount,
            approved_cents: p2.approved_cents,
            previous_approved_cents: plan.approved_cents,
            plan_status: p2.status,
            approved_by: p2.approved_by,
            ticket_approved_cents: t.approved_cents,
            budget_cents: t.budget_cents,
            actor: MCP_ACTOR,
          },
        );
      } catch (e) {
        return backendFail(e, "approve_spend");
      }
    },
  );

  // ------------------------------------------------------------------ get_report
  server.registerTool(
    "get_report",
    {
      title: "Get the ticket report",
      description:
        "The final report for a ticket as markdown: what data was bought and why, costs (data and GPU), before/after success " +
        "rates with confidence intervals, and the deployed checkpoint. Usually ready once the ticket has shipped; otherwise " +
        "this says it is not ready and gives the current status.",
      inputSchema: { ticket_id: ticketId },
      annotations: { title: "Get report", readOnlyHint: true, openWorldHint: true },
    },
    async ({ ticket_id }) => {
      try {
        const r = await orchestrator.getReport(ticket_id);
        const md = reportMarkdown(r.body, r.contentType);
        const status = (r.body as Row | null)?.task?.status ?? (r.body as Row | null)?.outcome?.status;
        const interim = typeof status === "string" && status !== "shipped"
          ? `Interim report: ticket #${ticket_id} is still in "${status}", so the after-training results may be missing.\n\n`
          : "";
        if (md) return text(interim + md);
        const b = (r.body ?? {}) as Row;
        return text(`${interim}The report for ticket #${ticket_id} has no markdown rendering; key fields follow.`, {
          task: b.task, outcome: b.outcome, baseline: b.baseline?.summary, final: b.final, total_cost: b.total_cost,
          deployed_checkpoint: b.deployed_checkpoint, fake: b.fake?.banner, missing: b.missing,
        });
      } catch (e) {
        if (!(e instanceof BackendError) || ![404, 409, 425, 503].includes(e.status)) return backendFail(e, "get_report");
        // Unknown ticket, or no report yet (route not deployed, or the ticket has not shipped).
        try {
          const agg = await orchestrator.getTicket(ticket_id);
          const t: Row = agg.ticket ?? {};
          return text(
            `Report not ready for ticket #${ticket_id} (status: ${t.status}, iteration ${t.iteration ?? 0}). ` +
              (e.code === "http_404" && e.message === "Not Found"
                ? "The orchestrator does not serve reports yet."
                : `The orchestrator said: ${e.message}.`) +
              " Use ticket_status for live progress.",
          );
        } catch (e2) {
          return backendFail(e2, "get_report");
        }
      }
    },
  );

  // ------------------------------------------------------------------ list_robots
  server.registerTool(
    "list_robots",
    {
      title: "List robots",
      description: "Robots registered with the factory (id, name, embodiment, status). Use it to pick robot_id for create_ticket.",
      inputSchema: {},
      annotations: { title: "List robots", readOnlyHint: true, openWorldHint: true },
    },
    async () => {
      try {
        const rows = await spacetime.sql("SELECT id, name, embodiment, status FROM robot");
        const robots = rows.filter((r) => !String(r.name).startsWith("test."));
        if (!robots.length) return text("No robots are registered (test robots hidden).");
        return text(robots.map((r) => `#${r.id} ${r.name} (${r.embodiment}): ${r.status}`).join("\n"), robots);
      } catch (e) {
        return backendFail(e, "list_robots");
      }
    },
  );

  return server;
}
