'use client';

// TDM-76: small pieces of the agents panel (status chip, predicate line, labels).

import { hasTable, useRows, type TableName } from '@/lib/spacetime/hooks';
import { leafCurrent, leafTarget, leafWords, leaves, predicateWords, tableAccessor, type PredicateLeaf, type PredicateNode } from '@/lib/tdm76/predicate';

export const ROLE_LABEL: Record<string, string> = {
  onboarding_droid: 'Onboarding Droid',
  data_droid: 'Data Droid',
  training_droid: 'Training Droid',
  planner: 'Planner',
  reviewer: 'Reviewer',
  dispatcher: 'Dispatcher',
};

export const ROLE_DOES: Record<string, string> = {
  onboarding_droid: 'writes and checks the robot adapter, cameras, success check and reset',
  data_droid: 'converts episodes, trims failures and assembles the dataset',
  training_droid: 'writes the training config, runs the fine-tune, reports the checkpoint',
};

/** Status colour system. The rail stripe is the agents' signature: people never get one. */
export const STATUS: Record<string, { label: string; chip: string; rail: string; dot: string }> = {
  starting: { label: 'starting', chip: 'bg-cyan-50 text-cyan-800 ring-cyan-600/30 dark:bg-cyan-950/50 dark:text-cyan-300', rail: '#0891b2', dot: 'bg-cyan-500' },
  running: { label: 'running', chip: 'bg-cyan-50 text-cyan-800 ring-cyan-600/30 dark:bg-cyan-950/50 dark:text-cyan-300', rail: '#0891b2', dot: 'bg-cyan-500' },
  waiting_for: { label: 'waiting', chip: 'bg-amber-50 text-amber-900 ring-amber-600/30 dark:bg-amber-950/50 dark:text-amber-300', rail: '#d97706', dot: 'bg-amber-500' },
  resuming: { label: 'waking up', chip: 'bg-violet-100 text-violet-900 ring-violet-600/40 dark:bg-violet-950/60 dark:text-violet-200', rail: '#7c3aed', dot: 'bg-violet-500' },
  paused: { label: 'paused', chip: 'bg-zinc-100 text-zinc-700 ring-zinc-500/25 dark:bg-zinc-800 dark:text-zinc-300', rail: '#a1a1aa', dot: 'bg-zinc-400' },
  done: { label: 'done', chip: 'bg-emerald-50 text-emerald-800 ring-emerald-600/30 dark:bg-emerald-950/50 dark:text-emerald-300', rail: '#059669', dot: 'bg-emerald-500' },
  failed: { label: 'failed', chip: 'bg-red-50 text-red-800 ring-red-600/30 dark:bg-red-950/50 dark:text-red-300', rail: '#dc2626', dot: 'bg-red-500' },
};
export const statusOf = (s: string) => STATUS[s] ?? STATUS.paused;

export function StatusChip({ status }: { status: string }) {
  const s = statusOf(status);
  const live = status === 'running' || status === 'starting' || status === 'resuming';
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${s.chip}`}>
      <span className="relative flex h-1.5 w-1.5">
        {live && <span className={`absolute inline-flex h-full w-full rounded-full opacity-60 motion-safe:animate-ping ${s.dot}`} />}
        <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${s.dot}`} />
      </span>
      {s.label}
    </span>
  );
}

/** Striped status rail on the left of every agent card: "this ran on its own". */
export function Rail({ status }: { status: string }) {
  const c = statusOf(status).rail;
  return (
    <span
      aria-hidden
      className="absolute inset-y-0 left-0 w-1.5 rounded-l-lg"
      style={{ background: `repeating-linear-gradient(-45deg, ${c} 0 4px, color-mix(in srgb, ${c} 55%, transparent) 4px 8px)` }}
    />
  );
}

export function shortId(id: string | undefined) {
  return id ? id.slice(0, 8) : '';
}

export function ago(us: number, nowMs: number): string {
  const s = Math.max(0, Math.round(nowMs / 1000 - us / 1e6));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function duration(s: number): string {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

export const fmtInt = (n: number) => Math.round(n).toLocaleString('en-US');

/** "waiting for 3 accepted episodes (1/3)", with the count live from the predicate's table. */
export function PredicateLine({ predicate, raw, live, className = '' }: { predicate?: PredicateNode; raw?: string; live: boolean; className?: string }) {
  const ls = leaves(predicate);
  if (!predicate) return <span className={className}>waiting{raw ? `: ${raw}` : ''}</span>;
  if (ls.length === 1) {
    return (
      <span className={className} title={ls[0].raw ?? raw}>
        {leafWords(ls[0])} {live && <LeafProgress leaf={ls[0]} />}
      </span>
    );
  }
  return (
    <span className={className} title={raw}>
      {predicateWords(predicate)}
      {live && ls.map((l, i) => <LeafProgress key={i} leaf={l} />)}
    </span>
  );
}

function LeafProgress({ leaf }: { leaf: PredicateLeaf }) {
  const acc = tableAccessor(leaf.table);
  if (!hasTable(acc)) return null;
  return <LiveLeafProgress leaf={leaf} accessor={acc} />;
}

function LiveLeafProgress({ leaf, accessor }: { leaf: PredicateLeaf; accessor: TableName }) {
  const [rows, ready] = useRows(accessor);
  if (!ready) return null;
  const cur = leafCurrent(leaf, rows as readonly object[]);
  const target = leafTarget(leaf);
  if (cur === undefined) return <span className="tabular-nums text-amber-700/80 dark:text-amber-300/70">(nothing yet)</span>;
  if (target === undefined) return <span className="tabular-nums text-amber-700/80 dark:text-amber-300/70">(now {String(cur)})</span>;
  const pct = Math.min(100, Math.round((cur / Math.max(1, target)) * 100));
  return (
    <span className="inline-flex items-center gap-2 align-middle">
      <span className="tabular-nums font-semibold">
        ({fmtInt(cur)}/{fmtInt(target)})
      </span>
      <span className="inline-block h-1.5 w-20 overflow-hidden rounded-full bg-amber-200/70 dark:bg-amber-900/60" role="progressbar" aria-valuenow={cur} aria-valuemax={target}>
        <span className="block h-full rounded-full bg-amber-500 transition-[width] duration-700" style={{ width: `${pct}%` }} />
      </span>
    </span>
  );
}

/** Lightning-in-a-database glyph for the resume moment. */
export function WakeGlyph({ className = 'h-4 w-4' }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" fill="none" className={className} aria-hidden>
      <ellipse cx="10" cy="4.5" rx="6.5" ry="2.5" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.5 4.5v11c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-11" stroke="currentColor" strokeWidth="1.5" />
      <path d="M11 7.5 8 12h2.5L9 16l3.5-5H10l1-3.5Z" fill="currentColor" />
    </svg>
  );
}
