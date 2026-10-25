'use client';
// The landing hero's showpiece: the real YAM (RobotTwin3D's Arm, yamFk/yamModel, /robot/yam/yam.glb) picking
// the sim scene's red cube and placing it on the plate, in a slow loop, on a dark foundry floor. Lazy-loaded by
// HeroForge.tsx (next/dynamic, ssr:false), so three.js never ships with first paint.
// Budget: dpr <= 1.5, one 1024 shadow map, <= 60 draws a second, nothing drawn off screen or in a hidden tab;
// the embers move on the GPU (one uniform per frame).
import { Environment, Grid, Lightformer } from '@react-three/drei';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { Suspense, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import * as THREE from 'three';
import { Arm } from '@/components/robotview/RobotTwin3D';
import { createYamKinematics } from '@/components/robotview/yamFk';
import { YAM_MODEL } from '@/components/robotview/yamModel';
import { usePageVisible } from '@/lib/teleop/useViewQuality';
import {
  CUBE_HALF, CUBE_START, HERO_LOOP_S, HERO_POSES, HERO_STILL_T, PLATE_POS, T_COOL_END, T_DISSOLVE, T_GRAB, T_POUR, T_RELEASE,
  heroArm, heroStage, ramp,
} from './heroLoop';

const KIN = createYamKinematics(YAM_MODEL);

const MELT = '#ff7a1f';
const r2t = (v: readonly number[]): [number, number, number] => [v[0], v[2], -v[1]];

declare global {
  interface Window {
    /** Dev probe for the hero (frame cost): draws so far and the mean CPU ms of a draw. */
    __heroForge?: { draws: () => number; msPerDraw: () => number; t: () => number };
  }
}

// ------------------------------------------------------------------------------------------ the loop state
interface Live { t: number; q: number[]; gripper: number }

const graspM = (q: readonly number[]) => new THREE.Matrix4().fromArray(KIN.graspSite(q) as unknown as number[]);
// The cube's pose in the grasp frame at the pick, so it rides the fingers exactly (and lands where FK says).
const CUBE_START_M = new THREE.Matrix4().makeTranslation(...CUBE_START);
const CUBE_IN_GRASP = graspM(HERO_POSES.atCube).invert().multiply(CUBE_START_M);
const CUBE_PLACED_M = graspM(HERO_POSES.atPlate).multiply(CUBE_IN_GRASP);

function Animator({ live, still, onStage }: { live: Live; still: boolean; onStage?: (i: number) => void }) {
  const stage = useRef(-1);
  useFrame((_, dt) => {
    if (!still) live.t = (live.t + Math.min(dt, 0.05)) % HERO_LOOP_S;
    const a = heroArm(live.t);
    live.q = a.q;
    live.gripper = a.gripper;
    const s = heroStage(live.t);
    if (s !== stage.current) {
      stage.current = s;
      onStage?.(s);
    }
  }, -2);
  return null;
}

function Cube({ live }: { live: Live }) {
  const ref = useRef<THREE.Mesh>(null);
  const mat = useMemo(() => new THREE.MeshStandardMaterial({ color: '#c42219', roughness: 0.42, metalness: 0.05, emissive: MELT, emissiveIntensity: 0 }), []);
  const m = useMemo(() => new THREE.Matrix4(), []);
  const s = useMemo(() => new THREE.Vector3(), []);
  useFrame(() => {
    const o = ref.current;
    if (!o) return;
    const t = live.t;
    let scale = 1;
    let heat = 0;
    if (t >= T_GRAB && t < T_RELEASE) m.copy(graspM(live.q)).multiply(CUBE_IN_GRASP);
    else if (t >= T_RELEASE && t < T_POUR[0]) {
      m.copy(CUBE_PLACED_M);
      const d = ramp(t, T_DISSOLVE[0], T_DISSOLVE[1]);
      scale = 1 - d;
      heat = Math.min(1, d * 2.5);
    } else if (t >= T_POUR[0]) {
      const p = ramp(t, T_POUR[0], T_POUR[1]);
      m.makeTranslation(CUBE_START[0], CUBE_START[1], CUBE_HALF * (0.2 + 0.8 * p));
      scale = 0.2 + 0.8 * p;
      heat = 1 - ramp(t, T_POUR[1] - 0.2, T_COOL_END);
    } else m.copy(CUBE_START_M);
    o.matrix.copy(m).scale(s.setScalar(Math.max(scale, 0.0001)));
    o.visible = scale > 0.01;
    mat.emissiveIntensity = heat * 2.2;
  });
  return (
    <mesh ref={ref} matrixAutoUpdate={false} material={mat} castShadow receiveShadow>
      <boxGeometry args={[2 * CUBE_HALF, 2 * CUBE_HALF, 2 * CUBE_HALF]} />
    </mesh>
  );
}

function Plate({ live }: { live: Live }) {
  const rim = useMemo(() => new THREE.MeshStandardMaterial({ color: '#3a2a20', roughness: 0.5, emissive: MELT, emissiveIntensity: 0.15 }), []);
  useFrame(() => {
    // the rim warms when the cube lands ("eval 3/3"), then settles
    const t = live.t;
    const pulse = t >= T_RELEASE ? ramp(t, T_RELEASE, T_RELEASE + 0.5) * (1 - ramp(t, T_RELEASE + 1.5, T_DISSOLVE[1])) : 0;
    rim.emissiveIntensity = 0.15 + pulse * 1.6;
  });
  return (
    <group position={PLATE_POS as unknown as [number, number, number]}>
      <mesh rotation={[Math.PI / 2, 0, 0]} receiveShadow castShadow>
        <cylinderGeometry args={[0.075, 0.07, 0.012, 40]} />
        <meshStandardMaterial color="#e6ddd2" roughness={0.38} />
      </mesh>
      <mesh position={[0, 0, 0.006]} material={rim}>
        <torusGeometry args={[0.073, 0.0035, 6, 40]} />
      </mesh>
    </group>
  );
}

// ------------------------------------------------------------------------------------------ the floor
function radialTexture(stops: Array<[number, string]>): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  for (const [o, col] of stops) grd.addColorStop(o, col);
  g.fillStyle = grd;
  g.fillRect(0, 0, 128, 128);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function Floor() {
  const fade = useMemo(() => radialTexture([[0, '#fff'], [0.35, '#ddd'], [0.75, '#333'], [1, '#000']]), []);
  const glow = useMemo(() => radialTexture([[0, 'rgba(255,122,31,0.55)'], [0.35, 'rgba(255,122,31,0.18)'], [1, 'rgba(255,122,31,0)']]), []);
  return (
    <group>
      {/* the floor fades into the page's ground at its edges (the canvas is transparent) */}
      <mesh position={[0.24, 0.02, 0]} receiveShadow>
        <planeGeometry args={[1.25, 1.25]} />
        <meshStandardMaterial color="#171310" roughness={0.92} metalness={0} alphaMap={fade} transparent depthWrite={false} />
      </mesh>
      <mesh position={[0.18, 0, 0.001]}>
        <planeGeometry args={[1.3, 1.3]} />
        <meshBasicMaterial map={glow} transparent blending={THREE.AdditiveBlending} depthWrite={false} opacity={0.32} />
      </mesh>
      {/* the arm's plinth: a dark foundry disc with a faint melt edge */}
      <mesh rotation={[Math.PI / 2, 0, 0]} position={[0, 0, 0.004]} receiveShadow>
        <cylinderGeometry args={[0.1, 0.106, 0.008, 40]} />
        <meshStandardMaterial color="#0f0d0b" roughness={0.7} metalness={0.1} />
      </mesh>
      <mesh position={[0, 0, 0.0085]}>
        <torusGeometry args={[0.1, 0.0016, 4, 48]} />
        <meshBasicMaterial color={MELT} transparent opacity={0.55} toneMapped={false} />
      </mesh>
    </group>
  );
}

// ------------------------------------------------------------------------------------------ embers
const EMBER_VERT = /* glsl */ `
  attribute float aSeed;
  uniform float uTime;
  uniform float uSize;
  varying float vA;
  void main() {
    float life = fract(uTime * (0.045 + aSeed * 0.03) + aSeed * 7.31);
    vec3 p = position;
    p.z += life * 0.62;
    p.x += sin(uTime * 0.6 + aSeed * 40.0) * 0.025 * life;
    p.y += cos(uTime * 0.5 + aSeed * 23.0) * 0.025 * life;
    vA = smoothstep(0.0, 0.12, life) * (1.0 - smoothstep(0.45, 1.0, life)) * (0.45 + 0.55 * fract(aSeed * 13.7));
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_PointSize = uSize * (0.55 + fract(aSeed * 5.3)) / -mv.z;
    gl_Position = projectionMatrix * mv;
  }`;
const EMBER_FRAG = /* glsl */ `
  varying float vA;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d) * vA;
    gl_FragColor = vec4(vec3(1.0, 0.52, 0.16) * a, a);
  }`;

function Embers({ count = 36 }: { count?: number }) {
  const dpr = useThree((s) => s.viewport.dpr);
  const { geo, mat } = useMemo(() => {
    const pos = new Float32Array(count * 3);
    const seed = new Float32Array(count);
    let s = 7;
    const rnd = () => ((s = (s * 16807) % 2147483647) / 2147483647);
    for (let i = 0; i < count; i++) {
      const a = rnd() * Math.PI * 2;
      const r = 0.12 + rnd() * 0.5;
      pos[i * 3] = 0.2 + Math.cos(a) * r;
      pos[i * 3 + 1] = Math.sin(a) * r;
      pos[i * 3 + 2] = rnd() * 0.05;
      seed[i] = rnd();
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('aSeed', new THREE.BufferAttribute(seed, 1));
    const m = new THREE.ShaderMaterial({
      vertexShader: EMBER_VERT,
      fragmentShader: EMBER_FRAG,
      uniforms: { uTime: { value: 0 }, uSize: { value: 9 } },
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    return { geo: g, mat: m };
  }, [count]);
  useEffect(() => void (mat.uniforms.uSize.value = 9 * dpr), [mat, dpr]);
  const clock = useRef(0);
  useFrame((_, dt) => {
    clock.current += Math.min(dt, 0.05);
    mat.uniforms.uTime.value = clock.current; // own clock: embers never jump at the loop seam
  });
  return <points geometry={geo} material={mat} frustumCulled={false} />;
}

// ------------------------------------------------------------------------------------------ camera + draw
// Robot-frame target and a slow sweep (no mouse in the recording); a pointer adds a little parallax.
const TARGET = new THREE.Vector3(...r2t([0.19, 0.01, 0.17]));
const CAM_R = 1.26;
const CAM_EL = 0.44;
const CAM_AZ = -1.2; // around three's y, from +x (the arm's forward) towards the viewer
const SWEEP = 0.26;
const SWEEP_S = 38;

function CameraRig({ still }: { still: boolean }) {
  const camera = useThree((s) => s.camera);
  const pointer = useRef({ x: 0, y: 0, sx: 0, sy: 0 });
  const clock = useRef(0);
  useEffect(() => {
    if (still) return;
    const on = (e: PointerEvent) => {
      pointer.current.x = (e.clientX / window.innerWidth) * 2 - 1;
      pointer.current.y = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener('pointermove', on, { passive: true });
    return () => window.removeEventListener('pointermove', on);
  }, [still]);
  useFrame((_, dt) => {
    const p = pointer.current;
    if (!still) {
      clock.current += Math.min(dt, 0.05);
      const k = 1 - Math.exp(-2.5 * Math.min(dt, 0.05));
      p.sx += (p.x - p.sx) * k;
      p.sy += (p.y - p.sy) * k;
    }
    const az = CAM_AZ + (still ? 0 : SWEEP * Math.sin((clock.current / SWEEP_S) * Math.PI * 2) + p.sx * 0.12);
    const el = CAM_EL - p.sy * 0.06;
    camera.position.set(
      TARGET.x + CAM_R * Math.cos(el) * Math.cos(az),
      TARGET.y + CAM_R * Math.sin(el),
      TARGET.z - CAM_R * Math.cos(el) * Math.sin(az),
    );
    camera.lookAt(TARGET);
  }, -1);
  return null;
}

/** Takes the draw over (priority 1): at most ~60 draws a second, even on a 120 Hz panel; times each draw. */
function Pacer({ live, still, onFirstFrame }: { live: Live; still: boolean; onFirstFrame: () => void }) {
  const last = useRef(0);
  const stat = useRef({ n: 0, ms: 0 });
  const first = useRef(false);
  useEffect(() => {
    const probe = { draws: () => stat.current.n, msPerDraw: () => (stat.current.n ? stat.current.ms / stat.current.n : 0), t: () => live.t };
    window.__heroForge = probe;
    return () => {
      if (window.__heroForge === probe) delete window.__heroForge;
    };
  }, [live]);
  useFrame((state) => {
    const now = performance.now();
    if (!still && now - last.current < 1000 / 60 - 3) return;
    last.current = now;
    state.gl.render(state.scene, state.camera);
    stat.current.ms += performance.now() - now;
    stat.current.n++;
    if (!first.current) {
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

function useMaterials() {
  return useMemo(() => ({
    graphite: new THREE.MeshPhysicalMaterial({ color: '#3b3430', metalness: 0.4, roughness: 0.36, clearcoat: 0.5, clearcoatRoughness: 0.3, envMapIntensity: 1.3 }),
    aluminium: new THREE.MeshPhysicalMaterial({ color: '#cfc6bd', metalness: 0.82, roughness: 0.3, envMapIntensity: 1.5 }),
    sensor: new THREE.MeshPhysicalMaterial({ color: '#141210', metalness: 0.2, roughness: 0.25, clearcoat: 0.8 }),
  }), []);
}

export interface HeroForgeSceneProps {
  /** prefers-reduced-motion: one still frame (the cube over the plate), no loop, no sweep. */
  still?: boolean;
  onStage?: (i: number) => void;
  onReady?: () => void;
}

export default function HeroForgeScene({ still = false, onStage, onReady }: HeroForgeSceneProps) {
  const wrap = useRef<HTMLDivElement>(null);
  const live = useMemo<Live>(() => ({ t: still ? HERO_STILL_T : 0, ...heroArm(still ? HERO_STILL_T : 0) }), [still]);
  const mats = useMaterials();
  const pageVisible = usePageVisible();
  const onScreen = useOnScreen(wrap);
  const frameloop: 'always' | 'demand' | 'never' = still ? 'demand' : onScreen && pageVisible ? 'always' : 'never';
  const dpr = useMemo(() => (typeof window === 'undefined' ? 1 : Math.min(1.5, window.devicePixelRatio || 1)), []);
  const gl = useMemo(() => ({ antialias: true, alpha: true, powerPreference: 'default' as const }), []);
  const readyOnce = useRef(false);
  const onFirstFrame = () => {
    if (readyOnce.current) return;
    readyOnce.current = true;
    onReady?.();
  };

  return (
    <div ref={wrap} className="absolute inset-0">
      <Canvas
        shadows
        dpr={dpr}
        frameloop={frameloop}
        gl={gl}
        camera={{ fov: 34, near: 0.02, far: 12, position: [1, 0.6, 0.8] }}
        style={{ touchAction: 'auto' }}
        data-frameloop={frameloop}
      >
        <Kick loop={frameloop} />
        <Animator live={live} still={still} onStage={onStage} />
        <CameraRig still={still} />
        <Pacer live={live} still={still} onFirstFrame={onFirstFrame} />

        <hemisphereLight args={['#ffe3cc', '#120b07', 0.55]} />
        {/* key: warm white, front-left, the one shadow */}
        <directionalLight
          position={r2t([0.75, -0.55, 1.25])}
          intensity={3.1}
          color="#ffe4cc"
          castShadow
          shadow-mapSize={[1024, 1024]}
          shadow-camera-left={-0.6}
          shadow-camera-right={0.6}
          shadow-camera-top={0.6}
          shadow-camera-bottom={-0.6}
          shadow-camera-near={0.3}
          shadow-camera-far={3.2}
          shadow-radius={6}
          shadow-bias={-0.0008}
        />
        {/* rim: molten orange from behind the arm */}
        <directionalLight position={r2t([-0.9, 0.55, 0.55])} intensity={2.6} color={MELT} />
        <pointLight position={r2t([0.25, 0.0, 0.03])} intensity={0.25} distance={0.9} decay={2} color={MELT} />

        <Environment resolution={64} frames={1}>
          <Lightformer form="rect" intensity={2} color="#ffe6d2" position={[0, 3, 0]} rotation={[Math.PI / 2, 0, 0]} scale={[4, 4, 1]} />
          <Lightformer form="rect" intensity={1.8} color={MELT} position={[-3, 0.8, -1.2]} rotation={[0, Math.PI / 2.4, 0]} scale={[3, 0.5, 1]} />
          <Lightformer form="rect" intensity={0.8} color="#ffcfa6" position={[3, 1.2, 1.5]} rotation={[0, -Math.PI / 2.4, 0]} scale={[2.5, 1, 1]} />
        </Environment>

        <Grid
          position={[0.2, 0.0005, 0]}
          args={[2.4, 2.4]}
          cellSize={0.05}
          cellThickness={0.6}
          cellColor="#2e2621"
          sectionSize={0.25}
          sectionThickness={1}
          sectionColor="#58300f"
          fadeDistance={0.62}
          fadeStrength={2.2}
          fadeFrom={0}
          followCamera={false}
          infiniteGrid={false}
        />

        {/* robot base frame, z up */}
        <group rotation={[-Math.PI / 2, 0, 0]}>
          <Floor />
          <Plate live={live} />
          <Cube live={live} />
          <Suspense fallback={null}>
            <Arm read={() => live} materials={mats} shadows />
          </Suspense>
          {!still && <Embers />}
        </group>
      </Canvas>
    </div>
  );
}

function useOnScreen(ref: RefObject<HTMLDivElement | null>): boolean {
  const [on, setOn] = useState(true);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(([e]) => setOn(e.isIntersecting), { rootMargin: '32px' });
    io.observe(el);
    return () => io.disconnect();
  }, [ref]);
  return on;
}

