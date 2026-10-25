'use client';

// Browser side of the TDM-113 orchestrator routes (/api/tdm113/* -> /texts/*). Every contributor call carries this
// browser's own SpacetimeDB identity + token; the orchestrator verifies it with SpacetimeDB.

import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';

export function tdm113Token(): string | null {
  try {
    return window.localStorage.getItem(BROWSER_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function tdm113Caller(identityHex: string | undefined): { identity: string; spacetime_token: string } {
  const token = tdm113Token();
  if (!identityHex || !token) throw new Error('Not connected to the factory yet. Try again in a moment.');
  return { identity: identityHex, spacetime_token: token };
}

export async function tdm113Post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const res = await fetch(`/api/tdm113/${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `Request failed (${res.status})`);
  return j as T;
}

/** Phone sign-in: become the contributor of the other device (its SpacetimeDB token), then reconnect. */
export function tdm113AdoptToken(token: string): void {
  try {
    window.localStorage.setItem(BROWSER_TOKEN_KEY, token);
  } catch {
    throw new Error('This browser blocks site storage (private mode?), so it cannot stay signed in.');
  }
  window.location.reload();
}

const GEN_KEY = (task: bigint | string) => `rdf.tdm57.gen.${String(task)}`;

/** The claim generation (task.claimed_at, ms) this browser drove last for a task: tells "my claim" from
 *  "my claim on my other device" when both devices are the same contributor. */
export function tdm57StoredGen(task: bigint | string): number | undefined {
  try {
    const v = window.localStorage.getItem(GEN_KEY(task));
    return v ? Number(v) : undefined;
  } catch {
    return undefined;
  }
}

export function tdm57StoreGen(task: bigint | string, gen: number): void {
  try {
    window.localStorage.setItem(GEN_KEY(task), String(gen));
  } catch {
    /* storage blocked: takeover just runs again on the next open */
  }
}
