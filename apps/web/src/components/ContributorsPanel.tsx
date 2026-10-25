'use client';

import { useEffect, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { PRESENCE_STALE_MS, usePresenceById } from '@/lib/contributor/hooks';
import { fmtCents } from '@/lib/format';

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`;
}

/** TDM-60: live contributor list with online presence (dashboard). */
export function ContributorsPanel({ limit = 20 }: { limit?: number }) {
  const [allRows, ready] = useRows('contributor');
  const rows = allRows.filter((c) => c.kind !== 'merged'); // a duplicate account folded into another (merge_contributor)
  const [presence] = usePresenceById();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(t);
  }, []);
  if (!ready) return <p className="text-sm text-zinc-500">Loading contributors…</p>;
  if (rows.length === 0) return <p className="text-sm text-zinc-500">No contributors yet. Scan the QR code to join.</p>;

  const list = [...rows]
    .sort((a, b) => Number(b.online) - Number(a.online) || Number(b.id - a.id))
    .slice(0, limit);
  const onlineCount = rows.filter((c) => c.online).length;

  return (
    <div className="rounded-lg border border-zinc-200 dark:border-zinc-800">
      <p className="border-b border-zinc-200 px-3 py-2 text-xs text-zinc-500 dark:border-zinc-800">
        {onlineCount} online · {rows.length} total
      </p>
      <ul className="divide-y divide-zinc-100 text-sm dark:divide-zinc-900">
        {list.map((c) => {
          const p = presence.get(c.id);
          const seenMs = p ? now - p.lastSeen.toDate().getTime() : undefined;
          // `online` is the server's word; a stale last_seen means the sweep is about to flip it.
          const stale = c.online && seenMs != null && seenMs > PRESENCE_STALE_MS;
          return (
            <li key={String(c.id)} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
              <span
                className={`inline-block h-2 w-2 rounded-full ${c.online ? (stale ? 'bg-amber-400' : 'bg-emerald-500') : 'bg-zinc-300 dark:bg-zinc-700'}`}
                title={c.online ? (stale ? 'online (heartbeat late)' : 'online') : 'offline'}
                aria-label={c.online ? 'online' : 'offline'}
              />
              <span className="font-medium">{c.name}</span>
              <span className="font-mono text-xs text-zinc-400">#{String(c.id)}</span>
              {c.kind === 'dataset_seller' && (
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">seller</span>
              )}
              {c.devices.length > 0 && <span className="text-xs text-zinc-500">{c.devices.join(', ')}</span>}
              <span className="ml-auto flex items-center gap-3 text-xs text-zinc-500">
                {p && <span>{p.page} · {ago(seenMs ?? 0)}</span>}
                <span>{c.payoutsEnabled ? 'payouts on' : 'payouts not set up'}</span>
                <span>{fmtCents(c.totalPaidCents)} paid</span>
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
