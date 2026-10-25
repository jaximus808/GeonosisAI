'use client';

// Dev route (not linked): the ticket cockpit's robot stage on the recorded sim fixture, for the performance-mode
// sample (spikes/teleop-perf/perf_sample.py). RobotView itself needs a registered robot, so this mounts what its
// sim mode draws: the main 3D twin and the TDM-105 sim-camera slot next to it, in RobotView's grid, fed by the
// fixture (no robot, no stream, no SpacetimeDB). The twin follows Quality: Smooth / Sharp like RobotView's.

import dynamic from 'next/dynamic';
import { useEffect, useState } from 'react';
import { simCamerasSlot } from '@/components/robotview/SimCameras';
import { useRobotLive } from '@/lib/robotview/useRobotLive';

const RobotTwin3D = dynamic(() => import('@/components/robotview/RobotTwin3D').then((m) => m.RobotTwin3D), { ssr: false });
// the Smooth main-view budget (lib/teleop/viewQuality.ts twinPerf('smooth', 'main')), inlined so this page also
// runs on code from before performance mode existed (the before sample)
const SMOOTH_MAIN = { maxDpr: 1.25, shadows: true, shadowMapSize: 1024, blurSamples: 8, maxFps: 60 };

export default function StagePerfDev() {
  const live = useRobotLive(null, { fixture: true });
  const [smooth, setSmooth] = useState(true);
  useEffect(() => {
    const read = () => {
      try {
        setSmooth(window.localStorage.getItem('rdf.teleop.view-quality.v1') !== 'sharp');
      } catch {
        setSmooth(true);
      }
    };
    read();
    window.addEventListener('rdf:view-quality', read);
    return () => window.removeEventListener('rdf:view-quality', read);
  }, []);
  return (
    <>
      <h1 className="mb-3 text-xl font-semibold">Ticket robot stage (sim fixture)</h1>
      <div className="gx-foundry relative overflow-hidden rounded-md bg-surface-sunken p-2 ring-2 ring-steel">
        <div className="grid gap-2 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          <div className="relative aspect-[4/3] overflow-hidden rounded-lg bg-black/30">
            <RobotTwin3D state={live.state} camera="top" sim className="h-full w-full" {...({ perf: smooth ? SMOOTH_MAIN : null } as object)} />
          </div>
          <div className="min-w-0">{simCamerasSlot({ live, robotId: 0, compact: false })}</div>
        </div>
      </div>
    </>
  );
}
