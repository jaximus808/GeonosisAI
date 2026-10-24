'use client';

import { useConnectionStatus } from '@/lib/spacetime/hooks';
import { SPACETIME_DB_NAME } from '@/lib/spacetime/config';

const DOT: Record<string, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-400 animate-pulse',
  disconnected: 'bg-red-500',
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
    <span className="inline-flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400" title={title} data-status={status}>
      <span className={`h-2 w-2 rounded-full ${DOT[status]}`} />
      {status}
      <span className="hidden font-mono text-zinc-400 sm:inline">{SPACETIME_DB_NAME}</span>
    </span>
  );
}
