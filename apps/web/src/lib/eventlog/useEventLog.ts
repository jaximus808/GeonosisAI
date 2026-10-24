'use client';

// Live event_log with its chain summary and display fields (TDM-77).

import { useEffect, useMemo, useRef, useState } from 'react';
import { useConnectionStatus, useRows, type RowOf } from '@/lib/spacetime/hooks';
import { EventLogChain, canonicalRecord, type ChainRow, type ChainSummary } from './chain';
import { actorKind, parsePayload, payloadPreview, payloadTicketId, testReason, type ActorKind } from './classify';

export type EventRow = RowOf<'eventLog'>;

export type EventItem = {
  row: EventRow;
  id: bigint;
  tsMicros: bigint;
  kind: ActorKind;
  test: boolean;
  /** Why it is a test row (shown on hover). */
  testWhy: string | undefined;
  payload: Record<string, unknown> | undefined;
  ticketId: string | undefined;
  preview: string;
  /** Position in id-ascending order, i.e. the index into the chain. */
  chainIndex: number;
};

/** A row that changed or disappeared while this page was open. */
export type WatchAlert = { id: string; change: 'changed' | 'removed'; seenAt: number };

export type EventLogState = {
  ready: boolean;
  /** Newest first. */
  items: EventItem[];
  summary: ChainSummary;
  hashAfter: (chainIndex: number) => string | undefined;
  watchingSince: number;
  alerts: WatchAlert[];
};

const REMOVED = '\u0000removed';

export function useEventLog(): EventLogState {
  const [rows, ready] = useRows('eventLog');
  const chainRef = useRef<EventLogChain | null>(null);
  if (!chainRef.current) chainRef.current = new EventLogChain();

  const { asc, summary } = useMemo(() => {
    const asc: ChainRow[] = rows
      .map((r) => ({ id: r.id, tsMicros: r.ts.microsSinceUnixEpoch, actor: r.actor, type: r.type, payloadJson: r.payloadJson }))
      .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    return { asc, summary: chainRef.current!.summarize(asc) };
  }, [rows]);

  const items = useMemo(() => {
    const byId = new Map<bigint, EventRow>();
    for (const r of rows) byId.set(r.id, r);
    const out: EventItem[] = new Array(asc.length);
    // A ticket whose ticket.created row is a test row makes every row on it a test row.
    const testTickets = new Set<string>();
    for (let i = 0; i < asc.length; i++) {
      const c = asc[i];
      const payload = parsePayload(c.payloadJson);
      const ticketId = payloadTicketId(payload);
      let why = testReason(c.type, c.actor, payload);
      if (why && ticketId && c.type === 'ticket.created') testTickets.add(ticketId);
      if (!why && ticketId && testTickets.has(ticketId)) why = `ticket ${ticketId} is a test ticket`;
      out[asc.length - 1 - i] = {
        row: byId.get(c.id)!,
        id: c.id,
        tsMicros: c.tsMicros,
        kind: actorKind(c.actor),
        test: why !== undefined,
        testWhy: why,
        payload,
        ticketId,
        preview: payloadPreview(c.payloadJson, payload),
        chainIndex: i,
      };
    }
    return out;
  }, [rows, asc]);

  // Tamper watch: remember every row as first seen; flag any later change or removal.
  const [watchingSince] = useState(() => Date.now());
  const seen = useRef(new Map<string, string>());
  const [alerts, setAlerts] = useState<WatchAlert[]>([]);
  const { status } = useConnectionStatus();
  useEffect(() => {
    // Skip snapshots taken while (re)connecting: an empty cache is not a deletion.
    if (!ready || status !== 'connected') return;
    if (asc.length === 0 && seen.current.size > 0) return;
    const now = new Map<string, string>();
    for (const c of asc) now.set(c.id.toString(), canonicalRecord(c));
    const fresh: WatchAlert[] = [];
    for (const [id, rec] of seen.current) {
      const cur = now.get(id);
      if (rec === REMOVED) {
        if (cur !== undefined) fresh.push({ id, change: 'changed', seenAt: Date.now() });
      } else if (cur === undefined) fresh.push({ id, change: 'removed', seenAt: Date.now() });
      else if (cur !== rec) fresh.push({ id, change: 'changed', seenAt: Date.now() });
    }
    for (const [id, rec] of now) if (!seen.current.has(id)) seen.current.set(id, rec);
    for (const a of fresh) seen.current.set(a.id, now.get(a.id) ?? REMOVED);
    if (fresh.length) setAlerts((prev) => [...prev, ...fresh]);
  }, [asc, ready, status]);

  return {
    ready,
    items,
    summary,
    hashAfter: (i) => chainRef.current!.hashAt(i),
    watchingSince,
    alerts,
  };
}
