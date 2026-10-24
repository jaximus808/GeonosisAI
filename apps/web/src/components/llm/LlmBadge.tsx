// Names the LLM backend and model that produced a decision (honesty rule: a Claude Code or droid
// decision never reads as a Claude API one; a record that does not say shows "backend n/a").
// Presentational only; the logic lives in lib/llmbackend/types.ts (llmBadge).

import { llmBadge, type LlmBadgeTone } from '@/lib/llmbackend/types';

const TONE: Record<LlmBadgeTone, string> = {
  api: 'border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-800 dark:bg-sky-950/50 dark:text-sky-200',
  local: 'border-amber-400 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950/50 dark:text-amber-200',
  droid: 'border-violet-300 bg-violet-50 text-violet-900 dark:border-violet-800 dark:bg-violet-950/50 dark:text-violet-200',
  unknown: 'border-zinc-300 bg-zinc-50 text-zinc-600 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400',
};

export function LlmBadge({
  rec,
  prefix,
  className = '',
}: {
  rec: { backend?: unknown; model?: unknown; served?: unknown; requested?: unknown } | null | undefined;
  prefix?: string;
  className?: string;
}) {
  const b = llmBadge(rec);
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 align-middle text-[11px] font-medium leading-none ${TONE[b.tone]} ${className}`}
      data-llm-backend={b.backend ?? 'unknown'}
      title={b.backend ? `Produced by the ${b.label} backend${b.model ? `, model ${b.model}` : ''}` : 'This record does not say which LLM backend produced it'}
    >
      {prefix ? <span className="opacity-70">{prefix}</span> : null}
      {b.text}
    </span>
  );
}
