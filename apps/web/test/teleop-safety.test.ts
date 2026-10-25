// Teleop wire safety: snap-to-grasp only on a sim robot; /control motion commands carry only the TDM-56
// contract fields (deltas from clutch-down, gripper, speed, lease stamp). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const imp = async <T>(p: string) => (await import(new URL(p, import.meta.url).href)) as T;
const safety = await imp<typeof import('../src/lib/teleop/teleopSafety')>('../src/lib/teleop/teleopSafety.ts');
const mode = await imp<typeof import('../src/lib/tdm107/robotMode')>('../src/lib/tdm107/robotMode.ts');
const src = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8');

describe('snap-to-grasp assist is sim-only', () => {
  const cases: [string | null | undefined, boolean][] = [
    ['{"sim":true}', true],
    ['{"sim":true,"cameras":["top"]}', true],
    ['{"sim":"true"}', false],
    ['{"sim":1}', false],
    ['{"sim":false}', false],
    ['{"cameras":["exo","wrist"]}', false],
    ['[{"sim":true}]', false],
    ['not json', false],
    ['', false],
    [null, false],
  ];
  for (const [cfg, want] of cases) {
    it(`camera_config_json ${JSON.stringify(cfg)} -> ${want ? 'allowed' : 'never'}`, () => {
      assert.equal(safety.teleopAssistAllowed({ cameraConfigJson: cfg }), want);
      assert.equal(safety.teleopAssistAllowed({ cameraConfigJson: cfg }), mode.tdm107IsSimRobot({ cameraConfigJson: cfg }));
    });
  }
  it('no robot row (yet) -> never', () => {
    assert.equal(safety.teleopAssistAllowed(undefined), false);
    assert.equal(safety.teleopAssistAllowed(null), false);
  });
});

describe('/control motion commands are pure deltas', () => {
  const base = { seq: 7, clutch: true, d_pos: [0.01, -0.002, 0], d_rot: [0, 0, 0.05], gripper: 0.4, speed_scale: 0.35 };
  it('keeps exactly the contract fields and the lease stamp', () => {
    const w = safety.tdm56WireCmd({ ...base, task_id: 12, claimed_at_us: 1729800000000000 })!;
    assert.deepEqual(Object.keys(w).sort(), [...safety.TDM56_CMD_FIELDS].sort());
    assert.deepEqual(w.d_pos, base.d_pos);
    assert.deepEqual(w.d_rot, base.d_rot);
  });
  it('drops any client-side IK target, joint vector or twin pose riding on the object', () => {
    const w = safety.tdm56WireCmd({
      ...base,
      ik_target: [0.3, 0.1, 0.05],
      ik_target_quat: [1, 0, 0, 0],
      joints: [0, 0.4, 0.8, 0, 0, 0],
      q: [0, 0, 0, 0, 0, 0],
      tool: [0.3, 0, 0.2],
      obj: [0.31, -0.14, 0.02],
      ee: { pos: [0.3, 0, 0.2] },
      target_pos: [0.3, 0, 0.1],
    })!;
    for (const k of Object.keys(w)) assert.ok((safety.TDM56_CMD_FIELDS as readonly string[]).includes(k), `unexpected field ${k}`);
    assert.equal(JSON.stringify(w).includes('0.31'), false);
  });
  it('refuses a malformed command (nothing is sent)', () => {
    assert.equal(safety.tdm56WireCmd({ ...base, d_pos: [NaN, 0, 0] }), null);
    assert.equal(safety.tdm56WireCmd({ ...base, d_pos: [0, 0] }), null);
    assert.equal(safety.tdm56WireCmd({ ...base, clutch: 'yes' }), null);
    assert.equal(safety.tdm56WireCmd({ ...base, gripper: Infinity }), null);
  });
  it('clamps gripper and speed to 0..1', () => {
    const w = safety.tdm56WireCmd({ ...base, gripper: 1.7, speed_scale: -2 })!;
    assert.equal(w.gripper, 1);
    assert.equal(w.speed_scale, 0);
  });
});

describe('the teleop console is wired that way', () => {
  const console_ = src('../src/components/teleop/Tdm56TeleopConsole.tsx');
  const session = src('../src/components/teleop/TeleopSession.tsx');
  it('every motion command goes through tdm56WireCmd before sendCmd', () => {
    const calls = [...console_.matchAll(/sendCmd\(([^)]*)\)/g)].map((m) => m[1]);
    assert.ok(calls.length >= 2);
    for (const a of calls) assert.equal(a.trim(), 'wire', `sendCmd(${a}) bypasses tdm56WireCmd`);
    assert.ok(/const wire = tdm56WireCmd\(/.test(console_));
  });
  it('the assist target is used only with simRobot === true, and the toggle is hidden otherwise', () => {
    assert.ok(console_.includes('const assistOn = simRobot === true && !!assistTarget;'));
    assert.ok(console_.includes('assistRef.current = assistOn ? assistTarget : undefined;'));
    assert.ok(console_.includes('hideSnap={!simRobot}'));
    assert.ok(/simRobot = false,/.test(console_), 'simRobot must default to false (real robot)');
  });
  it('the teleop page decides sim from the task robot row (camera_config_json.sim)', () => {
    assert.ok(session.includes('teleopAssistAllowed(robots[0])'));
    assert.ok(session.includes('simRobot={simRobot}'));
  });
});
