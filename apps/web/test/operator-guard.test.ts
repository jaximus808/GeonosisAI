// Operator guard on the web server's proxy routes: run with `pnpm --filter @rdf/web test`
// (plain `node --test`, Node >= 23 strips the types). The route modules are imported as-is; a resolve
// hook maps Next's `@/` alias. Upstream (orchestrator + SpacetimeDB) is one fake node:http server, so
// nothing here reaches maincloud or a robot.
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, existsSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { registerHooks } from 'node:module';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { after, before, describe, it } from 'node:test';
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

const PASS = 'test-passcode-0123456789';
const calls: { method: string; path: string; headers: Record<string, unknown> }[] = [];
let upstream: Server;
let base = '';

type Handler = (req: Request, ctx: { params: Promise<Record<string, unknown>> }) => Promise<Response> | Response;
let commands: Handler;
let estop: Handler;
let failing: Handler;
let revoke: Handler;
let tdm64Patch: Handler;
let tdm64Post: Handler;
let tdm74Post: Handler;
let tdm79Patch: Handler;
let tdm79Post: Handler;
let tdm79Get: Handler;
let session: { GET: Handler; POST: Handler; DELETE: Handler };
let mint: () => { value: string; expiresAt: number };

const params = (p: Record<string, unknown>) => ({ params: Promise.resolve(p) });

function req(method: string, path: string, body?: unknown, cookie?: string, extra: Record<string, string> = {}) {
  return new Request(`http://127.0.0.1:3737${path}`, {
    method,
    headers: { 'content-type': 'application/json', ...(cookie ? { cookie } : {}), ...extra },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

before(async () => {
  upstream = createServer(async (r, res) => {
    for await (const _ of r) {
      /* drain */
    }
    calls.push({ method: r.method!, path: r.url!, headers: r.headers });
    res.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ ok: true, command_id: 9, seq: 3, uri: 'x', id: 1 }));
  });
  await new Promise<void>((ok) => upstream.listen(0, '127.0.0.1', ok));
  base = `http://127.0.0.1:${(upstream.address() as AddressInfo).port}`;

  // Point every route at the fake, and keep the repo .env out of it (its real tokens must not be used).
  const emptyEnv = join(mkdtempSync(join(tmpdir(), 'rdf-opguard-')), 'empty.env');
  writeFileSync(emptyEnv, '');
  Object.assign(process.env, {
    RDF_ENV_FILE: emptyEnv,
    RDF_OPERATOR_PASSCODE: PASS,
    RDF_OPERATOR_KEY: 'fake-operator-key',
    RDF_ORCHESTRATOR_URL: base,
    NEXT_PUBLIC_ORCHESTRATOR_URL: base,
    ORCHESTRATOR_URL: base,
    NEXT_PUBLIC_SPACETIME_HOST: base,
    SPACETIME_HOST: base,
    NEXT_PUBLIC_SPACETIME_DB_NAME: 'opguard-test',
    SPACETIME_DB_NAME: 'opguard-test',
    RDF_WEB_SPACETIME_TOKEN: 'fake-web-token',
  });

  commands = (await import('@/app/api/tdm78/robots/[id]/commands/route')).POST as unknown as Handler;
  estop = (await import('@/app/api/tdm78/robots/[id]/estop/route')).POST as unknown as Handler;
  failing = (await import('@/app/api/tdm78/robots/[id]/interventions/flag/route')).POST as unknown as Handler;
  revoke = (await import('@/app/api/tdm57/tasks/[id]/revoke/route')).POST as unknown as Handler;
  const t64 = await import('@/app/api/tdm64/[...path]/route');
  tdm64Patch = t64.PATCH as unknown as Handler;
  tdm64Post = t64.POST as unknown as Handler;
  tdm74Post = (await import('@/app/api/tdm74/[...path]/route')).POST as unknown as Handler;
  const t79 = await import('@/app/api/tdm79/[...path]/route');
  tdm79Patch = t79.PATCH as unknown as Handler;
  tdm79Post = t79.POST as unknown as Handler;
  tdm79Get = t79.GET as unknown as Handler;
  session = (await import('@/app/api/ops/session/route')) as unknown as typeof session;
  mint = (await import('@/lib/operator/session')).mintOperatorSession;
});

after(() => upstream.close());

/** Every operator-only route: a fresh request each time (bodies are single-use). */
const guarded: [string, () => [Handler, (cookie?: string) => Request, ReturnType<typeof params>]][] = [
  ['tdm78 home', () => [commands, (c) => req('POST', '/api/tdm78/robots/1/commands', { type: 'home' }, c), params({ id: '1' })]],
  ['tdm78 start_teleop', () => [commands, (c) => req('POST', '/api/tdm78/robots/1/commands', { type: 'start_teleop' }, c), params({ id: '1' })]],
  ['tdm78 trial_outcome', () => [commands, (c) => req('POST', '/api/tdm78/robots/1/commands', { type: 'trial_outcome', payload: { success: true, eval_run_id: 3, index: 0 } }, c), params({ id: '1' })]],
  ['tdm69 Failing flag', () => [failing, (c) => req('POST', '/api/tdm78/robots/1/interventions/flag', { note: 'wandering' }, c), params({ id: '1' })]],
  ['tdm57 revoke', () => [revoke, (c) => req('POST', '/api/tdm57/tasks/5/revoke', {}, c), params({ id: '5' })]],
  ['tdm64 budget PATCH', () => [tdm64Patch, (c) => req('PATCH', '/api/tdm64/tickets/1/settings', { budget_cents: 500 }, c), params({ path: ['tickets', '1', 'settings'] })]],
  ['tdm74 create ticket', () => [tdm74Post, (c) => req('POST', '/api/tdm74/tickets', { prompt: 'x', budget_cents: 100 }, c), params({ path: ['tickets'] })]],
  ['tdm74 parse', () => [tdm74Post, (c) => req('POST', '/api/tdm74/tickets/parse', { prompt: 'x' }, c), params({ path: ['tickets', 'parse'] })]],
  ['tdm79 settings PATCH', () => [tdm79Patch, (c) => req('PATCH', '/api/tdm79/tickets/1/settings', { eval_trials: 10 }, c), params({ path: ['tickets', '1', 'settings'] })]],
  ['tdm79 report save', () => [tdm79Post, (c) => req('POST', '/api/tdm79/tickets/1/report/save', {}, c), params({ path: ['tickets', '1', 'report', 'save'] })]],
];

describe('operator-only proxy routes', () => {
  for (const [name, make] of guarded) {
    it(`${name}: 401 without the cookie, nothing forwarded`, async () => {
      const [h, r, p] = make();
      const n = calls.length;
      const res = await h(r(), p);
      assert.equal(res.status, 401);
      assert.equal(((await res.json()) as { code: string }).code, 'operator_login_required');
      assert.equal(calls.length, n, 'must not reach the orchestrator');
    });

    it(`${name}: 401 with a forged or expired cookie`, async () => {
      const [h, r, p] = make();
      const good = mint().value;
      const forged = good.replace(/\.[^.]+$/, '.' + Buffer.alloc(32).toString('base64url'));
      assert.equal((await h(r(`rdf_op=${forged}`), p)).status, 401);
      const [h2, r2, p2] = make();
      const expired = (await import('@/lib/operator/session')).mintOperatorSession(Date.now() - 13 * 3600_000).value;
      assert.equal((await h2(r2(`rdf_op=${expired}`), p2)).status, 401);
    });

    it(`${name}: forwarded with a valid session cookie`, async () => {
      const [h, r, p] = make();
      const n = calls.length;
      const res = await h(r(`theme=dark; rdf_op=${mint().value}`), p);
      assert.equal(res.status, 200, await res.clone().text());
      assert.equal(calls.length, n + 1);
    });
  }

  it('a passcode change logs every session out', async () => {
    const v = mint().value;
    process.env.RDF_OPERATOR_PASSCODE = 'another-passcode-987654';
    try {
      assert.equal((await commands(req('POST', '/api/tdm78/robots/1/commands', { type: 'home' }, `rdf_op=${v}`), params({ id: '1' }))).status, 401);
    } finally {
      process.env.RDF_OPERATOR_PASSCODE = PASS;
    }
  });

  it('no passcode configured: guarded routes fail closed (503), e-stop still works', async () => {
    delete process.env.RDF_OPERATOR_PASSCODE;
    try {
      const res = await commands(req('POST', '/api/tdm78/robots/1/commands', { type: 'home' }), params({ id: '1' }));
      assert.equal(res.status, 503);
      assert.equal((await estop(req('POST', '/api/tdm78/robots/1/estop', {}), params({ id: '1' }))).status, 200);
    } finally {
      process.env.RDF_OPERATOR_PASSCODE = PASS;
    }
  });
});

describe('routes that stay open', () => {
  it('soft e-stop: OK without any cookie (direct path to SpacetimeDB)', async () => {
    const n = calls.length;
    const res = await estop(req('POST', '/api/tdm78/robots/1/estop', { reason: 'test' }), params({ id: '1' }));
    assert.equal(res.status, 200);
    const b = (await res.json()) as { ok: boolean; path: string };
    assert.equal(b.ok, true);
    assert.equal(b.path, 'direct');
    assert.match(calls[n].path, /\/call\/issue_robot_command$/);
  });

  it('soft e-stop: OK with a junk cookie too', async () => {
    const res = await estop(req('POST', '/api/tdm78/robots/1/estop', {}, 'rdf_op=garbage'), params({ id: '1' }));
    assert.equal(res.status, 200);
  });

  it('soft e-stop: falls back to the orchestrator without a login', async () => {
    const saved = process.env.RDF_WEB_SPACETIME_TOKEN;
    delete process.env.RDF_WEB_SPACETIME_TOKEN;
    try {
      const n = calls.length;
      const res = await estop(req('POST', '/api/tdm78/robots/1/estop', {}), params({ id: '1' }));
      assert.equal(res.status, 200);
      assert.equal(((await res.json()) as { path: string }).path, 'orchestrator');
      assert.equal(calls[n].path, '/robots/1/commands');
    } finally {
      process.env.RDF_WEB_SPACETIME_TOKEN = saved;
    }
  });

  it('contributor onboarding (tdm64 POST) and read-only GETs are not operator-gated', async () => {
    const r1 = await tdm64Post(req('POST', '/api/tdm64/payments/contributors/3/refresh', {}), params({ path: ['payments', 'contributors', '3', 'refresh'] }));
    assert.equal(r1.status, 200);
    const r2 = await tdm79Get(req('GET', '/api/tdm79/tickets/1/settings'), params({ path: ['tickets', '1', 'settings'] }));
    assert.equal(r2.status, 200);
  });
});

describe('/api/ops/session', () => {
  it('wrong passcode: 401, no cookie', async () => {
    const res = await session.POST(req('POST', '/api/ops/session', { passcode: 'nope' }, undefined, { 'x-forwarded-for': '10.0.0.1' }), params({}));
    assert.equal(res.status, 401);
    assert.equal(res.headers.get('set-cookie'), null);
  });

  it('right passcode: HttpOnly SameSite=Strict 12 h cookie that unlocks the guarded routes', async () => {
    const res = await session.POST(req('POST', '/api/ops/session', { passcode: PASS }), params({}));
    assert.equal(res.status, 200);
    const sc = res.headers.get('set-cookie') ?? '';
    assert.match(sc, /^rdf_op=v1\.\d+\.[^;]+; Path=\/; Max-Age=43200; HttpOnly; SameSite=Strict$/);
    assert.doesNotMatch(sc, /Secure/, 'plain http: no Secure flag');
    const cookie = sc.split(';')[0];
    const ok = await commands(req('POST', '/api/tdm78/robots/1/commands', { type: 'home' }, cookie), params({ id: '1' }));
    assert.equal(ok.status, 200);
    const who = await session.GET(req('GET', '/api/ops/session', undefined, cookie), params({}));
    assert.equal(((await who.json()) as { operator: boolean }).operator, true);
  });

  it('Secure flag when the request came over https (tunnel X-Forwarded-Proto)', async () => {
    const res = await session.POST(req('POST', '/api/ops/session', { passcode: PASS }, undefined, { 'x-forwarded-proto': 'https' }), params({}));
    assert.match(res.headers.get('set-cookie') ?? '', /; Secure$/);
  });

  it('throttles after repeated wrong passcodes', async () => {
    const h = { 'x-forwarded-for': '10.9.9.9' };
    for (let i = 0; i < 8; i++) await session.POST(req('POST', '/api/ops/session', { passcode: `bad${i}` }, undefined, h), params({}));
    const res = await session.POST(req('POST', '/api/ops/session', { passcode: PASS }, undefined, h), params({}));
    assert.equal(res.status, 429);
  });

  it('logout clears the cookie', async () => {
    const res = await session.DELETE(req('DELETE', '/api/ops/session'), params({}));
    assert.match(res.headers.get('set-cookie') ?? '', /^rdf_op=; Path=\/; Max-Age=0;/);
  });
});
