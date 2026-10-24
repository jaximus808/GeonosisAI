'use client';

import { useEffect, useRef, useState } from 'react';
import { ACTOR_KIND_LABEL } from '@/lib/eventlog/classify';
import { canonicalRecord } from '@/lib/eventlog/chain';
import type { EventItem } from '@/lib/eventlog/useEventLog';
import { KIND_STYLE, TestTag } from './EventRow';
import { fmtMicros, isoUtc } from './time';

/** Side drawer with one row in full: fields, pretty payload, and where it sits in the chain. */
export function EventDrawer({
  item,
  hashAfter,
  onClose,
}: {
  item: EventItem;
  hashAfter: string | undefined;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const pretty = item.payload ? JSON.stringify(item.payload, null, 2) : item.row.payloadJson;
  const rowJson = JSON.stringify(
    {
      id: item.id.toString(),
      ts: isoUtc(item.tsMicros),
      ts_micros: item.tsMicros.toString(),
      actor: item.row.actor,
      type: item.row.type,
      payload_json: item.row.payloadJson,
    },
    null,
    2,
  );

  async function copyRow() {
    try {
      await navigator.clipboard.writeText(rowJson);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard blocked
    }
  }

  const s = KIND_STYLE[item.kind];
  const record = canonicalRecord({
    id: item.id,
    tsMicros: item.tsMicros,
    actor: item.row.actor,
    type: item.row.type,
    payloadJson: item.row.payloadJson,
  });

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <button type="button" aria-label="Close" tabIndex={-1} onClick={onClose} className="absolute inset-0 bg-zinc-950/30" />
      <aside className="relative flex h-full w-full max-w-xl flex-col overflow-y-auto border-l border-zinc-200 bg-white shadow-2xl motion-safe:animate-[drawer-in_160ms_ease-out] dark:border-zinc-800 dark:bg-zinc-900">
        <style>{`@keyframes drawer-in{from{transform:translateX(24px);opacity:.4}to{transform:none;opacity:1}}`}</style>
        <header className="flex items-start gap-3 border-b border-zinc-200 px-5 py-4 dark:border-zinc-800">
          <span aria-hidden className={`mt-1 w-[3px] self-stretch rounded-full ${s.rail}`} />
          <div className="min-w-0 flex-1">
            <h2 id="drawer-title" className="text-base font-semibold break-words">
              {item.row.type}
            </h2>
            <p className="mt-0.5 text-sm text-zinc-500">
              Row {item.id.toString()}, {fmtMicros(item.tsMicros, true)}
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded px-2 py-1 text-sm text-zinc-600 hover:bg-zinc-100 focus-visible:outline-2 focus-visible:outline-teal-600 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            Close
          </button>
        </header>

        <dl className="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-2 px-5 py-4 text-sm">
          <dt className="text-zinc-500">Actor</dt>
          <dd className="flex flex-wrap items-center gap-2">
            <span className={`rounded px-1.5 text-xs leading-5 ${s.chip}`}>{item.row.actor}</span>
            <span className="text-xs text-zinc-500">{ACTOR_KIND_LABEL[item.kind]}</span>
          </dd>
          <dt className="text-zinc-500">Time (UTC)</dt>
          <dd className="font-mono text-xs leading-5 break-all">{isoUtc(item.tsMicros)}</dd>
          {item.ticketId && (
            <>
              <dt className="text-zinc-500">Ticket</dt>
              <dd>{item.ticketId}</dd>
            </>
          )}
          {item.test && (
            <>
              <dt className="text-zinc-500">Marked</dt>
              <dd className="flex flex-wrap items-center gap-2">
                <TestTag why={item.testWhy} />
                <span className="text-xs text-zinc-500">{item.testWhy}</span>
              </dd>
            </>
          )}
          <dt className="text-zinc-500">Chain after</dt>
          <dd className="font-mono text-xs leading-5 break-all text-zinc-600 dark:text-zinc-400">{hashAfter ?? '-'}</dd>
        </dl>

        <div className="px-5 pb-4">
          <h3 className="mb-1.5 text-sm font-medium">Payload</h3>
          <pre className="max-h-[50vh] overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-5 whitespace-pre-wrap break-words dark:bg-zinc-950">
            {pretty || '(empty)'}
          </pre>
        </div>

        <details className="px-5 pb-4 text-sm">
          <summary className="cursor-pointer text-zinc-600 dark:text-zinc-400">Bytes that go into the chain hash</summary>
          <pre className="mt-2 overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-5 whitespace-pre-wrap break-all dark:bg-zinc-950">
            {record}
          </pre>
        </details>

        <div className="mt-auto flex items-center gap-3 border-t border-zinc-200 px-5 py-3 dark:border-zinc-800">
          <button
            type="button"
            onClick={copyRow}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-50 focus-visible:outline-2 focus-visible:outline-teal-600 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            Copy row as JSON
          </button>
          <span className="text-xs text-teal-700 dark:text-teal-400" aria-live="polite">
            {copied ? 'Row copied' : ''}
          </span>
        </div>
      </aside>
    </div>
  );
}
