'use client';

// TDM-108: which channel brought a contributor in. A job-drop link lands on
// /marketplace?task=N&src=<channel> (via the /j/N/<channel> short link). We remember src for this tab
// (sessionStorage, 2 h) and, when this browser claims a task, report it once to the orchestrator,
// which checks the browser's own SpacetimeDB identity against the task's assignee and logs
// job_drop.claimed {task_id, contributor_id, src}. Fire-and-forget: a failed report never blocks a claim.

import { useCallback, useEffect } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';

const TDM108_KEY = 'rdf.tdm108.src';
const TDM108_TTL_MS = 2 * 60 * 60 * 1000;
const TDM108_SRC = /^[a-z0-9_-]{1,24}$/;

type Stored = { src: string; task: string | null; at: number };

function readStored(): Stored | null {
  try {
    const s = JSON.parse(window.sessionStorage.getItem(TDM108_KEY) || 'null') as Stored | null;
    return s && TDM108_SRC.test(s.src) && Date.now() - s.at < TDM108_TTL_MS ? s : null;
  } catch {
    return null;
  }
}

/** Remember ?src= from the landing URL; returns a reporter to call after a successful claim. */
export function useTdm108Attribution(): (taskId: bigint) => void {
  const { identity } = useSpacetimeDB();
  const identityHex = identity?.toHexString();

  useEffect(() => {
    try {
      const q = new URLSearchParams(window.location.search);
      const src = q.get('src');
      if (src && TDM108_SRC.test(src) && src !== 'push') {
        const task = q.get('task');
        window.sessionStorage.setItem(
          TDM108_KEY,
          JSON.stringify({ src, task: task && /^\d{1,19}$/.test(task) ? task : null, at: Date.now() } satisfies Stored),
        );
      }
    } catch {
      // storage blocked: no attribution, nothing else changes
    }
  }, []);

  return useCallback(
    (taskId: bigint) => {
      const s = readStored();
      let token: string | null = null;
      try {
        token = window.localStorage.getItem(BROWSER_TOKEN_KEY);
      } catch {
        token = null;
      }
      if (!s || !identityHex || !token) return;
      void fetch('/api/tdm108/claim-src', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ task_id: Number(taskId), src: s.src, identity: identityHex, spacetime_token: token }),
        cache: 'no-store',
      }).catch(() => undefined);
    },
    [identityHex],
  );
}
