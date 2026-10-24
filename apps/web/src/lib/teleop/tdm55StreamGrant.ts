// TDM-55: get a camera-stream grant for the task this browser has claimed.
//
// Proves identity with this browser's own SpacetimeDB token (localStorage, see
// lib/spacetime/provider.tsx); the orchestrator checks it with SpacetimeDB and only answers
// for the contributor who currently holds the task's claim (services/orchestrator/.../stream_api.py).
// Goes through our Next server proxy (/api/tdm55/stream-token) because the orchestrator's CORS is GET-only.

import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';
import type { StreamGrant } from './tdm55CameraClient';

export class StreamGrantError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(`${status}: ${detail}`);
  }
}

export function taskStreamGrant(taskId: bigint | number | string, identityHex: string): () => Promise<StreamGrant> {
  return async () => {
    let token: string | null = null;
    try {
      token = window.localStorage.getItem(BROWSER_TOKEN_KEY);
    } catch {
      /* storage blocked */
    }
    if (!token) throw new StreamGrantError(401, 'no SpacetimeDB identity in this browser yet');
    const res = await fetch('/api/tdm55/stream-token', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ task_id: Number(taskId), identity: identityHex, spacetime_token: token }),
      cache: 'no-store',
    });
    const body = (await res.json().catch(() => ({}))) as StreamGrant & { detail?: string };
    if (!res.ok) throw new StreamGrantError(res.status, body.detail ?? res.statusText);
    return body;
  };
}

/** A fixed grant (dev page / ops token from `python -m rdf_common.stream_token mint`). */
export function staticStreamGrant(streamUrl: string, token: string, httpUrl?: string): () => Promise<StreamGrant> {
  return async () => {
    let exp = Date.now() / 1000 + 60;
    try {
      exp = JSON.parse(atob(token.split('.')[0].replace(/-/g, '+').replace(/_/g, '/'))).exp ?? exp;
    } catch {
      /* opaque token */
    }
    const http = httpUrl || streamUrl.replace(/^ws/, 'http').replace(/\/ws\/cameras$/, '');
    return { token, exp, stream_url: streamUrl, http_url: http };
  };
}
