// TDM-98: the robot view's source registry. Pure functions (no React, no SpacetimeDB client), so
// `node --test` covers them (test/robotview-sources.test.ts); the hooks that feed them live in
// components/robotview/RobotView.tsx (useRobotSources, useRobotViewMode).
//
//   sim vs live   decided per robot from what the robot-client registered:
//                   camera_config_json.sim === true                        (rdf-robot-client --sim)
//                   an adapter / embodiment name containing "sim" or "mujoco"
//                     (robot.embodiment, robot.adapter_version, camera_config_json.adapter,
//                      camera_config_json.arm.adapter, camera_config_json.arm.kwargs.sim)
//                 Anything else is a live robot. Unknown means live, so a real arm is never
//                 mislabelled as a simulation.
//   online        status != offline and a heartbeat within ONLINE_HEARTBEAT_MS.
//   selection     the robot the viewer picked last for that mode (remembered per viewer), else the
//                 first online robot of that kind (non-test names first), else the first of that kind.

export type RobotViewMode = 'sim' | 'live';

/** The robot row fields the registry reads (structural, so tests need no generated bindings). */
export interface RobotRowLike {
  id: bigint | number;
  name: string;
  embodiment: string;
  status: string;
  adapterVersion?: string;
  cameraConfigJson: string;
  lastHeartbeat?: { microsSinceUnixEpoch: bigint } | null;
}

export interface RobotStreamInfo {
  /** ws(s)://.../ws/cameras, as announced in camera_config_json.stream.url. */
  url: string | null;
  http: string | null;
  /** ws(s)://.../ws/state (TDM-96), when announced. */
  stateUrl: string | null;
  cams: string[];
}

export interface RobotSource {
  id: number;
  name: string;
  kind: RobotViewMode;
  /** Why the registry called it sim (for the tooltip), or null for live. */
  simReason: string | null;
  online: boolean;
  status: string;
  lastHeartbeatMs: number | null;
  /** 'off' | 'synthetic' | 'config' | null (not reported). */
  cameras: string | null;
  syntheticCameras: boolean;
  stream: RobotStreamInfo | null;
}

/** robot-client heartbeats every 5 s; three missed beats plus slack counts as offline. */
export const ONLINE_HEARTBEAT_MS = 20_000;

const SIM_NAME = /sim|mujoco/i;

function parseJson(s: string | undefined | null): Record<string, unknown> {
  if (!s) return {};
  try {
    const v = JSON.parse(s);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/** 'sim' with the reason, or 'live'. */
export function classifyRobot(r: Pick<RobotRowLike, 'embodiment' | 'adapterVersion' | 'cameraConfigJson'>): { kind: RobotViewMode; reason: string | null } {
  const cfg = parseJson(r.cameraConfigJson);
  if (cfg.sim === true) return { kind: 'sim', reason: 'camera_config_json.sim' };
  const arm = obj(cfg.arm);
  const kwargs = obj(arm?.kwargs);
  if (kwargs?.sim === true) return { kind: 'sim', reason: 'arm.kwargs.sim' };
  const names: [string, unknown][] = [
    ['embodiment', r.embodiment],
    ['adapter', cfg.adapter],
    ['arm.adapter', arm?.adapter],
    ['arm.embodiment', arm?.embodiment],
    ['adapter_version', r.adapterVersion],
  ];
  for (const [field, v] of names) {
    if (typeof v === 'string' && SIM_NAME.test(v)) return { kind: 'sim', reason: `${field} "${v}"` };
  }
  return { kind: 'live', reason: null };
}

export function heartbeatMs(r: Pick<RobotRowLike, 'lastHeartbeat'>): number | null {
  return r.lastHeartbeat ? Number(r.lastHeartbeat.microsSinceUnixEpoch / 1000n) : null;
}

export function isOnline(r: Pick<RobotRowLike, 'status' | 'lastHeartbeat'>, now: number): boolean {
  if (r.status === 'offline') return false;
  const hb = heartbeatMs(r);
  return hb != null && now - hb <= ONLINE_HEARTBEAT_MS;
}

export function streamInfo(cameraConfigJson: string): RobotStreamInfo | null {
  const s = obj(parseJson(cameraConfigJson).stream);
  if (!s) return null;
  return {
    url: typeof s.url === 'string' && s.url ? s.url : null,
    http: typeof s.http === 'string' && s.http ? s.http : null,
    stateUrl: typeof s.state_url === 'string' && s.state_url ? s.state_url : null,
    cams: Array.isArray(s.cams) ? s.cams.map(String) : [],
  };
}

export function toSource(r: RobotRowLike, now: number): RobotSource {
  const cfg = parseJson(r.cameraConfigJson);
  const { kind, reason } = classifyRobot(r);
  return {
    id: Number(r.id),
    name: r.name,
    kind,
    simReason: reason,
    online: isOnline(r, now),
    status: r.status,
    lastHeartbeatMs: heartbeatMs(r),
    cameras: typeof cfg.cameras === 'string' ? cfg.cameras : null,
    syntheticCameras: cfg.synthetic === true,
    stream: streamInfo(r.cameraConfigJson),
  };
}

const isTestName = (n: string) => n.startsWith('test.');

/** Online first, then non-test names, then by id. */
export function sortSources(list: RobotSource[]): RobotSource[] {
  return [...list].sort(
    (a, b) => Number(b.online) - Number(a.online) || Number(isTestName(a.name)) - Number(isTestName(b.name)) || a.id - b.id,
  );
}

export interface RobotRegistry {
  sim: RobotSource[];
  live: RobotSource[];
  byId: Map<number, RobotSource>;
}

export function buildRegistry(rows: readonly RobotRowLike[], now: number): RobotRegistry {
  const all = rows.map((r) => toSource(r, now));
  return {
    sim: sortSources(all.filter((s) => s.kind === 'sim')),
    live: sortSources(all.filter((s) => s.kind === 'live')),
    byId: new Map(all.map((s) => [s.id, s])),
  };
}

/**
 * The robot to show in `mode`.
 *   pinned     RobotView's robotId prop: it wins for its own kind.
 *   remembered the viewer's last pick for this mode, if that robot still exists and is of this kind.
 * Never returns a robot of the other kind: Live with no live robot is null ("no live robot"),
 * never a sim robot.
 */
export function pickSource(reg: RobotRegistry, mode: RobotViewMode, pinned?: number | null, remembered?: number | null): RobotSource | null {
  const list = reg[mode];
  const pin = pinned != null ? reg.byId.get(pinned) : undefined;
  if (pin && pin.kind === mode) return pin;
  const mem = remembered != null ? reg.byId.get(remembered) : undefined;
  if (mem && mem.kind === mode) return mem;
  return list[0] ?? null;
}

/**
 * The mode to open in: the viewer's remembered choice, else defaultMode (the ticket's robot mode,
 * TDM-107), else sim.
 */
export function initialMode(remembered: string | null, defaultMode?: RobotViewMode | null): RobotViewMode {
  if (remembered === 'sim' || remembered === 'live') return remembered;
  return defaultMode ?? 'sim';
}

// Per-viewer memory (localStorage). Every access is wrapped: storage can be blocked (private mode,
// sandboxed iframes) and the view must still work, just without remembering.
export const MODE_KEY = 'rdf.robotview.mode';
export const robotKey = (mode: RobotViewMode) => `rdf.robotview.robot.${mode}`;

export function readStored(key: string): string | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage blocked: the choice lasts for this page only */
  }
}

/** robot_id from an ops stream token's payload (base64url JSON before the dot), or null if opaque. */
export function tokenRobotId(token: string): number | null {
  try {
    const b64 = token.split('.')[0].replace(/-/g, '+').replace(/_/g, '/');
    const p = JSON.parse(atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))) as { robot_id?: unknown };
    return typeof p.robot_id === 'number' && p.robot_id > 0 ? p.robot_id : null;
  } catch {
    return null;
  }
}
