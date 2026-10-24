'use client';

// TDM-64: the Stripe test-mode marker. Shown wherever an amount is shown, never hidden.
// The text comes from the orchestrator's GET /payments/mode, with a fixed fallback.

import { useTestModeBadge } from '@/lib/payments/tdm64';

const STRIPES =
  'repeating-linear-gradient(-45deg, #f5b301 0 10px, #18181b 10px 20px)';

/** Full-width band for the top of a money page. */
export function TestModeBand({ detail }: { detail?: string }) {
  const { text } = useTestModeBadge();
  return (
    <div
      role="note"
      aria-label={text}
      className="flex items-stretch overflow-hidden rounded-md border border-amber-500/70 bg-amber-50 text-amber-950 dark:border-amber-500/50 dark:bg-amber-950/30 dark:text-amber-100"
    >
      <div aria-hidden className="w-6 shrink-0 sm:w-10" style={{ backgroundImage: STRIPES }} />
      <p className="px-3 py-2 text-sm">
        <span className="font-semibold">{text}.</span>{' '}
        {detail ?? 'Amounts are Stripe test transfers between test accounts. Nobody is paid real money.'}
      </p>
    </div>
  );
}

/** Small inline marker next to a figure. */
export function TestModeTag() {
  const { text } = useTestModeBadge();
  return (
    <span
      title={text}
      className="inline-flex items-center gap-1 rounded-sm border border-amber-600/60 bg-amber-50 px-1.5 py-px text-[11px] font-semibold text-amber-900 dark:bg-amber-950/40 dark:text-amber-200"
    >
      <span aria-hidden className="h-2 w-2 rounded-[1px]" style={{ backgroundImage: 'repeating-linear-gradient(-45deg, #f5b301 0 2px, #18181b 2px 4px)' }} />
      Test mode
    </span>
  );
}
