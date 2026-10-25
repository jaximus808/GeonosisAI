// [Sample] tickets (scripts/demo/seed_samples.py): the SAMPLE badge predicate, and the landing page's live
// numbers leave sample tickets and the sample robot out. `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { join, resolve } from 'node:path';
import { describe, it } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SAMPLE_SRC = resolve(fileURLToPath(new URL('.', import.meta.url)), '../src');
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith('@/')) {
      const target = join(SAMPLE_SRC, specifier.slice(2));
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

const M = (await import('@/lib/sample/sample')) as typeof import('../src/lib/sample/sample');
const L = (await import('@/lib/tdm111/landing')) as typeof import('../src/lib/tdm111/landing');

describe('sample tickets', () => {
  it('flags only task descriptions that start with [Sample]', () => {
    assert.equal(M.isSampleTicket('[Sample] Put the cube on the plate'), true);
    assert.equal(M.isSampleTicket({ taskDescription: '  [Sample] x' }), true);
    assert.equal(M.isSampleTicket('Put the cube on the plate [Sample]'), false);
    assert.equal(M.isSampleTicket({ taskDescription: 'test.seed [SEED] cup' }), false);
    assert.equal(M.isSampleTicket(null), false);
    assert.equal(M.isSampleRobot({ name: 'sample-sim-yam' }), true);
    assert.equal(M.isSampleRobot({ name: 'sim-yam' }), false);
  });

  it('leaves sample tickets and the sample robot out of the landing numbers', () => {
    const robots = [
      { name: 'sim-yam', status: 'online', cameraConfigJson: '{"sim":true}' },
      { name: 'sample-sim-yam', status: 'online', cameraConfigJson: '{"sim":true}' },
    ];
    const tickets = [
      { id: 1n, status: 'shipped', taskDescription: 'Put the cube on the plate' },
      { id: 2n, status: 'cancelled', taskDescription: 'Pick up the cup' },
      { id: 7n, status: 'shipped', taskDescription: '[Sample] Put the cube on the plate' },
      { id: 8n, status: 'failed', taskDescription: '[Sample] Stack the blocks' },
    ];
    const s = L.tdm111Stats(robots, [], tickets);
    assert.equal(s.robotsOnline?.total, 1);
    assert.equal(s.shipped, 1);
    assert.equal(L.tdm111FiledCount(tickets), 2);
    assert.deepEqual(s.latest, { id: 2, status: 'cancelled' });
  });
});
