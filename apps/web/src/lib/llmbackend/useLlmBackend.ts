'use client';

// Live LLM backend state from GET /api/llm-backend: one shared poll (every 5 s) for every component
// on the page. `hidden` is true in production (build-time NEXT_PUBLIC_RDF_ENV, or the server says
// locked) and when the orchestrator has no /admin/llm route: the controls then render nothing.

import { useCallback, useEffect, useState } from 'react';
import { isProductionEnv, parseLlmBackendState, type LlmBackendState } from './types';

export const LLM_POLL_MS = 5_000;

export interface LlmBackendView {
  state: LlmBackendState | null;
  /** null until the first answer. */
  error: string | null;
  loaded: boolean;
  hidden: boolean;
}

const BUILD_PRODUCTION = isProductionEnv({ NEXT_PUBLIC_RDF_ENV: process.env.NEXT_PUBLIC_RDF_ENV });

let current: LlmBackendView = { state: null, error: null, loaded: false, hidden: BUILD_PRODUCTION };
const listeners = new Set<(v: LlmBackendView) => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let inflight: Promise<void> | null = null;

function publish(v: LlmBackendView) {
  current = v;
  listeners.forEach((l) => l(v));
}

export function refreshLlmBackend(): Promise<void> {
  if (BUILD_PRODUCTION) return Promise.resolve();
  inflight ??= fetch('/api/llm-backend', { cache: 'no-store' })
    .then(async (r) => {
      const b = (await r.json().catch(() => ({}))) as Record<string, unknown>;
      const state = r.ok ? parseLlmBackendState(b) : null;
      if (state) publish({ state, error: null, loaded: true, hidden: state.locked });
      else
        publish({
          state: null,
          error: typeof b.detail === 'string' ? b.detail : `HTTP ${r.status}`,
          loaded: true,
          hidden: true,
        });
    })
    .catch((e: Error) => publish({ state: null, error: e.message, loaded: true, hidden: true }))
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** Put a state the PUT returned straight into every subscriber (no wait for the next poll). */
export function acceptLlmBackendState(state: LlmBackendState) {
  publish({ state, error: null, loaded: true, hidden: state.locked });
}

export function useLlmBackend(): LlmBackendView & { refresh: () => void } {
  const [v, setV] = useState<LlmBackendView>(current);
  useEffect(() => {
    if (BUILD_PRODUCTION) return;
    listeners.add(setV);
    setV(current);
    void refreshLlmBackend();
    if (!timer) timer = setInterval(() => void refreshLlmBackend(), LLM_POLL_MS);
    return () => {
      listeners.delete(setV);
      if (!listeners.size && timer) {
        clearInterval(timer);
        timer = null;
      }
    };
  }, []);
  const refresh = useCallback(() => void refreshLlmBackend(), []);
  return { ...v, refresh };
}
