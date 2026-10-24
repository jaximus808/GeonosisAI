'use client';

// TDM-58 dev harness for <RecordingControls/> (not linked from the nav).
//   /dev/tdm58-recording?control=ws://<robot>/control&cams=ws://<robot>/ws/cameras&token=<stream token>&task=<id>&kind=clean_teleop|recovery_takeover
// spikes/tdm-58-recording/e2e_stack.py prints these URLs for its local sim robot. The real page is
// TDM-56's /teleop/[taskId], which mounts the same component on its own /control socket.

import { useEffect, useMemo, useState } from 'react';
import { MockedBadge } from '@/components/MockedBadge';
import { CameraFeed } from '@/components/teleop/CameraFeed';
import { RecordingControls } from '@/components/teleop/RecordingControls';
import { Tdm58DevControlSocket } from '@/lib/teleop/tdm58DevControlSocket';
import type { RecordingPhase } from '@/lib/teleop/tdm58Recording';
import { staticStreamGrant } from '@/lib/teleop/tdm55StreamGrant';

export default function Tdm58RecordingDevPage() {
  const [q, setQ] = useState<{ control: string; cams: string; token: string; task: string; kind: string } | null>(null);
  const [phase, setPhase] = useState<RecordingPhase>('idle');
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    setQ({
      control: p.get('control') ?? '',
      cams: p.get('cams') ?? '',
      token: p.get('token') ?? '',
      task: p.get('task') ?? '',
      kind: p.get('kind') ?? 'clean_teleop',
    });
  }, []);
  const socket = useMemo(() => (q?.control && q.token ? new Tdm58DevControlSocket(q.control, q.token) : null), [q]);
  useEffect(() => {
    socket?.start();
    return () => socket?.stop();
  }, [socket]);
  const grant = useMemo(() => (q?.cams && q.token ? staticStreamGrant(q.cams, q.token) : null), [q]);

  return (
    <>
      <div className="mb-1 flex items-center gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Recording controls (TDM-58 dev)</h1>
        <MockedBadge label="dev harness" title="Dev page with a bare /control client; the real page is TDM-56's teleop page." />
      </div>
      <p className="mb-4 text-sm text-zinc-500">
        Task {q?.task || '?'} · {q?.kind} · authority: <span data-testid="tdm58-dev-phase">{phase}</span>
      </p>
      {!q ? null : !socket ? (
        <p className="text-sm">Pass ?control=…&amp;token=…&amp;task=…&amp;kind=…</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
          {grant ? <CameraFeed getGrant={grant} /> : <div />}
          <RecordingControls taskId={q.task} kind={q.kind} controlSocket={socket} onPhase={setPhase} />
        </div>
      )}
    </>
  );
}
