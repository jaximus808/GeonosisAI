// LLM backend switch: the /api/llm-backend proxy (operator gating, production lock) and the decision
// badge logic. Run with `pnpm --filter @rdf/web test`. The orchestrator is a fake node:http server.
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { registerHooks } from 'node:module';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, before, beforeEach, describe, it } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SRC = resolve(fileURLToPath(new URL('.', import.meta.url)), '../src');

registerHooks({
  resolve(specifier, context, nextResolve) {
    let target: string | null = null;
    if (specifier.startsWith('@/')) target = join(SRC, specifier.slice(2));
    else if (specifier.startsWith('.') && context.parentURL?.startsWith('file:') && !/\.[cm]?[jt]sx?$/.test(specifier)) {
      target = fileURLToPath(new URL(specifier, context.parentURL));
    }
    if (target) {
      for (const c of [target, `${target}.ts`, `${target}.tsx`, join(target, 'index.ts')]) {
        if (/\.[cm]?[jt]sx?$/.test(c) && existsSync(c)) return nextResolve(pathToFileURL(c).href, context);
      }
    }
    return nextResolve(specifier, context);
  },
});

const PASS = 'test-passcode-llm-0123456789';
type Call = { method: string; path: string; headers: Record<string, unknown>; body: string };
const calls: Call[] = [];
let upstream: Server;
let upstreamLocked = false;
let backend = 'api';

type Handler = (req: Request) => Promise<Response>;
let GET: () => Promise<Response>;
let PUT: Handler;
let mint: () => { value: string; expiresAt: number };
let types: typeof import('../src/lib/llmbackend/types.ts');

function state() {
  return {
    backend,
    model: null,
    effective_model: backend === 'claude_code' ? 'opus' : 'claude-opus-5-5',
    allowed: upstreamLocked ? ['api'] : ['api', 'droid', 'claude_code'],
    env: upstreamLocked ? 'production' : 'local',
    locked: upstreamLocked,
    source: 'runtime',
    labels: { api: 'Claude API', droid: 'Factory droid', claude_code: 'Claude Code (local)' },
    updated_at: null,
    updated_by: null,
  };
}

function put(body: unknown, cookie?: string) {
  return new Request('http://127.0.0.1:3999/api/llm-backend', {
    method: 'PUT',
    headers: { 'content-type': 'application/json', ...(cookie ? { cookie } : {}) },
    body: JSON.stringify(body),
  });
}

before(async () => {
  upstream = createServer(async (r, res) => {
    let body = '';
    for await (const c of r) body += c;
    calls.push({ method: r.method!, path: r.url!, headers: r.headers, body });
    if (r.url !== '/admin/llm') return void res.writeHead(404).end('{}');
    if (r.method === 'PUT') {
      if (upstreamLocked) {
        return void res.writeHead(403, { 'content-type': 'application/json' }).end(JSON.stringify({ detail: 'LLM backend is locked in production (api)' }));
      }
      const from = backend;
      backend = JSON.parse(body).backend;
      return void res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ ...state(), changed: { from, to: backend, model: null } }));
    }
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(state()));
  });
  await new Promise<void>((ok) => upstream.listen(0, '127.0.0.1', ok));
  const base = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`;
  const emptyEnv = join(mkdtempSync(join(tmpdir(), 'rdf-llmbackend-')), 'empty.env');
  writeFileSync(emptyEnv, '');
  for (const k of ['RDF_DEPLOY_ENV', 'NEXT_PUBLIC_RDF_ENV', 'FLY_APP_NAME']) delete process.env[k];
  Object.assign(process.env, {
    RDF_ENV_FILE: emptyEnv,
    RDF_OPERATOR_PASSCODE: PASS,
    RDF_OPERATOR_KEY: 'fake-operator-key',
    RDF_ORCHESTRATOR_URL: base,
  });
  const route = await import('@/app/api/llm-backend/route');
  GET = route.GET as unknown as () => Promise<Response>;
  PUT = route.PUT as unknown as Handler;
  mint = (await import('@/lib/operator/session')).mintOperatorSession;
  types = await import('../src/lib/llmbackend/types.ts');
});

after(() => upstream.close());

beforeEach(() => {
  upstreamLocked = false;
  backend = 'api';
  for (const k of ['RDF_DEPLOY_ENV', 'NEXT_PUBLIC_RDF_ENV', 'FLY_APP_NAME']) delete process.env[k];
});

describe('/api/llm-backend proxy', () => {
  it('GET is open and forwards to /admin/llm', async () => {
    const n = calls.length;
    const res = await GET();
    assert.equal(res.status, 200);
    const b = (await res.json()) as { backend: string; locked: boolean };
    assert.equal(b.backend, 'api');
    assert.equal(b.locked, false);
    assert.equal(calls[n].path, '/admin/llm');
    assert.equal(calls[n].headers['x-rdf-operator-key'], undefined, 'GET never carries the operator key');
  });

  it('PUT without an operator session: 401, nothing forwarded', async () => {
    const n = calls.length;
    const res = await PUT(put({ backend: 'claude_code' }));
    assert.equal(res.status, 401);
    assert.equal(((await res.json()) as { code: string }).code, 'operator_login_required');
    assert.equal(calls.length, n);
  });

  it('PUT with a forged cookie: 401', async () => {
    const forged = mint().value.replace(/\.[^.]+$/, '.' + Buffer.alloc(32).toString('base64url'));
    assert.equal((await PUT(put({ backend: 'droid' }, `rdf_op=${forged}`))).status, 401);
  });

  it('PUT as operator: forwarded with the server-side operator key, new state returned', async () => {
    const n = calls.length;
    const res = await PUT(put({ backend: 'claude_code', model: 'opus' }, `rdf_op=${mint().value}`));
    assert.equal(res.status, 200, await res.clone().text());
    const b = (await res.json()) as { backend: string; changed: { from: string; to: string } };
    assert.equal(b.backend, 'claude_code');
    assert.deepEqual([b.changed.from, b.changed.to], ['api', 'claude_code']);
    assert.equal(calls.length, n + 1);
    assert.equal(calls[n].method, 'PUT');
    assert.equal(calls[n].headers['x-rdf-operator-key'], 'fake-operator-key');
    assert.deepEqual(JSON.parse(calls[n].body), { backend: 'claude_code', model: 'opus', by: 'web:ops-settings' });
  });

  it('PUT with an unknown backend or a junk model: 400, nothing forwarded', async () => {
    const n = calls.length;
    const cookie = `rdf_op=${mint().value}`;
    assert.equal((await PUT(put({ backend: 'gpt' }, cookie))).status, 400);
    assert.equal((await PUT(put({ backend: 'api', model: 'x; rm -rf /' }, cookie))).status, 400);
    assert.equal(calls.length, n);
  });

  for (const [k, v] of [['RDF_DEPLOY_ENV', 'production'], ['NEXT_PUBLIC_RDF_ENV', 'prod'], ['FLY_APP_NAME', 'rdf-orchestrator']] as const) {
    it(`production (${k}=${v}): PUT refused with 403 before forwarding, even as operator; GET says locked`, async () => {
      process.env[k] = v;
      const n = calls.length;
      const res = await PUT(put({ backend: 'claude_code' }, `rdf_op=${mint().value}`));
      assert.equal(res.status, 403);
      assert.match(((await res.json()) as { detail: string }).detail, /locked in production/);
      assert.equal(calls.length, n, 'must not reach the orchestrator');
      const g = (await (await GET()).json()) as { locked: boolean; allowed: string[] };
      assert.equal(g.locked, true);
      assert.deepEqual(g.allowed, ['api']);
    });
  }

  it("the orchestrator's own production lock (403) passes through", async () => {
    upstreamLocked = true;
    const res = await PUT(put({ backend: 'droid' }, `rdf_op=${mint().value}`));
    assert.equal(res.status, 403);
    assert.match(((await res.json()) as { detail: string }).detail, /locked in production/);
    assert.equal(((await (await GET()).json()) as { locked: boolean }).locked, true);
  });

  it('orchestrator without /admin/llm: GET is a non-OK answer (the chip then renders nothing)', async () => {
    const saved = process.env.RDF_ORCHESTRATOR_URL;
    process.env.RDF_ORCHESTRATOR_URL = 'http://127.0.0.1:1';
    try {
      const res = await GET();
      assert.equal(res.ok, false);
    } finally {
      process.env.RDF_ORCHESTRATOR_URL = saved;
    }
  });
});

describe('decision badge (llmBadge)', () => {
  it('names each backend and the model that ran', () => {
    const api = types.llmBadge({ backend: 'api', requested: 'claude-opus-5-5', served: 'claude-opus-5-5' });
    assert.deepEqual([api.backend, api.tone, api.text], ['api', 'api', 'Claude API · claude-opus-5-5']);
    const cc = types.llmBadge({ backend: 'claude_code', model: 'opus', requested: 'claude-opus-5-5' });
    assert.deepEqual([cc.backend, cc.tone, cc.text], ['claude_code', 'local', 'Claude Code (local) · opus']);
    const dr = types.llmBadge({ backend: 'droid', model: 'claude-opus-5-5' });
    assert.deepEqual([dr.backend, dr.tone, dr.text], ['droid', 'droid', 'Factory droid · claude-opus-5-5']);
  });

  it('a claude_code or droid decision never reads as the API', () => {
    for (const b of ['claude_code', 'droid']) {
      const x = types.llmBadge({ backend: b, served: 'claude-opus-5-5' });
      assert.doesNotMatch(x.text, /Claude API/);
      assert.notEqual(x.tone, 'api');
    }
  });

  it('a record without a backend says so instead of guessing', () => {
    const old = types.llmBadge({ requested: 'claude-opus-5-5', served: 'claude-opus-5-5' });
    assert.equal(old.backend, null);
    assert.equal(old.tone, 'unknown');
    assert.equal(old.text, 'backend n/a · claude-opus-5-5');
    assert.equal(types.llmBadge(null).text, 'backend n/a');
    assert.equal(types.llmBadge({ backend: 'mystery' }).text, 'backend mystery');
  });

  it('isProductionEnv', () => {
    assert.equal(types.isProductionEnv({}), false);
    assert.equal(types.isProductionEnv({ RDF_DEPLOY_ENV: 'local' }), false);
    assert.equal(types.isProductionEnv({ RDF_DEPLOY_ENV: 'Production' }), true);
    assert.equal(types.isProductionEnv({ NEXT_PUBLIC_RDF_ENV: 'cloud' }), true);
    assert.equal(types.isProductionEnv({ FLY_APP_NAME: 'x' }), true);
  });
});
