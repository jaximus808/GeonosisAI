// TDM-111: the public allow-list of the access gate, the operator login granting access, and the landing
// page's live numbers. `pnpm --filter @rdf/web test`. Nothing here reaches maincloud.
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { join, resolve } from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const TDM111_SRC = resolve(fileURLToPath(new URL('.', import.meta.url)), '../src');
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@/')) {
      const target = join(TDM111_SRC, specifier.slice(2));
      for (const c of [target, `${target}.ts`, `${target}.tsx`]) {
        if (/\.[cm]?[jt]sx?$/.test(c) && existsSync(c)) return nextResolve(pathToFileURL(c).href, context);
      }
    }
    return nextResolve(specifier, context);
  },
});

const CODE = 'K7QM2X';
const PASS = 'tdm111-passcode-0123456789';
process.env.RDF_OPERATOR_PASSCODE = PASS;
process.env.RDF_PUBLIC_ACCESS_CODE = CODE;

const A = (await import('@/lib/tdm110/access')) as typeof import('../src/lib/tdm110/access');
const L = (await import('@/lib/tdm111/landing')) as typeof import('../src/lib/tdm111/landing');
const S = (await import('@/lib/operator/session')) as typeof import('../src/lib/operator/session');
const R = (await import('@/app/api/ops/session/route')) as typeof import('../src/app/api/ops/session/route');

const req = (path: string, extra: Partial<{ method: string; accept: string; cookie: string; operator: boolean }> = {}) => ({
  path, search: '', method: 'GET', accept: 'text/html', cookie: undefined, ...extra,
});

describe('tdm111 access gate allow-list', () => {
  it('opens the landing page, operator login, /join, short links and health without the code', () => {
    for (const p of ['/', '/join', '/ops/login', '/j/12/discord', '/api/tdm110/backend', '/_next/static/chunks/a.js', '/icon.svg', '/manifest.webmanifest']) {
      assert.deepEqual(A.tdm110AccessDecision(CODE, req(p)), { action: 'pass', access: 'required' }, p);
    }
    assert.deepEqual(A.tdm110AccessDecision(CODE, req('/api/ops/session', { method: 'POST', accept: '*/*' })), { action: 'pass', access: 'required' });
  });
  it('keeps the marketplace, tickets, teleop, robots and data APIs gated', () => {
    for (const p of ['/marketplace', '/tickets/1', '/teleop/3', '/robots/1', '/live', '/dashboard', '/ops/settings', '/j/12']) {
      const d = A.tdm110AccessDecision(CODE, req(p));
      assert.ok(d.action === 'redirect' && d.to.startsWith('/join?next='), p);
    }
    assert.deepEqual(A.tdm110AccessDecision(CODE, req('/api/tdm74/tickets', { method: 'POST', accept: '*/*' })), { action: 'deny' });
  });
  it('a valid operator session counts as access', () => {
    assert.deepEqual(A.tdm110AccessDecision(CODE, req('/marketplace', { operator: true })), { action: 'pass', access: 'ok' });
  });
});

describe('tdm111 operator login grants the access cookie', () => {
  it('POST /api/ops/session sets rdf_op and rdf_access; a wrong passcode sets neither', async () => {
    const post = (passcode: string) =>
      R.POST(new Request('https://geonosis.example/api/ops/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-forwarded-for': '198.51.100.7' },
        body: JSON.stringify({ passcode }),
      }));
    const bad = await post('wrong');
    assert.equal(bad.status, 401);
    assert.equal(bad.headers.getSetCookie().length, 0);
    const ok = await post(PASS);
    assert.equal(ok.status, 200);
    const cookies = ok.headers.getSetCookie();
    const op = cookies.find((c) => c.startsWith(`${S.OPERATOR_COOKIE}=`));
    const acc = cookies.find((c) => c.startsWith(`${A.TDM110_ACCESS_COOKIE}=`));
    assert.ok(op && acc, 'both cookies');
    assert.ok(acc.includes('SameSite=Lax') && acc.includes('HttpOnly') && acc.includes('Secure'));
    assert.ok(A.tdm110VerifyAccess(CODE, acc.split(';')[0].split('=')[1]), 'the gate accepts it');
    assert.ok(S.verifyOperatorSession(op.split(';')[0].slice(S.OPERATOR_COOKIE.length + 1)) != null);
  });
});

describe('tdm111 landing numbers', () => {
  it('counts online robots by mode, open tasks with pay, shipped and the latest ticket', () => {
    const s = L.tdm111Stats(
      [
        { name: 'yam-01', status: 'online', cameraConfigJson: '{}' },
        { name: 'sim-yam', status: 'busy', cameraConfigJson: '{"sim":true}' },
        { name: 'yam-02', status: 'offline', cameraConfigJson: '{}' },
        { name: 'test.tdm19-yam', status: 'online', cameraConfigJson: '{}' },
      ],
      [
        { status: 'open', priceCents: 350n },
        { status: 'open', priceCents: 200n },
        { status: 'claimed', priceCents: 900n },
      ],
      [
        { id: 1n, status: 'shipped' },
        { id: 3n, status: 'eval' },
        { id: 2n, status: 'failed' },
      ],
    );
    assert.deepEqual(s, {
      robotsOnline: { total: 2, sim: 1, real: 1 },
      openTasks: { count: 2, payCents: 550 },
      shipped: 1,
      latest: { id: 3, status: 'eval' },
    });
  });
  it('hides what has not synced (null, never a made-up number)', () => {
    assert.deepEqual(L.tdm111Stats(null, null, null), { robotsOnline: null, openTasks: null, shipped: null, latest: null });
    assert.equal(L.tdm111Stats(null, null, []).latest, null);
  });
});
