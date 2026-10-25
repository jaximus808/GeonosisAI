'use client';

// TDM-94: pieces shared by the ticket page's offer card and the seller page (/sell/[offerId]).

import { useState } from 'react';
import { fmtCents } from '@/lib/format';
import { StatusPill } from '@/components/ticket/Badges';
import {
  PAYMENT_STATUS_TEXT,
  type EpisodeView,
  type OfferView,
  type QaStage,
} from '@/lib/tdm94/offers';

// ---------------------------------------------------------------- slot rail

export type UploadSlot = { ref: string; pct: number; failed?: boolean };

const HATCH = 'repeating-linear-gradient(135deg, transparent 0 5px, rgba(113,113,122,.22) 5px 10px)';
const STAGE_TEXT: Record<QaStage, string> = {
  deterministic: 'Checking files',
  llm: 'LLM review',
  capped: 'QA paused',
  retrying: 'QA retrying',
  done: '',
};

/**
 * Exactly needed_count slots. Delivered episodes that still hold a slot (pending or accepted) fill
 * them in delivery order; uploads in flight fill the next open ones; the rest are open. Rejected
 * deliveries free their slot and are listed after the rail.
 */
export function SlotRail({ view, uploads = [], size = 'md' }: { view: OfferView; uploads?: UploadSlot[]; size?: 'sm' | 'md' }) {
  const n = view.offer.neededCount;
  const holding = view.episodes.filter((e) => e.verdict !== 'rejected');
  const rejected = view.episodes.filter((e) => e.verdict === 'rejected');
  const closed = !['proposed', 'accepted', 'ingesting', 'qa'].includes(view.offer.status);
  const h = size === 'sm' ? 'h-9' : 'h-16';
  const cells = Array.from({ length: n }, (_, i) => {
    if (i < holding.length) return { kind: 'ep' as const, ep: holding[i] };
    const up = uploads[i - holding.length];
    if (up) return { kind: 'up' as const, up };
    return { kind: 'open' as const };
  });

  return (
    <div>
      <ol
        className="grid gap-1.5"
        style={{ gridTemplateColumns: `repeat(${Math.min(n, size === 'sm' ? 12 : 10)}, minmax(0, 1fr))` }}
        aria-label={`${n} slots: ${holding.length} delivered, ${view.openSlots} open`}
      >
        {cells.map((c, i) => {
          if (c.kind === 'ep') {
            const v = c.ep;
            const tone =
              v.verdict === 'accepted'
                ? 'border-emerald-700 bg-emerald-600 text-white'
                : v.stage === 'capped' || v.stage === 'retrying'
                  ? 'border-amber-500 bg-amber-100 text-amber-900 dark:bg-amber-950/60 dark:text-amber-200'
                  : 'border-zinc-400 bg-zinc-100 text-zinc-700 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-200';
            return (
              <li
                key={i}
                title={`${v.ref}: ${v.verdict === 'accepted' ? 'accepted' : STAGE_TEXT[v.stage] || 'under QA'}`}
                className={`relative flex ${h} min-w-0 flex-col justify-end overflow-hidden rounded-[5px] border px-1.5 py-1 ${tone}`}
                style={v.verdict === 'pending' ? { backgroundImage: HATCH } : undefined}
              >
                {size === 'md' && <span className="truncate text-[11px] font-medium leading-tight">{v.ref}</span>}
                <span className="truncate text-[10px] leading-tight opacity-80">
                  {v.verdict === 'accepted' ? 'accepted' : STAGE_TEXT[v.stage] || 'in QA'}
                </span>
              </li>
            );
          }
          if (c.kind === 'up') {
            return (
              <li key={i} title={`${c.up.ref}: uploading ${c.up.pct}%`} className={`relative flex ${h} min-w-0 flex-col justify-end overflow-hidden rounded-[5px] border border-sky-500 bg-white px-1.5 py-1 dark:bg-zinc-900`}>
                <div className={`absolute inset-y-0 left-0 ${c.up.failed ? 'bg-red-200 dark:bg-red-900/50' : 'bg-sky-200 dark:bg-sky-900/60'}`} style={{ width: `${c.up.pct}%` }} />
                {size === 'md' && <span className="relative truncate text-[11px] font-medium leading-tight text-sky-900 dark:text-sky-100">{c.up.ref}</span>}
                <span className="relative text-[10px] tabular-nums leading-tight text-sky-900 dark:text-sky-100">{c.up.failed ? 'failed' : `${c.up.pct}%`}</span>
              </li>
            );
          }
          return (
            <li
              key={i}
              className={`flex ${h} items-end rounded-[5px] border border-dashed px-1.5 py-1 text-[10px] leading-tight ${
                closed ? 'border-zinc-200 text-zinc-300 dark:border-zinc-800 dark:text-zinc-700' : 'border-zinc-400 text-zinc-500 dark:border-zinc-600'
              }`}
            >
              {closed ? 'unused' : 'open'}
            </li>
          );
        })}
      </ol>
      {rejected.length > 0 && (
        <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
          <span>Rejected, slot reopened:</span>
          {rejected.map((r) => (
            <span key={r.episode.id.toString()} className="rounded-[4px] border border-red-300 bg-red-50 px-1.5 py-px font-medium text-red-800 line-through decoration-red-400/70 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
              {r.ref}
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- counts + arithmetic

export function OfferCounts({ view }: { view: OfferView }) {
  const o = view.offer;
  const items: [string, number | string, string][] = [
    ['Ingested', o.ingestedCount, 'every delivery, including rejected ones'],
    ['Accepted', view.billable, 'passed both QA layers; these are paid'],
    ['Rejected', view.rejected, 'QA said no; not paid, slot reopened'],
    ['In QA', view.pending, 'waiting for a verdict'],
  ];
  return (
    <dl className="grid grid-cols-4 gap-px overflow-hidden rounded-md border border-zinc-200 bg-zinc-200 text-center dark:border-zinc-800 dark:bg-zinc-800">
      {items.map(([k, v, t]) => (
        <div key={k} className="bg-white px-2 py-1.5 dark:bg-zinc-900" title={t}>
          <dt className="text-[11px] text-zinc-500">{k}</dt>
          <dd className={`text-lg font-semibold tabular-nums ${k === 'Accepted' && Number(v) > 0 ? 'text-emerald-700 dark:text-emerald-400' : k === 'Rejected' && Number(v) > 0 ? 'text-red-700 dark:text-red-400' : ''}`}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------- QA warnings

export function QaFlags({ view }: { view: OfferView }) {
  const out = [];
  if (view.guard?.suspect) {
    const g = view.guard;
    out.push(
      <Flag key="suspect" tone="bad" title="QA may be too lenient">
        The LLM reviewer accepted {g.accepted} of {g.reviewed} episodes ({Math.round((g.rate ?? 0) * 100)}%), above the {Math.round(g.maxRate * 100)}% limit
        for {g.minReviews}+ reviews. Spot-check the accepted clips before training on them.
      </Flag>,
    );
  }
  if (view.capped > 0) {
    out.push(
      <Flag key="cap" tone="warn" title="QA paused: cap reached">
        {view.capped} episode{view.capped === 1 ? '' : 's'} wait{view.capped === 1 ? 's' : ''} because this offer used its LLM review budget. They stay unpaid
        until an operator raises the cap.
      </Flag>,
    );
  }
  if (view.retrying > 0) {
    out.push(
      <Flag key="retry" tone="warn" title="QA retrying">
        QA could not run for {view.retrying} episode{view.retrying === 1 ? '' : 's'} (an outage on our side, not a problem with the data). They stay in QA and
        are retried automatically.
      </Flag>,
    );
  }
  return out.length ? <div className="space-y-2">{out}</div> : null;
}

function Flag({ tone, title, children }: { tone: 'bad' | 'warn'; title: string; children: React.ReactNode }) {
  const c =
    tone === 'bad'
      ? 'border-red-600 bg-red-50 text-red-950 dark:bg-red-950/40 dark:text-red-100'
      : 'border-amber-500 bg-amber-50 text-amber-950 dark:bg-amber-950/30 dark:text-amber-100';
  return (
    <div role="status" className={`rounded-md border-l-4 px-3 py-2 text-sm ${c}`}>
      <span className="font-semibold">{title}.</span> {children}
    </div>
  );
}

// ---------------------------------------------------------------- per-episode verdicts

function VerdictMark({ v }: { v: EpisodeView }) {
  if (v.verdict === 'accepted') return <StatusPill status="accepted" />;
  if (v.verdict === 'rejected') return <StatusPill status="rejected" />;
  if (v.stage === 'capped') return <StatusPill status="QA paused: cap reached" tone="warn" />;
  if (v.stage === 'retrying') return <StatusPill status="QA retrying" tone="warn" />;
  return <StatusPill status={v.stage === 'llm' ? 'LLM review' : 'checking files'} tone="busy" />;
}

export function EpisodeVerdicts({ view, priceCents }: { view: OfferView; priceCents: bigint }) {
  if (view.episodes.length === 0) {
    return <p className="text-sm text-zinc-500">No episodes delivered yet.</p>;
  }
  return (
    <ul className="divide-y divide-zinc-200 rounded-md border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
      {[...view.episodes].reverse().map((v) => (
        <EpisodeRow key={v.episode.id.toString()} v={v} priceCents={priceCents} />
      ))}
    </ul>
  );
}

function EpisodeRow({ v, priceCents }: { v: EpisodeView; priceCents: bigint }) {
  const [open, setOpen] = useState(v.verdict === 'rejected');
  const headline =
    v.verdict === 'rejected'
      ? v.reasons[0] ?? 'Rejected by QA'
      : v.verdict === 'accepted'
        ? v.review?.reason || 'Passed deterministic and LLM QA'
        : v.stage === 'capped' || v.stage === 'retrying'
          ? v.stageNote
          : v.stage === 'llm'
            ? 'Files passed; the LLM is checking task and success'
            : 'Checking files, timing and smoothness';
  return (
    <li className="px-3 py-2">
      <button type="button" onClick={() => setOpen((x) => !x)} aria-expanded={open} className="flex w-full items-start gap-3 text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600">
        <span className="mt-0.5 w-3 shrink-0 text-xs text-zinc-400" aria-hidden>{open ? '▾' : '▸'}</span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{v.ref}</span>
            <span className="text-xs text-zinc-500">episode {v.episode.id.toString()}</span>
            <VerdictMark v={v} />
            <span className="ml-auto text-xs tabular-nums text-zinc-500">
              {v.billable ? `${fmtCents(priceCents)} in the payout` : v.verdict === 'rejected' ? 'not paid' : 'unpaid until accepted'}
            </span>
          </span>
          <span className={`mt-0.5 block text-sm ${v.verdict === 'rejected' ? 'text-red-800 dark:text-red-300' : 'text-zinc-600 dark:text-zinc-400'}`}>{headline}</span>
        </span>
      </button>
      {open && (
        <div className="ml-6 mt-2 space-y-2">
          {v.checks.length === 0 ? (
            <p className="text-xs text-zinc-500">No QA results yet.</p>
          ) : (
            <table className="w-full text-xs">
              <tbody>
                {v.checks.map((c) => (
                  <tr key={c.name} className="align-top">
                    <td className="w-5 py-0.5">
                      <span className={c.passed ? 'text-emerald-600' : 'font-bold text-red-600'} aria-label={c.passed ? 'passed' : 'failed'}>
                        {c.passed ? '✓' : '✗'}
                      </span>
                    </td>
                    <td className="w-44 py-0.5 pr-3 font-medium">{c.label}</td>
                    <td className="py-0.5 text-zinc-600 dark:text-zinc-400">{c.reason || (c.passed ? 'passed' : 'failed')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {v.review && (
            <p className="text-xs">
              <span className="font-medium">Final review ({v.review.reviewer || 'reviewer'}):</span>{' '}
              <span className="text-zinc-600 dark:text-zinc-400">{v.review.reason || (v.review.accepted ? 'accepted' : 'rejected')}</span>
            </p>
          )}
          {(v.stage === 'capped' || v.stage === 'retrying') && <p className="text-xs text-amber-800 dark:text-amber-300">{v.stageNote}</p>}
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------- payout

export function PayoutStatus({ view }: { view: OfferView }) {
  const price = view.offer.pricePerEpisodeCents;
  const settled = view.offer.status === 'done';
  return (
    <div className="space-y-1.5 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <span className="tabular-nums">
          {settled ? 'Settled' : 'So far'}: {view.billable} accepted × {fmtCents(price)} = <strong>{fmtCents(view.projectedPayoutCents)}</strong>
        </span>
        <span className="text-zinc-500">of at most {fmtCents(view.maxSpendCents)}</span>
      </p>
      {view.payments.length === 0 ? (
        <p className="text-xs text-zinc-500">
          {settled
            ? 'No payout row yet.'
            : 'One payout for the whole offer is reserved once every delivered episode has a verdict and the slots are filled, or the seller says delivery is done.'}
        </p>
      ) : (
        <ul className="space-y-1">
          {view.payments.map((p) => (
            <li key={p.id.toString()} className="flex flex-wrap items-center gap-2 text-xs">
              <StatusPill status={p.status} />
              <span className="tabular-nums">{fmtCents(p.amountCents)}</span>
              <span className="text-zinc-500">{PAYMENT_STATUS_TEXT[p.status] ?? p.status}</span>
              {p.providerRef && <code className="text-zinc-500">{p.providerRef}</code>}
              {p.error && <span className="text-red-700 dark:text-red-400">{p.error}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
