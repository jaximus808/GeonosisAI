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

  // ---- OAuth for the claude.ai connector (TDM-81; see src/oauth.ts and docs/connector.md) ----
  // The operator passcode gates the consent page (same secret as the web app's /ops/login).
  // Unset = OAuth sign-in refuses (the MCP_TOKEN bearer still works).
  RDF_OPERATOR_PASSCODE: z.string().trim().optional(),
  // Public origin, e.g. https://rdf-mcp.fly.dev. Set it in prod; unset = taken from Host / X-Forwarded-*.
  MCP_PUBLIC_URL: z.string().trim().url().optional(),
  // HMAC key for OAuth clients/codes/tokens. Unset = derived from MCP_TOKEN. Rotating it revokes every connection.
  MCP_OAUTH_SECRET: z.string().trim().min(32, "must be at least 32 characters (e.g. `openssl rand -hex 32`)").optional(),
  // https redirect hosts accepted at client registration (loopback always allowed). "*" = any.
  MCP_OAUTH_REDIRECT_HOSTS: z.string().trim().default("claude.ai,claude.com"),
  // Hosts whose https client_id URLs are accepted as Client ID Metadata Documents. Blank-equivalent "-" = off.
  MCP_OAUTH_CIMD_HOSTS: z.string().trim().default("claude.ai,claude.com"),
  MCP_OAUTH_ACCESS_TTL_S: z.coerce.number().int().min(60).max(86_400).default(3600),
});

export function hostList(s: string): string[] {
  return s.split(",").map((h) => h.trim().toLowerCase()).filter((h) => h && h !== "-");
}

export type McpConfig = z.infer<typeof mcpServerEnv>;

export function loadConfig(): McpConfig {
  return loadEnv(mcpServerEnv, "mcp");
}

/** For tests: parse a fake env without touching .env. */
export function parseConfig(env: Record<string, string | undefined>): McpConfig {
  return parseAppEnv(mcpServerEnv, "mcp", { env, envFile: null });
}
