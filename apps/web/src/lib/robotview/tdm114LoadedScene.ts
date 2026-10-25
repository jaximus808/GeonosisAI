// TDM-114: the scene a sim robot ACTUALLY has loaded, from its event_log rows. Pure (no React), so
// `pnpm --filter @rdf/web test` (test/tdm114-loaded-scene.test.ts) runs it under plain Node.
//
// GET /robots/{id}/scene answers the robot's NEWEST deploy, which can still be pending: a ticket's scene is
// deployed while another ticket's job holds the robot, and the robot swaps scenes only when that job ends
// (scene_watch waits; an eval loads a pending scene before trial 0). The twin must draw what the robot has,
// so it uses the newest successful `scene.loaded` (written by the robot) matched by deploy_id to its
// `scene.deployed` row (which carries the spec).

import type { SceneSpec } from './scene';

export interface Tdm114SceneEvent {
  id: number;
  type: string;
  payload: Record<string, unknown>;
}

export interface Tdm114LoadedScene {
  /** Spec of the scene the robot confirmed loading; null when it never confirmed one (or its deploy row is gone). */
  spec: SceneSpec | null;
  deployId: string | null;
  /** Ticket whose scene is on the robot (0 = unknown). */
  ticketId: number;
  revision: number | null;
  /** The newest deploy, when it is newer than the loaded one and not loaded yet. */
  pending: { deployId: string; ticketId: number } | null;
}

const num = (v: unknown): number => {
  const x = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
  return Number.isFinite(x) ? x : 0;
};

export function tdm114LoadedScene(events: readonly Tdm114SceneEvent[], robotId: number): Tdm114LoadedScene {
  const mine = events.filter((e) => num(e.payload.robot_id) === robotId).sort((a, b) => a.id - b.id);
  const deployed = mine.filter((e) => e.type === 'scene.deployed');
  const loaded = mine.filter((e) => e.type === 'scene.loaded' && e.payload.ok === true);
  const last = loaded[loaded.length - 1];
  const dep = last ? deployed.find((d) => String(d.payload.deploy_id ?? '') === String(last.payload.deploy_id ?? '')) : undefined;
  const spec = dep && dep.payload.spec && typeof dep.payload.spec === 'object' ? (dep.payload.spec as SceneSpec) : null;
  const newest = deployed[deployed.length - 1];
  const newestDeploy = newest ? String(newest.payload.deploy_id ?? '') : '';
  const loadedIds = new Set(mine.filter((e) => e.type === 'scene.loaded').map((e) => String(e.payload.deploy_id ?? '')));
  const pending =
    newest && newestDeploy && !loadedIds.has(newestDeploy) && (!last || newest.id > last.id)
      ? { deployId: newestDeploy, ticketId: num(newest.payload.ticket_id) }
      : null;
  return {
    spec,
    deployId: last ? String(last.payload.deploy_id ?? '') || null : null,
    ticketId: last ? num(last.payload.ticket_id) || num(dep?.payload.ticket_id) : 0,
    revision: last ? num(last.payload.revision) || num(dep?.payload.revision) || null : null,
    pending,
  };
}

/** "Robot is set up for TKT-0003's scene; this ticket's scene loads when its eval starts." (null when it's this ticket's). */
export function tdm114SceneMismatch(loaded: Pick<Tdm114LoadedScene, 'ticketId'>, viewingTicketId: number | null | undefined): string | null {
  if (!viewingTicketId || !loaded.ticketId || loaded.ticketId === viewingTicketId) return null;
  return `Robot is set up for TKT-${String(loaded.ticketId).padStart(4, '0')}'s scene; this ticket's scene loads when its eval starts.`;
}
