'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { ACTOR_KIND_LABEL, TEST_TYPE_PREFIXES, type ActorKind } from '@/lib/eventlog/classify';
import { useEventLog, type EventItem } from '@/lib/eventlog/useEventLog';
import { UntouchedSeal } from './UntouchedSeal';
import { EventDrawer } from './EventDrawer';
import { EventRowButton, KIND_STYLE } from './EventRow';
import { hourKey, isoUtc } from './time';

const PAGE = 100;
const KINDS: ActorKind[] = ['agent', 'human', 'system'];

type Filters = { actor: string; type: string; ticket: string; kinds: ActorKind[]; hideTests: boolean };

function readFilters(sp: URLSearchParams): Filters {
  const kinds = (sp.get('kind') ?? '').split(',').filter((k): k is ActorKind => (KINDS as string[]).includes(k));
  return {
    actor: sp.get('actor') ?? '',
    type: sp.get('type') ?? '',
    ticket: sp.get('ticket') ?? '',
    kinds,
    hideTests: sp.get('tests') === 'hide',
  };
}

function matches(it: EventItem, f: Filters): boolean {
  if (f.actor && it.row.actor !== f.actor) return false;
  if (f.type && !it.row.type.startsWith(f.type)) return false;
  if (f.ticket && it.ticketId !== f.ticket) return false;
  if (f.kinds.length && !f.kinds.includes(it.kind)) return false;
  if (f.hideTests && it.test) return false;
  return true;
}

/** /events: the live, append-only event_log with the untouched seal (TDM-77). */
export function EventLogExplorer() {
  const log = useEventLog();
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const f = useMemo(() => readFilters(new URLSearchParams(sp.toString())), [sp]);

  const setFilter = useCallback(
    (patch: Partial<Filters>) => {
      const next = { ...f, ...patch };
      const q = new URLSearchParams();
      if (next.actor) q.set('actor', next.actor);
      if (next.type) q.set('type', next.type);
      if (next.ticket) q.set('ticket', next.ticket);
      if (next.kinds.length) q.set('kind', next.kinds.join(','));
      if (next.hideTests) q.set('tests', 'hide');
      const s = q.toString();
      router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
    },
    [f, pathname, router],
  );

  const options = useMemo(() => {
    const actors = new Map<string, number>();
    const types = new Set<string>();
    const tickets = new Map<string, number>();
    let tests = 0;
    for (const it of log.items) {
      actors.set(it.row.actor, (actors.get(it.row.actor) ?? 0) + 1);
      types.add(it.row.type);
      const dot = it.row.type.indexOf('.');
      if (dot > 0) types.add(it.row.type.slice(0, dot + 1));
      if (it.ticketId) tickets.set(it.ticketId, (tickets.get(it.ticketId) ?? 0) + 1);
      if (it.test) tests++;
    }
    return {
      actors: [...actors].sort((a, b) => a[0].localeCompare(b[0])),
      types: [...types].sort(),
      tickets: [...tickets].sort((a, b) => Number(a[0]) - Number(b[0])),
      tests,
    };
  }, [log.items]);

  const filtered = useMemo(() => log.items.filter((it) => matches(it, f)), [log.items, f]);
  const filtering = filtered.length !== log.items.length;

  // Infinite scroll: render PAGE rows at a time; a sentinel at the bottom loads more.
  const [shown, setShown] = useState(PAGE);
  const sentinel = useRef<HTMLDivElement>(null);
  const filterKey = sp.toString();
  const [lastFilterKey, setLastFilterKey] = useState(filterKey);
  if (filterKey !== lastFilterKey) {
    setLastFilterKey(filterKey);
    setShown(PAGE);
  }
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver((es) => es[0]?.isIntersecting && setShown((n) => n + PAGE), { rootMargin: '600px' });
    io.observe(el);
    return () => io.disconnect();
  }, [filtered.length, shown]);
  const visible = filtered.slice(0, shown);

  const [selectedId, setSelectedId] = useState<bigint | null>(null);
  const selected = selectedId === null ? undefined : log.items.find((it) => it.id === selectedId);
  const closeDrawer = useCallback(() => setSelectedId(null), []);

  function exportJson() {
    const doc = {
      exported_at: new Date().toISOString(),
      source: 'event_log',
      chain: {
        algorithm: log.summary.algorithm,
        count: log.summary.count,
        first_id: log.summary.firstId?.toString() ?? null,
        last_id: log.summary.lastId?.toString() ?? null,
        first_ts: log.summary.firstTsMicros !== undefined ? isoUtc(log.summary.firstTsMicros) : null,
        last_ts: log.summary.lastTsMicros !== undefined ? isoUtc(log.summary.lastTsMicros) : null,
        hash: log.summary.hash,
        covers: 'every row, regardless of the filter below',
      },
      filter: { actor: f.actor || null, type_prefix: f.type || null, ticket: f.ticket || null, kinds: f.kinds, hide_tests: f.hideTests },
      rows: [...filtered].reverse().map((it) => ({
        id: it.id.toString(),
        ts: isoUtc(it.tsMicros),
        ts_micros: it.tsMicros.toString(),
        actor: it.row.actor,
        type: it.row.type,
        payload_json: it.row.payloadJson,
      })),
    };
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `event_log_${log.summary.lastId ?? 0}_${log.summary.hash.slice(0, 8)}${filtering ? '_filtered' : ''}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  if (!log.ready) return <p className="text-sm text-zinc-500">Subscribing to event_log…</p>;

  const field =
    'h-9 rounded-md border border-zinc-300 bg-white px-2 text-sm focus-visible:outline-2 focus-visible:outline-teal-600 dark:border-zinc-700 dark:bg-zinc-900';

  return (
    <>
      <UntouchedSeal summary={log.summary} alerts={log.alerts} watchingSince={log.watchingSince} />

      <div className="sticky top-0 z-20 -mx-4 mb-3 border-b border-zinc-200 bg-zinc-50/95 px-4 py-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-zinc-500">
            Actor
            <select className={field} value={f.actor} onChange={(e) => setFilter({ actor: e.target.value })}>
              <option value="">Everyone</option>
              {f.actor && !options.actors.some(([a]) => a === f.actor) && <option value={f.actor}>{f.actor} (0)</option>}
              {options.actors.map(([a, n]) => (
                <option key={a} value={a}>
                  {a} ({n})
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-500">
            Type starts with
            <input
              className={`${field} w-48`}
              list="event-types-tdm77"
              placeholder="e.g. data_plan."
              value={f.type}
              onChange={(e) => setFilter({ type: e.target.value.trim() })}
            />
            <datalist id="event-types-tdm77">
              {options.types.map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-500">
            Ticket
            <select className={field} value={f.ticket} onChange={(e) => setFilter({ ticket: e.target.value })}>
              <option value="">All tickets</option>
              {f.ticket && !options.tickets.some(([t]) => t === f.ticket) && <option value={f.ticket}>Ticket {f.ticket} (0)</option>}
              {options.tickets.map(([t, n]) => (
                <option key={t} value={t}>
                  Ticket {t} ({n})
                </option>
              ))}
            </select>
          </label>

          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-xs text-zinc-500">Written by</legend>
            <div className="flex h-9 items-center gap-1">
              {KINDS.map((k) => {
                const on = f.kinds.includes(k);
                return (
                  <button
                    key={k}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setFilter({ kinds: on ? f.kinds.filter((x) => x !== k) : [...f.kinds, k] })}
                    className={`flex h-8 items-center gap-1.5 rounded-md border px-2 text-sm focus-visible:outline-2 focus-visible:outline-teal-600 ${
                      on
                        ? 'border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900'
                        : 'border-zinc-300 hover:bg-white dark:border-zinc-700 dark:hover:bg-zinc-900'
                    }`}
                  >
                    <span aria-hidden className={`size-2 rounded-full ${KIND_STYLE[k].dot}`} />
                    {ACTOR_KIND_LABEL[k]}
                  </button>
                );
              })}
            </div>
          </fieldset>

          <label
            className="flex h-9 items-center gap-2 text-sm"
            title={`Type starts with ${TEST_TYPE_PREFIXES.join(' ')}, a test actor or payload name, or a row on a test ticket`}
          >
            <input
              type="checkbox"
              className="size-4 accent-teal-700"
              checked={f.hideTests}
              onChange={(e) => setFilter({ hideTests: e.target.checked })}
            />
            Hide test rows ({options.tests})
          </label>

          <div className="ml-auto flex items-center gap-2">
            {filtering && (
              <button
                type="button"
                onClick={() => router.replace(pathname, { scroll: false })}
                className="h-9 rounded-md px-3 text-sm text-zinc-600 hover:bg-white focus-visible:outline-2 focus-visible:outline-teal-600 dark:text-zinc-400 dark:hover:bg-zinc-900"
              >
                Clear filters
              </button>
            )}
            <button
              type="button"
              onClick={exportJson}
              className="h-9 rounded-md border border-zinc-300 bg-white px-3 text-sm hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-teal-600 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
            >
              Export {filtering ? `${filtered.length} row${filtered.length === 1 ? '' : 's'}` : 'all rows'} as JSON
            </button>
          </div>
        </div>
        <p className="mt-2 text-xs text-zinc-500" aria-live="polite">
          {filtering
            ? `Showing ${filtered.length} of ${log.items.length} rows. The seal above always covers every row.`
            : `Showing all ${log.items.length} rows, newest first. Updates live.`}
        </p>
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
          {log.items.length === 0 ? 'No events yet. Rows appear here the moment a reducer writes one.' : 'No rows match these filters.'}
        </div>
      ) : (
        <ol className="overflow-hidden rounded-lg border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
          {visible.map((it, i) => {
            const hk = hourKey(it.tsMicros);
            const newHour = i === 0 || hourKey(visible[i - 1].tsMicros) !== hk;
            return (
              <li key={it.id.toString()} className={newHour ? '' : 'border-t border-zinc-100 dark:border-zinc-800'}>
                {newHour && (
                  <div className={`bg-zinc-50 px-3 py-1.5 text-xs font-medium text-zinc-500 dark:bg-zinc-950/60 ${i === 0 ? '' : 'border-t border-zinc-200 dark:border-zinc-800'}`}>
                    {hk}
                  </div>
                )}
                <EventRowButton item={it} selected={it.id === selectedId} onSelect={(x) => setSelectedId(x.id)} />
              </li>
            );
          })}
        </ol>
      )}
      {visible.length < filtered.length && (
        <div ref={sentinel} className="py-4 text-center text-xs text-zinc-500">
          Loading older rows…
        </div>
      )}

      {selected && <EventDrawer item={selected} hashAfter={log.hashAfter(selected.chainIndex)} onClose={closeDrawer} />}
    </>
  );
}
