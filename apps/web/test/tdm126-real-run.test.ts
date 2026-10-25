// TDM-126: the deployment page's real-arm runs (lib/tdm126/realRun.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm126RealArm, tdm126IsRealPolicyArm, tdm126RealCheckpointOk, tdm126Body } = (await import(
  new URL('../src/lib/tdm126/realRun.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm126/realRun');

const sim = { id: 2n, name: 'sim-yam-mujoco', status: 'online', cameraConfigJson: '{"sim":true}' };
const yamTeleop = { id: 3n, name: 'yam-01', status: 'online', cameraConfigJson: '{"sim":false,"teleop":true,"policy":false}' };
const yamPolicy = { ...yamTeleop, cameraConfigJson: '{"sim":false,"teleop":true,"policy":true}' };
const oldReal = { id: 5n, name: 'yam-old', status: 'online', cameraConfigJson: '{}' };

describe('tdm126RealArm', () => {
  it('offers the collect robot of a sim ticket once it advertises policy', () => {
    assert.equal(tdm126RealArm(2n, 3n, [sim, yamPolicy])?.id, 3n);
    assert.equal(tdm126RealArm(2n, 3n, [sim, yamTeleop]), undefined);
  });
  it('a real ticket robot without policy blocks the fallback', () => {
    assert.equal(tdm126RealArm(3n, 3n, [yamTeleop, { ...yamPolicy, id: 9n, name: 'yam-02' }]), undefined);
  });
  it('falls back to the only online real policy arm', () => {
    assert.equal(tdm126RealArm(2n, 2n, [sim, yamPolicy])?.id, 3n);
    assert.equal(tdm126RealArm(2n, 2n, [sim, { ...yamPolicy, status: 'offline' }]), undefined);
    assert.equal(tdm126RealArm(2n, 2n, [sim, yamPolicy, { ...yamPolicy, id: 9n, name: 'yam-02' }]), undefined);
  });
  it('an old real robot with no policy flag never gets a real-arm button', () => {
    assert.equal(tdm126IsRealPolicyArm(oldReal), false);
    assert.equal(tdm126IsRealPolicyArm(sim), false);
  });
});

describe('checkpoints and body', () => {
  it('only real modal-vol://rdf-checkpoints/ checkpoints', () => {
    assert.equal(tdm126RealCheckpointOk('modal-vol://rdf-checkpoints/runs/ticket-14/r/1999'), true);
    assert.equal(tdm126RealCheckpointOk('fake://stub-no-gpu/tickets/14/run-5'), false);
    assert.equal(tdm126RealCheckpointOk('gs://openpi-assets/checkpoints/pi05_base/params'), false);
    assert.equal(tdm126RealCheckpointOk(null), false);
  });
  it('the body names a choice, never a URI', () => {
    const b = tdm126Body('before', 3n);
    assert.deepEqual(b, { real_arm: true, confirm: 'HANDS_ON_ESTOP', checkpoint: 'before', robot_id: 3 });
    assert.equal('checkpoint_uri' in b, false);
  });
});
