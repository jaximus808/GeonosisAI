// TDM-81 end to end: sign in the way claude.ai does (OAuth: discovery, DCR, PKCE, operator consent),
// then "set up the harness": create_ticket from a prompt -> ticket_status -> approve_spend (raise the
// budget by $20) -> get_report, and check event_log for the human:via_mcp rows.
//
//   node apps/mcp/scripts/oauth-e2e.ts                        # MCP_URL default http://127.0.0.1:3002/mcp
//   MCP_URL=https://rdf-mcp.fly.dev/mcp node apps/mcp/scripts/oauth-e2e.ts
//   ... --robot-id 1 --prompt "..." --skip-event-log
//   ... --cimd https://claude.ai/oauth/claude-code-client-metadata --redirect http://localhost:51234/callback --auth-only
//       (sign in as Claude Code does: its published Client ID Metadata Document + a loopback redirect)
//
// The operator passcode comes from RDF_OPERATOR_PASSCODE (env or the repo .env) and is never printed;
// nor are tokens. The event_log check uses SPACETIME_HOST/DB_NAME/TOKEN from the env (point them at
// the same database the server uses). Creates a real ticket on whatever backend MCP_URL serves.
import { readEnv } from "@rdf/env";

import { connectWithOAuth, MemoryOAuthProvider } from "./oauth-flow.ts";

const env = readEnv();
const args = process.argv.slice(2);
const flag = (name: string) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};
const mcpUrl = env.MCP_URL ?? "http://127.0.0.1:3002/mcp";
const passcode = env.RDF_OPERATOR_PASSCODE;
if (!passcode) {
  console.error("RDF_OPERATOR_PASSCODE is not set (env or .env)");
  process.exit(2);
}
const prompt = flag("--prompt") ?? "Set up a deployment for the YAM: put the cup in the bin, 80% reliable, $50 budget, 10 eval trials";
const robotId = flag("--robot-id");

const t0 = Date.now();
const step = (s: string) => console.log(`\n=== [${((Date.now() - t0) / 1000).toFixed(1)}s] ${s}`);

step(`OAuth sign-in against ${mcpUrl}`);
const cimd = flag("--cimd");
const { client, provider } = await connectWithOAuth(
  mcpUrl,
  passcode,
  new MemoryOAuthProvider({ redirectUrl: flag("--redirect"), clientMetadataUrl: cimd }),
);
const tok = provider.saved;
const cid = String(provider.client?.client_id ?? "");
const how = cid.startsWith("https://") ? `CIMD (${cid})` : provider.client && "client_secret" in provider.client ? "DCR (confidential)" : "DCR (public)";
console.log(`signed in: client via ${how}, token_type=${tok?.token_type}, scope=${tok?.scope}, expires_in=${tok?.expires_in}s, refresh_token=${tok?.refresh_token ? "yes" : "no"}`);
const tools = (await client.listTools()).tools.map((t) => t.name);
console.log(`tools: ${tools.join(", ")}`);
if (args.includes("--auth-only")) {
  await client.close();
  process.exit(0);
}

async function call(name: string, a: Record<string, unknown>) {
  const r = (await client.callTool({ name, arguments: a })) as { content: { text?: string }[]; isError?: boolean };
  const text = r.content.map((c) => c.text ?? "").join("\n");
  console.log(`--- ${name}(${JSON.stringify(a)})${r.isError ? "  [isError]" : ""}\n${text}`);
  return { text, isError: Boolean(r.isError) };
}

step("create_ticket");
const created = await call("create_ticket", { prompt, ...(robotId ? { robot_id: Number(robotId) } : {}) });
const m = /Created ticket #(\d+)/.exec(created.text);
if (created.isError || !m) {
  console.error("create_ticket failed; stopping");
  process.exit(1);
}
const ticketId = Number(m[1]);

step("ticket_status (how is it going?)");
await call("ticket_status", { ticket_id: ticketId });

step("approve_spend (approve $20 more)");
const approved = await call("approve_spend", { ticket_id: ticketId, additional_budget_cents: 2000 });

step("ticket_status again");
await call("ticket_status", { ticket_id: ticketId });

step("get_report (give me the report)");
await call("get_report", { ticket_id: ticketId });
await client.close();

let auditOk: boolean | null = null;
if (!args.includes("--skip-event-log") && env.SPACETIME_HOST && env.SPACETIME_DB_NAME && env.SPACETIME_TOKEN) {
  step("event_log rows written by human:via_mcp");
  const r = await fetch(`${env.SPACETIME_HOST.replace(/\/+$/, "")}/v1/database/${env.SPACETIME_DB_NAME}/sql`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.SPACETIME_TOKEN}`, "content-type": "text/plain" },
    body: "SELECT * FROM event_log WHERE actor = 'human:via_mcp'",
  });
  if (!r.ok) console.log(`event_log query failed: HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
  else {
    const [res] = (await r.json()) as { schema: { elements: { name: { some?: string } }[] }; rows: unknown[][] }[];
    const cols = res.schema.elements.map((e) => e.name.some ?? "?");
    const rows = res.rows.map((row) => Object.fromEntries(cols.map((c, i) => [c, row[i]])));
    const mine = rows.filter((x) => String(x.payload_json ?? "").includes(`"ticket_id":${ticketId}`) || String(x.payload_json ?? "").includes(`"ticket_id": ${ticketId}`));
    for (const x of mine.length ? mine : rows.slice(-5)) console.log(`${x.type}  actor=${x.actor}  ${String(x.payload_json).slice(0, 160)}`);
    auditOk = mine.length > 0;
    console.log(`${mine.length} human:via_mcp row(s) for ticket ${ticketId} (${rows.length} in total)`);
  }
}

step(`done: ticket #${ticketId}; budget raise ${approved.isError ? "FAILED" : "ok"}; event_log ${auditOk === null ? "not checked" : auditOk ? "ok" : "MISSING rows"}`);
process.exit(approved.isError || auditOk === false ? 1 : 0);
