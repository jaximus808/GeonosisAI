#!/usr/bin/env node
// TDM-73: regenerate the SpacetimeDB TypeScript bindings for the web app.
// Usage: pnpm --filter @rdf/web gen:bindings
// Builds the module from spacetime/spacetimedb (the source that gets published to
// maincloud robo-factory-a706j) and writes src/module_bindings. Re-run after every
// schema publish. Needs the `spacetime` CLI 2.13 on PATH (~/.local/bin).
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, '..');
const modulePath = resolve(webRoot, '../../spacetime/spacetimedb');
const outDir = resolve(webRoot, 'src/module_bindings');

if (!existsSync(resolve(modulePath, 'node_modules'))) {
  console.log('[gen:bindings] installing module deps in spacetime/spacetimedb');
  const npm = spawnSync('npm', ['install'], { cwd: modulePath, stdio: 'inherit' });
  if (npm.status !== 0) process.exit(npm.status ?? 1);
}

const args = ['generate', '--lang', 'typescript', '--out-dir', outDir, '--module-path', modulePath, '--yes', '--no-config'];
console.log(`[gen:bindings] spacetime ${args.join(' ')}`);
const r = spawnSync('spacetime', args, { stdio: 'inherit' });
if (r.error) {
  console.error('[gen:bindings] could not run `spacetime`. Install it: curl -sSf https://install.spacetimedb.com | sh');
  process.exit(1);
}
process.exit(r.status ?? 1);
