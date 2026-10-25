'use client';

// Dev harness (not linked): the real teleop console on the recorded sim fixture, with FAKE lease and recording
// states you can switch, to check that no message ever moves or resizes the viewer or the control dock
// (spikes/teleop-perf/layout_check.py) and to sample performance mode (spikes/teleop-perf/perf_sample.py).
//   /dev/teleop-layout      the console as the control holder; the buttons above it inject messages

import { useEffect, useMemo, useRef, useState } from 'react';
import { MockedBadge } from '@/components/MockedBadge';
import { ControlLeaseBanner } from '@/components/teleop/ControlLease';
import { RecordingControls } from '@/components/teleop/RecordingControls';
import { Tdm56TeleopConsole } from '@/components/teleop/Tdm56TeleopConsole';
import { Tdm59TeleopOnboarding } from '@/components/teleop/Tdm59TeleopOnboarding';
import { useTdm86Phone } from '@/components/teleop/Tdm86Phone';
import type { TeleopLease } from '@/lib/teleop/tdm57Lease';
import type { ControlChannel, RecordingState, RobotControlMessage } from '@/lib/teleop/tdm58Recording';

type LeaseKind = 'held' | 'held_error' | 'lost' | 'view_only';

const LONG_ERROR =
  'Heartbeat failed: the control plane answered 503 three times in a row (upstream connect error or disconnect/reset before headers, reset reason: connection termination). Your hold is kept for now; this retries every 5 s.';

function fakeLease(kind: LeaseKind): TeleopLease {
  const base = { busy: false, reclaim: async () => {}, release: async () => {}, giveBack: async () => {}, takeover: async () => {}, stamp: <T extends object>(m: T) => ({ ...m, task_id: 0, claimed_at_us: 0 }) };
  if (kind === 'lost') return { ...base, state: 'lost', canControl: false, lostReason: 'expired', lostWhileHidden: true };
  if (kind === 'view_only') return { ...base, state: 'view_only', canControl: false, holderName: 'Someone with a very long display name indeed' };
  return { ...base, state: 'held', canControl: true, secondsLeft: 47, error: kind === 'held_error' ? LONG_ERROR : undefined };
}

const IDLE_REC: RecordingState = {
  v: 1, active: false, phase: 'idle', kind: 'clean_teleop', key: null, owner: null, ticket_id: null, task_id: 0, started_at_us: null,
  elapsed_ms: 0, frames: 0, takeover_ts: null, handback_ts: null, takeover_at_ms: null, handback_at_ms: null,
  can: { start: true, takeover: false, handback: false, stop: false, discard: false }, last: null,
};

/** A FAKE robot on the recording channel: reports idle state at 10 Hz and refuses every op with a long reason. */
function useFakeRecordingChannel(): ControlChannel {
  const subs = useRef(new Set<(m: RobotControlMessage) => void>());
  useEffect(() => {
    const id = window.setInterval(() => {
      for (const fn of subs.current) fn({ t: 'state', recording: IDLE_REC } as unknown as RobotControlMessage);
    }, 100);
    return () => window.clearInterval(id);
  }, []);
  return useMemo<ControlChannel>(
    () => ({
      send: (m) => {
        window.setTimeout(() => {
          for (const fn of subs.current)
            fn({ t: 'record_ack', op: m.t, req: m.req, ok: false, error: `[FAKE] ${LONG_ERROR}`, recording: IDLE_REC } as unknown as RobotControlMessage);
        }, 150);
        return true;
      },
      subscribe: (fn) => {
        subs.current.add(fn);
        return () => {
          subs.current.delete(fn);
        };
      },
    }),
    [],
  );
}

export default function TeleopLayoutDev() {
  const [lease, setLease] = useState<LeaseKind>('held');
  const l = fakeLease(lease);
  const rec = useFakeRecordingChannel();
  const { phone } = useTdm86Phone();
  return (
    <>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm" data-testid="layout-inject">
        <MockedBadge label="Fake lease + recording" />
        {(['held', 'held_error', 'lost', 'view_only'] as const).map((k) => (
          <button key={k} type="button" onClick={() => setLease(k)} aria-pressed={lease === k} className="rounded border px-2 py-1" data-testid={`inject-lease-${k}`}>
            lease: {k}
          </button>
        ))}
      </div>
      <Tdm59TeleopOnboarding taskId="0" />
      <Tdm56TeleopConsole
        taskId="0"
        dev={{ streamUrl: 'fixture', token: '', controlUrl: 'ws://127.0.0.1:9/ws/control', fixture: true }}
        canDrive={l.canControl}
        canDriveReason={l.canControl ? undefined : l.state === 'lost' ? 'You lost control.' : 'Controlled by someone else.'}
        holdsControl={l.state === 'held'}
        controlSlot={<ControlLeaseBanner lease={l} variant="bar" />}
        recordingSlot={<RecordingControls taskId={0} kind="clean_teleop" controlSocket={rec} compact={phone} />}
      />
    </>
  );
}
