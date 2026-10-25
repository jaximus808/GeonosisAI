// The hero's no-WebGL fallback: an SVG drawing of the same frame the 3D still shows (the cube held over the
// plate), projected from the YAM's forward kinematics through the scene's camera. Pure math, no three.js.
import { createYamKinematics } from '@/components/robotview/yamFk';
import { YAM_MODEL } from '@/components/robotview/yamModel';
import { CUBE_HALF, HERO_STILL_T, PLATE_POS, heroArm } from './heroLoop';

type V = [number, number, number];
const KIN = createYamKinematics(YAM_MODEL);
const W = 480;
const H = 480;

// Same camera as HeroForgeScene (robot frame, z up): target, distance, elevation, azimuth, vertical fov.
const T: V = [0.19, 0.01, 0.17];
const R = 1.26;
const EL = 0.44;
const AZ = -1.2;
const FOV = (34 * Math.PI) / 180;

const sub = (a: V, b: V): V => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a: V, b: V) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a: V, b: V): V => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a: V): V => {
  const n = Math.hypot(...a) || 1;
  return [a[0] / n, a[1] / n, a[2] / n];
};

const CAM: V = [T[0] + R * Math.cos(EL) * Math.cos(AZ), T[1] + R * Math.cos(EL) * Math.sin(AZ), T[2] + R * Math.sin(EL)];
const F = norm(sub(T, CAM));
const RIGHT = norm(cross(F, [0, 0, 1]));
const UP = cross(RIGHT, F);
const FPX = H / 2 / Math.tan(FOV / 2);

function px(p: V): [number, number] {
  const d = sub(p, CAM);
  const z = dot(d, F);
  return [W / 2 + (dot(d, RIGHT) / z) * FPX, H / 2 - (dot(d, UP) / z) * FPX];
}

const f1 = (n: number) => n.toFixed(1);
const pts = (ps: V[]) => ps.map((p) => px(p).map(f1).join(',')).join(' ');

function ellipse(c: V, r: number, n = 32): V[] {
  return Array.from({ length: n }, (_, i) => [c[0] + r * Math.cos((i / n) * 2 * Math.PI), c[1] + r * Math.sin((i / n) * 2 * Math.PI), c[2]] as V);
}

function drawing() {
  const { q, gripper } = heroArm(HERO_STILL_T);
  const t = KIN.bodyTransforms(q, gripper);
  const at = (name: string): V => [t[name][12], t[name][13], t[name][14]];
  const g = KIN.graspSite(q);
  const grasp: V = [g[12], g[13], g[14]];
  const chain = ['base', 'link1', 'link2', 'link3', 'link4', 'link5', 'gripper'].map(at);
  chain.push(grasp);
  const widths = [26, 22, 18, 17, 13, 12, 9];
  // the cube's 8 corners in the grasp frame (it rides the fingers), drawn as its hull
  const corners: V[] = [];
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
    const l: V = [sx * CUBE_HALF, sy * CUBE_HALF, sz * CUBE_HALF - 0.002];
    corners.push([g[12] + g[0] * l[0] + g[4] * l[1] + g[8] * l[2], g[13] + g[1] * l[0] + g[5] * l[1] + g[9] * l[2], g[14] + g[2] * l[0] + g[6] * l[1] + g[10] * l[2]]);
  }
  const hull = convexHull(corners.map(px));
  const plateTop: V = [PLATE_POS[0], PLATE_POS[1], 0.012];
  return { chain, widths, hull, plate: pts(ellipse(plateTop, 0.075)), plateEdge: pts(ellipse([PLATE_POS[0], PLATE_POS[1], 0], 0.075)), fingers: [at('tip_left'), at('tip_right')], grasp, shadow: px([grasp[0], grasp[1], 0]) };
}

function convexHull(p: Array<[number, number]>): string {
  const s = [...p].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const c = (o: number[], a: number[], b: number[]) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lo: Array<[number, number]> = [];
  for (const q of s) {
    while (lo.length >= 2 && c(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop();
    lo.push(q);
  }
  const up: Array<[number, number]> = [];
  for (const q of s.reverse()) {
    while (up.length >= 2 && c(up[up.length - 2], up[up.length - 1], q) <= 0) up.pop();
    up.push(q);
  }
  return [...lo.slice(0, -1), ...up.slice(0, -1)].map((q) => q.map(f1).join(',')).join(' ');
}

const D = drawing();

export function HeroForgeStill() {
  const seg = D.chain.slice(0, -1).map((a, i) => [px(a), px(D.chain[i + 1]), D.widths[i]] as const);
  const base = px([0, 0, 0]);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice" className="absolute inset-0 h-full w-full" aria-hidden>
      <defs>
        <radialGradient id="hf-floor" cx="50%" cy="50%" r="50%">
          <stop offset="0" stopColor="#ff7a1f" stopOpacity="0.22" />
          <stop offset="0.5" stopColor="#ff7a1f" stopOpacity="0.06" />
          <stop offset="1" stopColor="#ff7a1f" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="hf-link" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#ff7a1f" stopOpacity="0.9" />
          <stop offset="0.25" stopColor="#3a322c" />
          <stop offset="1" stopColor="#24201c" />
        </linearGradient>
      </defs>
      <ellipse cx={f1(base[0] + 30)} cy={f1(base[1] - 10)} rx="230" ry="78" fill="url(#hf-floor)" />
      <ellipse cx={f1(D.shadow[0])} cy={f1(D.shadow[1])} rx="34" ry="10" fill="#000" opacity="0.35" />
      <ellipse cx={f1(base[0])} cy={f1(base[1])} rx="44" ry="14" fill="#14110e" stroke="#ff7a1f" strokeOpacity="0.55" strokeWidth="1" />
      <polygon points={D.plateEdge} fill="#b9aea2" />
      <polygon points={D.plate} fill="#e6ddd2" stroke="#ff7a1f" strokeOpacity="0.5" strokeWidth="1.5" />
      {/* the arm: a melt rim pass, then the graphite links on top */}
      <g strokeLinecap="round" fill="none">
        {seg.map(([a, b, w], i) => (
          <line key={`r${i}`} x1={f1(a[0] - 2)} y1={f1(a[1] - 1)} x2={f1(b[0] - 2)} y2={f1(b[1] - 1)} stroke="#ff7a1f" strokeOpacity="0.75" strokeWidth={w + 3} />
        ))}
        {seg.map(([a, b, w], i) => (
          <line key={`l${i}`} x1={f1(a[0])} y1={f1(a[1])} x2={f1(b[0])} y2={f1(b[1])} stroke={i % 3 === 1 ? '#8f877f' : '#2b2622'} strokeWidth={w} />
        ))}
        {D.fingers.map((p, i) => {
          const a = px(D.chain[6]);
          const b = px(p);
          return <line key={`f${i}`} x1={f1(a[0])} y1={f1(a[1])} x2={f1(b[0])} y2={f1(b[1])} stroke="#b8b0a7" strokeWidth="5" />;
        })}
      </g>
      {D.chain.slice(1, -1).map((p, i) => {
        const [x, y] = px(p);
        return <circle key={i} cx={f1(x)} cy={f1(y)} r={Math.max(4, D.widths[i + 1] / 2.6)} fill="#1a1613" stroke="#ff7a1f" strokeOpacity="0.4" />;
      })}
      <polygon points={D.hull} fill="#c42219" stroke="#ff8a4a" strokeOpacity="0.5" strokeWidth="1" />
    </svg>
  );
}
