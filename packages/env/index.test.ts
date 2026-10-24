// Fake env only; the real .env is never read (envFile: null or a temp file).
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { EnvError, mcpEnv, parseAppEnv, webEnv } from "./index.ts";

const spacetime = { SPACETIME_HOST: "http://fake:3000", SPACETIME_DB_NAME: "fake-db", SPACETIME_TOKEN: "fake-token" };

test("loads when all required vars are present, applies defaults", () => {
  const env = parseAppEnv(mcpEnv, "mcp", { env: spacetime, envFile: null });
  assert.equal(env.SPACETIME_DB_NAME, "fake-db");
  assert.equal(env.ORCHESTRATOR_URL, "http://localhost:8000");
});

test("missing var throws EnvError naming it", () => {
  const { SPACETIME_TOKEN: _, ...partial } = spacetime;
  assert.throws(
    () => parseAppEnv(mcpEnv, "mcp", { env: partial, envFile: null }),
    (err: unknown) =>
      err instanceof EnvError &&
      err.problems.length === 1 &&
      err.problems[0] === "SPACETIME_TOKEN: missing (required)",
  );
});

test("blank value counts as missing", () => {
  assert.throws(
    () => parseAppEnv(webEnv, "web", { env: { ...spacetime, SPACETIME_HOST: "  " }, envFile: null }),
    (err: unknown) => err instanceof EnvError && err.message.includes("SPACETIME_HOST"),
  );
});

test("error message never contains values", () => {
  try {
    parseAppEnv(mcpEnv, "mcp", { env: { SPACETIME_TOKEN: "secret-xyz" }, envFile: null });
    assert.fail("should throw");
  } catch (err) {
    assert.ok(err instanceof EnvError);
    assert.ok(!err.message.includes("secret-xyz"));
  }
});

test(".env file is read, process env wins", () => {
  const dir = mkdtempSync(join(tmpdir(), "rdf-env-"));
  const file = join(dir, ".env");
  writeFileSync(file, "SPACETIME_HOST=http://from-file\nSPACETIME_DB_NAME=file-db\nSPACETIME_TOKEN=t\n");
  const env = parseAppEnv(mcpEnv, "mcp", { env: { SPACETIME_DB_NAME: "env-db" }, envFile: file });
  assert.equal(env.SPACETIME_HOST, "http://from-file");
  assert.equal(env.SPACETIME_DB_NAME, "env-db");
});
