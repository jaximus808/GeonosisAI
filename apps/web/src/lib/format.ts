import type { Timestamp } from 'spacetimedb';

export function fmtTs(ts: Timestamp | undefined): string {
  if (!ts) return '';
  return ts.toDate().toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function fmtCents(cents: number | bigint): string {
  return `$${(Number(cents) / 100).toFixed(2)}`;
}
