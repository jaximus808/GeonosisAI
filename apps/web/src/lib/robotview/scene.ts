/**
 * Scene spec v1 for the web (E15). TDM-104 owns the schema (Python: rdf_common.scene_spec); this is
 * the TypeScript mirror, written by TDM-97 first so the twin can render it. Keep it in step with the
 * Python model: TDM-104 may tighten it, but these field names and units are what RobotTwin3D reads.
 *
 * Frames and units: metres, radians, quaternions scalar first [w, x, y, z]. Poses are in the WORLD
 * (table) frame; `robot.mount` places the arm's base frame in it (identity in the MuJoCo sim, where
 * world == base). Sizes follow MuJoCo geom conventions, so the sim can pass them straight through:
 *   box      size = [half_x, half_y, half_z]
 *   cylinder size = [radius, half_height]       (axis along local z)
 *   sphere   size = [radius]
 */
import type { Quat, RobotLiveState, Vec3 } from './types';

export interface ScenePose {
  pos: Vec3;
  quat?: Quat;
}

export type ScenePrimitive = 'box' | 'cylinder' | 'sphere' | 'mesh';

export interface SceneObject {
  id: string;
  type: ScenePrimitive;
  size: number[];
  /** CSS colour, e.g. "#db1f1a". */
  color: string;
  /** For `mesh`: URL of a GLB/STL the web can fetch. */
  mesh_url?: string;
  mass?: number;
  friction?: number;
  /** Fixed objects (plates, fixtures) do not move; free objects get a free joint in MuJoCo. */
  fixed?: boolean;
  /** A coloured rim on a cylinder (how the sim draws plates). */
  rim_color?: string;
  pose: ScenePose;
  /** Reset randomisation: uniform offsets added to pose.pos per axis, and a yaw range (rad). */
  randomize?: { pos?: [Vec3, Vec3]; yaw?: [number, number] };
}

export interface SceneRegion {
  id: string;
  /** disc: size = [radius]; box: size = [half_x, half_y]. Lies on the table top. */
  type: 'disc' | 'box';
  size: number[];
  pos: Vec3;
  color?: string;
}

export interface SceneCamera {
  id: string; // 'top' | 'side' | 'front' | 'wrist' | anything
  /** World-frame placement; ignored when attached to the wrist. */
  pos?: Vec3;
  target?: Vec3;
  fovy: number; // degrees, vertical
  /** 'wrist' = rides the flange (the D405 mount). */
  attach?: 'world' | 'wrist';
}

export interface SceneTable {
  /** Table top extents [x, y] and its centre [x, y] in the world frame. */
  size: [number, number];
  center: [number, number];
  /** Height of the table TOP surface in the world frame (0 in the sim: base sits on the table). */
  top_z: number;
  thickness?: number;
  color?: string;
}

export interface SceneSpecV1 {
  version: 1;
  /** Task text, e.g. "Pick up the cube and place it in the empty plate." */
  task: string;
  robot: { mount: ScenePose };
  table: SceneTable;
  objects: SceneObject[];
  regions?: SceneRegion[];
  cameras?: SceneCamera[];
  /** Success predicate over object / region poses, e.g. "inside(cube, plate)". */
  success?: string;
}

/** Live scene poses on the state feed: `state.scene.objects[id]`, with the original cube/plate fields as fallback. */
export interface SceneLive {
  objects?: Record<string, { pos: Vec3; quat?: Quat }>;
  cube?: { pos: Vec3; quat: Quat };
  plate?: { pos: Vec3 };
}

export function sceneLive(state: RobotLiveState | null | undefined): SceneLive | null {
  return (state?.scene as SceneLive | undefined) ?? null;
}

/** Live pose of one object: `objects[id]`, else the legacy top-level field of the same name. */
export function livePose(live: SceneLive | null, id: string): { pos: Vec3; quat?: Quat } | null {
  if (!live) return null;
  const o = live.objects?.[id];
  if (o?.pos) return o;
  const legacy = (live as Record<string, unknown>)[id] as { pos?: Vec3; quat?: Quat } | undefined;
  return legacy?.pos ? { pos: legacy.pos, quat: legacy.quat } : null;
}

/**
 * The MuJoCo sim's built-in scene (services/robot-client/.../adapters/mujoco_scene.py SceneSpec
 * defaults), as a spec. Used for the sim when no ticket scene has been deployed yet.
 */
export const SIM_DEFAULT_SCENE: SceneSpecV1 = {
  version: 1,
  task: 'Pick up the cube and place it in the empty plate.',
  robot: { mount: { pos: [0, 0, 0], quat: [1, 0, 0, 0] } },
  table: { size: [0.96, 0.96], center: [0.3, 0], top_z: 0, thickness: 0.04 },
  objects: [
    { id: 'cube', type: 'box', size: [0.02, 0.02, 0.02], color: '#db1f1a', mass: 0.05, pose: { pos: [0.31, -0.14, 0.02], quat: [1, 0, 0, 0] }, randomize: { pos: [[-0.06, -0.07, 0], [0.06, 0.07, 0]], yaw: [-0.7, 0.7] } },
    { id: 'plate', type: 'cylinder', size: [0.075, 0.006], color: '#f0f0eb', rim_color: '#3366cc', fixed: true, pose: { pos: [0.31, 0.19, 0.006] } },
    { id: 'full_plate', type: 'cylinder', size: [0.075, 0.006], color: '#f0f0eb', rim_color: '#bfbfc7', fixed: true, pose: { pos: [0.47, -0.05, 0.006] } },
    { id: 'filler', type: 'cylinder', size: [0.028, 0.025], color: '#f2c726', mass: 0.08, pose: { pos: [0.47, -0.05, 0.037] } },
  ],
  regions: [{ id: 'plate', type: 'disc', size: [0.075], pos: [0.31, 0.19, 0] }],
  cameras: [
    { id: 'top', pos: [0.86, -0.5, 0.58], target: [0.27, 0.03, 0.06], fovy: 52 },
    { id: 'wrist', fovy: 72, attach: 'wrist' },
  ],
  success: 'inside(cube, plate)',
};

/** What to draw when the feed carries only the legacy cube / plate fields and no spec was given. */
export function legacyScene(live: SceneLive | null): SceneSpecV1 | null {
  if (!live || (!live.cube && !live.plate && !live.objects)) return null;
  const base = SIM_DEFAULT_SCENE;
  const ids = new Set<string>([...(live.cube ? ['cube'] : []), ...(live.plate ? ['plate'] : []), ...Object.keys(live.objects ?? {})]);
  return { ...base, objects: base.objects.filter((o) => ids.has(o.id)), regions: [] };
}

// ============================================================================================ TDM-104
// The canonical scene spec (the JSON the orchestrator stores per ticket and the MuJoCo sim loads):
// Python `rdf_common.scene_spec`, same field names. Everything above (`SceneSpecV1`) is the RENDER form
// RobotTwin3D draws; `toRenderSpec(spec)` maps one to the other so the twin shows exactly what MuJoCo
// simulates. Units: metres, radians, RGBA 0..1; world frame = robot base frame, table top = `table.top_z`.
//   box       size = FULL extents [sx, sy, sz], pose.pos = centre
//   cylinder  radius + FULL height,           pose.pos = centre
//   sphere    radius,                          pose.pos = centre
//   plate     radius + height (a fixed dish with a rim), pose.pos = its BOTTOM

export type Rgba = [number, number, number, number];
export type ScenePrimitiveV1 = 'box' | 'cylinder' | 'sphere' | 'plate';

export interface SceneSpecObject {
  id: string;
  label?: string;
  primitive: ScenePrimitiveV1;
  size?: Vec3;
  radius?: number;
  height?: number;
  color: Rgba;
  rim_color?: Rgba;
  mass?: number;
  friction?: Vec3;
  dynamic: boolean;
  graspable?: boolean;
  pose: { pos: Vec3; yaw: number };
  randomize?: { region: string; yaw?: [number, number] };
}

export interface SceneSpecRegion {
  x: [number, number];
  y: [number, number];
  color?: Rgba;
  /** Drawn on the table (a goal zone). Spawn regions are invisible. */
  visible?: boolean;
  label?: string;
}

export interface SceneSpecCamera {
  pos?: Vec3;
  target?: Vec3;
  fovy?: number;
  width?: number;
  height?: number;
  /** 'wrist_d405' = rides the flange. */
  mount?: string;
}

export interface SceneSuccess {
  type: 'object_in' | 'object_on';
  object: string;
  target?: string;
  region?: string;
  margin?: number;
  released?: boolean;
}

export interface SceneSpec {
  version: 1;
  name: string;
  task: string;
  notes?: string;
  physics?: { timestep: number };
  robot?: { model: string; mount: { pos: Vec3; yaw: number } };
  table: { center: [number, number]; size: [number, number]; top_z: number; color: Rgba };
  objects: SceneSpecObject[];
  regions: Record<string, SceneSpecRegion>;
  cameras: Record<string, SceneSpecCamera>;
  success: SceneSuccess;
  meta?: { ticket_id?: number; revision?: number; scene_id?: string; source?: string; fixes?: string[] };
}

const hex2 = (v: number) => Math.max(0, Math.min(255, Math.round(v * 255))).toString(16).padStart(2, '0');
/** RGBA 0..1 (or an already-CSS string) -> '#rrggbb'. */
export function cssColor(c: Rgba | number[] | string | undefined, fallback = '#b3b3b3'): string {
  if (typeof c === 'string') return c;
  if (!Array.isArray(c) || c.length < 3) return fallback;
  return `#${hex2(c[0])}${hex2(c[1])}${hex2(c[2])}`;
}

const yawQuat = (yaw: number): Quat => [Math.cos(yaw / 2), 0, 0, Math.sin(yaw / 2)];

/** The spoken form of the success predicate: `inside(cube, plate)` / `on_top(red_block, blue_block)`. */
export function describeSuccess(s: SceneSuccess | undefined | null): string {
  if (!s) return '';
  return `${s.type === 'object_on' ? 'on_top' : 'inside'}(${s.object}, ${s.target ?? s.region})`;
}

/** Canonical spec -> what RobotTwin3D draws. Plates become fixed cylinders (centre = bottom + h/2) with a rim. */
export function toRenderSpec(spec: SceneSpec): SceneSpecV1 {
  const objects: SceneObject[] = spec.objects.map((o) => {
    const [x, y, z] = o.pose.pos;
    const quat = yawQuat(o.pose.yaw || 0);
    const base = { id: o.id, color: cssColor(o.color), mass: o.mass, friction: o.friction?.[0], fixed: !o.dynamic };
    if (o.primitive === 'box') {
      const s = o.size ?? [0.04, 0.04, 0.04];
      return { ...base, type: 'box', size: [s[0] / 2, s[1] / 2, s[2] / 2], pose: { pos: [x, y, z], quat } };
    }
    if (o.primitive === 'sphere') return { ...base, type: 'sphere', size: [o.radius ?? 0.02], pose: { pos: [x, y, z], quat } };
    const h = o.height ?? 0.02;
    if (o.primitive === 'plate') {
      return {
        ...base,
        type: 'cylinder',
        size: [o.radius ?? 0.075, h / 2],
        fixed: true,
        rim_color: cssColor(o.rim_color ?? o.color),
        pose: { pos: [x, y, z + h / 2], quat },
      };
    }
    return { ...base, type: 'cylinder', size: [o.radius ?? 0.02, h / 2], pose: { pos: [x, y, z], quat } };
  });
  const regions: SceneRegion[] = Object.entries(spec.regions ?? {})
    .filter(([, r]) => r.visible)
    .map(([id, r]) => ({
      id,
      type: 'box',
      size: [(r.x[1] - r.x[0]) / 2, (r.y[1] - r.y[0]) / 2],
      pos: [(r.x[0] + r.x[1]) / 2, (r.y[0] + r.y[1]) / 2, spec.table.top_z],
      color: r.color ? cssColor(r.color) : undefined,
    }));
  const cameras: SceneCamera[] = Object.entries(spec.cameras ?? {}).map(([id, c]) =>
    c.mount || id === 'wrist'
      ? { id, fovy: c.fovy ?? 72, attach: 'wrist' }
      : { id, pos: c.pos, target: c.target, fovy: c.fovy ?? 50, attach: 'world' },
  );
  const m = spec.robot?.mount;
  return {
    version: 1,
    task: spec.task,
    robot: { mount: { pos: m?.pos ?? [0, 0, 0], quat: yawQuat(m?.yaw ?? 0) } },
    table: { size: spec.table.size, center: spec.table.center, top_z: spec.table.top_z, thickness: 0.04, color: cssColor(spec.table.color) },
    objects,
    regions,
    cameras,
    success: describeSuccess(spec.success),
  };
}

/** One revision in `GET /tickets/{id}/scene` (orchestrator scene_api). */
export interface SceneRevisionMeta {
  ticket_id: number;
  revision: number;
  scene_id: string;
  source: 'llm' | 'manual' | 'default' | string;
  task?: string;
  summary?: { name?: string; task?: string; objects?: string[]; success?: string };
  event_id?: number;
  by?: string;
  fixes?: string[];
}

export type SceneDeployStatus = 'none' | 'pending' | 'loaded' | 'failed';

export interface SceneDeployView {
  deploy_id: string;
  robot_id: number;
  ticket_id: number;
  revision: number;
  scene_id: string;
  by?: string;
  event_id?: number;
  status: Exclude<SceneDeployStatus, 'none'>;
  loaded: { ok: boolean; error?: string | null; event_id?: number; objects?: string[] } | null;
}

/** `GET /tickets/{id}/scene`. */
export interface TicketSceneResponse {
  ticket_id: number;
  revision: number | null;
  scene_id: string | null;
  spec: SceneSpec | null;
  source: string | null;
  success?: string;
  revisions: SceneRevisionMeta[];
  deployed: SceneDeployView | null;
  last_error: { error?: string; errors?: string[]; attempts?: number } | null;
}

/** `GET /robots/{id}/scene`. */
export interface RobotSceneResponse {
  robot_id: number;
  deployed: SceneDeployView | null;
  spec: SceneSpec | null;
  status: SceneDeployStatus;
}
