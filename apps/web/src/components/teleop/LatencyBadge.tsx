'use client';

// TDM-59: connection quality indicator with thresholds and advice ("Too laggy. Try Wi-Fi.").
//
//   <LatencyBadge videoMs={feed.worstLatencyMs} videoStale={feed.stale} rttMs={link.ackRttMs} cmdHz={link.cmdHz} driving />
//
// Pure: it grades whatever numbers it is given (lib/teleop/tdm59Quality.ts holds the thresholds),
// so the teleop page and practice mode show the same verdicts.

import { useEffect, useId, useRef, useState } from 'react';
import {
  GRADE_STYLE,
  TDM59_CMD_HZ,
  TDM59_RTT_MS,
  TDM59_VIDEO_MS,
  adviseConnection,
  gradeHz,
  gradeMs,
  readNetInfo,
  type Grade,
  type NetInfo,
} from '@/lib/teleop/tdm59Quality';

export interface LatencyBadgeProps {
  videoMs?: number | null;
  videoStale?: boolean;
  /** The camera feed is not connected at all: 'denied' | 'error' | 'closed'. */
  videoDown?: string;
  rttMs?: number | null;
  cmdHz?: number | null;
  driving?: boolean;
  /** Where driving pauses (the TDM-56 console default is 400 ms). */
  pauseAtMs?: number;
  /** Label the numbers as simulated (practice mode). */
  simulated?: boolean;
  className?: string;
}

export function LatencyBadge({ videoMs, videoStale, videoDown, rttMs, cmdHz, driving, pauseAtMs = 400, simulated, className = '' }: LatencyBadgeProps) {
  const [open, setOpen] = useState(false);
  const [net, setNet] = useState<NetInfo | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const pid = useId();
  useEffect(() => {
    setNet(readNetInfo());
    const id = window.setInterval(() => setNet(readNetInfo()), 5000);
    return () => window.clearInterval(id);
  }, []);
  useEffect(() => {
    if (!open) return;
    const off = (e: PointerEvent) => !root.current?.contains(e.target as Node) && setOpen(false);
    window.addEventListener('pointerdown', off);
    return () => window.removeEventListener('pointerdown', off);
  }, [open]);

  const a = adviseConnection({ videoMs, videoStale, videoDown, rttMs, cmdHz, driving, net: simulated ? null : net });
  const st = GRADE_STYLE[a.grade];
  const lead = videoDown ? 'off' : videoStale ? 'frozen' : videoMs != null ? `${Math.round(videoMs)} ms` : rttMs != null ? `${Math.round(rttMs)} ms` : '--';

  return (
    // one row, always: the "bad" tip sits beside the badge, truncated (the badge opens the full advice), so it
    // never pushes the teleop console down when the connection gets bad
    <div ref={root} className={`relative flex min-w-0 items-center gap-2 ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={pid}
        className={`flex shrink-0 items-center gap-2 rounded-full px-3 py-1.5 text-sm ring-1 ${st.chip}`}
        data-testid="tdm59-latency"
        data-grade={a.grade}
      >
        <span className={`inline-block h-2.5 w-2.5 rounded-full ${st.dot} ${a.grade === 'bad' ? 'animate-pulse' : ''}`} />
        <span className="font-semibold">{a.headline}</span>
        <span className="tabular-nums opacity-75">{lead}</span>
      </button>
      {a.grade === 'bad' && !open && (
        <p className="min-w-0 max-w-xs truncate text-xs font-medium text-red-700 dark:text-red-300" role="status" title={a.tips[0]} data-testid="tdm59-latency-tip">
          {a.tips[0]}
        </p>
      )}
      {open && (
        <div
          id={pid}
          className="absolute left-0 top-full z-40 mt-2 w-[min(22rem,calc(100vw-2rem))] rounded-2xl bg-white p-4 text-sm shadow-xl ring-1 ring-zinc-200 dark:bg-zinc-900 dark:ring-zinc-700"
          role="dialog"
          aria-label="Connection details"
        >
          <p className={`font-semibold ${st.text}`}>{a.headline}</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-zinc-700 dark:text-zinc-300">
            {a.tips.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
          <dl className="mt-3 grid grid-cols-[auto_auto_1fr] items-baseline gap-x-3 gap-y-1.5 border-t border-zinc-100 pt-3 dark:border-zinc-800">
            <Row label="Video delay" value={videoDown ? 'not connected' : videoStale ? 'frozen' : fmtMs(videoMs)} grade={videoDown || videoStale ? 'bad' : gradeMs(videoMs, TDM59_VIDEO_MS)} rule={`good under ${TDM59_VIDEO_MS.good}, laggy over ${TDM59_VIDEO_MS.ok}; driving pauses over ${pauseAtMs}`} />
            <Row label="Control round trip" value={fmtMs(rttMs)} grade={gradeMs(rttMs, TDM59_RTT_MS)} rule={`good under ${TDM59_RTT_MS.good}, laggy over ${TDM59_RTT_MS.ok}`} />
            <Row
              label="Commands sent"
              value={driving ? (cmdHz != null ? `${Math.round(cmdHz)}/s` : '--') : 'idle'}
              grade={driving ? gradeHz(cmdHz) : 'unknown'}
              rule={`while driving: good at ${TDM59_CMD_HZ.good}/s or more, jerky under ${TDM59_CMD_HZ.ok}/s`}
            />
            {!simulated && net?.type && <Row label="Network" value={net.type === 'cellular' ? 'mobile data' : net.type} grade={net.type === 'cellular' ? 'ok' : 'unknown'} />}
          </dl>
          {simulated && <p className="mt-2 text-xs text-zinc-500">Practice mode: these numbers are simulated so you can feel what lag does.</p>}
        </div>
      )}
    </div>
  );
}

function fmtMs(ms: number | null | undefined) {
  return ms == null ? '--' : `${Math.round(ms)} ms`;
}

function Row({ label, value, grade, rule }: { label: string; value: string; grade: Grade; rule?: string }) {
  return (
    <>
      <dt className="text-zinc-500">{label}</dt>
      <dd className="flex items-center gap-1.5 font-medium tabular-nums">
        <span className={`inline-block h-2 w-2 rounded-full ${GRADE_STYLE[grade].dot}`} />
        {value}
      </dd>
      <dd className="text-xs text-zinc-500">{rule}</dd>
    </>
  );
}
