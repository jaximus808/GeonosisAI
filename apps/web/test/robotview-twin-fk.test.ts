// TDM-97: the 3D twin's joint conventions (zero pose, axis signs) match the robot side.
// Reference values come from spikes/tdm-97/fk_reference.py: rdf_robot_client.adapters.yam_kinematics
// (PoE) and MuJoCo on the SDK-combined model (what the sim renders), for 3 poses.
// `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const fkLib = (await import(new URL('../src/components/robotview/yamFk.ts', import.meta.url).href)) as typeof import(
  '../src/components/robotview/yamFk'
);
const { YAM_MODEL } = (await import(new URL('../src/components/robotview/yamModel.ts', import.meta.url).href)) as typeof import(
  '../src/components/robotview/yamModel'
);
const fk = fkLib.createYamKinematics(YAM_MODEL);

type Ref = Record<string, { q: number[]; poe_pos: number[]; poe_z_axis: number[]; mujoco_pos: number[] }>;
const REF = JSON.parse(
  readFileSync(new URL('../../../spikes/tdm-97/fk_reference.json', import.meta.url), 'utf8'),
) as Ref;

const dist = (a: number[], b: number[]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

describe('YAM twin FK vs yam_kinematics + MuJoCo', () => {
  for (const [name, r] of Object.entries(REF)) {
    it(`${name}: tool point within 0.1 mm, approach axis within 0.001`, () => {
      const m = fk.graspSite(r.q);
      const pos = [m[12], m[13], m[14]];
      assert.ok(dist(pos, r.poe_pos) < 1e-4, `${name} twin ${pos} vs poe ${r.poe_pos}`);
      assert.ok(dist(pos, r.mujoco_pos) < 1e-4, `${name} twin ${pos} vs mujoco ${r.mujoco_pos}`);
      const z = [m[8], m[9], m[10]];
      assert.ok(dist(z, r.poe_z_axis) < 1e-3, `${name} approach ${z} vs ${r.poe_z_axis}`);
    });
  }

  it('rest pose points the tool along base +X at (0.255, 0, 0.173)', () => {
    const p = fk.graspSitePos(fkLib.YAM_REST);
    assert.ok(dist(p, [0.255, 0, 0.173]) < 1e-3, String(p));
  });

  it('gripper 1 = open (slide 0), 0 = closed (full travel)', () => {
    assert.equal(fk.slide(1), 0);
    assert.equal(fk.slide(0), 0.0475);
    assert.equal(fk.slide(Number.NaN), 0);
  });
});
