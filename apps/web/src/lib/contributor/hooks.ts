'use client';

// TDM-60: contributor identity and presence for the web app.
//
// Identity is this browser's own SpacetimeDB identity: the provider connects
// anonymously the first time and keeps the server-issued token in localStorage
// (lib/spacetime/provider.tsx), so a refresh reconnects as the same identity and
// finds the same contributor row. Never the owner token.

import { useCallback, useEffect, useRef } from 'react';
import { useReducer, useSpacetimeDB } from 'spacetimedb/react';
import { reducers } from '@/module_bindings';
import { useRows, type RowOf } from '@/lib/spacetime/hooks';

export type Contributor = RowOf<'contributor'>;
export type ContributorKind = 'contributor' | 'dataset_seller';

export const PRESENCE_INTERVAL_MS = 30_000;
/** Server sweep marks a contributor offline after 75 s without a heartbeat. */
export const PRESENCE_STALE_MS = 75_000;

export interface RegisterInput {
  name: string; // "" = guest name from the identity
  email: string; // "" = none / keep the stored one
  devices: string[];
  kind: ContributorKind;
  sourceTicketId?: bigint;
}

/**
 * The contributor row bound to this browser's identity (live), plus register().
 *   const { contributor, ready, register } = useContributor();
 * `ready` is false until the connection and the contributor subscription are up.
 */
export function useContributor() {
  const { identity, isActive } = useSpacetimeDB();
  const [rows, rowsReady] = useRows('contributor');
  const registerReducer = useReducer(reducers.registerContributor);

  const contributor: Contributor | undefined = identity
    ? rows.find((c) => c.identity != null && c.identity.isEqual(identity))
    : undefined;

  const register = useCallback(
    (input: RegisterInput) =>
      registerReducer({
        name: input.name,
        email: input.email,
        devices: input.devices,
        kind: input.kind,
        sourceTicketId: input.sourceTicketId ?? 0n,
      }),
    [registerReducer],
  );

  return {
    contributor,
    identity: identity?.toHexString(),
    ready: isActive && rowsReady && identity != null,
    register,
  };
}

/**
 * Presence heartbeat while a marketplace page is open: one heartbeat on mount,
 * then every 30 s, and when the tab becomes visible again. On pagehide (tab
 * closed / navigated off the site) it tells the server we left. Client-side
 * navigation between pages does not send "left": the next page heartbeats,
 * and the server sweep covers anyone who disappears silently.
 * No-op until this browser has signed up at /join.
 */
export function useContributorPresence(page: string) {
  const { getConnection, isActive } = useSpacetimeDB();
  const { contributor } = useContributor();
  const contributorId = contributor?.id;
  const pageRef = useRef(page);
  pageRef.current = page;

  useEffect(() => {
    if (!isActive || contributorId == null) return;
    const beat = () => {
      const conn = getConnection();
      if (!conn) return;
      conn.reducers.contributorHeartbeat({ page: pageRef.current }).catch(() => {
        // transient (reconnecting); the next beat retries
      });
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') beat();
    };
    const onPageHide = () => {
      getConnection()?.reducers.contributorOffline({}).catch(() => {});
    };
    beat();
    const timer = window.setInterval(beat, PRESENCE_INTERVAL_MS);
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [isActive, contributorId, getConnection]);
}

/** Live presence rows keyed by contributor id. */
export function usePresenceById() {
  const [rows, ready] = useRows('contributorPresence');
  const map = new Map<bigint, RowOf<'contributorPresence'>>();
  for (const r of rows) map.set(r.contributorId, r);
  return [map, ready] as const;
}
