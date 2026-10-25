// TDM-125: split-robot tickets (lib/tdm125/collect.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm125Split, tdm125CollectWaiting, tdm125CollectRobotIdSetting } = (await import(
  new URL('../src/lib/tdm125/collect.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm125/collect');

const sim = { id: 2n, name: 'sim-yam-mujoco', status: 'online', cameraConfigJson: '{"sim":true}' };
const yam = { id: 3n, name: 'yam-01', status: 'online', cameraConfigJson: '{"cameras":[]}' };

describe('tdm125Split', () => {
  it('not split without collect_robot_id', () => {
    const s = tdm125Split({ robotId: 2n }, { hyperparamsJson: '{}' }, [sim, yam]);
    assert.equal(s.split, false);
    assert.equal(s.collectRobotId, 2n);
    assert.equal(s.line, null);
  });
  it('sim eval + real collect: the line counts accepted episodes on the collect robot only', () => {
    const eps = [
      { robotId: 3n, kind: 'clean_teleop', reviewStatus: 'accepted' },
      { robotId: 3n, kind: 'clean_teleop', reviewStatus: 'accepted' },
      { robotId: 3n, kind: 'clean_teleop', reviewStatus: 'rejected' },
      { robotId: 2n, kind: 'clean_teleop', reviewStatus: 'accepted' },
      { robotId: 2n, kind: 'eval', reviewStatus: 'not_reviewed' },
    ];
    const s = tdm125Split({ robotId: 2n }, { hyperparamsJson: '{"collect_robot_id":3}' }, [sim, yam], eps);
    assert.equal(s.split, true);
    assert.equal(s.collectMode, 'real');
    assert.equal(s.evalMode, 'sim');
    assert.equal(s.line, 'Data: 2 real-arm episodes (yam-01) · Evals: sim robot');
  });
  it('collect_robot_id equal to the ticket robot is not a split', () => {
    assert.equal(tdm125Split({ robotId: 3n }, { hyperparamsJson: '{"collect_robot_id":3}' }, [sim, yam]).split, false);
    assert.equal(tdm125CollectRobotIdSetting({ hyperparamsJson: 'nope' }), 0n);
  });
});

describe('tdm125CollectWaiting', () => {
  it('offline collect robot during dispatch: Waiting for yam-01 (offline)', () => {
    const s = tdm125Split({ robotId: 2n }, { hyperparamsJson: '{"collect_robot_id":3}' }, [sim, { ...yam, status: 'offline' }]);
    const w = tdm125CollectWaiting(s, 'dispatch', 3);
    assert.equal(w?.reason, 'robot_offline');
    assert.match(w!.text, /^Waiting for yam-01 \(offline\)/);
    assert.equal(w?.stationKey, 'collect');
    assert.equal(tdm125CollectWaiting(s, 'eval', 3), undefined);
  });
  it('online collect robot: no wait', () => {
    const s = tdm125Split({ robotId: 2n }, { hyperparamsJson: '{"collect_robot_id":3}' }, [sim, yam]);
    assert.equal(tdm125CollectWaiting(s, 'dispatch', 3), undefined);
  });
});
