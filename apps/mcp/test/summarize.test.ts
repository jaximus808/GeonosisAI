import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { describePredicate, reportMarkdown, summarizeEval, summarizeTicket, usd, waitingOn } from "../src/summarize.ts";
import { aggregate, baselineEval, proposedPlan } from "./fixtures.ts";

describe("usd / summarizeEval", () => {
  it("formats cents", () => {
    assert.equal(usd(5000), "$50.00");
    assert.equal(usd(5), "$0.05");
    assert.equal(usd(undefined), "$0.00");
  });

  it("reports rate and Wilson CI from rows", () => {
    const s = summarizeEval(baselineEval)!;
    assert.equal(s.success_rate, 0.3);
    assert.match(s.text, /3\/10 succeeded = 30%, 95% CI 11%–60%/);
  });

  it("does not invent a CI or rate before trials run", () => {
    const s = summarizeEval({ ...baselineEval, status: "running", completed: 0, successes: 0, ci_low: 0, ci_high: 0 })!;
    assert.equal(s.success_rate, null);
    assert.equal(s.ci_low, null);
    assert.match(s.text, /0 of 10 trials run yet/);
  });

  it("marks partial evals", () => {
    const s = summarizeEval({ ...baselineEval, kind: "candidate", status: "running", completed: 4, successes: 3, ci_low: 0, ci_high: 0 })!;
    assert.match(s.text, /3\/4 succeeded = 75% \(4 of 10 trials so far\)$/);
  });
});

describe("waitingOn", () => {
  it("asks the user to approve a proposed plan, with the call to make", () => {
    const w = waitingOn(aggregate({ data_plans: [proposedPlan] }));
    assert.equal(w[0].who, "you");
    assert.match(w[0].text, /data plan #4: 6 recovery_takeover episodes at \$5.00 each \(estimated \$30.00\)/);
    assert.match(w[0].text, /approve_spend\(ticket_id=7, data_plan_id=4, approved_cents=/);
  });

  it("flags payouts blocked by the budget", () => {
    const agg = aggregate({ ticket: { ...aggregate().ticket, status: "review" } });
    agg.spend.payments_by_status_cents = { paid: 1000, blocked_budget: 500 };
    const w = waitingOn(agg);
    assert.ok(w.some((x) => x.who === "you" && /\$5.00 of contributor payouts are blocked/.test(x.text)));
  });

  it("describes agent wait predicates", () => {
    const agg = aggregate({
      ticket: { ...aggregate().ticket, status: "dispatch" },
      agent_sessions: [{ id: 1, role: "dispatcher", name: "dispatch-7", status: "waiting_for", waiting_for_json: JSON.stringify({ all: [{ table: "dataset", ticket_id: 7, field: "accepted_count", op: ">=", value: 5 }, { table: "task", field: "status", op: "==", value: "completed" }] }) }],
    });
    const w = waitingOn(agg);
    assert.equal(w[0].who, "agent");
    assert.match(w[0].text, /waiting for dataset.accepted_count >= 5 and task.status == "completed"/);
  });

  it("reports a running eval and the flywheel's reason", () => {
    const running = { ...baselineEval, id: 9, kind: "candidate", status: "running", completed: 2 };
    const w = waitingOn(aggregate({ ticket: { ...aggregate().ticket, status: "eval" }, latest_eval_run: running }), { decision: { to: null, reason: "eval 9 still running (2/10)" } });
    assert.deepEqual(w.map((x) => x.who), ["robot", "orchestrator"]);
  });

  it("falls back to the stage when nothing is pending", () => {
    const w = waitingOn(aggregate({ ticket: { ...aggregate().ticket, status: "train" } }));
    assert.deepEqual(w, [{ who: "orchestrator", text: "Nothing from you: fine-tuning the policy on Modal." }]);
  });

  it("says nothing is pending once shipped", () => {
    const w = waitingOn(aggregate({ ticket: { ...aggregate().ticket, status: "shipped", deployed_checkpoint_uri: { some: "modal-vol://ckpt/7" } } }));
    assert.equal(w[0].who, "nobody");
    assert.match(w[0].text, /modal-vol:\/\/ckpt\/7/);
  });
});

describe("summarizeTicket", () => {
  it("summarises status, iteration, evals, spend and waiting-on", () => {
    const candidate = { id: 8, kind: "candidate", status: "done", trials: 10, completed: 10, successes: 7, ci_low: 0.3968, ci_high: 0.8922 };
    const agg = aggregate({ latest_eval_run: candidate, eval_runs: [baselineEval, candidate], data_plans: [proposedPlan] });
    agg.ticket.status = "plan_data";
    agg.spend = { budget_cents: 5000, approved_cents: 1000, spent_cents: 600, remaining_cents: 4400, payments_by_status_cents: { paid: 600 }, payment_count: 2 };
    const s = summarizeTicket(agg, { max_iterations: 3, decision: { reason: "waiting for approve_spend on data plan 4" } });
    assert.match(s.text, /^Ticket #7: Put the cup in the bin \(robot fake-yam-1 #1, online\)/);
    assert.match(s.text, /Status: plan_data, iteration 1 of max 3/);
    assert.match(s.text, /Target: 80% success over 10 eval trials/);
    assert.match(s.text, /Latest eval: candidate eval #8 \(done\): 7\/10 succeeded = 70%, 95% CI 40%–89%/);
    assert.match(s.text, /Baseline: baseline eval #3 \(done\): 3\/10 succeeded = 30%/);
    assert.match(s.text, /Spend: budget \$50.00, approved \$10.00, spent \$6.00, remaining \$44.00 \(payouts: paid \$6.00\)/);
    assert.match(s.text, /- \[you\] Spend approval for data plan #4/);
    // The flywheel reason duplicates the approval ask, so it is not repeated.
    assert.ok(!/\[orchestrator\]/.test(s.text));
    assert.equal(s.data.latest_eval.success_rate, 0.7);
    assert.equal(s.data.iteration, 1);
    assert.equal(s.data.spend.remaining_cents, 4400);
    assert.equal(s.data.open_data_plans[0].id, 4);
  });

  it("handles a fresh ticket with no evals", () => {
    const s = summarizeTicket(aggregate({ ticket: { ...aggregate().ticket, status: "created", iteration: 0 } }));
    assert.match(s.text, /Latest eval: none yet/);
    assert.equal(s.data.baseline_eval, null);
  });
});

describe("describePredicate / reportMarkdown", () => {
  it("handles junk predicates", () => {
    assert.equal(describePredicate("not json"), "not json");
    assert.equal(describePredicate(null), "an external event");
  });

  it("finds markdown in TDM-71 shaped bodies", () => {
    assert.equal(reportMarkdown({ markdown: "# R" }), "# R");
    assert.equal(reportMarkdown({ report: { report_markdown: "# R2" } }), "# R2");
    assert.equal(reportMarkdown("# plain", "text/markdown"), "# plain");
    assert.equal(reportMarkdown({ task: "x" }), null);
  });
});
