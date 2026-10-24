'use client';

// Is this browser logged in as an operator? The session cookie is HttpOnly, so ask the server
// (GET /api/ops/session). One request is shared by every component on the page.

import { useCallback, useEffect, useState } from 'react';

export interface OperatorState {
  /** null while the first check is in flight. */
  operator: boolean | null;
  configured: boolean;
  expiresAt: number | null;
}

let inflight: Promise<OperatorState> | null = null;
const listeners = new Set<(s: OperatorState) => void>();

function check(): Promise<OperatorState> {
  inflight ??= fetch('/api/ops/session', { cache: 'no-store' })
    .then((r) => r.json())
    .then((b: { operator?: boolean; configured?: boolean; expiresAt?: number | null }) => ({
      operator: b.operator === true,
      configured: b.configured === true,
      expiresAt: b.expiresAt ?? null,
    }))
    .catch(() => ({ operator: false, configured: true, expiresAt: null }))
    .then((s) => {
      listeners.forEach((l) => l(s));
      return s;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

export function useOperator(): OperatorState & { recheck: () => void } {
  const [s, setS] = useState<OperatorState>({ operator: null, configured: true, expiresAt: null });
  useEffect(() => {
    listeners.add(setS);
    void check();
    return () => {
      listeners.delete(setS);
    };
  }, []);
  const recheck = useCallback(() => void check(), []);
  return { ...s, recheck };
}

/** /ops/login link that comes back to the current page. */
export function operatorLoginHref(next: string): string {
  return `/ops/login?next=${encodeURIComponent(next)}`;
}
