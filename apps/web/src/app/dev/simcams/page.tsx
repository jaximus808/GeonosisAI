'use client';

// TDM-105 dev route: the rendered sim cameras on the recorded sim fixture (no robot needed).
//   /dev/simcams             top / side / wrist, fixture playback at ~30 Hz
//   /dev/simcams?robot=<id>  a real sim robot's state feed instead (operator login or #stream_token)
//   /dev/simcams?narrow=1    also the narrow layout (RobotView's side column)

import { useEffect, useState } from 'react';
import { SimCameras } from '@/components/robotview/SimCameras';
import { useRobotLive } from '@/lib/robotview/useRobotLive';

export default function SimCamerasDevPage() {
  const [robot, setRobot] = useState<number | null | undefined>(undefined);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const r = Number(new URLSearchParams(window.location.search).get('robot'));
    setRobot(Number.isFinite(r) && r > 0 ? r : null);
    setNarrow(new URLSearchParams(window.location.search).get('narrow') === '1');
  }, []);
  const live = useRobotLive(robot ?? null, { enabled: robot !== undefined, fixture: robot === null });
  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Sim camera views</h1>
      <p className="mb-4 text-sm text-zinc-500">
        {robot ? `Robot #${robot}'s state feed.` : 'Recorded sim fixture, played back at about 30 Hz.'} Each view is drawn in the browser from the joint state.
      </p>
      <div className="max-w-5xl" data-testid="simcams-frame">
        <SimCameras state={live.state} stale={live.stale} hz={live.hz} waitingDetail={live.error} robotName={robot ? `#${robot}` : 'fixture'} />
      </div>
      {narrow && (
      <div className="mt-6 max-w-md">
        <p className="mb-2 text-sm text-zinc-500">Narrow column (RobotView&apos;s side slot):</p>
        <SimCameras state={live.state} stale={live.stale} hz={live.hz} />
      </div>
      )}
    </>
  );
}
