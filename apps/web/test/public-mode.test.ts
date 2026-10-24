// make demo-public: the /api/orch GET allow-list (lib/public/orchProxy.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { orchProxyTarget } = (await import(new URL('../src/lib/public/orchProxy.ts', import.meta.url).href)) as typeof import(
  '../src/lib/public/orchProxy'
);

const q = (s = '') => new URLSearchParams(s);

describe('orchProxyTarget', () => {
  it('forwards the three browser GETs', () => {
    assert.equal(orchProxyTarget(['media', 'sign'], q('uri=modal-vol://rdf-media/a b.mp4')), 'media/sign?uri=modal-vol%3A%2F%2Frdf-media%2Fa%20b.mp4');
    assert.equal(orchProxyTarget(['tickets', '42', 'decision'], q()), 'tickets/42/decision');
    assert.equal(orchProxyTarget(['agents', 'replay', 'source'], q()), 'agents/replay/source');
  });
  it('refuses everything else, including the unauthenticated orchestrator writes', () => {
    for (const parts of [['tickets'], ['tickets', '1', 'advance'], ['tickets', 'x', 'decision'], ['stream', 'token'],
      ['robots', '1', 'commands'], ['healthz'], ['media', 'list'], ['..', 'tickets']]) {
      assert.equal(orchProxyTarget(parts, q()), null, parts.join('/'));
    }
    assert.equal(orchProxyTarget(['media', 'sign'], q()), null, 'uri is required');
    assert.equal(orchProxyTarget(['media', 'sign'], q(`uri=${'a'.repeat(3000)}`)), null, 'uri length capped');
  });
});
