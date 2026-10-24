// Fail-fast env loaders for the TS apps. Mirrors services/common (Python).
//
//   import { loadEnv, mcpEnv } from "@rdf/env";
//   const env = loadEnv(mcpEnv, "mcp");   // exits(2) naming any missing var
//
// Values come from process.env first, then the repo-root .env (gitignored),
// which never overrides a var that is already set. Blank counts as missing.
// Values are never printed. Key names match .env.example.
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";
import { z } from "zod";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
export const DEFAULT_ENV_FILE = process.env.RDF_ENV_FILE ?? resolve(REPO_ROOT, ".env");

const required = z.string().trim().min(1);
const withDefault = (value: string) => z.string().trim().min(1).default(value);

// ---------- building blocks ----------

export const spacetimeEnv = z.object({
  SPACETIME_HOST: required,
  SPACETIME_DB_NAME: required,
});

export const spacetimeOwnerEnv = spacetimeEnv.extend({
  SPACETIME_TOKEN: required,
});

export const storageEnv = z.object({
  STORAGE_ENDPOINT: required,
  STORAGE_BUCKET: required,
  STORAGE_ACCESS_KEY: required,
  STORAGE_SECRET_KEY: required,
});

// ---------- per-app schemas ----------

/** Dashboard + phone task pages. Browser-side code must not see secrets. */
export const webEnv = spacetimeEnv.extend({
  ORCHESTRATOR_URL: withDefault("http://localhost:8000"),
  STRIPE_PUBLISHABLE_KEY: z.string().trim().optional(),
});

/** Claude connector (remote MCP server). */
export const mcpEnv = spacetimeOwnerEnv.extend({
  ORCHESTRATOR_URL: withDefault("http://localhost:8000"),
});

export const schemas = { web: webEnv, mcp: mcpEnv } as const;
export type AppName = keyof typeof schemas;

// ---------- loader ----------

type Env = Record<string, string | undefined>;

/** process.env merged over the .env file (env wins); blank values dropped. */
export function readEnv(envFile: string | null = DEFAULT_ENV_FILE, env: Env = process.env): Env {
  const fromFile: Env = envFile && existsSync(envFile) ? parseEnv(readFileSync(envFile, "utf8")) : {};
  const merged: Env = { ...fromFile };
  for (const [k, v] of Object.entries(env)) if (v !== undefined && v.trim() !== "") merged[k] = v;
  for (const [k, v] of Object.entries(merged)) if (v === undefined || v.trim() === "") delete merged[k];
  return merged;
}

/** 'ENV_VAR: reason' lines for every problem, never including values. */
export function envErrors(error: z.ZodError): string[] {
  return error.issues.map((issue) => {
    const name = issue.path.join(".") || "?";
    const missing = issue.code === "invalid_type" && issue.received === "undefined";
    return `${name}: ${missing ? "missing (required)" : issue.message}`;
  });
}

export class EnvError extends Error {
  readonly app: string;
  readonly problems: string[];

  constructor(app: string, problems: string[]) {
    super(
      `[${app}] config error: ${problems.length} env var(s) missing or invalid:\n` +
        problems.map((p) => `  - ${p}`).join("\n") +
        "\n  Set them in the environment or in .env (see .env.example; check with ./scripts/check-env.sh).",
    );
    this.name = "EnvError";
    this.app = app;
    this.problems = problems;
  }
}

/** Parse or throw EnvError. Tests use this with a fake env and envFile: null. */
export function parseAppEnv<S extends z.ZodTypeAny>(
  schema: S,
  app: string,
  opts: { env?: Env; envFile?: string | null } = {},
): z.infer<S> {
  const result = schema.safeParse(readEnv(opts.envFile === undefined ? DEFAULT_ENV_FILE : opts.envFile, opts.env));
  if (!result.success) throw new EnvError(app, envErrors(result.error));
  return result.data;
}

/** Parse or print the problems and exit(2). Call once at startup. */
export function loadEnv<S extends z.ZodTypeAny>(
  schema: S,
  app: string,
  opts: { env?: Env; envFile?: string | null } = {},
): z.infer<S> {
  try {
    return parseAppEnv(schema, app, opts);
  } catch (err) {
    if (err instanceof EnvError) {
      console.error(err.message);
      process.exit(2);
    }
    throw err;
  }
}
