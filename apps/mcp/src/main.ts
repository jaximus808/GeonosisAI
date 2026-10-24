// Entry point: `pnpm --filter @rdf/mcp dev` (watch) or `start`. Env: see src/config.ts / .env.example.
import { OrchestratorClient, SpacetimeHttp } from "./backend.ts";
import { loadConfig } from "./config.ts";
import { createMcpHttpServer } from "./http.ts";

function log(event: string, fields: Record<string, unknown> = {}): void {
  console.log(JSON.stringify({ ts: new Date().toISOString(), svc: "mcp", event, ...fields }));
}

const cfg = loadConfig();
const deps = {
  orchestrator: new OrchestratorClient(cfg.ORCHESTRATOR_URL),
  spacetime: new SpacetimeHttp(cfg.SPACETIME_HOST, cfg.SPACETIME_DB_NAME, cfg.SPACETIME_TOKEN),
  maxSpendCents: cfg.MCP_MAX_SPEND_CENTS,
  log,
};
const server = createMcpHttpServer({ token: cfg.MCP_TOKEN, deps, log });
server.listen(cfg.PORT, cfg.HOST, () => {
  log("mcp.listening", { url: `http://${cfg.HOST}:${cfg.PORT}/mcp`, orchestrator: cfg.ORCHESTRATOR_URL, spacetime_db: cfg.SPACETIME_DB_NAME });
});
for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => server.close(() => process.exit(0)));
}
