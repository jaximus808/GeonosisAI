'use client';

// TDM-104: the scene deployed to a SIM robot, in the twin's render form.
//   const { spec, raw, status } = useRobotScene(sim ? robotId : null);
//   <RobotTwin3D spec={spec} ... />
// Polls GET /api/tdm104/robots/{id}/scene (fast while a deploy is pending). `spec` is null until a scene was
// deployed to the robot; the twin then falls back to the legacy cube/plate drawing.

import { useEffect, useMemo, useState } from 'react';
import { toRenderSpec, type RobotSceneResponse, type SceneSpecV1 } from './scene';

export interface RobotSceneResult {
  spec: SceneSpecV1 | null;
  raw: RobotSceneResponse | null;
  status: RobotSceneResponse['status'] | 'unknown';
  error: string | null;
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
  const spec = useMemo(() => {
    try {
      return raw?.spec ? toRenderSpec(raw.spec) : null;
    } catch {
      return null;
    }
  }, [raw]);
  return { spec, raw, status: raw?.status ?? 'unknown', error };
}
