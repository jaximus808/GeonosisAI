'use client';

import { useMemo, type ReactNode } from 'react';
import { SpacetimeDBProvider } from 'spacetimedb/react';
import { DbConnection } from '@/module_bindings';
import { BROWSER_TOKEN_KEY, SPACETIME_DB_NAME, SPACETIME_HOST } from './config';

function readToken(): string | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    return window.localStorage.getItem(BROWSER_TOKEN_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

function writeToken(token: string | undefined) {
  try {
    if (token) window.localStorage.setItem(BROWSER_TOKEN_KEY, token);
    else window.localStorage.removeItem(BROWSER_TOKEN_KEY);
  } catch {
    // storage blocked (private mode): stay anonymous per page load
  }
}

/**
 * Live SpacetimeDB connection for the whole app (read-only from the browser's side:
 * the UI subscribes to public tables and does not call privileged reducers).
 * Identity: anonymous on first visit; the server-issued token is kept in localStorage
 * so the browser keeps the same identity across reloads.
 */
export function SpacetimeProvider({ children }: { children: ReactNode }) {
  const builder = useMemo(
    () =>
      DbConnection.builder()
        .withUri(SPACETIME_HOST)
        .withDatabaseName(SPACETIME_DB_NAME)
        .withToken(readToken())
        .onConnect((_conn, _identity, token) => writeToken(token))
        .onConnectError(() => writeToken(undefined)),
    [],
  );
  return <SpacetimeDBProvider connectionBuilder={builder}>{children}</SpacetimeDBProvider>;
}
