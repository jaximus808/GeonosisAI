'use client';

import type { ActorKind } from '@/lib/eventlog/classify';
import type { EventItem } from '@/lib/eventlog/useEventLog';
import { fmtClock } from './time';

/** Rail and chip colours per actor kind. People are amber, agents teal, the module grey. */
export const KIND_STYLE: Record<ActorKind, { rail: string; chip: string; dot: string }> = {
  human: {
    rail: 'bg-amber-500',
    chip: 'bg-amber-100 text-amber-900 dark:bg-amber-500/15 dark:text-amber-300',
    dot: 'bg-amber-500',
  },
  agent: {
    rail: 'bg-teal-600 dark:bg-teal-500',
    chip: 'bg-teal-50 text-teal-900 dark:bg-teal-500/15 dark:text-teal-300',
    dot: 'bg-teal-600 dark:bg-teal-500',
  },
  system: {
    rail: 'bg-zinc-300 dark:bg-zinc-600',
    chip: 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300',
    dot: 'bg-zinc-400',
  },
};

/** Diagonal hatching marks rows written while building and testing. */
export const TEST_HATCH =
  'bg-[repeating-linear-gradient(135deg,transparent_0_6px,rgb(113_113_122/0.06)_6px_12px)]';

export function TestTag({ why }: { why?: string }) {
  return (
    <span
      title={`Written while building or testing${why ? ` (${why})` : ''}`}
      className="rounded border border-dashed border-zinc-400 px-1 text-[11px] leading-4 text-zinc-500 dark:border-zinc-600"
    >
      test row
    </span>
  );
}

export function EventRowButton({
  item,
  selected,
  onSelect,
}: {
  item: EventItem;
  selected?: boolean;
  onSelect?: (item: EventItem) => void;
}) {
  const s = KIND_STYLE[item.kind];
  const body = (
    <>
      <span className="w-[4.75rem] shrink-0 pt-0.5 text-xs text-zinc-500 tabular-nums">{fmtClock(item.tsMicros)}</span>
      <span aria-hidden className={`w-[3px] shrink-0 self-stretch rounded-full ${item.test ? 'opacity-40' : ''} ${s.rail}`} />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={`text-sm font-medium ${item.test ? 'text-zinc-500' : ''}`}>{item.row.type}</span>
          <span className={`rounded px-1.5 text-xs leading-5 ${s.chip}`}>{item.row.actor}</span>
          {item.ticketId && <span className="text-xs text-zinc-500">ticket {item.ticketId}</span>}
          {item.test && <TestTag why={item.testWhy} />}
        </span>
        {item.preview && (
          <span className="mt-0.5 block truncate font-mono text-xs text-zinc-500" title={item.row.payloadJson}>
            {item.preview}
          </span>
        )}
      </span>
      <span className="shrink-0 pt-0.5 text-xs text-zinc-400 tabular-nums">#{item.id.toString()}</span>
    </>
  );
  const cls = `flex w-full items-start gap-3 px-3 py-2.5 text-left ${item.test ? TEST_HATCH : ''} ${
    selected ? 'bg-zinc-100 dark:bg-zinc-800' : ''
  }`;
  if (!onSelect) return <div className={cls}>{body}</div>;
  return (
    <button
      type="button"
      onClick={() => onSelect(item)}
      aria-pressed={selected}
      className={`${cls} hover:bg-zinc-50 focus-visible:relative focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-teal-600 dark:hover:bg-zinc-800/60`}
    >
      {body}
    </button>
  );
}
