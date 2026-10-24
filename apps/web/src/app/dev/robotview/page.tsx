'use client';

// TDM-98 prototype route for <RobotView> (not linked from the nav; TDM-100 mounts it on real pages).
//   /dev/robotview                     registry picks a robot per mode
//   /dev/robotview?robot=2&mode=live   pin robot 2, open in Live on a first visit
//   /dev/robotview?compact=1           compact layout (camera as picture-in-picture)
//   /dev/robotview?simslot=1           fill the simCameras slot with TDM-105's rendered top / side / wrist views
//   add #stream_token=<ops token> to watch cameras without holding arm control
//     (python -m rdf_common.stream_token mint --robot-id <id> --robot-name <name> --scope view --ttl 600 --sub ops:<you>)

import { useEffect, useState } from 'react';
import { RobotView } from '@/components/robotview/RobotView';
import { simCamerasSlot } from '@/components/robotview/SimCameras';

export default function RobotViewDevPage() {
  const [q, setQ] = useState<{ robot?: number; mode?: 'sim' | 'live'; compact: boolean; simslot: boolean } | null>(null);
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const robot = Number(p.get('robot'));
    const mode = p.get('mode');
    setQ({
      robot: Number.isFinite(robot) && robot > 0 ? robot : undefined,
      mode: mode === 'sim' || mode === 'live' ? mode : undefined,
      compact: p.get('compact') === '1',
      simslot: p.get('simslot') === '1',
    });
  }, []);
  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Robot view</h1>
      <p className="mb-4 text-sm text-zinc-500">
        Prototype of the shared robot view. Switch between the simulated and the live robot; your choice is remembered in this browser.
      </p>
      {q && (
        <div className={q.compact ? 'max-w-md' : ''}>
          <RobotView
            robotId={q.robot}
            defaultMode={q.mode}
            compact={q.compact}
            simCameras={q.simslot ? simCamerasSlot : undefined}
          />
        </div>
      )}
    </>
  );
}
