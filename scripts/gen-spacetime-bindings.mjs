#!/usr/bin/env node
// TDM-16: regenerate every SpacetimeDB client binding in one command.
//
//   pnpm gen:bindings              # from the repo root (TS + Python)
//   pnpm gen:bindings --ts-only    # or --py-only
//
// 1. TypeScript: `spacetime generate` (public tables only) from the module source (spacetime/spacetimedb) into
//    packages/spacetime-bindings/src/module_bindings (the shared copy, @rdf/spacetime-bindings).
//    While apps/web still keeps its own src/module_bindings (TDM-73), the same output is
//    mirrored there so the two never drift.
// 2. Python: rdf_common.spacetime_codegen reads the *published* database's /schema and writes
//    services/common/src/rdf_common/spacetime_tables.py (row dataclasses + reducer wrappers).
//
// TS comes from source and Python from what is published, so publish first (see
// spacetime/README.md); the script warns when the two disagree. Needs the `spacetime` CLI 2.13
// (~/.local/bin) and uv.
import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const modulePath = resolve(root, 'spacetime/spacetimedb');
const tsOut = resolve(root, 'packages/spacetime-bindings/src/module_bindings');
const webMirror = resolve(root, 'apps/web/src/module_bindings');
const pyProject = resolve(root, 'services/common');
const pyOut = resolve(pyProject, 'src/rdf_common/spacetime_tables.py');

const argv = process.argv.slice(2);
const doTs = !argv.includes('--py-only');
const doPy = !argv.includes('--ts-only');
const env = { ...process.env, PATH: `${resolve(homedir(), '.local/bin')}:${process.env.PATH ?? ''}` };

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: 'inherit', env, ...opts });
  if (r.error) {
    console.error(`[gen:bindings] could not run ${cmd}: ${r.error.message}`);
    process.exit(1);
  }
  if (r.status !== 0) process.exit(r.status ?? 1);
}

function tsNames() {
  const files = readdirSync(tsOut);
  const pick = (suffix) => files.filter((f) => f.endsWith(suffix)).map((f) => f.slice(0, -suffix.length)).sort();
  return { tables: pick('_table.ts'), reducers: pick('_reducer.ts') };
}

if (doTs) {
  if (!existsSync(resolve(modulePath, 'node_modules'))) {
    console.log('[gen:bindings] installing module deps in spacetime/spacetimedb');
    run('npm', ['install'], { cwd: modulePath });
  }
  rmSync(tsOut, { recursive: true, force: true });
  console.log('[gen:bindings] ts: spacetime generate -> packages/spacetime-bindings/src/module_bindings');
  run('spacetime', ['generate', '--lang', 'typescript', '--out-dir', tsOut, '--module-path', modulePath, '--yes', '--no-config']);
  if (existsSync(webMirror)) {
    rmSync(webMirror, { recursive: true, force: true });
    cpSync(tsOut, webMirror, { recursive: true });
    console.log('[gen:bindings] ts: mirrored to apps/web/src/module_bindings');
  }
}

if (doPy) {
  console.log('[gen:bindings] py: published /schema -> services/common/src/rdf_common/spacetime_tables.py');
  run('uv', ['run', '--quiet', 'python', '-m', 'rdf_common.spacetime_codegen'], { cwd: pyProject });
}

if (doTs && doPy && existsSync(pyOut)) {
  const py = readFileSync(pyOut, 'utf8');
  const tuple = (name) => [...(py.match(new RegExp(`^${name} = \\((.*)\\)$`, 'm'))?.[1] ?? '').matchAll(/'([^']+)'/g)].map((m) => m[1]).sort();
  const published = { tables: tuple('PUBLIC_TABLES'), reducers: tuple('REDUCERS').filter((r) => r !== 'init') };
  const source = tsNames();
  const diff = (a, b) => a.filter((x) => !b.includes(x));
  const notPublished = [...diff(source.tables, published.tables), ...diff(source.reducers, published.reducers)];
  const notInSource = [...diff(published.tables, source.tables), ...diff(published.reducers, source.reducers)];
  if (notPublished.length || notInSource.length) {
    console.warn('[gen:bindings] WARNING: module source and the published database differ.');
    if (notPublished.length) console.warn(`  in source, not published yet: ${notPublished.join(', ')}`);
    if (notInSource.length) console.warn(`  published, not in source:      ${notInSource.join(', ')}`);
    console.warn('  Publish the module (spacetime/README.md), then re-run `pnpm gen:bindings`.');
  } else {
    console.log(`[gen:bindings] TS and Python agree: ${source.tables.length} tables, ${source.reducers.length} reducers`);
  }
}
