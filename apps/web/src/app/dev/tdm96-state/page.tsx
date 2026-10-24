'use client';
// TDM-96 dev page: what useRobotLive(robotId) delivers (rate, latency, staleness, source, joints).
//   /dev/tdm96-state?robot=1                      operator login -> view token via /api/robotview/state-token
//   /dev/tdm96-state?url=ws://127.0.0.1:8196/ws/state#token=<view token>   direct (stream_token mint --scope view)
//   /dev/tdm96-state?fixture=1                    the recorded sim fixture (SIMULATED)

import { useEffect, useState } from 'react';
import { useRobotLive } from '@/lib/robotview/useRobotLive';

export default function Tdm96StatePage() {
  const [p, setP] = useState<{ robot: number | null; url?: string; token?: string; fixture: boolean } | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const h = new URLSearchParams(window.location.hash.slice(1));
    setP({
      robot: q.get('robot') ? Number(q.get('robot')) : null,
      url: q.get('url') ?? undefined,
      token: h.get('token') ?? undefined,
      fixture: q.get('fixture') === '1',
    });
  }, []);
  const live = useRobotLive(p?.robot ?? null, { enabled: !!p, url: p?.url, token: p?.token, fixture: p?.fixture });
  const s = live.state;
  return (
    <div className="space-y-4 font-mono text-sm">
      <h1 className="text-lg font-semibold">TDM-96 live robot state</h1>
      {live.source === 'sim' && (
        <div className="rounded bg-violet-100 px-3 py-1 text-violet-900">SIMULATED{live.transport === 'fixture' ? ' (recorded fixture)' : ''}</div>
      )}
      <div data-testid="tdm96-stats" className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
        <span>hz: <b data-testid="hz">{live.hz.toFixed(1)}</b></span>
        <span>latency: <b data-testid="latency">{live.latencyMs == null ? '-' : live.latencyMs.toFixed(1)}</b> ms</span>
        <span>stale: <b data-testid="stale">{String(live.stale)}</b></span>
        <span>source: <b data-testid="source">{live.source}</b></span>
        <span>transport: <b data-testid="transport">{live.transport}</b></span>
        <span>rtt: {live.rttMs == null ? '-' : live.rttMs.toFixed(1)} ms</span>
        <span>heartbeat: {live.heartbeat ? live.heartbeat.status : '-'}</span>
        <span className="col-span-2 text-red-700">{live.error ?? ''}</span>
      </div>
      {s && (
        <pre data-testid="state" className="overflow-x-auto rounded bg-neutral-100 p-3">
          {JSON.stringify(
            { robot_state: s.robot_state, control_source: s.control_source, estop: s.estop, joints: s.joints, gripper: s.gripper, ee: s.ee, clamps_1s: s.safety.clamps_1s, checkpoint_uri: s.checkpoint_uri, scene: s.scene, workspace: s.workspace },
            null,
            1,
          )}
        </pre>
      )}
    </div>
  );
}
