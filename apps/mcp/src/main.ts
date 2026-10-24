// Entry point: `pnpm --filter @rdf/mcp dev` (watch) or `start`. Env: see src/config.ts / .env.example.
import { OrchestratorClient, SpacetimeHttp } from "./backend.ts";
import { hostList, loadConfig } from "./config.ts";
import { createMcpHttpServer } from "./http.ts";
import { deriveSigningKey, OAuthServer } from "./oauth.ts";

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
const oauth = new OAuthServer({
  passcode: cfg.RDF_OPERATOR_PASSCODE || undefined,
  signingKey: deriveSigningKey(cfg.MCP_OAUTH_SECRET, cfg.MCP_TOKEN),
  publicUrl: cfg.MCP_PUBLIC_URL,
  redirectHosts: hostList(cfg.MCP_OAUTH_REDIRECT_HOSTS),
  cimdHosts: hostList(cfg.MCP_OAUTH_CIMD_HOSTS),
  accessTtlS: cfg.MCP_OAUTH_ACCESS_TTL_S,
  log,
});
const server = createMcpHttpServer({ token: cfg.MCP_TOKEN, deps, oauth, log });
server.listen(cfg.PORT, cfg.HOST, () => {
  log("mcp.listening", {
    url: `http://${cfg.HOST}:${cfg.PORT}/mcp`,
    public_url: cfg.MCP_PUBLIC_URL ?? "(from Host header)",
    oauth: oauth.enabled ? "enabled" : "disabled: RDF_OPERATOR_PASSCODE unset",
    orchestrator: cfg.ORCHESTRATOR_URL,
    spacetime_db: cfg.SPACETIME_DB_NAME,
  });
});
for (const sig of ["SIGINT", "SIGTERM"] as const) {
  process.on(sig, () => server.close(() => process.exit(0)));
}
