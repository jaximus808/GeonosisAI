'use client';

// TDM-55: live robot camera feeds for the teleop / takeover pages (TDM-56 mounts this).
//
//   <TaskCameraFeed taskId={task.id} onHealth={(h) => setCanDrive(!h.stale)} />   // claimant only
//   <CameraFeed getGrant={staticStreamGrant(url, token)} />                        // ops / dev
//
// Each camera gets a canvas with a latency + fps badge (capture on the robot -> on this screen,
// clock-synced) and a red STALE overlay when the newest image is older than `staleMs`.
// `onHealth` reports {state, stale, worstLatencyMs} about 4x a second, so the teleop page can
// stop sending motion while the operator is driving blind.
// Transport and auth: lib/teleop/tdm55CameraClient.ts, lib/teleop/tdm55StreamGrant.ts.

import { useEffect, useMemo, useRef, useState } from 'react';
import { useContributor } from '@/lib/contributor/hooks';
import { CameraStreamClient, type FeedSnapshot, type StreamGrant } from '@/lib/teleop/tdm55CameraClient';
import { taskStreamGrant } from '@/lib/teleop/tdm55StreamGrant';

export interface FeedHealth {
  state: FeedSnapshot['state'];
  stale: boolean; // any shown camera is stale (or no frame yet)
  worstLatencyMs: number | null;
  detail?: string;
}

export interface CameraFeedProps {
  getGrant: () => Promise<StreamGrant>;
  /** Cameras to show, in order (default: whatever the robot offers, e.g. top + wrist). */
  cams?: string[];
  /** Ask the robot for fewer fps (default: the robot's rate, ~20). */
  fps?: number;
  /** An image older than this is flagged stale. Default 700 ms. */
  staleMs?: number;
  /** 'fill' (phone cockpit): fill the parent's height, cameras stacked (side by side in landscape), status over the picture. */
  layout?: 'grid' | 'stack' | 'fill';
  onHealth?: (h: FeedHealth) => void;
  className?: string;
}

const LABEL: Record<string, string> = { top: 'Top', wrist: 'Wrist', exo: 'Top' };

function latencyTone(ms: number | null) {
  if (ms == null) return 'bg-zinc-700/80 text-zinc-200';
  if (ms < 150) return 'bg-emerald-600/85 text-white';
  if (ms < 300) return 'bg-amber-500/90 text-black';
  return 'bg-red-600/90 text-white';
}

const STATE_TEXT: Record<FeedSnapshot['state'], string> = {
  connecting: 'Connecting',
  live: 'Live',
  fallback: 'Fallback (snapshots)',
  reconnecting: 'Reconnecting',
  denied: 'No access',
  error: 'Error',
  closed: 'Closed',
};

export function CameraFeed({ getGrant, cams, fps, staleMs = 700, layout = 'grid', onHealth, className }: CameraFeedProps) {
  const canvases = useRef<Record<string, HTMLCanvasElement | null>>({});
  const [snap, setSnap] = useState<FeedSnapshot>({ state: 'connecting', cams: [], stats: {}, rttMs: null, tokenExp: null });
  const healthRef = useRef(onHealth);
  healthRef.current = onHealth;
  const grantRef = useRef(getGrant);
  grantRef.current = getGrant;
  const camsKey = cams?.join(',') ?? '';

  useEffect(() => {
    const client = new CameraStreamClient(
      () => grantRef.current(),
      (cam, bmp) => {
        const c = canvases.current[cam];
        if (!c) return;
        if (c.width !== bmp.width || c.height !== bmp.height) {
          c.width = bmp.width;
          c.height = bmp.height;
        }
        c.getContext('2d')?.drawImage(bmp, 0, 0);
      },
      setSnap,
      { cams: camsKey ? camsKey.split(',') : undefined, fps },
    );
    client.start();
    return () => client.stop();
  }, [camsKey, fps]);

  const shownKey = (cams ?? (snap.cams.length ? snap.cams : ['top', 'wrist'])).join(',');
  const shown = useMemo(() => shownKey.split(','), [shownKey]);

  useEffect(() => {
    const st = shown.map((c) => snap.stats[c]);
    const stale = snap.state !== 'live' && snap.state !== 'fallback' ? true : st.some((s) => !s || s.ageMs == null || s.ageMs > staleMs);
    const lats = st.map((s) => s?.latencyMs).filter((x): x is number => x != null);
    healthRef.current?.({ state: snap.state, stale, worstLatencyMs: lats.length ? Math.max(...lats) : null, detail: snap.detail });
  }, [snap, staleMs, shown]);

  const fill = layout === 'fill';
  return (
    <div className={fill ? `relative flex h-full min-h-0 flex-col ${className ?? ''}` : className}>
      <div
        className={
          fill
            ? 'flex min-h-0 flex-1 flex-col gap-1.5 [@media(orientation:landscape)]:flex-row'
            : layout === 'grid'
              ? 'grid grid-cols-1 gap-2 sm:grid-cols-2'
              : 'flex flex-col gap-2'
        }
      >
        {shown.map((cam) => {
          const s = snap.stats[cam];
          const stale = !s || s.ageMs == null || s.ageMs > staleMs || (snap.state !== 'live' && snap.state !== 'fallback');
          return (
            <figure
              key={cam}
              className={`relative overflow-hidden rounded-lg bg-black ring-2 ${fill ? 'min-h-0 min-w-0 flex-1' : ''} ${stale && s?.lastFrameAt ? 'ring-red-600' : 'ring-transparent'}`}
            >
              <canvas
                ref={(el) => {
                  canvases.current[cam] = el;
                }}
                width={424}
                height={318}
                className={fill ? 'block h-full w-full object-contain' : 'block aspect-[4/3] w-full object-contain'}
                aria-label={`${LABEL[cam] ?? cam} camera`}
              />
              <figcaption className="absolute left-2 top-2 rounded bg-black/60 px-1.5 py-0.5 text-xs font-medium text-white">
                {LABEL[cam] ?? cam}
              </figcaption>
              <div
                className={`absolute right-2 top-2 rounded px-1.5 py-0.5 font-mono text-xs tabular-nums ${latencyTone(s?.latencyMs ?? null)}`}
                title={
                  s
                    ? `capture->screen p50 ${s.latencyMs?.toFixed(0) ?? '?'} ms, p95 ${s.latencyP95Ms?.toFixed(0) ?? '?'} ms; ${s.fps} fps; ${s.kbps} kbit/s; ${s.width}x${s.height}; RTT ${snap.rttMs ?? '?'} ms`
                    : 'no frames yet'
                }
              >
                {s?.latencyMs != null ? `${Math.round(s.latencyMs)} ms` : '-- ms'} · {s?.fps ?? 0} fps
              </div>
              {stale && (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/55 text-center text-white">
                  {s?.lastFrameAt ? (
                    <>
                      <span className="rounded bg-red-600 px-2 py-0.5 text-sm font-semibold tracking-wide">STALE</span>
                      <span className="text-xs">
                        Image is {((s.ageMs ?? 0) / 1000).toFixed(1)} s old. Don&apos;t move the arm.
                      </span>
                    </>
                  ) : (
                    <span className="text-sm">{STATE_TEXT[snap.state]}…</span>
                  )}
                </div>
              )}
            </figure>
          );
        })}
      </div>
      <div
        className={
          fill
            ? 'pointer-events-none absolute inset-x-1.5 bottom-1.5 flex min-w-0 flex-nowrap items-center gap-x-2 overflow-hidden whitespace-nowrap rounded bg-black/60 px-1.5 py-0.5 text-[11px] text-zinc-200'
            : 'mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400'
        }
      >
        <span className="inline-flex items-center gap-1">
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              snap.state === 'live' ? 'bg-emerald-500' : snap.state === 'fallback' ? 'bg-amber-500' : snap.state === 'denied' || snap.state === 'error' ? 'bg-red-500' : 'bg-zinc-400'
            }`}
          />
          {STATE_TEXT[snap.state]}
        </span>
        {snap.rttMs != null && <span>RTT {snap.rttMs} ms</span>}
        {snap.detail && <span className="min-w-0 truncate">{snap.detail}</span>}
      </div>
    </div>
  );
}

/** The feed for a task this browser has claimed (token minted only for the current claimant). */
export function TaskCameraFeed({ taskId, ...rest }: Omit<CameraFeedProps, 'getGrant'> & { taskId: bigint | number | string }) {
  const { identity } = useContributor();
  const getGrant = useMemo(() => (identity ? taskStreamGrant(taskId, identity) : null), [taskId, identity]);
  if (!getGrant) {
    return (
      <div className={`rounded-lg bg-black/80 p-6 text-center text-sm text-zinc-300 ${rest.layout === 'fill' ? 'grid h-full place-items-center' : ''}`}>Connecting to the factory…</div>
    );
  }
  return <CameraFeed key={`${taskId}:${identity}`} getGrant={getGrant} {...rest} />;
}
