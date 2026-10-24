// TDM-106: easier driving + practice on the scene spec. `pnpm --filter @rdf/web test`.
// The last test is the "only the on-screen hints" check: a bot that presses ONLY the keys the coach
// highlights picks the cube and puts it on the plate of the sim's default scene.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const imp = async <T>(p: string) => (await import(new URL(p, import.meta.url).href)) as T;
const fk = await imp<typeof import('../src/components/robotview/yamFk')>('../src/components/robotview/yamFk.ts');
const model = await imp<typeof import('../src/components/robotview/yamModel')>('../src/components/robotview/yamModel.ts');
const ikLib = await imp<typeof import('../src/lib/teleop/tdm106Ik')>('../src/lib/teleop/tdm106Ik.ts');
const drive = await imp<typeof import('../src/lib/teleop/tdm106Drive')>('../src/lib/teleop/tdm106Drive.ts');
const guideLib = await imp<typeof import('../src/lib/teleop/tdm106Guide')>('../src/lib/teleop/tdm106Guide.ts');
const simLib = await imp<typeof import('../src/lib/teleop/tdm106SceneSim')>('../src/lib/teleop/tdm106SceneSim.ts');
const input = await imp<typeof import('../src/lib/teleop/tdm56Input')>('../src/lib/teleop/tdm56Input.ts');

const ik = ikLib.createTdm106Ik(fk.createYamKinematics(model.YAM_MODEL), model.YAM_BODIES);

// The sim's default scene (lib/robotview/scene.ts SIM_DEFAULT_SCENE), inlined so this test does not
// depend on that file's other exports.
const SCENE = {
  version: 1 as const,
  task: 'Pick up the cube and place it in the empty plate.',
  robot: { mount: { pos: [0, 0, 0] as [number, number, number], quat: [1, 0, 0, 0] as [number, number, number, number] } },
  table: { size: [0.96, 0.96] as [number, number], center: [0.3, 0] as [number, number], top_z: 0, thickness: 0.04 },
  objects: [
    { id: 'cube', type: 'box' as const, size: [0.02, 0.02, 0.02], color: '#db1f1a', pose: { pos: [0.31, -0.14, 0.02] as [number, number, number], quat: [1, 0, 0, 0] as [number, number, number, number] }, randomize: { pos: [[-0.06, -0.07, 0], [0.06, 0.07, 0]] as [[number, number, number], [number, number, number]], yaw: [-0.7, 0.7] as [number, number] } },
    { id: 'plate', type: 'cylinder' as const, size: [0.075, 0.006], color: '#f0f0eb', fixed: true, pose: { pos: [0.31, 0.19, 0.006] as [number, number, number] } },
  ],
  regions: [{ id: 'plate', type: 'disc' as const, size: [0.075], pos: [0.31, 0.19, 0] as [number, number, number] }],
  success: 'inside(cube, plate)',
};

describe('tdm106Ik', () => {
  it('reaches the cube and the plate with the home tool orientation', () => {
    const R = ik.toolRot(fk.YAM_READY);
    for (const p of [[0.31, -0.14, 0.02], [0.31, 0.19, 0.05], [0.4, 0, 0.1], [0.2, 0.2, 0.15]] as [number, number, number][]) {
      let q = fk.YAM_READY.slice();
      const from = ik.toolPos(q);
      for (let s = 1; s <= 30; s++) q = ik.ik(from.map((v, i) => v + ((p[i] - v) * s) / 30) as [number, number, number], R, q, 4).q;
      const r = ik.ik(p, R, q, 6);
      assert.ok(r.posErr < 1e-3, `${p}: ${r.posErr}`);
    }
  });
});

describe('tdm106Drive', () => {
  it('moves only on translation keys unless rotation is on; Shift is precise', () => {
    const held = new Set(['KeyW', 'ArrowLeft']);
    assert.deepEqual(drive.tdm106KeyTwist(held, { rotation: false, shift: false }), { lin: [1, 0, 0], ang: [0, 0, 0] });
    assert.deepEqual(drive.tdm106KeyTwist(held, { rotation: true, shift: false }).ang, [0, 0, 1]);
    assert.ok(Math.abs(drive.tdm106KeyTwist(held, { rotation: false, shift: true }).lin[0] - drive.TDM106_PRECISE) < 1e-9);
    assert.equal(drive.tdm106KeysWantDrive(new Set(['ArrowLeft']), false), false);
    assert.equal(drive.tdm106KeysWantDrive(new Set(['KeyE']), false), true);
  });

  it('ramps up smoothly and stops fast', () => {
    const r = new drive.Tdm106Ramp();
    const full = { lin: [1, 0, 0] as [number, number, number], ang: [0, 0, 0] as [number, number, number] };
    const first = r.step(full, 1 / 30).lin[0];
    assert.ok(first > 0 && first < 0.15, `first tick ${first}`);
    for (let i = 0; i < 12; i++) r.step(full, 1 / 30);
    assert.equal(r.cur.lin[0], 1);
    for (let i = 0; i < 3; i++) r.step(input.ZERO_TWIST, 1 / 30);
    assert.equal(r.cur.lin[0], 0);
  });

  it('G toggles, hold mode closes while held, and every change is a short gripper pulse', () => {
    const g = new drive.Tdm106Gripper();
    g.keyDown(1000);
    assert.equal(g.value, 0);
    assert.ok(g.pulse(1500) && !g.pulse(1000 + drive.TDM106_GRIP_PULSE_MS + 1));
    g.keyDown(3000);
    assert.equal(g.value, 1);
    g.mode = 'hold';
    g.keyDown(5000);
    assert.equal(g.value, 0);
    g.keyUp(5200);
    assert.equal(g.value, 1);
  });

  it('snap pulls toward the object only when close', () => {
    assert.deepEqual(drive.tdm106Snap([0.3, 0, 0.1], [0.5, 0, 0.02]), [0, 0]); // far
    const [x, y] = drive.tdm106Snap([0.3, 0, 0.06], [0.32, -0.01, 0.02]);
    assert.ok(x > 0 && y < 0);
  });
});

describe('Tdm106SceneSim + coach', () => {
  it('parses inside(cube, plate)', () => {
    assert.deepEqual(simLib.tdm106Task(SCENE), { obj: 'cube', goal: 'plate' });
  });

  it('a bot pressing only the highlighted keys finishes pick-and-place in under 2 minutes', () => {
    hintBot(SCENE);
  });

  it('runs the deployed canonical scene (cube_plate.json through TDM-104 toRenderSpec)', async () => {
    const sceneLib = await imp<typeof import('../src/lib/robotview/scene')>('../src/lib/robotview/scene.ts');
    const raw = JSON.parse(readFileSync(new URL('../../../services/robot-client/src/rdf_robot_client/sim_assets/scenes/cube_plate.json', import.meta.url), 'utf8'));
    const spec = sceneLib.toRenderSpec(raw);
    assert.deepEqual(simLib.tdm106Task(spec), { obj: 'cube', goal: 'plate' });
    hintBot(spec);
  });
});

function hintBot(spec: Parameters<typeof simLib.tdm106Task>[0]) {
  {
    const sim = new simLib.Tdm106SceneSim({ spec, ik, home: fk.YAM_READY });
    const integ = new input.Tdm56ClutchIntegrator();
    const ramp = new drive.Tdm106Ramp();
    const grip = new drive.Tdm106Gripper();
    const dt = 1 / 30;
    let now = 0;
    let engaged = false;
    let seq = 0;
    let lastG = -1e9;
    let gHeld = false;
    let done = false;
    const steps = new Set<string>();
    for (let tick = 0; tick < 120 * 30 && !done; tick++) {
      now += dt * 1000;
      const obj = sim.taskObject();
      const guide = guideLib.tdm106Guide({ tool: sim.tool, gripperOpen: grip.open, gripperMoving: Math.abs(sim.jaw - sim.gripperCmd) > 0.02 && !obj?.held, obj, goal: sim.goal(), done: sim.succeeded });
      steps.add(guide.step);
      // a person reads the hint and presses its keys; G is a tap (press, then wait for it to act)
      const held = new Set(guide.keys.filter((k) => k !== 'KeyG'));
      if (guide.keys.includes('KeyG') && held.size === 0) {
        if (!gHeld && now - lastG > 1500) {
          grip.keyDown(now);
          lastG = now;
          gHeld = true;
        }
      } else gHeld = false;
      if (gHeld && now - lastG > 100) gHeld = false;
      const want = drive.tdm106KeysWantDrive(held, false) || grip.pulse(now);
      if (want) {
        if (!engaged) {
          integ.reset();
          ramp.reset();
          engaged = true;
        }
        let tw = ramp.step(drive.tdm106KeyTwist(held, { rotation: false, shift: false }), dt);
        const snap = obj && !obj.held ? drive.tdm106Snap(sim.tool, obj.pos) : [0, 0];
        if (held.size) tw = { lin: [tw.lin[0] + snap[0], tw.lin[1] + snap[1], tw.lin[2]], ang: tw.ang };
        integ.step(tw, dt, drive.TDM106_DEFAULT_SPEED);
        sim.command({ seq: ++seq, clutch: true, d_pos: integ.dPos, d_rot: integ.dRot, gripper: grip.value, speed_scale: drive.TDM106_DEFAULT_SPEED }, now);
      } else if (engaged) {
        engaged = false;
        sim.command({ seq: ++seq, clutch: false, d_pos: integ.dPos, d_rot: integ.dRot, gripper: grip.value, speed_scale: drive.TDM106_DEFAULT_SPEED }, now);
      }
      sim.step(dt, now);
      for (const ev of sim.drainEvents()) if (ev.k === 'drop' && ev.success) done = true;
    }
    assert.ok(done, `not done after 2 min; steps seen: ${[...steps].join(',')}; tool ${sim.tool.map((v) => v.toFixed(3))}`);
    assert.ok(now < 120_000, `took ${(now / 1000).toFixed(1)} s`);
    console.log(`  bot finished in ${(now / 1000).toFixed(1)} s, steps: ${[...steps].join(' > ')}`);
  }
}
