'use client';

// TDM-76: the orchestrator's own agents (clustering, planner, reviewer, QA, dispatcher, eval,
// flywheel). They run inside the orchestrator with no Factory session; TDM-35 derives them from the
// event_log rows they write (status: active if they logged in the last 5 min).

import { useState } from 'react';
import type { OrchAgentView } from '@/lib/tdm76/feed';
import { ago } from './bits';

const STATUS_TEXT: Record<string, string> = { active: 'active', idle: 'idle', error: 'last action failed', none: 'no activity yet' };

export function OrchestratorAgents({ agents, nowMs }: { agents: OrchAgentView[]; nowMs: number }) {
  const [open, setOpen] = useState<string | undefined>(undefined);
  return (
    <ul className="divide-y divide-zinc-200 overflow-hidden rounded-lg border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900/60">
      {agents.map((a) => {
        const active = a.status === 'active';
        const failed = a.status === 'error';
        const isOpen = open === a.key;
        return (
          <li key={a.key} className="relative pl-1.5">
            <span
              aria-hidden
              className="absolute inset-y-0 left-0 w-1.5"
              style={{
                background: a.last
                  ? `repeating-linear-gradient(-45deg, ${failed ? '#dc2626' : active ? '#0891b2' : '#a1a1aa'} 0 4px, transparent 4px 8px)`
                  : 'transparent',
              }}
            />
            <button
              type="button"
              disabled={a.count === 0}
              aria-expanded={isOpen}
              onClick={() => setOpen(isOpen ? undefined : a.key)}
              className="grid w-full grid-cols-1 items-baseline gap-x-6 gap-y-0.5 px-4 py-2.5 text-left enabled:hover:bg-zinc-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-cyan-600 sm:grid-cols-[15rem_1fr_auto] dark:enabled:hover:bg-zinc-800/50"
            >
              <span>
                <span className="font-medium">{a.label}</span>
                <span className={`ml-2 whitespace-nowrap text-xs ${failed ? 'text-red-600 dark:text-red-400' : active ? 'text-cyan-700 dark:text-cyan-400' : 'text-zinc-400'}`}>
                  {STATUS_TEXT[a.status] ?? a.status}
                </span>
              </span>
              <span className="min-w-0 truncate text-sm text-zinc-600 dark:text-zinc-400">
                {a.last ? a.last.text : a.does}
              </span>
              <span className="text-xs tabular-nums text-zinc-500">
                {a.last ? `${a.count} ${a.count === 1 ? 'action' : 'actions'}, last ${ago(a.last.tsUs, nowMs)}` : ''}
              </span>
            </button>
            {isOpen && (
              <ol className="max-h-72 overflow-y-auto border-t border-zinc-100 bg-zinc-50/60 px-4 py-2 text-xs dark:border-zinc-800 dark:bg-zinc-950/40">
                {[...a.recent].reverse().map((e) => (
                  <li key={e.key} className="flex gap-3 py-0.5">
                    <span className="w-20 shrink-0 tabular-nums text-zinc-400">{ago(e.tsUs, nowMs)}</span>
                    <span className="w-44 shrink-0 font-mono text-zinc-700 dark:text-zinc-300">{e.type}</span>
                    <span className={`min-w-0 truncate ${e.isError ? 'text-red-700 dark:text-red-400' : 'text-zinc-600 dark:text-zinc-400'}`}>{e.text}</span>
                  </li>
                ))}
              </ol>
            )}
          </li>
        );
      })}
    </ul>
  );
}
