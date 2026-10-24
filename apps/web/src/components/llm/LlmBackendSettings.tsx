'use client';

// /ops/settings "Model backend": a segmented switch (Claude Code (local) / Factory droid / Claude API)
// plus an optional model id. PUT /api/llm-backend (operator login required). The change takes effect
// on the orchestrator's next LLM call; calls already running finish on the old backend.

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { LLM_BACKEND_LABELS, LLM_BACKENDS, parseLlmBackendState, type LlmBackend } from '@/lib/llmbackend/types';
import { acceptLlmBackendState, useLlmBackend } from '@/lib/llmbackend/useLlmBackend';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';

const HINT: Record<LlmBackend, string> = {
  claude_code: 'Your local Claude Code (claude -p). Heavy local testing only.',
  droid: 'Factory droid exec on Factory credits. Some testing compute.',
  api: 'Anthropic API (claude-opus-5-5). The real demo and E2E runs; the only option in production.',
};

export function LlmBackendSettings() {
  const op = useOperator();
  const { state, error, loaded, hidden } = useLlmBackend();
  const [pick, setPick] = useState<LlmBackend | null>(null);
  const [model, setModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (state && pick === null) {
      setPick(state.backend);
      setModel(state.model ?? '');
    }
  }, [state, pick]);

  if (!loaded) return <p className="text-sm text-zinc-500">Loading the current backend…</p>;
  if (state?.locked) {
    return <p className="text-sm text-zinc-600 dark:text-zinc-400">The LLM backend is locked to the Claude API in this environment ({state.env}).</p>;
  }
  if (hidden || !state) {
    return (
      <p className="text-sm text-red-600">
        Could not read the orchestrator&apos;s LLM backend{error ? `: ${error}` : ''}. Is the orchestrator running with /admin/llm?
      </p>
    );
  }

  async function save() {
    if (!pick) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await fetch('/api/llm-backend', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ backend: pick, model: model.trim() || null }),
        cache: 'no-store',
      });
      const b = (await res.json().catch(() => ({}))) as Record<string, unknown>;
      const next = res.ok ? parseLlmBackendState(b) : null;
      if (next) {
        acceptLlmBackendState(next);
        setModel(next.model ?? '');
        setMsg({ ok: true, text: `Now ${LLM_BACKEND_LABELS[next.backend]} (${next.effective_model || 'default model'}). The next LLM call uses it.` });
      } else {
        setMsg({ ok: false, text: typeof b.detail === 'string' ? b.detail : `HTTP ${res.status}` });
      }
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const dirty = pick !== state.backend || (model.trim() || null) !== (state.model ?? null);
  return (
    <section className="space-y-4 rounded-lg border border-zinc-200 bg-white p-4 text-sm dark:border-zinc-800 dark:bg-zinc-900/60">
      <div>
        <h2 className="font-semibold">Model backend</h2>
        <p className="text-zinc-500">
          Now: <span className="font-medium text-zinc-800 dark:text-zinc-200" data-testid="llm-current">{LLM_BACKEND_LABELS[state.backend]}</span>
          {' · '}model <span className="font-mono">{state.effective_model || 'default'}</span>
          {' · '}set by {state.source}
          {state.updated_by ? ` (${state.updated_by}${state.updated_at ? `, ${new Date(state.updated_at).toLocaleString()}` : ''})` : ''}
          {' · '}env {state.env}
        </p>
      </div>
      <div role="radiogroup" aria-label="Model backend" className="inline-flex overflow-hidden rounded-md border border-zinc-300 dark:border-zinc-700">
        {LLM_BACKENDS.filter((b) => !state.allowed.length || state.allowed.includes(b)).map((b) => (
          <button
            key={b}
            type="button"
            role="radio"
            aria-checked={pick === b}
            onClick={() => setPick(b)}
            className={`border-r border-zinc-300 px-3 py-2 last:border-r-0 dark:border-zinc-700 ${
              pick === b ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : 'hover:bg-zinc-100 dark:hover:bg-zinc-800'
            }`}
          >
            {LLM_BACKEND_LABELS[b]}
          </button>
        ))}
      </div>
      {pick && <p className="text-xs text-zinc-500">{HINT[pick]}</p>}
      <label className="block max-w-sm">
        <span className="text-xs text-zinc-500">Model (optional; blank = the backend&apos;s default)</span>
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder={pick === 'claude_code' ? 'opus' : 'claude-opus-5-5'}
          className="mt-1 w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 font-mono dark:border-zinc-700"
        />
      </label>
      {op.operator === false ? (
        <p>
          <Link href={operatorLoginHref('/ops/settings')} className="underline">
            Log in as operator
          </Link>{' '}
          to change it.
        </p>
      ) : (
        <button
          type="button"
          onClick={() => void save()}
          disabled={busy || !dirty || !op.operator}
          className="rounded-md bg-zinc-900 px-3 py-2 font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {busy ? 'Switching…' : 'Switch'}
        </button>
      )}
      {msg && <p className={msg.ok ? 'text-emerald-700 dark:text-emerald-400' : 'text-red-600'}>{msg.text}</p>}
    </section>
  );
}
