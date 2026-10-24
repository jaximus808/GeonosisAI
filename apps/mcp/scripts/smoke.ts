// Smoke client: lists the tools and calls them over Streamable HTTP with the official SDK client.
//   node scripts/smoke.ts                          # list tools, list_robots
//   node scripts/smoke.ts call ticket_status '{"ticket_id":1}'
// Env: MCP_URL (default http://127.0.0.1:3002/mcp), MCP_TOKEN (from the env or the repo .env).
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { readEnv } from "@rdf/env";

const env = readEnv();
const url = new URL(env.MCP_URL ?? "http://127.0.0.1:3002/mcp");
if (!env.MCP_TOKEN) {
  console.error("MCP_TOKEN is not set (env or .env)");
  process.exit(2);
}
const client = new Client({ name: "rdf-mcp-smoke", version: "0" });
await client.connect(new StreamableHTTPClientTransport(url, { requestInit: { headers: { authorization: `Bearer ${env.MCP_TOKEN}` } } }));

const [, , cmd, name, args] = process.argv;
if (cmd === "call" && name) {
  const r = (await client.callTool({ name, arguments: args ? JSON.parse(args) : {} })) as { content: { text?: string }[]; isError?: boolean };
  console.log(`--- ${name}${r.isError ? " (isError)" : ""}`);
  for (const c of r.content) console.log(c.text);
} else {
  const { tools } = await client.listTools();
  for (const t of tools) console.log(`${t.name}: ${Object.keys((t.inputSchema as { properties?: object }).properties ?? {}).join(", ")}`);
  const r = (await client.callTool({ name: "list_robots", arguments: {} })) as { content: { text?: string }[] };
  console.log("--- list_robots\n" + r.content[0]?.text);
}
await client.close();
