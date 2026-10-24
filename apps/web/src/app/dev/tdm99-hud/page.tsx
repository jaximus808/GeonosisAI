'use client';
// TDM-99 dev page: <RobotHud/> in <RobotView/>'s hud slot, against a real robot or the recorded fixture.
//   /dev/tdm99-hud?robot=1[&ticket=3]#stream_token=<view token>   live: RobotView + HUD on the robot's /ws/state
//   /dev/tdm99-hud?robot=1&url=ws://127.0.0.1:8199/ws/state#stream_token=<view token>   same, explicit state URL
//   /dev/tdm99-hud?fixture=1&robot=1                                the recorded sim fixture (SIMULATED)
// Mint a view token: python -m rdf_common.stream_token mint --robot-id <id> --robot-name <name> --scope view
// For timing checks the page records when the e-stop banner first rendered: window.__tdm99 (estopShownAt, stateTms).

import { useEffect, useLayoutEffect, useState } from 'react';
import { RobotHud } from '@/components/robotview/Hud';
import { RobotView, useRobotSources } from '@/components/robotview/RobotView';
import { useRobotLive } from '@/lib/robotview/useRobotLive';

type Params = { robot: number | null; ticket: number | null; url?: string; token?: string; fixture: boolean };

declare global {
  interface Window {
    __tdm99?: { estopShownAt: number | null; estopStateTms: number | null; renders: number };
  }
}

export default function Tdm99HudPage() {
  const [p, setP] = useState<Params | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const h = new URLSearchParams(window.location.hash.slice(1));
    setP({
      robot: q.get('robot') ? Number(q.get('robot')) : null,
      ticket: q.get('ticket') ? Number(q.get('ticket')) : null,
      url: q.get('url') ?? undefined,
      token: h.get('stream_token') ?? h.get('token') ?? undefined,
      fixture: q.get('fixture') === '1',
    });
  }, []);
  const { registry } = useRobotSources();
  const src = p?.robot != null ? registry.byId.get(p.robot) : undefined;
  const stateUrl = p?.url ?? src?.stream?.stateUrl ?? undefined;
  const live = useRobotLive(p?.robot ?? null, {
    enabled: !!p && (p.fixture || (!!p.token && !!stateUrl)),
    fixture: p?.fixture,
    url: stateUrl,
    token: p?.token,
  });
  const estop = !!live.state && (live.state.estop || live.state.robot_state === 'estop');
  useLayoutEffect(() => {
    const w = (window.__tdm99 ??= { estopShownAt: null, estopStateTms: null, renders: 0 });
    w.renders += 1;
    if (estop && w.estopShownAt == null) {
      w.estopShownAt = Date.now();
      w.estopStateTms = live.state?.t_ms ?? null;
    }
    if (!estop) {
      w.estopShownAt = null;
      w.estopStateTms = null;
    }
  });

  if (!p) return null;
  const hud =
    p.robot != null ? <RobotHud robotId={p.robot} ticketId={p.ticket} state={live.state} stale={live.stale} /> : null;
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">TDM-99 robot HUD</h1>
      {p.robot == null && <p className="text-sm">Add ?robot=&lt;id&gt; (and #stream_token=&lt;view token&gt;) to the address.</p>}
      {p.fixture ? (
        <div className="space-y-2 rounded-xl bg-violet-950 p-2 ring-2 ring-violet-500/80">
          <span className="inline-block rounded bg-violet-600 px-2 py-0.5 text-xs font-bold text-white">SIMULATED, recorded fixture</span>
          {hud}
        </div>
      ) : (
        <RobotView robotId={p.robot ?? undefined} defaultMode={src?.kind} hud={hud} />
      )}
      <p className="font-mono text-xs text-zinc-500" data-testid="tdm99-feed">
        hud feed: {live.transport} {live.hz.toFixed(0)} Hz{live.latencyMs != null ? `, ${live.latencyMs.toFixed(0)} ms` : ''}
        {live.error ? `, ${live.error}` : ''}
      </p>
    </div>
  );
}
