'use client';

// TDM-76: one agent_session (a Factory Droid, or an orchestrator agent with a session row).

import { useState } from 'react';
import type { AgentView } from '@/lib/tdm76/feed';
import { PredicateLine, ROLE_DOES, ROLE_LABEL, Rail, StatusChip, WakeGlyph, ago, duration, fmtInt, shortId } from './bits';
import { TurnTimeline } from './TurnTimeline';

export function AgentCard({ a, nowMs, live, defaultOpen }: { a: AgentView; nowMs: number; live: boolean; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const s = a.session;
  const role = ROLE_LABEL[s.role] ?? s.role;
  const finished = s.status === 'done' || s.status === 'failed';
  const elapsedS = ((finished ? s.lastUpdateUs : nowMs * 1000) - s.createdUs) / 1e6;
  const spacetimeWakes = a.resumes.filter((r) => r.bySpacetime);
  const justWoken = a.lastResume?.bySpacetime && nowMs / 1000 - a.lastResume.tsUs / 1e6 < 90;
  const panelId = `tdm76-agent-${s.id}`;

  return (
    <article
      className={`relative overflow-hidden rounded-lg border bg-white pl-1.5 shadow-[0_1px_0_rgba(24,24,27,0.04)] dark:bg-zinc-900/60 ${
        s.status === 'resuming' || justWoken ? 'border-violet-400/70 dark:border-violet-500/60' : 'border-zinc-200 dark:border-zinc-800'
      } ${a.isTest ? 'opacity-70' : ''}`}
      data-agent-session={s.id}
      data-status={s.status}
    >
      <Rail status={s.status} />
      <div className="px-4 pb-3 pt-3.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <h3 className="text-base font-semibold tracking-tight">{role}</h3>
          {s.name && s.name !== s.role && <span className="text-sm text-zinc-500">{s.name}</span>}
          <span className="text-xs tabular-nums text-zinc-400">session #{s.id}</span>
          {s.ticketId > 0 && (
            <a href={`/tickets/${s.ticketId}`} className="text-xs text-zinc-500 underline-offset-2 hover:text-zinc-900 hover:underline dark:hover:text-zinc-100">
              ticket {s.ticketId}
            </a>
          )}
          {a.isTest && <span className="rounded-sm border border-zinc-400 px-1 text-[10px] font-semibold text-zinc-500">TEST</span>}
          <span className="ml-auto flex flex-wrap items-center gap-2">
            {s.status === 'resuming' && (
              <span className="inline-flex items-center gap-1 rounded-full bg-violet-600 px-2 py-0.5 text-xs font-semibold text-white motion-safe:animate-pulse">
                <WakeGlyph className="h-3.5 w-3.5" /> Waking up
              </span>
            )}
            {s.status !== 'resuming' && spacetimeWakes.length > 0 && (
              <span
                title={spacetimeWakes[spacetimeWakes.length - 1].reason}
                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${
                  justWoken ? 'bg-violet-600 text-white' : 'bg-violet-100 text-violet-800 dark:bg-violet-950/70 dark:text-violet-200'
                }`}
              >
                <WakeGlyph className="h-3.5 w-3.5" />
                {justWoken ? 'Just woken by SpacetimeDB' : `Woken by SpacetimeDB${spacetimeWakes.length > 1 ? ` ×${spacetimeWakes.length}` : ''}`}
              </span>
            )}
            <StatusChip status={s.status} />
          </span>
        </div>
        {ROLE_DOES[s.role] && <p className="mt-0.5 text-sm text-zinc-500">{ROLE_DOES[s.role]}</p>}

        {s.status === 'waiting_for' && (
          <p className="mt-3 text-[15px] font-medium text-amber-900 dark:text-amber-200">
            <PredicateLine predicate={s.waitingFor} raw={s.waitingForRaw} live={live} />
          </p>
        )}
        {s.status === 'paused' && (
          <p className="mt-3 text-sm text-zinc-600 dark:text-zinc-400">Its last turn ended without DONE or WAITING_FOR, so it is idle until it gets a message.</p>
        )}

        {s.status !== 'waiting_for' && <p className="mt-2 flex items-baseline gap-2 text-sm">
          <span className="shrink-0 text-zinc-500">{finished ? 'Last step' : 'Now'}</span>
          <span className="min-w-0 truncate text-zinc-900 dark:text-zinc-100" title={s.currentStep}>
            {s.currentStep || '…'}
          </span>
        </p>}

        <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-zinc-500">
          <div className="flex gap-1.5">
            <dt>{finished ? 'Ran for' : s.status === 'waiting_for' || s.status === 'paused' ? 'Session age' : 'Running for'}</dt>
            <dd className="tabular-nums text-zinc-700 dark:text-zinc-300">{duration(elapsedS)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt>Updated</dt>
            <dd className="tabular-nums text-zinc-700 dark:text-zinc-300">{ago(s.lastUpdateUs, nowMs)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt>Turns</dt>
            <dd className="tabular-nums text-zinc-700 dark:text-zinc-300">{a.turns.length}</dd>
          </div>
          {a.credits !== undefined && (
            <div className="flex gap-1.5">
              <dt>Factory credits</dt>
              <dd className="tabular-nums text-zinc-700 dark:text-zinc-300">{fmtInt(a.credits)}</dd>
            </div>
          )}
        </dl>

        {a.factorySessions.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-zinc-500">Factory {a.factorySessions.length === 1 ? 'session' : 'sessions'}</span>
            {a.factorySessions.map((f) => (
              <span
                key={f.id}
                title={f.id}
                className={`inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 ${
                  f.restart ? 'border-violet-300 dark:border-violet-700' : 'border-zinc-200 dark:border-zinc-700'
                }`}
              >
                <span className="font-mono text-zinc-800 dark:text-zinc-200">{shortId(f.id)}</span>
                <span className="text-zinc-500">
                  {f.restart ? 'Protocol B restart' : f.protocols.includes('native_resume') ? 'Protocol A, resumed in place' : 'first session'}
                  {f.turns.length > 0 && `, turn${f.turns.length > 1 ? 's' : ''} ${f.turns[0]}${f.turns.length > 1 ? `–${f.turns[f.turns.length - 1]}` : ''}`}
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 border-t border-zinc-100 px-4 py-2 text-left text-sm text-zinc-600 hover:bg-zinc-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-cyan-600 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800/50"
      >
        <span className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}>›</span>
        {open ? 'Hide' : 'Show'} what it did ({a.stepCount} steps)
      </button>
      {open && (
        <div id={panelId} className="border-t border-zinc-100 bg-zinc-50/60 dark:border-zinc-800 dark:bg-zinc-950/40">
          <TurnTimeline turns={a.turns} live={live} nowMs={nowMs} />
        </div>
      )}
    </article>
  );
}
