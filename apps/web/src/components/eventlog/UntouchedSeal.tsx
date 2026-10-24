'use client';

import { useState } from 'react';
import type { ChainSummary } from '@/lib/eventlog/chain';
import type { WatchAlert } from '@/lib/eventlog/useEventLog';
import { APPEND_ONLY_ENFORCED, VERIFY_COMMAND } from '@/lib/eventlog/config';
import { fmtDuration, fmtMicros } from './time';

type Check = { ok: boolean | 'pending'; label: string; detail: string };

/**
 * The "untouched" seal for event_log (TDM-77). Every line here is checked from the live rows
 * in this browser; the one thing the module does not enforce yet is said plainly.
 */
export function UntouchedSeal({
  summary,
  alerts,
  watchingSince,
}: {
  summary: ChainSummary;
  alerts: WatchAlert[];
  watchingSince: number;
}) {
  const [copied, setCopied] = useState<'hash' | 'cmd' | null>(null);
  const missing = summary.gaps.reduce((n, g) => n + g.missing, 0n);
  const startsAtOne = summary.firstId === undefined || summary.firstId === 1n;

  const checks: Check[] = [
    {
      ok: summary.gaps.length === 0,
      label: summary.gaps.length === 0 ? 'No gaps in the ids' : `${missing} id${missing === 1n ? '' : 's'} missing`,
      detail:
        summary.gaps.length === 0
          ? `Every id from ${summary.firstId ?? '-'} to ${summary.lastId ?? '-'} is present.${startsAtOne ? '' : ' The log does not start at id 1.'}`
          : `Missing after ${summary.gaps.slice(0, 4).map((g) => `#${g.after}`).join(', ')}${summary.gaps.length > 4 ? '…' : ''}.`,
    },
    {
      ok: summary.tsInversions === 0,
      label: summary.tsInversions === 0 ? 'Times rise with the ids' : `${summary.tsInversions} time${summary.tsInversions === 1 ? '' : 's'} out of order`,
      detail: 'ts is the database clock at commit (ctx.timestamp). Callers cannot set it.',
    },
    {
      ok: alerts.length === 0,
      label: alerts.length === 0 ? 'No row changed while this page was open' : `${alerts.length} row${alerts.length === 1 ? '' : 's'} changed while open`,
      detail:
        alerts.length === 0
          ? `Watching every row since ${new Date(watchingSince).toLocaleTimeString()}.`
          : alerts.slice(0, 4).map((a) => `#${a.id} ${a.change}`).join(', '),
    },
    {
      ok: APPEND_ONLY_ENFORCED ? true : 'pending',
      label: APPEND_ONLY_ENFORCED ? 'The module refuses edits and deletes' : 'Append-only is a convention for now',
      detail: APPEND_ONLY_ENFORCED
        ? 'No reducer updates or deletes event_log rows (TDM-15).'
        : 'No reducer edits rows today, but the module does not block it yet. TDM-15 adds that rule.',
    },
  ];

  const broken = checks.some((c) => c.ok === false);
  const groups = summary.hash.match(/.{4}/g) ?? [];

  async function copy(what: 'hash' | 'cmd', text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      // clipboard blocked: the text is selectable on screen
    }
  }

  return (
    <section
      aria-labelledby="seal-title"
      className={`mb-8 rounded-xl border-2 bg-white dark:bg-zinc-900 ${
        broken ? 'border-red-500/70' : 'border-emerald-600/50 dark:border-emerald-500/40'
      }`}
    >
      <div className="flex flex-wrap items-start gap-x-8 gap-y-5 p-5 sm:p-6">
        <div className="min-w-0 flex-1 basis-[22rem]">
          <h2 id="seal-title" className="flex items-center gap-2 text-lg font-semibold tracking-tight">
            <SealMark broken={broken} />
            {broken ? 'The log has been disturbed' : 'Untouched log'}
          </h2>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            {summary.count === 0 ? (
              'No rows yet.'
            ) : (
              <>
                <span className="font-medium text-zinc-900 tabular-nums dark:text-zinc-100">{summary.count} rows</span>, ids{' '}
                <span className="tabular-nums">{summary.firstId?.toString()}</span> to{' '}
                <span className="tabular-nums">{summary.lastId?.toString()}</span>, written over{' '}
                {fmtDuration(summary.firstTsMicros, summary.lastTsMicros)}.
              </>
            )}
          </p>

          <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <dt className="text-zinc-500">First row</dt>
            <dd className="tabular-nums">{fmtMicros(summary.firstTsMicros, true)}</dd>
            <dt className="text-zinc-500">Last row</dt>
            <dd className="tabular-nums">{fmtMicros(summary.lastTsMicros, true)}</dd>
          </dl>

          <div className="mt-5">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-sm text-zinc-500">Chain hash over every row</span>
              <span className="text-xs text-zinc-400">{summary.algorithm}</span>
            </div>
            <button
              type="button"
              onClick={() => copy('hash', summary.hash)}
              title="Copy the full hash"
              key={summary.hash}
              className="mt-1 grid w-full grid-cols-4 gap-x-3 gap-y-0.5 rounded-lg px-2 py-2 text-left font-mono text-[15px] leading-6 tracking-wide text-zinc-900 hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-emerald-600 sm:grid-cols-8 dark:text-zinc-100 dark:hover:bg-zinc-800"
            >
              {groups.map((g, i) => (
                <span key={i}>{g}</span>
              ))}
            </button>
            <p className="mt-1 h-4 text-xs text-emerald-700 dark:text-emerald-400" aria-live="polite">
              {copied === 'hash' ? 'Hash copied' : ''}
            </p>
          </div>
        </div>

        <ul className="min-w-0 flex-1 basis-[18rem] space-y-3">
          {checks.map((c) => (
            <li key={c.label} className="flex gap-3">
              <CheckMark ok={c.ok} />
              <div className="min-w-0">
                <p className="text-sm font-medium">{c.label}</p>
                <p className="text-xs text-zinc-500">{c.detail}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-zinc-200 px-5 py-3 text-xs text-zinc-500 sm:px-6 dark:border-zinc-800">
        <span>Get the same count and hash from a terminal:</span>
        <button
          type="button"
          onClick={() => copy('cmd', VERIFY_COMMAND)}
          className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-zinc-700 hover:bg-zinc-200 focus-visible:outline-2 focus-visible:outline-emerald-600 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700"
        >
          {VERIFY_COMMAND}
        </button>
        <span aria-live="polite" className="text-emerald-700 dark:text-emerald-400">
          {copied === 'cmd' ? 'Command copied' : ''}
        </span>
      </div>
    </section>
  );
}

function SealMark({ broken }: { broken: boolean }) {
  return broken ? (
    <svg viewBox="0 0 20 20" className="size-5 text-red-600" aria-hidden>
      <circle cx="10" cy="10" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M10 5.5v5.5M10 13.6v.9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  ) : (
    <svg viewBox="0 0 20 20" className="size-5 text-emerald-600" aria-hidden>
      <circle cx="10" cy="10" r="8.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M6.3 10.4l2.4 2.4 5-5.4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CheckMark({ ok }: { ok: boolean | 'pending' }) {
  if (ok === 'pending')
    return (
      <span aria-label="Not enforced yet" className="mt-0.5 size-4 shrink-0 rounded-full border-2 border-dashed border-amber-500" />
    );
  return ok ? (
    <span aria-label="Passed" className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-emerald-600 text-white">
      <svg viewBox="0 0 12 12" className="size-2.5" aria-hidden>
        <path d="M2.5 6.2l2.2 2.2 4.6-4.9" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  ) : (
    <span aria-label="Failed" className="mt-0.5 grid size-4 shrink-0 place-items-center rounded-full bg-red-600 text-[10px] font-bold text-white">
      !
    </span>
  );
}
