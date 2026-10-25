// TDM-115: every static path of the isometric robot cell, computed once at module load with the
// vendored Anatomy kit (./kit). World units: x and y on the floor, z up, about 1 unit = 1 cm of a
// small shop floor. Camera: azimuth 45, elevation 30 (the kit default). Moving and state-coloured parts
// are drawn from these same paths; only classes and transforms change at runtime.
//
// Plan (top view, x to the right-down, y to the left-down on screen):
//
//              collect dock      review gate
//   plan desk  +--------- belt ----------+   training furnace
//              |   robot table   eval    |
//   diagnose   |   (YAM arm)     bench   |
//              +-------------------------+   ship bay
//     droid bay

import {
  axisVector,
  circleRing,
  cylinder,
  extrude,
  fitProjection,
  insetPlan,
  iso,
  pathOf,
  planOutline,
  ring,
  segment,
  sideMatrix,
  slabOf,
  topMatrix,
  topTicks,
  type Flat,
  type Plan,
  type Point3,
  type Projection,
  type SolidPaths,
} from './kit/iso-kit';

export const VIEW_W = 1000;
export const VIEW_H = 620;

export const FLOOR: Plan = { x: 0, y: 0, w: 400, d: 300, r: 6 };

export const P: Projection = fitProjection(
  [
    [0, 0, -8], [400, 0, -8], [0, 300, -8], [400, 300, -8],
    [365, 100, 70], [155, 170, 95], [130, 30, 44], [250, 30, 44],
  ],
  VIEW_W,
  VIEW_H,
  { pad: 18, headroom: 40 },
);

/** World point -> SVG viewBox point. */
export const at = (p: Point3): Flat => iso(p, P);
/** World point -> percent of the viewBox, for HTML overlays. */
export function pct(p: Point3): { left: string; top: string } {
  const [x, y] = at(p);
  return { left: `${((x / VIEW_W) * 100).toFixed(2)}%`, top: `${((y / VIEW_H) * 100).toFixed(2)}%` };
}
/** SVG transform that moves something built at the origin to a world point. */
export const moveTo = (p: Point3) => {
  const [x, y] = axisVector(P, p);
  return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
};

const box = (x: number, y: number, w: number, d: number, z: number, h: number, r = 0.6, bevel = 0): SolidPaths =>
  slabOf({ x, y, w, d, r }, z, h, P, r > 0 ? 4 : 1, bevel);
const cyl = (cx: number, cy: number, r: number, z: number, h: number, steps = 24) => cylinder(cx, cy, r, z, h, P, steps);

/** A rectangle in the plane x = const (the +x face, right side on screen). */
export const faceX = (x: number, y0: number, y1: number, z0: number, z1: number) =>
  pathOf([at([x, y0, z0]), at([x, y1, z0]), at([x, y1, z1]), at([x, y0, z1])], true);
/** A rectangle in the plane y = const (the +y face, left side on screen). */
export const faceY = (y: number, x0: number, x1: number, z0: number, z1: number) =>
  pathOf([at([x0, y, z0]), at([x1, y, z0]), at([x1, y, z1]), at([x0, y, z1])], true);

/** Text engraved on a +x face (reads along -y) or a +y face (reads along +x). */
export const textOnX = (x: number, y: number, z: number) => sideMatrix([x, y], z, P, 'right');
export const textOnY = (x: number, y: number, z: number) => sideMatrix([x, y], z, P, 'left');
export const textOnTop = (x: number, y: number, z: number) => topMatrix([x, y], z, P, 'x');

// ------------------------------------------------------------------------------ floor and belt

export const floorArt = {
  slab: slabOf(FLOOR, -8, 8, P, 4, 1.2),
  dots: (() => {
    const out: Flat[] = [];
    for (let x = 20; x < 400; x += 20) for (let y = 20; y < 300; y += 20) out.push(at([x, y, 0]));
    return out;
  })(),
  screws: [[6, 6], [394, 6], [6, 294], [394, 294]].map(([x, y]) => at([x, y, 0])),
  ticksFront: topTicks(10, 390, 5, 5, 296, 0, [2, 4], P, 'x'),
  ticksLeft: topTicks(10, 290, 5, 5, 396, 0, [2, 4], P, 'y'),
  seam: planOutline(insetPlan(FLOOR, 3), 0, P, 4),
};

export const BELT: Plan = { x: 90, y: 80, w: 220, d: 150, r: 28 };
export const BELT_W = 14;
export const beltArt = {
  outer: slabOf(BELT, 0, 4, P, 10, 0.6),
  island: slabOf(insetPlan(BELT, BELT_W), 0, 5.5, P, 10, 0.6),
  tread: planOutline(insetPlan(BELT, BELT_W / 2), 4, P, 10),
  edgeOut: planOutline(insetPlan(BELT, 1.5), 4, P, 10),
  edgeIn: planOutline(insetPlan(BELT, BELT_W - 1.5), 4, P, 10),
};
/** Points along the belt centreline, clockwise on screen from the left side. */
export const BELT_STOPS: Record<'eval' | 'diagnose' | 'plan' | 'collect' | 'review' | 'train' | 'ship', Point3> = {
  eval: [262, 223, 4],
  diagnose: [97, 190, 4],
  plan: [97, 110, 4],
  collect: [130, 87, 4],
  review: [250, 87, 4],
  train: [303, 115, 4],
  ship: [303, 205, 4],
};
/** The belt path a part travels from one stop to the next (via the corners it passes). */
const CORNERS: Point3[] = [
  [97, 223, 4], // left-front corner
  [97, 87, 4], // back corner
  [303, 87, 4], // right corner
  [303, 223, 4], // front corner
];
const LOOP: Point3[] = [BELT_STOPS.diagnose, BELT_STOPS.plan, CORNERS[1], BELT_STOPS.collect, BELT_STOPS.review, CORNERS[2], BELT_STOPS.train, BELT_STOPS.ship, CORNERS[3], BELT_STOPS.eval, CORNERS[0]];
const LOOP_KEYS: (string | null)[] = ['diagnose', 'plan', null, 'collect', 'review', null, 'train', 'ship', null, 'eval', null];
export function beltRoute(from: string, to: string, back = false): Point3[] {
  const a = LOOP_KEYS.indexOf(from);
  const b = LOOP_KEYS.indexOf(to);
  if (a < 0 || b < 0) return [];
  const out: Point3[] = [];
  const n = LOOP.length;
  const dir = back ? -1 : 1;
  for (let i = a, k = 0; k <= n; i = (i + dir + n) % n, k++) {
    out.push(LOOP[i]);
    if (i === b && k > 0) break;
  }
  return out;
}

/** Where the parts sit on the belt. */
export const PART_SPOTS: Record<'eval_runs' | 'episodes' | 'accepted' | 'checkpoints', Point3> = {
  eval_runs: [97, 205, 4],
  episodes: [190, 87, 4],
  accepted: [290, 87, 4],
  checkpoints: [303, 165, 4],
};
export const crateArt = {
  body: box(-5, -5, 10, 10, 0, 7, 0.6, 0.6),
  lid: planOutline({ x: -3.5, y: -3.5, w: 7, d: 7, r: 0.4 }, 7, P, 2),
  band: segment([5, -5, 3.5], [5, 5, 3.5], P) + segment([-5, 5, 3.5], [5, 5, 3.5], P),
  text: textOnX(5, 4.2, 2.2),
};

// ------------------------------------------------------------------------------ robot cell (centre)

// The arm sits left of centre so the tall link never hides the collect dock behind it.
const RX = -45;
const RY = 20;
const rb = (x: number, y: number, w: number, d: number, z: number, h: number, r = 0.6, bevel = 0) => box(x + RX, y + RY, w, d, z, h, r, bevel);
const rc = (cx: number, cy: number, r: number, z: number, h: number, steps = 24) => cyl(cx + RX, cy + RY, r, z, h, steps);
const ra = (x: number, y: number, z: number) => at([x + RX, y + RY, z]);
export const ROBOT_SPOT: Point3 = [200 + RX, 155 + RY, 0];
export const robotArt = {
  shelf: rb(169, 134, 62, 42, 9, 2, 0.4),
  legs: [[168, 133], [232, 133], [168, 177], [232, 177]].map(([x, y]) => rc(x, y, 1.6, 5.5, 21, 12)),
  top: rb(165, 130, 70, 50, 26, 4, 1, 0.8),
  topSeam: planOutline({ x: 168 + RX, y: 133 + RY, w: 64, d: 44, r: 0.6 }, 30, P, 4),
  plate: rc(206, 166, 8, 30, 1.4, 32),
  plateRim: ring(206 + RX, 166 + RY, 6, 31.4, P, 32),
  cube: rb(224.3, 147.8, 4.4, 4.4, 30, 4.4, 0.2),
  base: rc(185, 150, 7.5, 30, 4, 32),
  baseBolts: circleRing(185 + RX, 150 + RY, 6, 8).map(([x, y]) => at([x, y, 34])),
  turret: rc(185, 150, 5, 34, 9, 24),
  upper: rb(182, 147, 6, 6, 43, 44, 1),
  shoulder: rc(185, 150, 3.6, 43, 2, 16),
  forearm: rb(181.5, 147, 47, 6, 83, 6, 1),
  elbowCap: rc(185, 150, 4.2, 89, 1.2, 20),
  wrist: rb(224, 147.5, 5, 5, 66, 17, 0.8),
  gripper: rb(220.5, 145.5, 12, 9, 63, 3, 0.5),
  fingerBack: rb(222.5, 145.6, 8, 1.6, 55, 8, 0.2),
  fingerFront: rb(222.5, 152.8, 8, 1.6, 55, 8, 0.2),
  cable: pathOf([ra(188, 150, 80), ra(190, 151, 66), ra(189, 151, 52), ra(188, 152, 40)]),
  /** Lit band on the wrist: melt while the arm runs a trial. */
  wristBand: faceX(229 + RX, 152.5 + RY, 147.5 + RY, 70, 72),
};

// Eval bench: a standing light board right of the arm, two rows of trial lights on its +x face.
const EX = 215;
const EY = 150;
export const EVAL_BENCH = { x: EX, y: EY, w: 24, d: 40 };
export const LIGHT_COLS = 10;
export const evalArt = {
  base: box(EX, EY, 24, 40, 5.5, 4, 0.6, 0.5),
  panel: box(EX + 2, EY + 2, 5, 36, 9.5, 26, 0.4),
  cap: box(EX + 1, EY + 1, 7, 38, 35.5, 2, 0.4),
  /** lights[row][col]: row 0 = baseline (upper), row 1 = fine-tuned. Trial 1 is at the left on screen. */
  lights: [27, 17].map((z) =>
    Array.from({ length: LIGHT_COLS }, (_, i) => {
      const y1 = EY + 36.5 - i * 3.3;
      return faceX(EX + 7, y1, y1 - 2.4, z, z + 2.4);
    }),
  ),
  rowText: [textOnX(EX + 7, EY + 39.6, 27.4), textOnX(EX + 7, EY + 39.6, 17.4)],
  feet: [[2, 2], [22, 2], [2, 38], [22, 38]].map(([x, y]) => at([EX + x, EY + y, 9.5])),
};

// ------------------------------------------------------------------------------ stations around the belt

export const diagArt = {
  body: box(13, 172, 26, 36, 0, 24, 1, 0.8),
  hood: box(15, 174, 22, 32, 24, 4, 0.8),
  slit: faceX(39, 205, 175, 15, 17),
  vent: [8, 11, 14].map((z) => segment([39, 202, z], [39, 178, z], P)).join(''),
  bins: [0, 1, 2, 3].map((i) => {
    const y = 172.5 + i * 9;
    return {
      body: box(42, y, 14, 8, 0, 9, 0.4),
      cavity: pathOf([at([43.4, y + 1.4, 9]), at([54.6, y + 1.4, 9]), at([54.6, y + 6.6, 9]), at([43.4, y + 6.6, 9])], true),
      text: textOnX(56, y + 5.5, 3.2),
      y,
    };
  }),
};

export const planArt = {
  legs: [[14, 79], [54, 79], [14, 119], [54, 119]].map(([x, y]) => box(x, y, 2, 2, 0, 16, 0.2)),
  top: box(13, 78, 44, 44, 16, 3, 0.8, 0.6),
  monitor: box(18, 84, 3, 22, 19, 15, 0.4),
  stand: box(20.5, 93, 3, 4, 19, 3, 0.3),
  screen: faceX(21, 104.5, 85.5, 21, 32.5),
  sheet: box(30, 90, 16, 22, 19, 0.6, 0.2),
  sheetLines: [94, 98, 102].map((y) => segment([33, y, 19.6], [43, y, 19.6], P)).join(''),
  tag: box(48, 110, 6, 6, 19, 1.2, 0.3),
};

export const collectArt = {
  deck: box(102, 17, 56, 36, 0, 8, 0.8, 0.6),
  posts: [cyl(106, 30, 1.4, 8, 24, 12), cyl(154, 30, 1.4, 8, 24, 12)],
  rail: box(104, 29, 52, 2.2, 31, 2, 0.3),
  cards: Array.from({ length: 8 }, (_, i) => box(108 + i * 5.8, 31, 4.2, 0.8, 18, 11, 0.1)),
  hooks: Array.from({ length: 8 }, (_, i) => segment([110.1 + i * 5.8, 31.4, 29], [110.1 + i * 5.8, 31.4, 31], P)).join(''),
  mast: cyl(150, 46, 1.1, 8, 30, 10),
  beacon: cyl(150, 46, 2.6, 38, 3.4, 16),
  stripes: [110, 118, 126, 134, 142].map((x) => segment([x, 53, 6], [x + 4, 53, 2], P)).join(''),
};

export const reviewArt = {
  base: box(228, 17, 48, 36, 0, 4, 0.8, 0.5),
  postBack: box(230, 30, 4, 4, 4, 30, 0.4),
  postFront: box(268, 30, 4, 4, 4, 30, 0.4),
  bar: box(229, 29.5, 44, 5, 34, 4, 0.5),
  lamp: cyl(251, 32, 2.6, 38, 3, 16),
  pass: box(262, 40, 12, 10, 4, 4, 0.4),
  reject: box(230, 40, 12, 10, 4, 7, 0.4),
  rejectCavity: pathOf([at([231.4, 41.4, 11]), at([240.6, 41.4, 11]), at([240.6, 48.6, 11]), at([231.4, 48.6, 11])], true),
  passText: textOnY(263.5, 50, 6.8),
  rejectText: textOnY(231.5, 50, 8.6),
  rollers: [236, 244, 252, 260].map((x) => segment([x, 31, 4.2], [x, 33, 4.2], P)).join(''),
};

export const trainArt = {
  body: box(343, 91, 44, 48, 0, 38, 1.2, 1),
  plinth: box(341, 89, 48, 52, 0, 3, 0.8),
  mouth: faceY(139, 352, 378, 7, 23),
  grate: [356, 361, 366, 371, 376].map((x) => segment([x, 139, 7], [x, 139, 23], P)).join(''),
  chimney: cyl(376, 101, 5, 38, 26, 24),
  chimneyCap: cyl(376, 101, 6, 64, 2, 24),
  rivets: [8, 30].flatMap((z) => [348, 360, 372, 384].map((x) => at([x, 139, z]))),
  label: box(387, 102, 0.6, 26, 26, 6, 0.1),
  labelText: textOnX(387.6, 127, 27.4),
};

export const shipArt = {
  deck: box(343, 218, 44, 44, 0, 6, 0.8, 0.6),
  postL: box(344, 219, 3, 3, 6, 32, 0.3),
  postR: box(383, 219, 3, 3, 6, 32, 0.3),
  lintel: box(343, 218, 44, 4, 38, 3, 0.4),
  crate: box(355, 232, 16, 16, 6, 14, 0.6, 0.6),
  crateBand: segment([371, 232, 13], [371, 248, 13], P) + segment([355, 248, 13], [371, 248, 13], P),
  rails: [226, 254].map((y) => segment([345, y, 6], [385, y, 6], P)).join(''),
};

export const bayArt = {
  pad: box(40, 262, 112, 26, 0, 1.6, 0.6),
  hatch: Array.from({ length: 13 }, (_, i) => segment([44 + i * 8, 285, 1.6], [50 + i * 8, 265, 1.6], P)).join(''),
};

// ------------------------------------------------------------------------------ droid (built at the origin)

const DS = 1.5; // droid scale
const dbox = (x: number, y: number, w: number, d: number, z: number, h: number, r = 0.6) => box(x * DS, y * DS, w * DS, d * DS, z * DS, h * DS, r * DS);
export const droidArt = {
  ring: ring(0, 0, 7 * DS, 0, P, 28),
  wheel: cyl(0, 0, 3.2 * DS, 0, 2.4 * DS, 16),
  torso: dbox(-3, -2.5, 6, 5, 2.4, 7, 1),
  armL: dbox(-4.4, -1, 1.4, 2, 4, 5, 0.3),
  armR: dbox(3, -1, 1.4, 2, 4, 5, 0.3),
  neck: cyl(0, 0, 1 * DS, 9.4 * DS, 0.8 * DS, 8),
  head: dbox(-3.3, -2.9, 6.6, 5.8, 10.2, 5, 1.2),
  visor: faceY(2.9 * DS, -2.4 * DS, 2.4 * DS, 11.6 * DS, 13.4 * DS),
  chip: faceY(2.5 * DS, -1 * DS, 1 * DS, 5.2 * DS, 7 * DS),
  antenna: segment([1.5 * DS, 0, 15.2 * DS], [1.5 * DS, 0, 18.4 * DS], P),
  tip: at([1.5 * DS, 0, 18.6 * DS]),
  head0: at([0, 0, 20 * DS]),
};
const ORIGIN = at([0, 0, 0]);
/** The screen offset of a droid's head top, relative to its feet. */
export const DROID_HEAD: Flat = [droidArt.head0[0] - ORIGIN[0], droidArt.head0[1] - ORIGIN[1]];

/** Where each agent stands (world, on the floor). */
export const AGENT_SPOTS: Record<string, Point3> = {
  eval_runner: [236, 206, 5.5],
  clusterer: [70, 214, 0],
  planner: [68, 144, 0],
  dispatcher: [120, 66, 0],
  data_droid: [150, 66, 0],
  reviewer: [236, 66, 0],
  dataset_qa: [262, 66, 0],
  training_droid: [330, 150, 0],
  onboarding_droid: [116, 206, 5.5],
};
export const BAY_SPOTS: Point3[] = [
  [60, 275, 1.6],
  [90, 275, 1.6],
  [120, 275, 1.6],
];

/** Machine anchors for the HTML plates (front-bottom corner of each machine). */
export const MACHINE_ANCHOR: Record<'eval' | 'diagnose' | 'plan' | 'collect' | 'review' | 'train' | 'ship', Point3> = {
  eval: [239, 190, 5.5],
  diagnose: [56, 210, 0],
  plan: [57, 122, 0],
  collect: [158, 53, 0],
  review: [276, 53, 0],
  train: [387, 139, 0],
  ship: [387, 262, 0],
};

/** Depth for painter's order: nearer the viewer is larger. */
export const depth = ([x, y, z]: Point3) => x + y + z * 0.01;

/** A hit area for a machine (its footprint at the floor, raised by h): one closed path. */
export function hitArea(plan: Plan, h: number) {
  return extrude(
    [
      [plan.x, plan.y],
      [plan.x + plan.w, plan.y],
      [plan.x + plan.w, plan.y + plan.d],
      [plan.x, plan.y + plan.d],
    ],
    0,
    h,
    P,
  ).fill;
}
export const HIT = {
  eval: hitArea({ x: 118, y: 148, w: 124, d: 56, r: 0 }, 80),
  diagnose: hitArea({ x: 10, y: 168, w: 50, d: 44, r: 0 }, 30),
  plan: hitArea({ x: 10, y: 76, w: 50, d: 48, r: 0 }, 36),
  collect: hitArea({ x: 100, y: 15, w: 60, d: 40, r: 0 }, 44),
  review: hitArea({ x: 226, y: 15, w: 52, d: 40, r: 0 }, 44),
  train: hitArea({ x: 340, y: 88, w: 50, d: 54, r: 0 }, 66),
  ship: hitArea({ x: 340, y: 215, w: 50, d: 50, r: 0 }, 42),
};
