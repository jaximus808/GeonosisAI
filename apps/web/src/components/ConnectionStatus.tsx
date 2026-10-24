'use client';

import { useConnectionStatus } from '@/lib/spacetime/hooks';
import { SPACETIME_DB_NAME } from '@/lib/spacetime/config';

/** TDM-109: brand live indicator. Arc rivet when live, steel while connecting, slag when down. */
const TONE: Record<string, string> = {
  connected: '',
  connecting: 'is-connecting',
  disconnected: 'is-down',
};
const WORD: Record<string, string> = {
  connected: 'live',
  connecting: 'connecting',
  disconnected: 'offline',
};

export function ConnectionStatus() {
  const { status, identity, error } = useConnectionStatus();
  const title =
    status === 'connected'
      ? `Connected to ${SPACETIME_DB_NAME} as ${identity?.slice(0, 12)}…`
      : status === 'disconnected'
        ? `Disconnected: ${error ?? 'unknown error'} (retrying)`
        : `Connecting to ${SPACETIME_DB_NAME}…`;
  return (
    <span className={`gx-live ${TONE[status] ?? ''}`} title={title} data-status={status}>
      <i className={`gx-rivet ${status === 'connecting' ? 'animate-pulse' : ''}`} aria-hidden />
      <span className="hidden text-ink-muted sm:inline">{SPACETIME_DB_NAME} ·</span>
      {WORD[status] ?? status}
    </span>
  );
}
