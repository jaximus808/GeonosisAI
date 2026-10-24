'use client';

// Header chip: the LLM backend the orchestrator's next Claude call will use, live (5 s poll).
// Renders nothing in production or when the orchestrator has no /admin/llm route. Links to
// /ops/settings, where an operator switches it.

import Link from 'next/link';
import { LLM_BACKEND_LABELS } from '@/lib/llmbackend/types';
import { useLlmBackend } from '@/lib/llmbackend/useLlmBackend';

const DOT = { api: 'bg-sky-500', droid: 'bg-violet-500', claude_code: 'bg-amber-500' } as const;

export function LlmBackendChip() {
  const { state, hidden } = useLlmBackend();
  if (hidden || !state) return null;
  const label = LLM_BACKEND_LABELS[state.backend];
  return (
    <Link
      href="/ops/settings"
      className="inline-flex items-center gap-1.5 rounded-full border border-zinc-200 px-2 py-0.5 text-xs text-zinc-600 hover:border-zinc-400 dark:border-zinc-700 dark:text-zinc-400"
      title={`Model backend: ${label}, model ${state.effective_model || 'default'} (${state.source}). Click to change.`}
      data-llm-backend={state.backend}
    >
      <span className={`h-2 w-2 rounded-full ${DOT[state.backend]}`} />
      <span className="hidden text-zinc-400 sm:inline">LLM</span>
      {label}
    </Link>
  );
}
