// TDM-104: the canonical scene spec (rdf_common.scene_spec) -> the twin's render form.
// `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const lib = (await import(new URL('../src/lib/robotview/scene.ts', import.meta.url).href)) as typeof import('../src/lib/robotview/scene');

// = rdf_common.scene_spec.CUBE_PLATE after validate()
const TDM104_CUBE_PLATE = {
  version: 1, name: 'cube_plate', task: 'Pick up the cube and place it in the empty plate.',
  physics: { timestep: 0.002 }, robot: { model: 'yam_linear_4310', mount: { pos: [0, 0, 0], yaw: 0 } },
  table: { center: [0.3, 0], size: [0.96, 0.96], top_z: 0, color: [0.62, 0.5, 0.37, 1] },
  objects: [
    { id: 'cube', primitive: 'box', size: [0.04, 0.04, 0.04], color: [0.86, 0.12, 0.1, 1], dynamic: true, graspable: true,
      pose: { pos: [0.31, -0.14, 0.02], yaw: 0 }, randomize: { region: 'cube_spawn', yaw: [-0.7, 0.7] } },
    { id: 'plate', primitive: 'plate', radius: 0.075, height: 0.012, color: [0.94, 0.94, 0.92, 1], rim_color: [0.2, 0.42, 0.8, 1],
      dynamic: false, pose: { pos: [0.31, 0.19, 0], yaw: 0 } },
    { id: 'filler', primitive: 'cylinder', radius: 0.028, height: 0.05, color: [0.95, 0.78, 0.15, 1], dynamic: true,
      pose: { pos: [0.47, -0.05, 0.0371], yaw: 0 } },
    { id: 'ball', primitive: 'sphere', radius: 0.02, color: '#00ff00', dynamic: true, pose: { pos: [0.2, 0.1, 0.02], yaw: Math.PI / 2 } },
  ],
  regions: { cube_spawn: { x: [0.25, 0.37], y: [-0.21, -0.07] }, goal: { x: [0.3, 0.4], y: [0.1, 0.2], visible: true, color: [0, 1, 0, 1] } },
  cameras: { top: { pos: [0.86, -0.5, 0.58], target: [0.27, 0.03, 0.06], fovy: 52 }, wrist: { mount: 'wrist_d405', fovy: 72 } },
  success: { type: 'object_in', object: 'cube', target: 'plate', margin: 0.012, released: true },
} as unknown as import('../src/lib/robotview/scene').SceneSpec;

describe('toRenderSpec', () => {
  const r = lib.toRenderSpec(TDM104_CUBE_PLATE);
  const by = Object.fromEntries(r.objects.map((o) => [o.id, o]));
  it('box: full extents -> half sizes, centre pose', () => {
    assert.deepEqual(by.cube.size, [0.02, 0.02, 0.02]);
    assert.deepEqual(by.cube.pose.pos, [0.31, -0.14, 0.02]);
    assert.equal(by.cube.type, 'box');
    assert.equal(by.cube.color, '#db1f1a');
    assert.equal(by.cube.fixed, false);
  });
  it('plate: a fixed cylinder centred half its height above its bottom, with a rim', () => {
    assert.equal(by.plate.type, 'cylinder');
    assert.deepEqual(by.plate.size, [0.075, 0.006]);
    assert.ok(Math.abs(by.plate.pose.pos[2] - 0.006) < 1e-9);
    assert.equal(by.plate.fixed, true);
    assert.equal(by.plate.rim_color, '#336bcc');
  });
  it('cylinder / sphere sizes and yaw quaternion', () => {
    assert.deepEqual(by.filler.size, [0.028, 0.025]);
    assert.deepEqual(by.ball.size, [0.02]);
    const q = by.ball.pose.quat!;
    assert.ok(Math.abs(q[0] - Math.SQRT1_2) < 1e-9 && Math.abs(q[3] - Math.SQRT1_2) < 1e-9);
    assert.equal(by.ball.color, '#00ff00');
  });
  it('only visible regions are drawn; cameras keep their ids', () => {
    assert.deepEqual(r.regions?.map((g) => g.id), ['goal']);
    assert.deepEqual(r.regions?.[0].size.map((v) => Math.round(v * 1000)), [50, 50]);
    assert.deepEqual(r.cameras?.map((c) => [c.id, c.attach]), [['top', 'world'], ['wrist', 'wrist']]);
  });
  it('success predicate and table', () => {
    assert.equal(r.success, 'inside(cube, plate)');
    assert.equal(lib.describeSuccess({ type: 'object_on', object: 'red_block', target: 'blue_block' }), 'on_top(red_block, blue_block)');
    assert.equal(r.table.color, '#9e805e');
    assert.deepEqual(r.table.size, [0.96, 0.96]);
  });
});
