#!/usr/bin/env node
// Usage: node packages/env/check.ts <web|mcp>
// Exits 2 naming missing/invalid vars; exits 0 silently-ish when all is well.
import { loadEnv, schemas, type AppName } from "./index.ts";

const app = process.argv[2] as AppName | undefined;
if (!app || !(app in schemas)) {
  console.error(`usage: check.ts <${Object.keys(schemas).join("|")}>`);
  process.exit(64);
}
loadEnv(schemas[app], app);
console.log(`[${app}] env ok`);
