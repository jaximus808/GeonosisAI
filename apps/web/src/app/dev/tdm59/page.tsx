'use client';

// TDM-59 dev harness: every onboarding piece in one place, driven by fake inputs (labelled).
//   - claim gate (arm-control vs reset) with readiness set / cleared
//   - the walkthrough for each task kind
//   - LatencyBadge at each grade
//   - Tdm59TeleopOnboarding reading a FAKE console's DOM markup, to check the telemetry adapter

import { useState } from 'react';
import { MockedBadge } from '@/components/MockedBadge';
import { InstructionsOverlay, type OverlayKind } from '@/components/teleop/InstructionsOverlay';
import { LatencyBadge } from '@/components/teleop/LatencyBadge';
import { Tdm59ClaimGate } from '@/components/teleop/Tdm59ClaimGate';
import { Tdm59TeleopOnboarding } from '@/components/teleop/Tdm59TeleopOnboarding';
import { clearReadiness, saveReadiness, useTdm59Readiness } from '@/lib/teleop/tdm59Readiness';

const FAKE = {
  connecting: { link: 'Connecting', clutch: 'idle', feed: { state: 'connecting', stale: true, worstLatencyMs: null }, hz: 0, rec: 'idle', up: null },
  viewing: { link: 'Connected', clutch: 'idle', feed: { state: 'live', stale: false, worstLatencyMs: 120 }, hz: 0, rec: 'idle', up: null },
  controlling: { link: 'Connected', clutch: 'drive', feed: { state: 'live', stale: false, worstLatencyMs: 240 }, hz: 30, rec: 'idle', up: null },
  laggy: { link: 'Connected', clutch: 'drive', feed: { state: 'live', stale: false, worstLatencyMs: 380 }, hz: 17, rec: 'idle', up: null },
  recording: { link: 'Connected', clutch: 'drive', feed: { state: 'live', stale: false, worstLatencyMs: 110 }, hz: 30, rec: 'recording', up: null },
  uploading: { link: 'Connected', clutch: 'idle', feed: { state: 'live', stale: false, worstLatencyMs: 110 }, hz: 0, rec: 'idle', up: 'pending' },
} as const;

export default function Tdm59Dev() {
  const { status, record } = useTdm59Readiness();
  const [overlay, setOverlay] = useState<OverlayKind | null>(null);
  const [fake, setFake] = useState<keyof typeof FAKE>('viewing');
  const f = FAKE[fake];

  return (
    <div className="space-y-8">
      <h1 className="flex items-center gap-2 text-xl font-semibold">
        TDM-59 onboarding harness <MockedBadge label="Fake inputs" />
      </h1>

      <section className="space-y-2">
        <h2 className="font-semibold">Readiness and the claim gate</h2>
        <p className="text-sm">
          Status: <b data-testid="dev-ready">{status}</b> {record && `(passed ${new Date(record.passedAt).toLocaleTimeString()})`}
        </p>
        <div className="flex gap-2">
          <button type="button" className="rounded border px-2 py-1 text-sm" onClick={() => saveReadiness({ practiceMs: 0, lessons: [], network: null })}>
            Mark ready
          </button>
          <button type="button" className="rounded border px-2 py-1 text-sm" onClick={() => clearReadiness()}>
            Clear readiness
          </button>
        </div>
        <div className="grid max-w-xl gap-4 sm:grid-cols-2">
          {(['clean_teleop', 'reset'] as const).map((t) => (
            <div key={t} className="rounded-lg border border-zinc-300 p-3 dark:border-zinc-700">
              <p className="mb-2 text-sm font-medium">{t}</p>
              <Tdm59ClaimGate taskType={t} taskId={7}>
                <button type="button" className="w-full rounded-md bg-zinc-900 px-4 py-3 font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">
                  Claim for $10
                </button>
              </Tdm59ClaimGate>
            </div>
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Walkthrough</h2>
        <div className="flex gap-2">
          {(['clean_teleop', 'recovery_takeover', 'practice'] as const).map((k) => (
            <button key={k} type="button" className="rounded border px-2 py-1 text-sm" onClick={() => setOverlay(k)}>
              {k}
            </button>
          ))}
        </div>
        <InstructionsOverlay open={overlay != null} onClose={() => setOverlay(null)} kind={overlay ?? 'clean_teleop'} taskText="Put the cup in the bin" practiceHref="/practice" />
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Latency badge grades</h2>
        <div className="flex flex-wrap items-start gap-3">
          <LatencyBadge videoMs={90} rttMs={60} />
          <LatencyBadge videoMs={220} rttMs={140} />
          <LatencyBadge videoMs={420} rttMs={260} />
          <LatencyBadge videoMs={120} cmdHz={14} driving />
          <LatencyBadge videoStale />
        </div>
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Teleop page strip reading a fake console</h2>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(FAKE) as (keyof typeof FAKE)[]).map((k) => (
            <button key={k} type="button" aria-pressed={fake === k} className={`rounded border px-2 py-1 text-sm ${fake === k ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : ''}`} onClick={() => setFake(k)}>
              {k}
            </button>
          ))}
        </div>
        <Tdm59TeleopOnboarding taskId="dev" />
        {/* the same markup the TDM-56 console / TDM-58 controls render */}
        <div className="rounded border border-dashed border-zinc-400 p-2 text-xs text-zinc-500">
          Fake console markup:
          <span data-testid="tdm56-clutch-status" data-state={f.clutch}> clutch={f.clutch}</span>
          <span>
            {' '}link=<span data-testid="tdm56-link-state">{f.link}</span>
            {f.link === 'Connected' && <span> 85 ms</span>}
          </span>
          <span data-testid="tdm56-cmd-hz"> {f.hz} cmd/s</span>
          <span className="sr-only" data-testid="tdm56-feed-health">
            {JSON.stringify(f.feed)}
          </span>
          <span data-testid="tdm58-recording-controls" data-phase={f.rec}> rec={f.rec}</span>
          {f.up && <span data-testid="tdm58-upload" data-state={f.up}> upload={f.up}</span>}
        </div>
      </section>
    </div>
  );
}
