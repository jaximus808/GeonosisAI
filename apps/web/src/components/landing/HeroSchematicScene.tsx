'use client';
// The landing hero's 3D piece: the real YAM (yam.glb + yamFk) drawn as a hidden-line technical drawing. Every
// mesh is filled in the page's own ground colour and outlined in ink (crease edges + a screen-space silhouette),
// so it reads like a plate from a drawing set. It turns slowly on a turntable; once a loop the gripper slides
// home onto the wrist and its seam flashes molten orange, then cools. Lazy-loaded by HeroSchematic.tsx
// (next/dynamic, ssr:false), so three.js never ships with first paint.
// Budget: dpr <= 1.5, no lights, no shadows, <= 30 draws a second, nothing drawn off screen or in a hidden tab.
import { useGLTF } from '@react-three/drei';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { Suspense, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import * as THREE from 'three';
import { mergeVertices } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js';
import { createYamKinematics } from '@/components/robotview/yamFk';
import { YAM_BODIES, YAM_MODEL } from '@/components/robotview/yamModel';
import { LOOP_S, START_T, STILL_T, handAt, type FeedKey } from './heroBeat';

const GLB_URL = '/robot/yam/yam.glb';
const KIN = createYamKinematics(YAM_MODEL);

/** A calm, upright work pose; the gripper is held a little open (gripperToSlide: 1 = open). */
const POSE = [0.0, 1.05, 1.32, -0.62, 0.0, 0.0];
const GRIPPER = 0.55;
/** The bodies that slide home in the assembly beat: the hand and everything hung off it. */
const HAND = new Set(['gripper', 'tip_left', 'tip_right', 'wrist_cam_mount']);

const SLIDE_M = 0.055;

export interface Palette { ground: string; ink: string; muted: string; faint: string; melt: string }

declare global {
  interface Window {
    /** Dev probe for the hero (frame cost): draws so far and the mean CPU ms of a draw. */
    __heroSchematic?: { draws: () => number; msPerDraw: () => number; t: () => number; seek: (t: number) => void };
  }
}


// ------------------------------------------------------------------------------------------ materials
/** Silhouette: back faces pushed out along their normals by a fixed number of pixels (an inverted hull). */
function hullMaterial(color: string, px: number) {
  return new THREE.ShaderMaterial({
    side: THREE.BackSide,
    uniforms: { uColor: { value: new THREE.Color(color) }, uPx: { value: px }, uRes: { value: new THREE.Vector2(800, 600) } },
    vertexShader: /* glsl */ `
      uniform float uPx; uniform vec2 uRes;
      void main() {
        vec4 clip = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        vec3 n = normalize(normalMatrix * normal);
        vec2 dir = (projectionMatrix * vec4(n, 0.0)).xy;
        float len = length(dir);
        if (len > 1e-5) clip.xy += (dir / len) * uPx * 2.0 / uRes * clip.w;
        gl_Position = clip;
      }`,
    fragmentShader: /* glsl */ `
      uniform vec3 uColor;
      void main() { gl_FragColor = vec4(uColor, 1.0); }`,
  });
}

function fillMaterial(color: string) {
  // pushed back a hair so the crease lines lying on its faces always win the depth test
  return new THREE.MeshBasicMaterial({ color, polygonOffset: true, polygonOffsetFactor: 1.5, polygonOffsetUnits: 2 });
}

function lineMaterial(color: string, px: number, opts: Partial<{ dashed: boolean; opacity: number; dashSize: number; gapSize: number }> = {}) {
  const m = new LineMaterial({
    color: new THREE.Color(color).getHex(),
    linewidth: px,
    worldUnits: false,
    dashed: !!opts.dashed,
    dashSize: opts.dashSize ?? 0.012,
    gapSize: opts.gapSize ?? 0.008,
    transparent: (opts.opacity ?? 1) < 1,
    opacity: opts.opacity ?? 1,
  });
  return m;
}

function segments(points: number[], mat: LineMaterial, dashed = false): LineSegments2 {
  const g = new LineSegmentsGeometry();
  g.setPositions(points);
  const l = new LineSegments2(g, mat);
  if (dashed) l.computeLineDistances();
  return l;
}

// ------------------------------------------------------------------------------------------ the arm
interface BodyDraw { name: string; group: THREE.Group }

function useSchematicArm(pal: Palette) {
  const gltf = useGLTF(GLB_URL);
  return useMemo(() => {
    const fill = fillMaterial(pal.ground);
    const hull = hullMaterial(pal.ink, 1.1);
    const edge = lineMaterial(pal.ink, 1.05);
    const hot = lineMaterial(pal.melt, 1.25);
    const byBody: Record<string, THREE.BufferGeometry[]> = {};
    gltf.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (!m.isMesh) return;
      (byBody[m.name.split('__')[0]] ??= []).push(m.geometry);
    });
    const bodies: BodyDraw[] = [];
    const handEdges: LineSegments2[] = [];
    for (const b of YAM_BODIES) {
      const group = new THREE.Group();
      group.matrixAutoUpdate = false;
      for (const src of byBody[b.name] ?? []) {
        const solid = new THREE.Mesh(src, fill);
        // a welded copy with smooth normals, so the silhouette shell has no cracks at hard edges
        const welded = src.clone();
        for (const k of Object.keys(welded.attributes)) if (k !== 'position') welded.deleteAttribute(k);
        const merged = mergeVertices(welded, 1e-4);
        merged.computeVertexNormals();
        const shell = new THREE.Mesh(merged, hull);
        const edgesGeo = new THREE.EdgesGeometry(src, 32);
        const lines = segments(Array.from(edgesGeo.attributes.position.array as Float32Array), HAND.has(b.name) ? hot.clone() : edge);
        if (HAND.has(b.name)) handEdges.push(lines);
        group.add(shell, solid, lines);
      }
      bodies.push({ name: b.name, group });
    }
    return { bodies, hull, edge, hot, handEdges, fill };
  }, [gltf, pal]);
}

// ------------------------------------------------------------------------------------------ the cell
/** Turntable (rotates with the arm): a ring with degree ticks; its zero tick is molten. */
function turntableLines(): { ring: number[]; ticks: number[]; zero: number[] } {
  const ring: number[] = [];
  const ticks: number[] = [];
  const R = 0.2;
  const N = 96;
  for (let i = 0; i < N; i++) {
    const a0 = (i / N) * Math.PI * 2;
    const a1 = ((i + 1) / N) * Math.PI * 2;
    ring.push(R * Math.cos(a0), R * Math.sin(a0), 0, R * Math.cos(a1), R * Math.sin(a1), 0);
    const r2 = R * 0.72;
    ring.push(r2 * Math.cos(a0), r2 * Math.sin(a0), 0, r2 * Math.cos(a1), r2 * Math.sin(a1), 0);
  }
  for (let i = 1; i < 36; i++) {
    const a = (i / 36) * Math.PI * 2;
    const r0 = i % 9 === 0 ? R * 0.9 : R * 0.95;
    ticks.push(r0 * Math.cos(a), r0 * Math.sin(a), 0, R * Math.cos(a), R * Math.sin(a), 0);
  }
  return { ring, ticks, zero: [R * 0.84, 0, 0, R * 1.08, 0, 0] };
}

/** The fixed assembly gantry behind the turntable, square to the camera: a portal, its hoist and a floor rail. */
export const GANTRY = { back: 0.3, half: 0.34, h: 0.66 };
const VIEW = [Math.SQRT1_2, -Math.SQRT1_2]; // robot-frame horizontal direction towards the camera
const PERP = [Math.SQRT1_2, Math.SQRT1_2];
export function gantryAt(t: number, back = GANTRY.back): [number, number] {
  return [-VIEW[0] * back + PERP[0] * t, -VIEW[1] * back + PERP[1] * t];
}
function gantryLines(): number[] {
  const { half, h } = GANTRY;
  const p: number[] = [];
  const seg = (a: number[], b: number[]) => p.push(...a, ...b);
  const [lx, ly] = gantryAt(-half);
  const [rx, ry] = gantryAt(half);
  seg([lx, ly, 0], [lx, ly, h]);
  seg([rx, ry, 0], [rx, ry, h]);
  seg([lx, ly, h], [rx, ry, h]);
  seg([lx, ly, h - 0.035], [rx, ry, h - 0.035]);
  // the floor rail the portal runs on
  const [fx0, fy0] = gantryAt(-half - 0.06);
  const [fx1, fy1] = gantryAt(half + 0.06);
  seg([fx0, fy0, 0], [fx1, fy1, 0]);
  // the hoist: a trolley on the beam, its line down, a hook
  const [hx, hy] = gantryAt(0.12);
  seg([hx - 0.03 * PERP[0], hy - 0.03 * PERP[1], h - 0.035], [hx - 0.03 * PERP[0], hy - 0.03 * PERP[1], h - 0.06]);
  seg([hx + 0.03 * PERP[0], hy + 0.03 * PERP[1], h - 0.035], [hx + 0.03 * PERP[0], hy + 0.03 * PERP[1], h - 0.06]);
  seg([hx - 0.03 * PERP[0], hy - 0.03 * PERP[1], h - 0.06], [hx + 0.03 * PERP[0], hy + 0.03 * PERP[1], h - 0.06]);
  seg([hx, hy, h - 0.06], [hx, hy, h - 0.2]);
  return p;
}

function Cell({ pal, live, overlay, still }: { pal: Palette; live: Live; overlay: RefObject<OverlayRefs | null>; still: boolean }) {
  const arm = useSchematicArm(pal);
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    live.armed = true;
    invalidate(); // the still (demand) frame waits for the meshes
    // and redraws once framing and the overlay's measurements (fonts) have settled, so its marks line up
    const ids = [250, 900].map((ms) => window.setTimeout(invalidate, ms));
    return () => ids.forEach(window.clearTimeout);
  }, [live, invalidate]);
  const size = useThree((s) => s.size);
  const camera = useThree((s) => s.camera);
  const spin = useRef<THREE.Group>(null);

  const fixed = useMemo(() => {
    const g = new THREE.Group();
    const faint = lineMaterial(pal.faint, 1, { dashed: true, dashSize: 0.016, gapSize: 0.01 });
    g.add(segments(gantryLines(), faint, true));
    // dimension: the portal's clear height, just outside its left post
    const dim = lineMaterial(pal.muted, 1);
    const [x0, y0] = gantryAt(-GANTRY.half - 0.07);
    const [x1, y1] = gantryAt(-GANTRY.half - 0.01);
    const [xt, yt] = gantryAt(-GANTRY.half - 0.085);
    const H = GANTRY.h;
    g.add(segments([x0, y0, 0, x0, y0, H, xt, yt, 0, x1, y1, 0, xt, yt, H, x1, y1, H], dim));
    return { group: g, mats: [faint, dim] };
  }, [pal]);

  const table = useMemo(() => {
    const t = turntableLines();
    const g = new THREE.Group();
    const ringMat = lineMaterial(pal.muted, 1, { opacity: 0.9 });
    const zeroMat = lineMaterial(pal.melt, 1.6);
    g.add(segments([...t.ring, ...t.ticks], ringMat), segments(t.zero, zeroMat));
    return { group: g, mats: [ringMat, zeroMat] };
  }, [pal]);

  // the weld seam: a ring around the wrist flange, in the hand's frame
  const seam = useMemo(() => {
    const p: number[] = [];
    const N = 48;
    const r = 0.034;
    for (let i = 0; i < N; i++) {
      const a0 = (i / N) * Math.PI * 2;
      const a1 = ((i + 1) / N) * Math.PI * 2;
      p.push(r * Math.cos(a0), r * Math.sin(a0), 0, r * Math.cos(a1), r * Math.sin(a1), 0);
    }
    const mat = lineMaterial(pal.melt, 2.2, { opacity: 1 });
    mat.transparent = true;
    const l = segments(p, mat);
    l.matrixAutoUpdate = false;
    l.renderOrder = 2;
    mat.depthTest = false;
    // a soft glow disc at the seam, additive
    const c = document.createElement('canvas');
    c.width = c.height = 64;
    const x = c.getContext('2d')!;
    const grd = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    grd.addColorStop(0, 'rgba(255,255,255,1)');
    grd.addColorStop(0.25, 'rgba(255,255,255,0.45)');
    grd.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = grd;
    x.fillRect(0, 0, 64, 64);
    const tex = new THREE.CanvasTexture(c);
    const glowMat = new THREE.SpriteMaterial({ map: tex, color: pal.melt, blending: THREE.AdditiveBlending, depthTest: false, transparent: true, opacity: 0 });
    const glow = new THREE.Sprite(glowMat);
    glow.scale.setScalar(0.14);
    glow.renderOrder = 3;
    return { line: l, mat, glow, glowMat };
  }, [pal]);

  const allLineMats = useMemo(
    () => [arm.edge, arm.hot, ...arm.handEdges.map((l) => l.material as LineMaterial), ...fixed.mats, ...table.mats, seam.mat],
    [arm, fixed, table, seam],
  );
  useEffect(() => {
    for (const m of allLineMats) m.resolution.set(size.width, size.height);
    arm.hull.uniforms.uRes.value.set(size.width, size.height);
  }, [allLineMats, arm, size]);

  const ink = useMemo(() => new THREE.Color(pal.ink), [pal]);
  const melt = useMemo(() => new THREE.Color(pal.melt), [pal]);
  const tmp = useMemo(() => ({ m: new THREE.Matrix4(), d: new THREE.Matrix4(), g: new THREE.Matrix4(), gi: new THREE.Matrix4(), v: new THREE.Vector3(), c: new THREE.Color() }), []);
  const world = useMemo(() => KIN.bodyTransforms(POSE, GRIPPER), []);

  useFrame(() => {
    const t = live.t;
    if (spin.current) spin.current.rotation.z = (t / LOOP_S) * Math.PI * 2 - 0.55;

    // assembly beat (heroBeat.ts): the hand backs off along its approach axis while the last packets land, slides
    // home, the seam flashes, and the part cools from molten to ink
    const hand = handAt(t);
    const slide = SLIDE_M * hand.slide;
    const flash = hand.flash;
    const heat = hand.heat;
    tmp.g.fromArray(world.gripper as unknown as number[]);
    tmp.gi.copy(tmp.g).invert();
    tmp.d.makeTranslation(0, 0, slide);
    tmp.d.premultiply(tmp.g).multiply(tmp.gi); // world-space delta: slide along the gripper's own z
    for (const b of arm.bodies) {
      tmp.m.fromArray(world[b.name] as unknown as number[]);
      if (HAND.has(b.name)) tmp.m.premultiply(tmp.d);
      b.group.matrix.copy(tmp.m);
      b.group.matrixWorldNeedsUpdate = true;
    }
    tmp.c.copy(ink).lerp(melt, Math.pow(heat, 0.7));
    for (const l of arm.handEdges) (l.material as LineMaterial).color.copy(tmp.c);
    // the seam ring sits on the wrist flange (link5 side), facing along the gripper axis
    seam.line.matrix.copy(tmp.g);
    seam.line.matrixWorldNeedsUpdate = true;
    seam.mat.opacity = 0.35 + 0.65 * flash;
    seam.mat.linewidth = 1.4 + 1.6 * flash;
    tmp.v.set(tmp.g.elements[12], tmp.g.elements[13], tmp.g.elements[14]);
    seam.glow.position.copy(tmp.v);
    seam.glowMat.opacity = 0.85 * Math.pow(flash, 1.5);
    seam.glow.scale.setScalar(0.07 + 0.12 * flash);

    // the overlay's live bits: the axis marker follows the turntable, the callout leader follows the seam
    const o = overlay.current;
    if (o && spin.current) {
      spin.current.updateMatrixWorld();
      const proj = (p: THREE.Vector3) => {
        p.applyMatrix4(spin.current!.matrixWorld).project(camera);
        return [(p.x * 0.5 + 0.5) * size.width, (-p.y * 0.5 + 0.5) * size.height] as const;
      };
      const s = proj(tmp.v.set(tmp.g.elements[12], tmp.g.elements[13], tmp.g.elements[14]));
      o.seam(s[0], s[1], flash);
      const origin = proj(new THREE.Vector3(0, 0, 0));
      const ax = proj(new THREE.Vector3(1, 0, 0).multiplyScalar(0.1));
      const ay = proj(new THREE.Vector3(0, 1, 0).multiplyScalar(0.1));
      const az = proj(new THREE.Vector3(0, 0, 1).multiplyScalar(0.1));
      o.axes([ax[0] - origin[0], ax[1] - origin[1]], [ay[0] - origin[0], ay[1] - origin[1]], [az[0] - origin[0], az[1] - origin[1]]);
      // the dimension's label sits at the middle of its line (the gantry does not turn)
      const g = spin.current.parent!;
      g.updateMatrixWorld();
      const [mx, my] = gantryAt(-GANTRY.half - 0.07);
      const mid = new THREE.Vector3(mx, my, GANTRY.h / 2).applyMatrix4(g.matrixWorld).project(camera);
      o.dim((mid.x * 0.5 + 0.5) * size.width, (-mid.y * 0.5 + 0.5) * size.height);
      // where each agent's conduit docks: data into the base, train into the shoulder, review the wrist, eval the hand
      const at = (body: string, z = 0) => {
        const m = world[body];
        return proj(new THREE.Vector3(m[12], m[13], m[14] + z));
      };
      FEED_ANCHORS.data = proj(new THREE.Vector3(0, 0, 0.03));
      FEED_ANCHORS.train = at('link2');
      FEED_ANCHORS.review = at('link4');
      FEED_ANCHORS.eval = s;
      o.feed(FEED_ANCHORS, t, still);
    }
  });

  return (
    <group>
      <primitive object={fixed.group} />
      <group ref={spin}>
        <primitive object={table.group} />
        {arm.bodies.map((b) => (
          <primitive key={b.name} object={b.group} />
        ))}
        <primitive object={seam.line} />
        <primitive object={seam.glow} />
      </group>
    </group>
  );
}

// ------------------------------------------------------------------------------------------ camera + draw
interface Live { t: number; armed?: boolean }

const FEED_ANCHORS = {} as Record<FeedKey, readonly [number, number]>;

export interface OverlayRefs {
  feed: (anchors: Record<FeedKey, readonly [number, number]>, t: number, still: boolean) => void;
  seam: (x: number, y: number, flash: number) => void;
  axes: (x: readonly number[], y: readonly number[], z: readonly number[]) => void;
  dim: (x: number, y: number) => void;
}

/** An orthographic three-quarter view: an axonometric plate, framed to the box on every resize. */
function Framing() {
  const camera = useThree((s) => s.camera) as THREE.OrthographicCamera;
  const invalidate = useThree((s) => s.invalidate);
  const size = useThree((s) => s.size);
  useEffect(() => {
    const el = 0.42;
    const az = -0.78;
    // short boxes (phones) sit the drawing a little higher, clear of the title block
    const target = new THREE.Vector3(0.03, size.height < 300 ? 0.295 : 0.315, 0.03);
    const R = 3;
    camera.position.set(target.x + R * Math.cos(el) * Math.cos(az), target.y + R * Math.sin(el), target.z - R * Math.cos(el) * Math.sin(az));
    camera.up.set(0, 1, 0);
    camera.lookAt(target);
    // the cell spans about 0.95 m across and 0.85 m tall on screen; the title block sits in the corner
    camera.zoom = Math.min(size.width / 1.06, size.height / 0.97);
    camera.updateProjectionMatrix();
    invalidate();
  }, [camera, size, invalidate]);
  return null;
}

function Clock({ live, still }: { live: Live; still: boolean }) {
  useFrame((_, dt) => {
    if (!still) live.t = (live.t + Math.min(dt, 0.1)) % LOOP_S;
  }, -2);
  return null;
}

/** Takes the draw over (priority 1): at most ~30 draws a second; times each draw. */
function Pacer({ live, still, onFirstFrame }: { live: Live; still: boolean; onFirstFrame: () => void }) {
  const last = useRef(0);
  const stat = useRef({ n: 0, ms: 0 });
  const first = useRef(false);
  useEffect(() => {
    const probe = { draws: () => stat.current.n, msPerDraw: () => (stat.current.n ? stat.current.ms / stat.current.n : 0), t: () => live.t, seek: (t: number) => void (live.t = t % LOOP_S) };
    window.__heroSchematic = probe;
    return () => {
      if (window.__heroSchematic === probe) delete window.__heroSchematic;
    };
  }, [live]);
  useFrame((state) => {
    const now = performance.now();
    if (!still && first.current && now - last.current < 1000 / 30 - 2) return;
    last.current = now;
    state.gl.render(state.scene, state.camera);
    stat.current.ms += performance.now() - now;
    stat.current.n++;
    if (!first.current && live.armed) {
      first.current = true;
      onFirstFrame();
    }
  }, 1);
  return null;
}

/** R3F's shared loop stops when every canvas pauses; resuming has to restart it. */
function Kick({ loop }: { loop: string }) {
  const invalidate = useThree((s) => s.invalidate);
  useEffect(() => {
    if (loop !== 'never') invalidate();
  }, [loop, invalidate]);
  return null;
}

export interface HeroSchematicSceneProps {
  still?: boolean;
  running: boolean;
  palette: Palette;
  overlay: RefObject<OverlayRefs | null>;
  onReady?: () => void;
}

export default function HeroSchematicScene({ still = false, running, palette, overlay, onReady }: HeroSchematicSceneProps) {
  const live = useMemo<Live>(() => ({ t: still ? STILL_T : START_T }), [still]);
  const frameloop: 'always' | 'demand' | 'never' = still ? 'demand' : running ? 'always' : 'never';
  const [dpr] = useState(() => (typeof window === 'undefined' ? 1 : Math.min(1.5, window.devicePixelRatio || 1)));
  const gl = useMemo(() => ({ antialias: true, alpha: true, powerPreference: 'low-power' as const }), []);
  const readyOnce = useRef(false);
  const onFirstFrame = () => {
    if (readyOnce.current) return;
    readyOnce.current = true;
    onReady?.();
  };
  return (
    <Canvas
      orthographic
      dpr={dpr}
      frameloop={frameloop}
      gl={gl}
      flat
      camera={{ near: 0.1, far: 10, zoom: 400, position: [2, 1.5, 2] }}
      style={{ position: 'absolute', inset: 0, touchAction: 'auto' }}
      data-frameloop={frameloop}
    >
      <Kick loop={frameloop} />
      <Clock live={live} still={still} />
      <Framing />
      <Pacer live={live} still={still} onFirstFrame={onFirstFrame} />
      {/* robot base frame, z up: (x, y, z) -> three (x, z, -y) */}
      <group rotation={[-Math.PI / 2, 0, 0]}>
        <Suspense fallback={null}>
          <Cell pal={palette} live={live} overlay={overlay} still={still} />
        </Suspense>
      </group>
    </Canvas>
  );
}
