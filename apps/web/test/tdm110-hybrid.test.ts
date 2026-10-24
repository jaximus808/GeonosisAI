// TDM-110 hybrid deploy: orchestrator discovery + edge proof (lib/tdm110/edge.ts) and the web-side access
// code (lib/tdm110/access.ts). `pnpm --filter @rdf/web test`. The Python vectors come from
// scripts/demo/public_guard.py (edge_proof, mint_access), so both sides stay byte-compatible.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const E = (await import(new URL('../src/lib/tdm110/edge.ts', import.meta.url).href)) as typeof import('../src/lib/tdm110/edge');
const A = (await import(new URL('../src/lib/tdm110/access.ts', import.meta.url).href)) as typeof import('../src/lib/tdm110/access');

const SECRET = 'x'.repeat(32);

describe('tdm110 edge proof', () => {
  it('matches public_guard.edge_proof', () => {
    assert.equal(
      E.tdm110Proof(SECRET, 'nonce-0123456789abcdef', 'https://a-b.trycloudflare.com/'),
      '0cdf71856f7ee45fb6da55942d3044dcbb87a163b12b39e413cf7355ca1faa68',
    );
  });
});

describe('tdm110PickEndpoint', () => {
  const row = (id: number, actor: string, payload: unknown, type = E.TDM110_EVENT) => ({ id, actor, type, payload_json: JSON.stringify(payload) });
  it('takes the newest row written as actor orchestrator', () => {
    const p = E.tdm110PickEndpoint([
      row(5, 'orchestrator', { url: 'https://old.trycloudflare.com' }),
      row(9, 'web', { url: 'https://evil.example.com' }),
      row(7, 'orchestrator', { url: 'https://new.trycloudflare.com/', robot_url: 'https://r.trycloudflare.com' }),
      row(8, 'orchestrator', { url: 'https://other.example.com' }, 'orchestrator.other'),
    ]);
    assert.deepEqual(p, { id: 7, url: 'https://new.trycloudflare.com', robotUrl: 'https://r.trycloudflare.com' });
  });
  it('a state=down row (url null) wins when newest; paths and plain http are refused', () => {
    assert.equal(E.tdm110PickEndpoint([row(1, 'orchestrator', { url: 'https://a.x' }), row(2, 'orchestrator', { url: null, state: 'down' })])?.url, null);
    assert.equal(E.tdm110PickEndpoint([row(3, 'orchestrator', { url: 'https://a.x/path' })])?.url, null);
    assert.equal(E.tdm110PickEndpoint([row(3, 'orchestrator', { url: 'http://a.example.com' })])?.url, null);
    assert.equal(E.tdm110PickEndpoint([row(3, 'orchestrator', { url: 'http://127.0.0.1:8030' })])?.url, 'http://127.0.0.1:8030');
  });
});

describe('tdm110Route', () => {
  it('rewrites the sentinel, the Vercel fallback and a relative NEXT_PUBLIC base', () => {
    const env = { RDF_ORCHESTRATOR_URL: E.TDM110_SENTINEL, VERCEL: '1', NEXT_PUBLIC_ORCHESTRATOR_URL: '/api/orch', RDF_EDGE_SECRET: SECRET };
    assert.deepEqual(E.tdm110Route(`${E.TDM110_SENTINEL}/tickets/1`, env), { kind: 'discover', rest: '/tickets/1' });
    assert.deepEqual(E.tdm110Route('http://localhost:8000/stream/token', env), { kind: 'discover', rest: '/stream/token' });
    assert.deepEqual(E.tdm110Route('/api/orch/tickets', env), { kind: 'discover', rest: '/tickets' });
    assert.equal(E.tdm110Route('https://maincloud.spacetimedb.com/v1/database/x/sql', env), null);
    assert.equal(E.tdm110Route('http://localhost:8000/x', { ...env, VERCEL: undefined }), null, 'localhost only on Vercel');
  });
  it('an explicit RDF_ORCHESTRATOR_URL wins and just gets the secret', () => {
    const env = { RDF_ORCHESTRATOR_URL: 'https://orch.example.com', RDF_EDGE_SECRET: SECRET, VERCEL: '1' };
    assert.deepEqual(E.tdm110Route('https://orch.example.com/healthz', env), { kind: 'secret' });
    assert.equal(E.tdm110Route('https://orch.example.com.evil.com/x', env), null);
    assert.equal(E.tdm110Route('http://localhost:8000/x', env), null);
  });
});

function fakeFetch(handlers: { sql: () => unknown[]; proof: (url: string, nonce: string) => { status: number; body: unknown } }) {
  const calls: string[] = [];
  const f = (async (input: RequestInfo | URL) => {
    const u = String(input);
    calls.push(u);
    if (u.endsWith('/sql')) {
      const rows = handlers.sql();
      return new Response(JSON.stringify([{ schema: { elements: ['id', 'ts', 'actor', 'type', 'payload_json'].map((n) => ({ name: { some: n } })) }, rows }]));
    }
    const m = /^(.*)\/_rdf\/proof\?nonce=(.+)$/.exec(u);
    if (m) {
      const r = handlers.proof(m[1], m[2]);
      return new Response(JSON.stringify(r.body), { status: r.status });
    }
    throw new Error(`unexpected fetch ${u}`);
  }) as typeof fetch;
  return { f, calls };
}

describe('Tdm110Resolver', () => {
  const cfg = { secret: SECRET, stdbHost: 'https://stdb.test', db: 'd', stdbViaEdge: false };
  const ev = (id: number, url: string | null) => [id, 0, 'orchestrator', E.TDM110_EVENT, JSON.stringify({ url })];

  it('uses the published URL only after a matching proof, caches 10 s, follows a restart', async () => {
    let now = 1_000_000;
    let rows: unknown[] = [ev(1, 'https://one.trycloudflare.com')];
    const { f, calls } = fakeFetch({ sql: () => rows, proof: (url, nonce) => ({ status: 200, body: { proof: E.tdm110Proof(SECRET, nonce, url) } }) });
    const r = new E.Tdm110Resolver(cfg, f, () => now);
    assert.equal(await r.resolve(), 'https://one.trycloudflare.com');
    const n = calls.length;
    now += 5_000;
    assert.equal(await r.resolve(), 'https://one.trycloudflare.com');
    assert.equal(calls.length, n, 'cached');
    rows = [ev(4, 'https://two.trycloudflare.com')]; // backend restarted: new tunnel URL
    now += 6_000;
    assert.equal(await r.resolve(), 'https://two.trycloudflare.com');
    assert.match(calls.at(-2) ?? '', /\/sql$/);
    rows = [];
    now += 11_000;
    assert.equal(await r.resolve(), 'https://two.trycloudflare.com', 'no newer row: keeps the proven URL');
  });

  it('refuses a URL whose proof does not match, and reports a published state=down', async () => {
    let now = 0;
    let rows: unknown[] = [ev(1, 'https://evil.trycloudflare.com')];
    const { f } = fakeFetch({ sql: () => rows, proof: () => ({ status: 200, body: { proof: 'ab'.repeat(32) } }) });
    const r = new E.Tdm110Resolver(cfg, f, () => now);
    await assert.rejects(r.resolve(), /failed the edge proof/);
    rows = [ev(2, null)];
    now += 11_000;
    await assert.rejects(r.resolve(), /backend offline/);
  });
});

describe('tdm110 access code', () => {
  it('reads the cookie public_guard.mint_access writes (and vice versa)', () => {
    const py = 'v1.605800.z2pRITfr2Pv4csPQt_bk7rXWQ8mgEx0tUr_07J_EyMc';
    assert.equal(A.tdm110MintAccess('K7QM2X', 1000), py);
    assert.ok(A.tdm110VerifyAccess('k7qm-2x', py, 1001));
    assert.ok(!A.tdm110VerifyAccess('OTHER1', py, 1001));
    assert.ok(!A.tdm110VerifyAccess('K7QM2X', py, 605800), 'expired');
    assert.ok(!A.tdm110VerifyAccess('K7QM2X', py.replace('605800', '605801'), 1001), 'tampered');
  });
  it('one variable: RDF_PUBLIC_ACCESS_CODE, enforced by the web only on Vercel (or RDF_WEB_GATE=1)', () => {
    assert.equal(A.tdm110GateCode({ RDF_PUBLIC_ACCESS_CODE: ' K7QM2X ', VERCEL: '1' }), 'K7QM2X');
    assert.equal(A.tdm110GateCode({ RDF_PUBLIC_ACCESS_CODE: 'K7QM2X' }), '', 'behind make demo-public the edge gates');
    assert.equal(A.tdm110GateCode({ RDF_PUBLIC_ACCESS_CODE: 'K7QM2X', RDF_WEB_GATE: '1' }), 'K7QM2X');
  });
  it('gates like the edge: pages redirect, API 401, /join open', () => {
    const req = (path: string, extra: Partial<{ method: string; accept: string; cookie: string }> = {}) => ({
      path, search: '', method: 'GET', accept: 'text/html', cookie: undefined, ...extra,
    });
    assert.deepEqual(A.tdm110AccessDecision('', req('/x')), { action: 'pass', access: 'off' });
    assert.deepEqual(A.tdm110AccessDecision('K7QM2X', req('/marketplace')), { action: 'redirect', to: '/join?next=/marketplace' });
    const d = A.tdm110AccessDecision('K7QM2X', { ...req('/marketplace'), search: '?task=3&src=discord' });
    assert.ok(d.action === 'redirect');
    assert.equal(new URL(d.to, 'https://x').searchParams.get('next'), '/marketplace?task=3&src=discord', 'every query param survives');
    assert.deepEqual(A.tdm110AccessDecision('K7QM2X', req('/api/tdm74/tickets', { method: 'POST', accept: '*/*' })), { action: 'deny' });
    assert.deepEqual(A.tdm110AccessDecision('K7QM2X', req('/join')), { action: 'pass', access: 'required' });
    assert.deepEqual(A.tdm110AccessDecision('K7QM2X', req('/marketplace', { cookie: A.tdm110MintAccess('K7QM2X') })), { action: 'pass', access: 'ok' });
  });
});
