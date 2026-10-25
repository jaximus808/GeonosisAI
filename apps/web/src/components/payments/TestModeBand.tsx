'use client';

// TDM-64: the one discreet Stripe test-mode marker, a small muted chip next to the earnings balance.
// The title comes from the orchestrator's GET /payments/mode, with a fixed fallback.

import { useTestModeBadge } from '@/lib/payments/tdm64';

/** Small muted chip next to a balance. */
export function TestModeTag() {
  const { text } = useTestModeBadge();
  return (
    <span
      title={text}
      data-testid="test-mode-chip"
      className="inline-flex items-center rounded-sm border border-zinc-300 px-1.5 py-px align-middle text-[11px] font-medium text-zinc-500 dark:border-zinc-700 dark:text-zinc-400"
    >
      Test mode
    </span>
  );
}
