// End to end inside one process: SDK client -> our Streamable HTTP server -> a fake orchestrator
// and fake SpacetimeDB (one node:http server recording every call).
import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, it } from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { OrchestratorClient, SpacetimeHttp } from "../src/backend.ts";
import { createMcpHttpServer } from "../src/http.ts";
import { aggregate, proposedPlan } from "./fixtures.ts";

const TOKEN = "test-token-0123456789abcdef";
const calls: { method: string; path: string; body: string; headers: Record<string, unknown> }[] = [];
let state = aggregate({ data_plans: [{ ...proposedPlan }] });
let reportRoute = false;

function fakeBackend(): Server {
  return createServer(async (req, res) => {
    let body = "";
    for await (const c of req) body += c;
    calls.push({ method: req.method!, path: req.url!, body, headers: req.headers });
    const json = (s: number, b: unknown) => res.writeHead(s, { "content-type": "application/json" }).end(JSON.stringify(b));
    const url = req.url!;
    if (req.method === "POST" && url === "/tickets") {
      const b = JSON.parse(body);
      if (b.budget_cents === undefined) return json(422, { detail: { code: "budget_required", message: "How much may the factory spend?", parsed: { task_description: "Put the cup in the bin" } } });
      return json(201, { ticket_id: 7, robot: state.robot, robot_selection: "only_online_robot", ticket: { ...state.ticket, status: "created", budget_cents: b.budget_cents }, run_settings: state.run_settings, parsed: { policy_prompt: "put the cup in the bin" }, defaults_applied: ["target_success=0.8"] });
    }
    if (req.method === "GET" && url === "/tickets/7") return json(200, state);
    if (req.method === "GET" && url === "/tickets/99") return json(404, { detail: { code: "ticket_not_found", message: "No ticket 99." } });
    if (req.method === "GET" && url === "/flywheel/tickets/7") return json(200, { max_iterations: 3, decision: { to: null, reason: "waiting for approve_spend on data plan 4" } });
    if (req.method === "GET" && url === "/tickets/7/settings") return json(200, { ticket_id: 7, budget_cents: state.ticket.budget_cents, ticket: state.ticket });
    if (req.method === "PATCH" && url === "/tickets/7/settings") {
      state.ticket.budget_cents = JSON.parse(body).budget_cents;
      return json(200, { ticket_id: 7, budget_cents: state.ticket.budget_cents, ticket: state.ticket, audit_logged: true });
    }
    if (req.method === "GET" && url === "/tickets/7/report?include_markdown=true") {
      return reportRoute ? json(200, { task: { status: state.ticket.status }, markdown: "# Ticket 7 report\nBaseline 30% -> 70%" }) : json(404, { detail: "Not Found" });
    }
    if (req.method === "POST" && url === "/v1/database/testdb/call/approve_spend") {
      const [tid, pid, cents, by] = JSON.parse(body);
      if (cents > state.ticket.budget_cents) return res.writeHead(530).end(`approving ${cents} brings ticket ${tid} approved spend to ${cents} > budget_cents ${state.ticket.budget_cents}`);
      const p = state.data_plans.find((x: any) => x.id === pid);
      Object.assign(p, { approved_cents: cents, approved_by: by, status: "approved" });
      state.ticket.approved_cents = cents;
      return res.writeHead(200).end();
    }
    if (req.method === "POST" && url === "/v1/database/testdb/sql") {
      return json(200, [{ schema: { elements: [{ name: { some: "id" } }, { name: { some: "name" } }, { name: { some: "embodiment" } }, { name: { some: "status" } }] }, rows: [[1, "fake-yam-1", "i2rt_yam", "online"], [2, "test.tdm19-yam", "i2rt_yam", "offline"]] }]);
    }
    json(404, { detail: "Not Found" });
  });
}

async function listen(s: Server): Promise<string> {
  await new Promise<void>((r) => s.listen(0, "127.0.0.1", r));
  return `http://127.0.0.1:${(s.address() as AddressInfo).port}`;
}

let backend: Server, mcp: Server, mcpUrl: string, client: Client;

async function call(name: string, args: Record<string, unknown>) {
  const r = (await client.callTool({ name, arguments: args })) as { content: { type: string; text: string }[]; isError?: boolean };
  return { text: r.content.map((c) => c.text).join("\n"), isError: Boolean(r.isError) };
}

before(async () => {
  backend = fakeBackend();
  const backendUrl = await listen(backend);
  mcp = createMcpHttpServer({
    token: TOKEN,
    deps: { orchestrator: new OrchestratorClient(backendUrl), spacetime: new SpacetimeHttp(backendUrl, "testdb", "stdb-secret"), maxSpendCents: 50_000 },
  });
  mcpUrl = (await listen(mcp)) + "/mcp";
  client = new Client({ name: "tdm80-test", version: "0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(mcpUrl), { requestInit: { headers: { authorization: `Bearer ${TOKEN}` } } }));
});

after(async () => {
  await client.close();
  mcp.close();
  backend.close();
});

describe("auth", () => {
  it("rejects a missing or wrong bearer token", async () => {
    const init = { method: "POST", headers: { "content-type": "application/json", accept: "application/json, text/event-stream" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) };
    const none = await fetch(mcpUrl, init);
    assert.equal(none.status, 401);
    assert.match(none.headers.get("www-authenticate") ?? "", /Bearer/);
    const wrong = await fetch(mcpUrl, { ...init, headers: { ...init.headers, authorization: "Bearer nope" } });
    assert.equal(wrong.status, 401);
  });

  it("serves /healthz without auth", async () => {
    const r = await fetch(mcpUrl.replace("/mcp", "/healthz"));
    assert.equal(r.status, 200);
  });
});

describe("tools", () => {
  it("lists the four tools plus list_robots", async () => {
    const names = (await client.listTools()).tools.map((t) => t.name).sort();
    assert.deepEqual(names, ["approve_spend", "create_ticket", "get_report", "list_robots", "ticket_status"]);
  });

  it("create_ticket relays budget_required as a question for the user", async () => {
    const r = await call("create_ticket", { prompt: "put the cup in the bin" });
    assert.ok(r.isError);
    assert.match(r.text, /budget_required/);
    assert.match(r.text, /Ask the user how much/);
  });

  it("create_ticket posts to /tickets", async () => {
    const r = await call("create_ticket", { prompt: "put the cup in the bin, $50", budget_cents: 5000, settings: { eval_trials: 10 } });
    assert.ok(!r.isError, r.text);
    assert.match(r.text, /Created ticket #7/);
    const sent = JSON.parse(calls.filter((c) => c.path === "/tickets").at(-1)!.body);
    assert.deepEqual(sent, { prompt: "put the cup in the bin, $50", budget_cents: 5000, settings_overrides: { eval_trials: 10 } });
  });

  it("ticket_status summarises and points at the pending approval", async () => {
    const r = await call("ticket_status", { ticket_id: 7 });
    assert.match(r.text, /Status: plan_data, iteration 1 of max 3/);
    assert.match(r.text, /\[you\] Spend approval for data plan #4/);
    const missing = await call("ticket_status", { ticket_id: 99 });
    assert.ok(missing.isError);
    assert.match(missing.text, /ticket_not_found/);
  });

  it("approve_spend refuses ambiguous, implicit or oversized amounts without calling the backend", async () => {
    const before = calls.length;
    assert.match((await call("approve_spend", { ticket_id: 7 })).text, /exactly one mode/);
    assert.match((await call("approve_spend", { ticket_id: 7, data_plan_id: 4 })).text, /explicit approved_cents/);
    assert.match((await call("approve_spend", { ticket_id: 7, data_plan_id: 4, approved_cents: 100, additional_budget_cents: 100 })).text, /exactly one mode/);
    assert.match((await call("approve_spend", { ticket_id: 7, additional_budget_cents: 60_000 })).text, /per-call cap of \$500.00/);
    assert.equal(calls.length, before);
  });

  it("approve_spend relays the reducer's refusal", async () => {
    const r = await call("approve_spend", { ticket_id: 7, data_plan_id: 4, approved_cents: 9000 });
    assert.ok(r.isError);
    assert.match(r.text, /reducer_rejected.*> budget_cents 5000/);
  });

  it("approve_spend approves a plan as human:via_mcp with the server's token and echoes it", async () => {
    const r = await call("approve_spend", { ticket_id: 7, data_plan_id: 4, approved_cents: 3000 });
    assert.ok(!r.isError, r.text);
    assert.match(r.text, /Approved \$30.00 for data plan #4 on ticket #7/);
    assert.match(r.text, /approved spend is now \$30.00 of a \$50.00 budget/);
    const red = calls.find((c) => c.path.endsWith("/call/approve_spend") && JSON.parse(c.body)[2] === 3000)!;
    assert.deepEqual(JSON.parse(red.body), [7, 4, 3000, "human:via_mcp"]);
    assert.equal(red.headers.authorization, "Bearer stdb-secret");
    assert.ok(!r.text.includes("stdb-secret"));
  });

  it("approve_spend raises the budget through PATCH /settings with X-Human-Id", async () => {
    const r = await call("approve_spend", { ticket_id: 7, additional_budget_cents: 2500 });
    assert.ok(!r.isError, r.text);
    assert.match(r.text, /by \$25.00: \$50.00 -> \$75.00/);
    const patch = calls.filter((c) => c.method === "PATCH").at(-1)!;
    assert.deepEqual(JSON.parse(patch.body), { budget_cents: 7500 });
    assert.equal(patch.headers["x-human-id"], "via_mcp");
  });

  it("get_report says not ready until the route exists, then returns markdown", async () => {
    reportRoute = false;
    const r = await call("get_report", { ticket_id: 7 });
    assert.ok(!r.isError);
    assert.match(r.text, /Report not ready for ticket #7 \(status: plan_data/);
    reportRoute = true;
    assert.match((await call("get_report", { ticket_id: 7 })).text, /^Interim report: ticket #7 is still in "plan_data"[\s\S]*# Ticket 7 report/);
    state.ticket.status = "shipped";
    assert.match((await call("get_report", { ticket_id: 7 })).text, /^# Ticket 7 report/);
    state.ticket.status = "plan_data";
  });

  it("list_robots hides test robots", async () => {
    const r = await call("list_robots", {});
    assert.match(r.text, /#1 fake-yam-1 \(i2rt_yam\): online/);
    assert.ok(!r.text.includes("test.tdm19"));
  });
});
