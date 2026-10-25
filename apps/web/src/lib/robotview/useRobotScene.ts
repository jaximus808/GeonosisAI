'use client';

// TDM-104: the scene deployed to a SIM robot, in the twin's render form.
//   const { spec, raw, status } = useRobotScene(sim ? robotId : null);
//   <RobotTwin3D spec={spec} ... />
// Polls GET /api/tdm104/robots/{id}/scene (fast while a deploy is pending). `spec` is null until a scene was
// deployed to the robot; the twin then falls back to the legacy cube/plate drawing.
// TDM-114: `spec` is the scene the robot has LOADED (newest ok `scene.loaded` + its `scene.deployed` spec, live
// from event_log), not a newer deploy still pending behind another ticket's job. `loadedTicketId` says whose
// scene that is; `pendingTicketId` whose deploy waits.

import { useEffect, useMemo, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { toRenderSpec, type RobotSceneResponse, type SceneSpecV1 } from './scene';
import { tdm114LoadedScene, type Tdm114SceneEvent } from './tdm114LoadedScene';

export interface RobotSceneResult {
  spec: SceneSpecV1 | null;
  raw: RobotSceneResponse | null;
  status: RobotSceneResponse['status'] | 'unknown';
  error: string | null;
  /** TDM-114: ticket whose scene the robot has loaded (0 = unknown / none confirmed). */
  loadedTicketId: number;
  /** TDM-114: ticket whose newer deploy is waiting to load (0 = none). */
  pendingTicketId: number;
}

type Tdm114EvRow = { id: bigint; type: string; payloadJson: string };

function tdm114Parse(rows: readonly Tdm114EvRow[]): Tdm114SceneEvent[] {
  const out: Tdm114SceneEvent[] = [];
  for (const r of rows) {
    try {
      const v = JSON.parse(r.payloadJson);
      if (v && typeof v === 'object' && !Array.isArray(v)) out.push({ id: Number(r.id), type: r.type, payload: v as Record<string, unknown> });
    } catch {
      /* not JSON: skip */
    }
  }
  return out;
}

export function useRobotScene(robotId: number | null | undefined): RobotSceneResult {
  const [raw, setRaw] = useState<RobotSceneResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (robotId == null || !Number.isSafeInteger(robotId) || robotId < 1) {
      setRaw(null);
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let next = 15_000;
      try {
        const r = await fetch(`/api/tdm104/robots/${robotId}/scene`, { cache: 'no-store' });
        const j = (await r.json()) as RobotSceneResponse & { detail?: string };
        if (!alive) return;
        if (r.ok) {
          setRaw((prev) => (prev?.deployed?.deploy_id === j.deployed?.deploy_id && prev?.status === j.status ? prev : j));
          setError(null);
          if (j.status === 'pending') next = 1_500;
        } else setError(j.detail || `HTTP ${r.status}`);
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
      if (alive) timer = setTimeout(tick, next);
    };
    void tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [robotId]);
  const [loadedRows] = useRows('eventLog', (r) => r.type.eq('scene.loaded'));
  const [deployedRows] = useRows('eventLog', (r) => r.type.eq('scene.deployed'));
  const onRobot = useMemo(
    () => (robotId == null || !Number.isSafeInteger(robotId) || robotId < 1 ? null : tdm114LoadedScene(tdm114Parse([...loadedRows, ...deployedRows]), robotId)),
    [loadedRows, deployedRows, robotId],
  );
  const spec = useMemo(() => {
    try {
      if (robotId == null) return null;
      if (onRobot?.spec) return toRenderSpec(onRobot.spec);
      if (onRobot?.deployId) return null; // loaded, but its deploy row (the spec) is not in the log
      // No confirmed load in the event log (yet): the REST answer only counts when its newest deploy is loaded.
      return raw?.spec && raw.status === 'loaded' ? toRenderSpec(raw.spec) : null;
    } catch {
      return null;
    }
  }, [raw, onRobot, robotId]);
  return {
    spec,
    raw,
    status: raw?.status ?? 'unknown',
    error,
    loadedTicketId: onRobot?.ticketId ?? 0,
    pendingTicketId: onRobot?.pending?.ticketId ?? 0,
  };
}
