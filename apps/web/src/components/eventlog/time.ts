// Time formatting for the event-log views (TDM-77). ts values are micros since the epoch.

export function microsToDate(micros: bigint): Date {
  return new Date(Number(micros / 1000n));
}

/** Local time, e.g. "Sat, Oct 24, 03:12:09". withZone adds the zone name. */
export function fmtMicros(micros: bigint | undefined, withZone = false): string {
  if (micros === undefined) return '-';
  return microsToDate(micros).toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    ...(withZone ? { timeZoneName: 'short' } : {}),
  });
}

export function fmtClock(micros: bigint): string {
  return microsToDate(micros).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}

/** Hour bucket label for timeline group headers, e.g. "Sat, Oct 24, 03:00". */
export function hourKey(micros: bigint): string {
  const d = microsToDate(micros);
  d.setMinutes(0, 0, 0);
  return d.toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function isoUtc(micros: bigint): string {
  // Keep full microsecond precision: Date only holds milliseconds.
  const ms = micros / 1000n;
  const rest = (((micros % 1000n) + 1000n) % 1000n).toString().padStart(3, '0');
  return new Date(Number(ms)).toISOString().replace('Z', `${rest}Z`);
}

export function fmtDuration(from: bigint | undefined, to: bigint | undefined): string {
  if (from === undefined || to === undefined) return '-';
  let s = Number((to - from) / 1_000_000n);
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${s} s`;
  return `${s} s`;
}
