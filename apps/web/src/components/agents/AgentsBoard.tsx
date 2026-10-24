'use client';

// TDM-76: the agents panel. Used full-page on /agents and embedded on the ticket page.

import { useEffect, useState } from 'react';
import { MockedBadge } from '@/components/MockedBadge';
import { useAgentFeed, useLiveFeedSource, useNow, useReplayFeedSource } from '@/lib/tdm76/feed';
import { fmtInt } from './bits';
import { AgentCard } from './AgentCard';
import { OrchestratorAgents } from './OrchestratorAgents';

const WAKE_KEYFRAMES = `@keyframes tdm76-wake{0%{box-shadow:0 0 0 0 rgba(124,58,237,.55);transform:scale(.985)}35%{transform:scale(1)}100%{box-shadow:0 0 0 14px rgba(124,58,237,0)}}`;

export function AgentsBoard({ ticketId, replayFile, variant = 'page' }: { ticketId?: number; replayFile?: string; variant?: 'page' | 'ticket' }) {
  const live = useLiveFeedSource();
  const replay = useReplayFeedSource(replayFile);
  const source = replayFile ? replay : live;
  const feed = useAgentFeed(source, { ticketId });
  const nowMs = useNow(1000);
  const [showTests, setShowTests] = useState(false);

  const droids = feed.agents.filter((a) => a.isDroid);
  const otherSessions = feed.agents.filter((a) => !a.isDroid);
  const visible = (xs: typeof droids) => xs.filter((a) => showTests || !a.isTest);
  const testCount = feed.agents.filter((a) => a.isTest).length;
  const running = droids.filter((a) => ['running', 'starting', 'resuming'].includes(a.session.status) && !a.isTest).length;
  const waiting = droids.filter((a) => a.session.status === 'waiting_for' && !a.isTest).length;
  const finished = droids.filter((a) => ['done', 'failed'].includes(a.session.status) && !a.isTest).length;
  const wakes = droids.reduce((n, a) => n + a.resumes.filter((r) => r.bySpacetime).length, 0);
  const credits = droids.reduce((n, a) => n + (a.credits ?? 0), 0);
  const isLive = feed.mode === 'live';
  const firstOpen = visible(droids).find((a) => !['done', 'failed'].includes(a.session.status)) ?? visible(droids)[0];

  return (
    <div>
      <style>{WAKE_KEYFRAMES}</style>
      {feed.mode === 'replay' && feed.replay && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md border border-amber-400/60 bg-amber-50/70 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <MockedBadge label="Replay" title="Recorded droid events played back at 4x. Real events from an earlier run, not live." />
          <span>
            Playing back <code className="font-mono text-xs">{feed.replay.file}</code>, a recorded droid run, at {feed.replay.speed}× speed ({feed.replay.shown}/{feed.replay.total} events).
            Nothing here is live.
          </span>
          <a href={variant === 'page' ? '/agents' : '?'} className="ml-auto font-medium underline underline-offset-2">
            Back to live
          </a>
        </div>
      )}

      {feed.ready && (
        <p className="mb-5 max-w-3xl text-[15px] leading-relaxed text-zinc-600 dark:text-zinc-400">
          {droids.length === 0 ? (
            ticketId !== undefined ? 'No Factory Droid has worked on this ticket yet.' : 'No Factory Droid sessions yet.'
          ) : (
            <>
              <span className="font-medium text-zinc-900 dark:text-zinc-100">{headline(running, waiting, finished)}</span>
              {wakes > 0 && (
                <>
                  , <span className="font-medium text-violet-700 dark:text-violet-300">{wakes} {wakes === 1 ? 'wake-up' : 'wake-ups'} from SpacetimeDB</span>
                </>
              )}
              {credits > 0 && <>, {fmtInt(credits)} Factory credits so far</>}.
            </>
          )}
        </p>
      )}

      {!feed.ready ? (
        <p className="text-sm text-zinc-500">Subscribing to agent sessions…</p>
      ) : (
        <>
          <section className="mb-10" aria-labelledby="tdm76-droids">
            <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 id="tdm76-droids" className={variant === 'page' ? 'text-lg font-semibold tracking-tight' : 'text-base font-semibold'}>
                Factory Droids
              </h2>
              <span className="text-sm text-zinc-500">long-running engineering agents; they park on a database condition and wake when it comes true</span>
              {testCount > 0 && (
                <button type="button" onClick={() => setShowTests((v) => !v)} className="ml-auto text-xs text-zinc-500 underline underline-offset-2 hover:text-zinc-800 dark:hover:text-zinc-200">
                  {showTests ? 'Hide' : 'Show'} {testCount} test {testCount === 1 ? 'session' : 'sessions'}
                </button>
              )}
            </div>
            {visible(droids).length === 0 ? (
              <EmptyDroids replayable={variant === 'page' && isLive} />
            ) : (
              <div className="space-y-4">
                {visible(droids).map((a) => (
                  <AgentCard key={a.session.id} a={a} nowMs={nowMs} live={isLive} defaultOpen={a === firstOpen} />
                ))}
              </div>
            )}
          </section>

          <section aria-labelledby="tdm76-orch">
            <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 id="tdm76-orch" className={variant === 'page' ? 'text-lg font-semibold tracking-tight' : 'text-base font-semibold'}>
                Orchestrator agents
              </h2>
              <span className="text-sm text-zinc-500">run inside our backend; shown from the event log rows they write</span>
            </div>
            {visible(otherSessions).length > 0 && (
              <div className="mb-4 space-y-4">
                {visible(otherSessions).map((a) => (
                  <AgentCard key={a.session.id} a={a} nowMs={nowMs} live={isLive} defaultOpen={false} />
                ))}
              </div>
            )}
            {isLive ? (
              <OrchestratorAgents agents={feed.orchestrator} nowMs={nowMs} />
            ) : (
              <p className="text-sm text-zinc-500">Not part of the recording.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function headline(running: number, waiting: number, finished: number): string {
  const parts: string[] = [];
  if (running) parts.push(`${running} running`);
  if (waiting) parts.push(`${waiting} parked until the physical world catches up`);
  if (finished) parts.push(`${finished} finished`);
  return parts.length ? parts.join(', ') : 'No droid is active';
}

function EmptyDroids({ replayable }: { replayable: boolean }) {
  return (
    <div className="rounded-lg border border-dashed border-zinc-300 px-4 py-4 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
      <p>A droid shows up here the moment the orchestrator starts one, and its steps stream in as it works.</p>
      {replayable && <RecordingLinks className="mt-2" lead="To see one now, replay a recorded run:" />}
    </div>
  );
}

/** Links to recorded droid runs (services/factory/work/state/*.events.jsonl) for replay mode. */
export function RecordingLinks({ className = '', lead }: { className?: string; lead: string }) {
  const [files, setFiles] = useState<string[]>([]);
  useEffect(() => {
    fetch('/api/tdm76/recording')
      .then((r) => (r.ok ? r.json() : { files: [] }))
      .then((d: { files?: string[] }) => setFiles(d.files ?? []))
      .catch(() => setFiles([]));
  }, []);
  if (files.length === 0) return null;
  return (
    <p className={`text-sm text-zinc-500 ${className}`}>
      {lead}{' '}
      {files.map((f) => (
        <a key={f} href={`/agents?replay=${encodeURIComponent(f)}`} className="mr-2 font-mono text-xs text-cyan-700 underline underline-offset-2 dark:text-cyan-400">
          {f}
        </a>
      ))}
    </p>
  );
}
