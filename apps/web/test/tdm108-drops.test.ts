// TDM-108: the /api/tdm108 allow-list and the /j short link (lib/tdm108/proxy.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm108Route, tdm108ShortTarget } = (await import(new URL('../src/lib/tdm108/proxy.ts', import.meta.url).href)) as typeof import(
  '../src/lib/tdm108/proxy'
);

const q = (s = '') => new URLSearchParams(s);

describe('tdm108Route', () => {
  it('operator routes need a login, share and claim-src do not', () => {
    assert.deepEqual(tdm108Route('GET', ['drops'], q()), { target: 'drops', operator: true });
    assert.deepEqual(tdm108Route('GET', ['drops', 'task-12', 'card', 'discord.png'], q()), {
      target: 'drops/task-12/card/discord.png',
      operator: true,
    });
    assert.deepEqual(tdm108Route('POST', ['drops', 'task-12', 'post'], q()), { target: 'drops/task-12/post', operator: true });
    assert.deepEqual(tdm108Route('POST', ['drops', 'preview'], q()), { target: 'drops/preview', operator: true });
    assert.deepEqual(tdm108Route('POST', ['claim-src'], q()), { target: 'drops/claim-src', operator: false });
    assert.deepEqual(tdm108Route('PUT', ['drops', 'tickets', '4', 'channels'], q()), { target: 'drops/tickets/4/channels', operator: true });
    assert.deepEqual(tdm108Route('GET', ['drops', 'tickets', '4', 'channels'], q()), { target: 'drops/tickets/4/channels', operator: true });
    assert.equal(tdm108Route('PUT', ['drops', 'task-1', 'post'], q()), null);
    assert.deepEqual(tdm108Route('GET', ['share', '12.png'], q('size=wide&download=1')), {
      target: 'drops/share/12.png?size=wide',
      operator: false,
      download: 'geonosisai-task-12-wide.png',
    });
    assert.equal(tdm108Route('GET', ['share', '12.png'], q('size=huge'))?.target, 'drops/share/12.png?size=square');
  });
  it('refuses everything else', () => {
    for (const parts of [['drops', '..', 'card', 'x.png'], ['drops', 'task-1', 'card', 'mastodon.png'], ['share', 'abc.png'],
      ['share', '12'], ['push', 'subscribe'], ['drops', 'task-1'], ['tickets']]) {
      assert.equal(tdm108Route('GET', parts, q()), null, parts.join('/'));
    }
    assert.equal(tdm108Route('POST', ['drops'], q()), null);
    assert.equal(tdm108Route('POST', ['drops', 'Task_1', 'post'], q()), null);
  });
});

describe('tdm108ShortTarget', () => {
  it('maps /j/N/src to the marketplace deep link', () => {
    assert.equal(tdm108ShortTarget('12', 'discord'), '/marketplace?task=12&src=discord');
    assert.equal(tdm108ShortTarget('0', 'x'), null);
    assert.equal(tdm108ShortTarget('12', 'https://evil'), null);
    assert.equal(tdm108ShortTarget('1e3', 'x'), null);
  });
});

// ---- TDM-108 per-task invites (lib/tdm108/invite.ts) + the Vercel gate (lib/tdm110/access.ts)
const inv = (await import(new URL('../src/lib/tdm108/invite.ts', import.meta.url).href)) as typeof import('../src/lib/tdm108/invite');
const acc = (await import(new URL('../src/lib/tdm110/access.ts', import.meta.url).href)) as typeof import('../src/lib/tdm110/access');
const TDM108_SECRET = 'tdm108-test-secret-0123456789abcdef';

describe('tdm108 per-task invites', () => {
  it('match the orchestrator and edge vectors', () => {
    // pinned in services/orchestrator/tests/test_tdm108_job_drops.py and scripts/demo/test_public_guard.py
    assert.equal(inv.tdm108InviteK(TDM108_SECRET, 7, 'discord'), '3-ZQ1oRJDbQOYT503udnHy');
    assert.equal(inv.tdm108InviteCode(TDM108_SECRET, 7, 'discord'), 'CXGYS8');
    const v = 't1.7-9.3.2.1800003600.w1_WyOK6ITs-MQnszOKl3byLs8vASzkb';
    assert.equal(inv.tdm108MintTaskCookie(TDM108_SECRET, [7, 9], 3, 2, 1800000000, 3600), v);
    assert.deepEqual(inv.tdm108VerifyTaskCookie(TDM108_SECRET, v, 1800000000), { ids: [7, 9], ticket: 3, robot: 2, exp: 1800003600 });
    assert.equal(inv.tdm108VerifyTaskCookie(TDM108_SECRET, v.replace('t1.7-9', 't1.8-9'), 1800000000), null);
    assert.equal(inv.tdm108VerifyTaskCookie('another-secret', v, 1800000000), null);
    assert.equal(inv.tdm108VerifyTaskCookie(TDM108_SECRET, v, 1800003601), null, 'expired');
  });
  it('an invite is for one task and one channel', () => {
    assert.ok(inv.tdm108CheckInvite(TDM108_SECRET, 7, 'discord', '3-ZQ1oRJDbQOYT503udnHy'));
    assert.ok(inv.tdm108CheckInvite(TDM108_SECRET, 7, 'discord', null, 'cxg-ys8'));
    assert.ok(!inv.tdm108CheckInvite(TDM108_SECRET, 8, 'discord', '3-ZQ1oRJDbQOYT503udnHy'));
    assert.ok(!inv.tdm108CheckInvite(TDM108_SECRET, 7, 'x', null, 'CXGYS8'));
    assert.ok(!inv.tdm108CheckInvite('', 7, 'discord', '3-ZQ1oRJDbQOYT503udnHy'));
  });
  it('the Vercel gate: a task-7 invite opens task 7 only, not task 8, tickets or the dashboard', () => {
    const scope = { ids: [7], ticket: 3, robot: 2, exp: 0 };
    const decide = (path: string, search = '', method = 'GET') =>
      acc.tdm110AccessDecision('K7QM2X', {
        path, search, method, accept: 'text/html', cookie: undefined,
        task: { ids: scope.ids, allows: inv.tdm108TaskScopeAllows(scope, method, path, search) },
      });
    for (const [p, s] of [['/marketplace', '?task=7&src=discord'], ['/teleop/7', ''], ['/reset/7', ''], ['/practice', '?ticket=3']]) {
      assert.deepEqual(decide(p, s), { action: 'pass', access: 'task:7' }, p + s);
    }
    for (const [p, s] of [['/marketplace', '?task=8'], ['/marketplace', ''], ['/teleop/8', ''], ['/tickets/3', ''],
      ['/dashboard', ''], ['/practice', '?ticket=4'], ['/robots/2', ''], ['/earnings', '?contributor=3']]) {
      assert.equal(decide(p, s).action, 'redirect', p + s);
    }
    assert.deepEqual(decide('/api/tdm55/stream-token', '', 'POST'), { action: 'pass', access: 'task:7' });
    assert.deepEqual(decide('/api/tdm62/resets/7'), { action: 'pass', access: 'task:7' });
    for (const p of ['/api/tdm62/resets/8', '/api/tdm104/robots/5/scene', '/api/tdm64/payments/mode', '/api/tdm108/drops']) {
      assert.equal(decide(p).action, 'deny', p);
    }
    // TDM-86: /join shows the invitee the join form (scope task:7), not the site-wide code form; without the
    // invite it stays open and asks for the code. The operator and the site-wide cookie still open everything.
    assert.deepEqual(decide('/join'), { action: 'pass', access: 'task:7' });
    // TDM-86: the static files practice and teleop load (the 3D arm), and the push worker
    for (const p of ['/robot/yam/yam.glb', '/tdm65-sw.js', '/tdm65/icon-192.png']) assert.deepEqual(decide(p), { action: 'pass', access: 'task:7' }, p);
    assert.equal(decide('/robot/../tickets/3').action, 'redirect');
    assert.deepEqual(decide('/earnings'), { action: 'pass', access: 'task:7' }); // TDM-86: their own payout
    assert.deepEqual(acc.tdm110AccessDecision('K7QM2X', { path: '/join', search: '', method: 'GET', accept: 'text/html', cookie: undefined }),
      { action: 'pass', access: 'required' });
    assert.deepEqual(acc.tdm110AccessDecision('K7QM2X', { path: '/tickets/3', search: '', method: 'GET', accept: 'text/html',
      cookie: acc.tdm110MintAccess('K7QM2X') }), { action: 'pass', access: 'ok' });
  });
});
