// TDM-86: the phone cockpit's logic (touch coach wording, gripper state, height readout, teleop coach
// from a sim robot's state). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const imp = async <T>(p: string) => (await import(new URL(p, import.meta.url).href)) as T;
const ph = await imp<typeof import('../src/lib/teleop/tdm86Phone')>('../src/lib/teleop/tdm86Phone.ts');
const guideLib = await imp<typeof import('../src/lib/teleop/tdm106Guide')>('../src/lib/teleop/tdm106Guide.ts');

type V3 = [number, number, number];

describe('tdm86 touch wording', () => {
  it('turns key hints into thumb hints', () => {
    assert.equal(ph.tdm86TouchWords('Move back 8 cm (S), right 14 cm (D).'), 'Move back 8 cm, right 14 cm.');
    assert.equal(ph.tdm86TouchWords('Hold E (15 cm to go). The fingers go around it.'), 'Pull the Up/down stick down (15 cm to go). The fingers go around it.');
    assert.equal(ph.tdm86TouchWords('Press G to close the gripper.'), 'Tap Grip to close the gripper.');
    assert.equal(ph.tdm86TouchWords('Lift first (Q), then move left 3 cm (A).'), 'Lift first (Up/down stick up), then move left 3 cm.');
    assert.equal(ph.tdm86TouchWords('Hold W A S D to move, Q and E to go up and down.'), 'Drag the Move stick. The Up/down stick lifts and lowers.');
  });
  it('rewrites every step of a real pick-and-place without a key letter left', () => {
    const cube: V3 = [0.31, -0.14, 0.02];
    const plate = { pos: [0.31, 0.19, 0] as V3, radius: 0.075 };
    const cases = [
      { tool: [0.3, 0, 0.2] as V3, gripperOpen: true, held: false, pos: cube },
      { tool: [0.31, -0.14, 0.2] as V3, gripperOpen: true, held: false, pos: cube },
      { tool: [0.31, -0.14, 0.02] as V3, gripperOpen: true, held: false, pos: cube },
      { tool: [0.31, -0.14, 0.02] as V3, gripperOpen: false, held: true, pos: cube },
      { tool: [0.31, -0.14, 0.1] as V3, gripperOpen: false, held: true, pos: [0.31, -0.14, 0.1] as V3 },
      { tool: [0.31, 0.19, 0.1] as V3, gripperOpen: false, held: true, pos: [0.31, 0.19, 0.1] as V3 },
    ];
    for (const c of cases) {
      const g = guideLib.tdm106Guide({ tool: c.tool, gripperOpen: c.gripperOpen, obj: { pos: c.pos, half: [0.02, 0.02, 0.02], held: c.held }, goal: plate, done: false });
      const words = ph.tdm86TouchWords(`${g.title} ${g.detail}`);
      assert.doesNotMatch(words, /\((?:W|A|S|D|Q|E|G)\)|\b(?:Hold|Press|press) [WASDQEG]\b/, `${g.step}: ${words}`);
    }
  });
});

describe('tdm86 readouts', () => {
  it('names the gripper state', () => {
    assert.deepEqual(ph.tdm86GripState({ opening: 1, cmd: 1, holding: false }), { text: 'Gripper open', tone: 'open' });
    assert.deepEqual(ph.tdm86GripState({ opening: 0.9, cmd: 0, holding: false }), { text: 'Closing…', tone: 'moving' });
    assert.deepEqual(ph.tdm86GripState({ opening: 0.3, cmd: 0, holding: true }), { text: 'Holding cube', tone: 'holding' });
    assert.equal(ph.tdm86GripState({ opening: 0, cmd: 0, holding: false }).tone, 'closed');
  });
  it('reads height against the cube when over it, else the table', () => {
    const obj = { pos: [0.31, -0.14, 0.02] as V3, half: [0.02, 0.02, 0.02] as V3, held: false };
    assert.equal(ph.tdm86HeightText([0.31, -0.14, 0.05], 0, obj), '3.0 cm above the cube');
    assert.equal(ph.tdm86HeightText([0.31, -0.14, 0.021], 0, obj), 'At the cube');
    assert.equal(ph.tdm86HeightText([0.1, 0.2, 0.2], 0, obj), 'Hand 20 cm above the table');
  });
});

describe('tdm86 teleop coach input from the state feed', () => {
  const scene = { cube: { pos: [0.31, -0.14, 0.02] as V3, size: 0.04 }, plate: { pos: [0.31, 0.19, 0.006] as V3, radius: 0.075 } };
  it('infers held from closed fingers at the cube', () => {
    const gi = ph.tdm86TeleopGuideInput([0.31, -0.14, 0.03], 0.4, 0, scene)!;
    assert.equal(gi.obj!.held, true);
    assert.equal(guideLib.tdm106Guide(gi).step, 'lift');
    assert.equal(ph.tdm86TeleopGuideInput([0.31, -0.14, 0.03], 1, 1, scene)!.obj!.held, false);
  });
  it('is done when the cube rests on the plate and is let go', () => {
    const gi = ph.tdm86TeleopGuideInput([0.31, 0.19, 0.2], 1, 1, { ...scene, cube: { pos: [0.31, 0.2, 0.026], size: 0.04 } })!;
    assert.equal(gi.done, true);
    assert.equal(guideLib.tdm106Guide(gi).step, 'done');
  });
  it('has nothing to coach without a cube in the feed', () => {
    assert.equal(ph.tdm86TeleopGuideInput([0.3, 0, 0.2], 1, 1, {}), null);
  });
  it('aims the landing ghost at the cube, the plate when carrying, else the table', () => {
    const obj = { pos: [0.31, -0.14, 0.02] as V3, half: [0.02, 0.02, 0.02] as V3, held: false };
    assert.equal(ph.tdm86LandingZ([0.31, -0.13, 0.2], 0, obj, null), 0.02);
    assert.equal(ph.tdm86LandingZ([0.0, 0.3, 0.2], 0, obj, null), 0.02);
    const held = { pos: [0.31, 0.19, 0.12] as V3, half: [0.02, 0.02, 0.02] as V3, held: true };
    const z = ph.tdm86LandingZ([0.31, 0.19, 0.13], 0, held, { pos: [0.31, 0.19, 0.006], radius: 0.075 });
    assert.ok(Math.abs(z - (0.13 - (0.1 - 0.012))) < 1e-9, String(z));
  });
});
