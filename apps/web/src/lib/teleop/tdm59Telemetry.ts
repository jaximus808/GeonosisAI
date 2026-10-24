'use client';

// TDM-59: read the live teleop console's health without reaching into it.
//
// The TDM-56 console and TDM-58 recording controls already publish their state as DOM attributes
// (data-testid / data-state / data-phase, plus the feed-health JSON in a screen-reader span). This
// hook polls those 4x a second so the onboarding strip can grade the connection and show the
// session state while those tickets stay untouched. If TDM-56 later exposes `feed` on its context,
// pass it to <LatencyBadge> directly instead.

import { useEffect, useState } from 'react';
import type { FeedHealth } from '@/components/teleop/CameraFeed';

export type SessionState = 'connecting' | 'viewing' | 'controlling' | 'recording' | 'uploading';

export interface ConsoleTelemetry {
  present: boolean; // the console is on the page
  feed: FeedHealth | null;
  linkText: string | null; // "Connected", "Reconnecting", "No access", ...
  linkOpen: boolean;
  rttMs: number | null;
  cmdHz: number | null;
  clutch: 'drive' | 'blocked' | 'idle' | null;
  recPhase: string | null; // tdm58: idle | recording | policy | human | handed_back
  uploadState: string | null; // tdm58: pending | retrying | failed | none | done
  session: SessionState;
}

const EMPTY: ConsoleTelemetry = {
  present: false,
  feed: null,
  linkText: null,
  linkOpen: false,
  rttMs: null,
  cmdHz: null,
  clutch: null,
  recPhase: null,
  uploadState: null,
  session: 'connecting',
};

const q = (id: string) => (typeof document === 'undefined' ? null : document.querySelector<HTMLElement>(`[data-testid="${id}"]`));

export function readConsoleTelemetry(): ConsoleTelemetry {
  const status = q('tdm56-clutch-status');
  if (!status) return EMPTY;
  let feed: FeedHealth | null = null;
  try {
    const raw = q('tdm56-feed-health')?.textContent;
    feed = raw ? (JSON.parse(raw) as FeedHealth) : null;
  } catch {
    feed = null;
  }
  const linkEl = q('tdm56-link-state');
  const linkText = linkEl?.textContent?.trim() ?? null;
  const rttM = linkEl?.parentElement?.textContent?.match(/(\d+)\s*ms/);
  const hzM = q('tdm56-cmd-hz')?.textContent?.match(/([\d.]+)/);
  const clutch = (status.dataset.state as ConsoleTelemetry['clutch']) ?? null;
  const recPhase = q('tdm58-recording-controls')?.dataset.phase ?? null;
  const uploadState = q('tdm58-upload')?.dataset.state ?? null;
  const linkOpen = linkText === 'Connected';

  let session: SessionState = 'viewing';
  if (uploadState === 'pending' || uploadState === 'retrying') session = 'uploading';
  else if (recPhase && recPhase !== 'idle' && recPhase !== 'handed_back') session = 'recording';
  else if (!linkOpen || !feed || feed.state === 'connecting' || feed.state === 'reconnecting') session = 'connecting';
  else if (clutch === 'drive') session = 'controlling';

  return {
    present: true,
    feed,
    linkText,
    linkOpen,
    rttMs: rttM ? Number(rttM[1]) : null,
    cmdHz: hzM ? Number(hzM[1]) : null,
    clutch,
    recPhase,
    uploadState,
    session,
  };
}

export function useConsoleTelemetry(periodMs = 250): ConsoleTelemetry {
  const [t, setT] = useState<ConsoleTelemetry>(EMPTY);
  useEffect(() => {
    const tick = () => setT(readConsoleTelemetry());
    tick();
    const id = window.setInterval(tick, periodMs);
    return () => window.clearInterval(id);
  }, [periodMs]);
  return t;
}
