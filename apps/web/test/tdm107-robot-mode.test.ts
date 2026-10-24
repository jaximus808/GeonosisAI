// TDM-107: per-ticket robot mode labels (lib/tdm107/robotMode.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm107IsSimRobot, tdm107ModeOf, tdm107SimIds, tdm107DefaultMode } = (await import(
  new URL('../src/lib/tdm107/robotMode.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm107/robotMode');

const sim = { id: 2n, cameraConfigJson: JSON.stringify({ sim: true, cameras: 'mujoco' }) };
const real = { id: 1n, cameraConfigJson: JSON.stringify({ cameras: 'config' }) };

describe('tdm107 robot mode', () => {
  it('labels a robot sim only on a literal "sim": true (same rule as the reducer)', () => {
    assert.equal(tdm107IsSimRobot(sim), true);
    assert.equal(tdm107IsSimRobot(real), false);
    assert.equal(tdm107IsSimRobot({ cameraConfigJson: '{"sim":"yes"}' }), false);
    assert.equal(tdm107IsSimRobot({ cameraConfigJson: 'not json' }), false);
    assert.equal(tdm107ModeOf(sim), 'sim');
    assert.equal(tdm107ModeOf(real), 'real');
    assert.equal(tdm107ModeOf(undefined), null);
  });
  it('collects the sim robot ids so each row is labelled by its own robot', () => {
    assert.deepEqual([...tdm107SimIds([sim, real])], [2n]);
  });
  it('defaults new tickets to the real arm only when one is online', () => {
    assert.equal(tdm107DefaultMode([sim]), 'sim');
    assert.equal(tdm107DefaultMode([sim, real]), 'real');
    assert.equal(tdm107DefaultMode([]), 'sim');
  });
});
