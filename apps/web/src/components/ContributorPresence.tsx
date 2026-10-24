'use client';

import Link from 'next/link';
import { useContributor, useContributorPresence } from '@/lib/contributor/hooks';

/**
 * TDM-60: mount on every marketplace page (tasks, teleop, reset). Sends the
 * presence heartbeat and shows who this browser is, or a join link.
 */
export function ContributorPresence({ page }: { page: string }) {
  useContributorPresence(page);
  const { contributor, ready } = useContributor();
  if (!ready) return null;
  if (!contributor) {
    return (
      <p className="mb-4 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-200">
        You are browsing as a guest. <Link href="/join" className="font-medium underline">Join as a contributor</Link> to claim paid tasks.
      </p>
    );
  }
  return (
    <p className="mb-4 flex items-center gap-2 text-sm text-zinc-600 dark:text-zinc-400">
      <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
      Signed in as <span className="font-medium text-zinc-900 dark:text-zinc-100">{contributor.name}</span>
      <span className="font-mono text-xs text-zinc-400">#{String(contributor.id)}</span>
    </p>
  );
}
