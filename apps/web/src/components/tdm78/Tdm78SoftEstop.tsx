'use client';

// TDM-78: the robot page's soft e-stop. Built to work when everything else on the page is broken:
// it needs only the robot id from the URL (no SpacetimeDB subscription, no robot row), sits outside
// every error boundary, never asks for confirmation, and can be pressed again at any time.
//
// Path: POST /api/tdm78/robots/{id}/estop (same origin) -> SpacetimeDB soft_estop as the web
// service identity, falling back to the orchestrator. Network errors are retried 3x. The live
// "robot locked" readout below the button is a separate, boundary-wrapped extra.

import { useEffect, useRef, useState } from 'react';
import { useRobot, useRows } from '@/lib/spacetime/hooks';
import { Tdm78Boundary } from './Tdm78Boundary';

type Phase = 'idle' | 'sending' | 'sent' | 'failed';
interface Sent {
  path: string;
  totalMs: number;
  roundTripMs: number;
  at: number; // Date.now() when pressed
  detail?: string;
}

export function Tdm78SoftEstop({ robotId }: { robotId: string }) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [sent, setSent] = useState<Sent | null>(null);
  const [paths, setPaths] = useState<{ direct: boolean } | null>(null);
  const busy = useRef(false);

  useEffect(() => {
    // Warm the server's connection to SpacetimeDB and learn which path is configured.
    fetch(`/api/tdm78/robots/${robotId}/estop`, { cache: 'no-store' })
      .then((r) => r.json())
      .then((b: { direct?: boolean }) => setPaths({ direct: Boolean(b.direct) }))
      .catch(() => setPaths(null));
  }, [robotId]);

  async function press() {
    if (busy.current) return;
    busy.current = true;
    const at = Date.now();
    const t0 = performance.now();
    setPhase('sending');
    let lastErr = '';
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const res = await fetch(`/api/tdm78/robots/${robotId}/estop`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ reason: 'soft e-stop pressed on the robot page' }),
          cache: 'no-store',
          keepalive: true,
        });
        const b = (await res.json().catch(() => ({}))) as { ok?: boolean; path?: string; totalMs?: number; detail?: string };
        if (res.ok && b.ok) {
          setSent({ path: b.path ?? '?', totalMs: b.totalMs ?? 0, roundTripMs: Math.round(performance.now() - t0), at });
          setPhase('sent');
          busy.current = false;
          return;
        }
        lastErr = b.detail || `HTTP ${res.status}`;
        break; // the server tried both paths already
      } catch (e) {
        lastErr = (e as Error).message;
        await new Promise((r) => setTimeout(r, 250));
      }
    }
    setSent({ path: 'none', totalMs: 0, roundTripMs: Math.round(performance.now() - t0), at, detail: lastErr });
    setPhase('failed');
    busy.current = false;
  }

  return (
    <div className="rounded-xl bg-[#f2b705] p-3 text-zinc-950 shadow-[inset_0_0_0_2px_rgba(0,0,0,0.18)] lg:p-4">
      <div className="flex items-center gap-3 lg:flex-col lg:items-stretch lg:gap-3">
        <button
          type="button"
          onClick={press}
          aria-label="Soft e-stop: stop the arm and lock it"
          className="group relative flex h-16 flex-1 items-center justify-center rounded-full bg-[radial-gradient(circle_at_35%_30%,#f0444b_0%,#c8102e_55%,#8a0a1d_100%)] text-lg font-extrabold tracking-wide text-white shadow-[0_6px_0_#6d0716,0_10px_18px_rgba(0,0,0,0.35)] outline-none transition-[transform,box-shadow] duration-75 focus-visible:ring-4 focus-visible:ring-zinc-950 active:translate-y-[5px] active:shadow-[0_1px_0_#6d0716,0_3px_8px_rgba(0,0,0,0.35)] lg:mx-auto lg:aspect-square lg:h-auto lg:w-full lg:max-w-[13rem] lg:flex-none lg:text-2xl"
        >
          <span className="leading-tight">
            {phase === 'sending' ? 'Stopping…' : 'STOP'}
            <span className="hidden text-sm font-semibold lg:block">soft e-stop</span>
          </span>
        </button>
        <div className="min-w-0 flex-1 text-sm leading-snug lg:text-center">
          <p className="font-semibold">Stops the arm and locks it.</p>
          <p className="text-xs text-zinc-800">Clearing the lock needs a person at the arm.</p>
        </div>
      </div>
      <div aria-live="assertive" className="mt-2 text-xs leading-snug text-zinc-900 lg:text-center">
        {phase === 'sent' && sent && (
          <p>
            Sent via {sent.path === 'direct' ? 'SpacetimeDB (direct)' : 'the orchestrator'} in {sent.roundTripMs} ms.
          </p>
        )}
        {phase === 'failed' && sent && (
          <p className="rounded bg-zinc-950 px-2 py-1 font-medium text-[#f2b705]">
            Not sent: {sent.detail}. Press again, or use the red button on the arm.
          </p>
        )}
        {phase === 'idle' && paths && !paths.direct && <p>Direct path not set up; goes through the orchestrator.</p>}
        {sent && phase === 'sent' && (
          <Tdm78Boundary label="Lock readout">
            <Tdm78LockReadout robotId={robotId} pressedAt={sent.at} />
          </Tdm78Boundary>
        )}
      </div>
    </div>
  );
}

/** Live follow-up: the soft_estop row's ack (robot side, SpacetimeDB clock) and the robot's status. */
function Tdm78LockReadout({ robotId, pressedAt }: { robotId: string; pressedAt: number }) {
  const id = BigInt(robotId);
  const [robot] = useRobot(id);
  const [cmds] = useRows('robotCommand', (r) => r.robotId.eq(id));
  const estop = [...cmds]
    .filter((c) => c.type === 'soft_estop' && Number(c.issuedAt.microsSinceUnixEpoch / 1000n) >= pressedAt - 5_000)
    .sort((a, b) => (a.seq < b.seq ? 1 : -1))[0];
  const ackMs =
    estop?.ackedAt != null ? Number((estop.ackedAt.microsSinceUnixEpoch - estop.issuedAt.microsSinceUnixEpoch) / 1000n) : null;
  let stopMs: number | null = null; // the robot-client's own issue -> arm stopped figure (TDM-49)
  try {
    const r = estop?.resultJson ? JSON.parse(estop.resultJson) : null;
    if (r && typeof r.issue_to_stop_ms === 'number') stopMs = r.issue_to_stop_ms;
  } catch {
    /* not JSON */
  }
  return (
    <p>
      {estop ? `Command #${estop.seq} ${estop.status}` : 'Waiting for the command row'}
      {stopMs != null
        ? `; the arm stopped ${Math.round(stopMs)} ms after it was issued`
        : ackMs != null && `; the robot acked it ${ackMs} ms after it was issued`}
      {robot?.status === 'estop' ? '. Robot is locked.' : '.'}
    </p>
  );
}
