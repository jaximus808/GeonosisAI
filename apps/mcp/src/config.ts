// Env for the Claude connector. Builds on @rdf/env's mcpEnv (SPACETIME_*, ORCHESTRATOR_URL).
// Values are never printed.
import { loadEnv, mcpEnv, parseAppEnv } from "@rdf/env";
import { z } from "zod";

export const mcpServerEnv = mcpEnv.extend({
  // Shared secret Claude (or the MCP Inspector) sends as `Authorization: Bearer <MCP_TOKEN>`.
  MCP_TOKEN: z.string().trim().min(16, "must be at least 16 characters (e.g. `openssl rand -hex 32`)"),
  HOST: z.string().trim().min(1).default("127.0.0.1"),
  PORT: z.coerce.number().int().min(1).max(65535).default(3002),
  // Largest single approve_spend call (plan approval or budget increase), in cents. Default $500.
  MCP_MAX_SPEND_CENTS: z.coerce.number().int().min(1).default(50_000),
});

export type McpConfig = z.infer<typeof mcpServerEnv>;

export function loadConfig(): McpConfig {
  return loadEnv(mcpServerEnv, "mcp");
}

/** For tests: parse a fake env without touching .env. */
export function parseConfig(env: Record<string, string | undefined>): McpConfig {
  return parseAppEnv(mcpServerEnv, "mcp", { env, envFile: null });
}
