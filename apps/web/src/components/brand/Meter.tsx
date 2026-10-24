import { gxCx } from './cx';

/** TDM-109: budget meter. An ochre fill (money) on a sunken track. */
export function Meter({ value, max, label, className }: { value: number; max: number; label: string; className?: string }) {
  const frac = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <div className={gxCx('gx-meter', className)} role="img" aria-label={label}>
      <span style={{ width: `${(frac * 100).toFixed(1)}%` }} />
    </div>
  );
}

/** Dollars with cents: 350 -> "$3.50". */
export function gxDollars(cents: number | bigint): string {
  const c = typeof cents === 'bigint' ? Number(cents) : cents;
  return `$${(c / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
