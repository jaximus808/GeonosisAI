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
