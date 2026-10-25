// TDM-113: text job offer helpers (lib/tdm113/texts.ts) and the task-scope allow for /api/tdm113. `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const { tdm113Route, tdm113WorkTarget, tdm113PhoneOk, TDM113_CONSENT_TEXT } = (await import(
  new URL('../src/lib/tdm113/texts.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm113/texts');
const { tdm108TaskScopeAllows } = (await import(new URL('../src/lib/tdm108/invite.ts', import.meta.url).href)) as typeof import(
  '../src/lib/tdm108/invite'
);

describe('tdm113Route', () => {
  it('forwards the contributor posts and the operator status only', () => {
    assert.deepEqual(tdm113Route('POST', ['optin']), { target: 'texts/optin', operator: false });
    assert.deepEqual(tdm113Route('POST', ['state']), { target: 'texts/state', operator: false });
    assert.deepEqual(tdm113Route('POST', ['optout']), { target: 'texts/optout', operator: false });
    assert.deepEqual(tdm113Route('GET', ['status']), { target: 'texts/status', operator: true });
    assert.equal(tdm113Route('POST', ['webhook']), null); // Inkbox reaches the orchestrator through its own tunnel
    assert.equal(tdm113Route('GET', ['optin']), null);
    assert.equal(tdm113Route('POST', ['optin', 'x']), null);
  });
});

describe('tdm113WorkTarget', () => {
  it('sends a text claimant straight to the work page of a claimed task', () => {
    assert.equal(tdm113WorkTarget('12', 'recovery_takeover', 'claimed'), '/teleop/12');
    assert.equal(tdm113WorkTarget('12', 'clean_teleop', 'claimed'), '/teleop/12');
    assert.equal(tdm113WorkTarget('12', 'reset', 'claimed'), '/reset/12');
    assert.equal(tdm113WorkTarget('12', 'reset', 'open'), null); // not claimed: the marketplace deep link
  });
});

describe('tdm113PhoneOk', () => {
  it('accepts US 10/11 digit and +E.164, rejects the rest', () => {
    assert.ok(tdm113PhoneOk('(415) 555-0123'));
    assert.ok(tdm113PhoneOk('1 415 555 0123'));
    assert.ok(tdm113PhoneOk('+44 20 7946 0958'));
    assert.ok(!tdm113PhoneOk('555-0123'));
    assert.ok(!tdm113PhoneOk('+0123'));
  });
});

describe('consent text', () => {
  it('matches the orchestrator byte for byte (it refuses any other wording)', () => {
    const py = readFileSync(new URL('../../../services/orchestrator/src/rdf_orchestrator/text_offers.py', import.meta.url), 'utf8');
    assert.ok(py.includes(`CONSENT_TEXT = "${TDM113_CONSENT_TEXT}"`));
  });
});

describe('task-scope cookie', () => {
  it('lets a drop-invite visitor opt in to texts (POST only)', () => {
    const scope = { ids: [5], ticket: 1, robot: 2, exp: 0 };
    assert.ok(tdm108TaskScopeAllows(scope, 'POST', '/api/tdm113/optin', ''));
    assert.ok(tdm108TaskScopeAllows(scope, 'POST', '/api/tdm113/state', ''));
    assert.ok(!tdm108TaskScopeAllows(scope, 'GET', '/api/tdm113/status', ''));
  });
});

describe('two devices, one person', () => {
  it('proxies the release / takeover / phone sign-in posts, and the task scope allows them', async () => {
    for (const p of ['release', 'takeover', 'login_code', 'login']) {
      assert.deepEqual(tdm113Route('POST', [p]), { target: `texts/${p}`, operator: false });
      assert.ok(tdm108TaskScopeAllows({ ids: [7], ticket: 1, robot: 4, exp: 0 }, 'POST', `/api/tdm113/${p}`, ''));
    }
    assert.equal(tdm113Route('GET', ['login']), null);
  });

  it('takes over a claim this browser did not drive, never its own', async () => {
    const { tdm57TakeoverNeeded } = (await import(new URL('../src/lib/tdm113/texts.ts', import.meta.url).href)) as typeof import(
      '../src/lib/tdm113/texts'
    );
    assert.equal(tdm57TakeoverNeeded('held', 1000, undefined), true); // text YES claim / other device
    assert.equal(tdm57TakeoverNeeded('held', 1000, 900), true); // the other device's newer generation
    assert.equal(tdm57TakeoverNeeded('held', 1000, 1000), false); // this browser's own (reload)
    assert.equal(tdm57TakeoverNeeded('view_only', 1000, undefined), true); // asks; the server decides
    assert.equal(tdm57TakeoverNeeded('available', undefined, undefined), false);
    assert.equal(tdm57TakeoverNeeded('closed', 1000, undefined), false);
  });

  it('accepts 6-digit codes only', async () => {
    const { tdm113CodeOk } = (await import(new URL('../src/lib/tdm113/texts.ts', import.meta.url).href)) as typeof import(
      '../src/lib/tdm113/texts'
    );
    assert.equal(tdm113CodeOk('123 456'), '123456');
    assert.equal(tdm113CodeOk('123-456'), '123456');
    assert.equal(tdm113CodeOk('12345'), null);
    assert.equal(tdm113CodeOk('12345a'), null);
  });
});
