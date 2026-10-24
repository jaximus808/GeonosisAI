// TDM-106: which scene practice runs on.
//
//   /practice?ticket=<id>   the scene TDM-104 generated for that ticket (orchestrator GET /tickets/{id}/scene)
//   /practice?robot=<id>    the scene deployed on that sim robot          (orchestrator GET /robots/{id}/scene)
//   /practice               the MuJoCo sim's built-in scene (SIM_DEFAULT_SCENE)
//
// The page (server) fetches the orchestrator's answer and hands it here as-is.
//
// The orchestrator stores the canonical spec (the MuJoCo adapter's sim_assets/scenes JSON, TDM-104);
// TDM-104's `toRenderSpec` turns it into the twin's render shape, the same one the robot view draws,
// so practice and the deployed sim scene match. Nothing here re-implements the conversion.

import { SIM_DEFAULT_SCENE, toRenderSpec, type SceneSpec, type SceneSpecV1 } from '@/lib/robotview/scene';

export interface Tdm106SceneSource {
  spec: SceneSpecV1;
  /** Where it came from, for the label. */
  from: 'ticket' | 'robot' | 'default';
  /** Human-readable note when we fell back. */
  note?: string;
  ticketId?: string;
  robotId?: string;
  revision?: number;
}

export interface Tdm106SceneFetch {
  ticketId?: string;
  robotId?: string;
  /** The orchestrator's JSON ({spec, revision?, ...}), or null when it failed / had none. */
  body: unknown;
  error?: string;
}

/** The scene spec in the twin's render shape, or null if it is not in a shape this build can read. */
export function tdm106RenderSpec(raw: unknown): SceneSpecV1 | null {
  if (!raw || typeof raw !== 'object') return null;
  const c = raw as Partial<SceneSpec>;
  // the canonical spec: objects carry `primitive`
  if (Array.isArray(c.objects) && c.objects.every((o) => o && typeof (o as { primitive?: unknown }).primitive === 'string')) {
    try {
      const out = toRenderSpec(c as SceneSpec);
      return Array.isArray(out.objects) && out.objects.length ? out : null;
    } catch {
      return null;
    }
  }
  const s = raw as Partial<SceneSpecV1>;
  // already the render shape (objects carry `type`, the table has `top_z`)
  if (Array.isArray(s.objects) && s.objects.every((o) => o && typeof (o as { type?: unknown }).type === 'string') && s.table && typeof s.table.top_z === 'number') {
    return s as SceneSpecV1;
  }
  return null;
}

export function tdm106PracticeScene(f: Tdm106SceneFetch | null): Tdm106SceneSource {
  const fallback = (note?: string): Tdm106SceneSource => ({ spec: SIM_DEFAULT_SCENE, from: 'default', note, ticketId: f?.ticketId, robotId: f?.robotId });
  if (!f || (!f.ticketId && !f.robotId)) return fallback();
  const what = f.ticketId ? `ticket ${f.ticketId}` : `robot ${f.robotId}`;
  if (f.error) return fallback(`Could not load the scene for ${what} (${f.error}). Practising on the default sim scene.`);
  const body = (f.body ?? {}) as { spec?: unknown; revision?: number };
  if (!body.spec) return fallback(`${what[0].toUpperCase()}${what.slice(1)} has no scene yet. Practising on the default sim scene.`);
  const spec = tdm106RenderSpec(body.spec);
  if (!spec) return fallback(`The scene for ${what} is not in a shape this page can read. Practising on the default sim scene.`);
  return { spec, from: f.ticketId ? 'ticket' : 'robot', ticketId: f.ticketId, robotId: f.robotId, revision: body.revision };
}
