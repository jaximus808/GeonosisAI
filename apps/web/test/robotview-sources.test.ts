// TDM-98: the robot view's source registry (lib/robotview/sources.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { RobotRowLike } from '../src/lib/robotview/sources';

// Imported by URL so tsc (no allowImportingTsExtensions) and node --test (needs the .ts) both accept it.
const { buildRegistry, classifyRobot, initialMode, isOnline, pickSource, tokenRobotId } = (await import(
  new URL('../src/lib/robotview/sources.ts', import.meta.url).href
)) as typeof import('../src/lib/robotview/sources');

const NOW = 1_800_000_000_000;
const hb = (msAgo: number) => ({ microsSinceUnixEpoch: BigInt(NOW - msAgo) * 1000n });
const row = (o: Partial<RobotRowLike> & { id: number; name: string }): RobotRowLike => ({
  embodiment: 'i2rt_yam',
  status: 'online',
  adapterVersion: 'yam-0.3',
  cameraConfigJson: '{}',
  lastHeartbeat: hb(1000),
  ...o,
});

describe('classifyRobot', () => {
  it('sim from camera_config_json.sim (rdf-robot-client --sim)', () => {
    assert.equal(classifyRobot(row({ id: 1, name: 'a', cameraConfigJson: '{"cameras":"synthetic","sim":true}' })).kind, 'sim');
  });
  it('sim from an adapter or embodiment name containing sim / mujoco', () => {
    assert.equal(classifyRobot(row({ id: 1, name: 'a', embodiment: 'sim_yam' })).kind, 'sim');
    assert.equal(classifyRobot(row({ id: 1, name: 'a', cameraConfigJson: '{"arm":{"adapter":"mujoco"}}' })).kind, 'sim');
    assert.equal(classifyRobot(row({ id: 1, name: 'a', adapterVersion: 'MuJoCo-3.2' })).kind, 'sim');
    assert.equal(classifyRobot(row({ id: 1, name: 'a', cameraConfigJson: '{"arm":{"adapter":"yam","kwargs":{"sim":true}}}' })).kind, 'sim');
  });
  it('live otherwise, including bad JSON and sim:false', () => {
    assert.equal(classifyRobot(row({ id: 1, name: 'a' })).kind, 'live');
    assert.equal(classifyRobot(row({ id: 1, name: 'a', cameraConfigJson: 'not json' })).kind, 'live');
    assert.equal(classifyRobot(row({ id: 1, name: 'a', cameraConfigJson: '{"sim":false}' })).kind, 'live');
  });
});

describe('isOnline', () => {
  it('needs a non-offline status and a fresh heartbeat', () => {
    assert.equal(isOnline(row({ id: 1, name: 'a' }), NOW), true);
    assert.equal(isOnline(row({ id: 1, name: 'a', status: 'offline' }), NOW), false);
    assert.equal(isOnline(row({ id: 1, name: 'a', lastHeartbeat: hb(60_000) }), NOW), false);
    assert.equal(isOnline(row({ id: 1, name: 'a', lastHeartbeat: null }), NOW), false);
  });
});

describe('registry + selection', () => {
  const reg = buildRegistry(
    [
      row({ id: 1, name: 'yam-1', status: 'offline', lastHeartbeat: hb(3_600_000) }),
      row({ id: 2, name: 'test.sim-yam-1', cameraConfigJson: '{"sim":true}' }),
      row({ id: 3, name: 'sim-b', cameraConfigJson: '{"sim":true}', lastHeartbeat: hb(90_000) }),
      row({ id: 4, name: 'sim-c', cameraConfigJson: '{"sim":true}' }),
    ],
    NOW,
  );
  it('splits by kind, online first, test names last', () => {
    assert.deepEqual(reg.live.map((s) => s.id), [1]);
    assert.deepEqual(reg.sim.map((s) => s.id), [4, 2, 3]);
  });
  it('Live never picks a sim robot, even when pinned or remembered', () => {
    const noLive = buildRegistry([row({ id: 2, name: 's', cameraConfigJson: '{"sim":true}' })], NOW);
    assert.equal(pickSource(noLive, 'live', 2, 2), null);
    assert.equal(pickSource(reg, 'live', 4, 4)?.id, 1);
  });
  it('pinned robot wins its own kind, then the remembered pick, then the first', () => {
    assert.equal(pickSource(reg, 'sim', 3, 2)?.id, 3);
    assert.equal(pickSource(reg, 'sim', 1, 2)?.id, 2);
    assert.equal(pickSource(reg, 'sim', null, 99)?.id, 4);
  });
  it('mode: remembered, else defaultMode (ticket robot mode), else sim', () => {
    assert.equal(initialMode('sim', 'live'), 'sim');
    assert.equal(initialMode('live', 'sim'), 'live');
    assert.equal(initialMode(null, 'live'), 'live');
    assert.equal(initialMode('junk', undefined), 'sim');
    assert.equal(initialMode(null), 'sim');
  });
});

describe('tokenRobotId', () => {
  it('reads robot_id from the payload, null when opaque', () => {
    const p = Buffer.from(JSON.stringify({ v: 1, robot_id: 7 })).toString('base64url');
    assert.equal(tokenRobotId(`${p}.sig`), 7);
    assert.equal(tokenRobotId('garbage'), null);
  });
});
