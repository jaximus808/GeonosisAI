'use client';

// TDM-76: one droid's turns, newest at the bottom: tool calls with their results, what the droid
// said, where it parked, and the moment a SpacetimeDB update woke it.

import { useEffect, useRef } from 'react';
import type { ResumeMark, StepItem, Turn } from '@/lib/tdm76/feed';
import { predicateWords, toNode } from '@/lib/tdm76/predicate';
import { PredicateLine, WakeGlyph, duration, fmtInt, shortId } from './bits';

const PROTOCOL: Record<string, string> = {
  start: 'Started from the brief',
  send: 'Message from the orchestrator',
  native_resume: 'Resumed in the same Factory session',
  restart: 'Restarted in a new Factory session',
};

const ENDED: Record<string, string> = {
  waiting_for: 'parked',
  done: 'finished',
  paused: 'stopped without a status line',
  failed: 'failed',
};

export function TurnTimeline({ turns, live, maxSteps = 200, nowMs }: { turns: Turn[]; live: boolean; maxSteps?: number; nowMs: number }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const total = turns.reduce((n, t) => n + t.steps.length, 0);

  // Follow new steps unless the reader scrolled up to look at something.
  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [total, turns.length]);

  if (turns.length === 0) return <p className="px-4 py-3 text-sm text-zinc-500">No steps logged yet. They stream in here as the droid works.</p>;

  // Drop the oldest steps past maxSteps, keeping turn headers.
  let budget = maxSteps;
  const shown = [...turns].reverse().map((t) => {
    const keep = Math.max(0, Math.min(t.steps.length, budget));
    budget -= keep;
    return { ...t, steps: t.steps.slice(t.steps.length - keep), hidden: t.steps.length - keep };
  }).reverse();

  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="max-h-[32rem] overflow-y-auto overscroll-contain"
    >
      <ol className="relative">
        {shown.map((t, i) => (
          <li key={t.n}>
            {(t.protocol === 'native_resume' || t.protocol === 'restart') && <ResumeBand turn={t} mark={t.resume} nowMs={nowMs} />}
            {/* Live counts only make sense for the wait the droid is in now, not for past waits. */}
            <TurnBlock turn={t} live={live && i === shown.length - 1 && t.completed?.status === 'waiting_for'} hidden={t.hidden} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function TurnBlock({ turn, live, hidden }: { turn: Turn; live: boolean; hidden: number }) {
  const c = turn.completed;
  return (
    <section className="border-t border-zinc-200/80 px-4 py-3 first:border-t-0 dark:border-zinc-800">
      <header className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
        <h4 className="font-semibold">Turn {turn.n}</h4>
        <span className="text-zinc-600 dark:text-zinc-400">{PROTOCOL[turn.protocol] ?? turn.protocol}</span>
        {turn.factorySessionId && (
          <span className="font-mono text-xs text-zinc-500" title={turn.factorySessionId}>
            session {shortId(turn.factorySessionId)}
          </span>
        )}
        <span className="ml-auto flex gap-3 text-xs tabular-nums text-zinc-500">
          {c ? (
            <>
              {c.seconds !== undefined && Number.isFinite(c.seconds) && <span>{duration(c.seconds)}</span>}
              {c.credits !== undefined && <span>{fmtInt(c.credits)} credits</span>}
              <span className={c.status === 'failed' ? 'text-red-600' : ''}>{ENDED[c.status] ?? c.status}</span>
            </>
          ) : (
            <span className="text-cyan-700 dark:text-cyan-400">working</span>
          )}
        </span>
      </header>

      {turn.prompt && <Prompt turn={turn} />}
      {hidden > 0 && <p className="mb-1 pl-6 text-xs text-zinc-400">{hidden} earlier steps not shown</p>}

      <ul className="space-y-1">
        {turn.steps.map((s) => (
          <Step key={s.key} s={s} live={live} />
        ))}
      </ul>
      {c?.error && <p className="mt-2 rounded bg-red-50 px-2 py-1 text-xs text-red-800 dark:bg-red-950/40 dark:text-red-300">{c.error}</p>}
    </section>
  );
}

function Prompt({ turn }: { turn: Turn }) {
  const first = turn.summary || turn.prompt!.split('\n').find((l) => l.trim()) || '';
  return (
    <details className="group mb-2 ml-6 text-xs">
      <summary className="cursor-pointer list-none text-zinc-500 marker:hidden hover:text-zinc-800 dark:hover:text-zinc-200">
        <span className="mr-1 inline-block transition-transform group-open:rotate-90">›</span>
        {turn.protocol === 'start' ? 'Brief' : 'Message'}: <span className="text-zinc-700 dark:text-zinc-300">{first.slice(0, 160)}</span>
      </summary>
      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-zinc-100 p-2 font-mono text-[11px] leading-relaxed text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">{turn.prompt}</pre>
    </details>
  );
}

/** A droid's own workdir is noise in a path: show it as workdir/. */
function shortenPaths(t: string): string {
  return t.replace(/\S*\/sessions\/\d+\//g, 'workdir/');
}

const TOOL_TINT = 'bg-zinc-900 text-zinc-50 dark:bg-zinc-100 dark:text-zinc-900';

function Step({ s, live }: { s: StepItem; live: boolean }) {
  if (s.kind === 'tool_call') {
    const rest = shortenPaths(s.tool && s.text.startsWith(s.tool) ? s.text.slice(s.tool.length).trim() : s.text);
    return (
      <li className="flex items-start gap-2 text-sm">
        <span className="mt-0.5 w-4 shrink-0 text-center text-zinc-400" aria-hidden>
          ▸
        </span>
        <span className={`mt-px shrink-0 rounded px-1.5 py-px font-mono text-[11px] leading-5 ${TOOL_TINT}`}>{s.tool ?? 'tool'}</span>
        <span className="min-w-0 flex-1 break-words font-mono text-[12.5px] leading-6 text-zinc-700 dark:text-zinc-300">{rest}</span>
        {s.result ? (
          <span
            title={s.result.preview ?? s.result.text}
            className={`mt-0.5 shrink-0 text-xs ${s.result.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}`}
          >
            {s.result.ok ? 'ok' : 'error'}
          </span>
        ) : (
          <span className="mt-0.5 shrink-0 text-xs text-zinc-400">…</span>
        )}
      </li>
    );
  }
  if (s.kind === 'assistant_text') {
    const full = typeof s.data.full_text === 'string' ? s.data.full_text : s.text;
    // The droid contract's machine lines (RESUME_PAYLOAD, WAITING_FOR, DONE) read as code, the rest as speech.
    const lines = full.trim().split('\n');
    const isContract = (l: string) => /^(DONE\s*$|WAITING_FOR\b|RESUME_PAYLOAD\b)/.test(l.trim());
    const prose = lines.filter((l) => !isContract(l)).join('\n').trim();
    const contract = lines.filter(isContract);
    return (
      <li className="py-0.5">
        {prose && (
          <div className="flex items-start gap-2">
            <span className="mt-0.5 w-4 shrink-0 text-center text-cyan-600" aria-hidden>
              “
            </span>
            <p className="min-w-0 max-w-prose whitespace-pre-wrap text-[14px] leading-relaxed text-zinc-800 dark:text-zinc-200">{prose}</p>
          </div>
        )}
        {contract.map((l, i) => (
          <div key={i} className="mt-1 flex items-start gap-2">
            <span className="w-4 shrink-0" />
            <code className="min-w-0 break-all rounded border border-zinc-300 px-1.5 py-px font-mono text-xs text-zinc-700 dark:border-zinc-700 dark:text-zinc-300">{l.trim()}</code>
          </div>
        ))}
      </li>
    );
  }
  if (s.kind === 'waiting_for') {
    const pred = toNode(s.data.predicate);
    return (
      <li className="mt-1 flex items-start gap-2 rounded-md bg-amber-50 px-2 py-1.5 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
        <span className="w-4 shrink-0 text-center" aria-hidden>
          ‖
        </span>
        <span>
          Parked, <PredicateLine predicate={pred} live={live} />
        </span>
      </li>
    );
  }
  if (s.kind === 'session_started' || s.kind === 'session_restarted') {
    return (
      <li className="flex items-start gap-2 text-xs text-zinc-500">
        <span className="w-4 shrink-0 text-center" aria-hidden>
          ○
        </span>
        <span>
          {s.text}
          {typeof s.data.model === 'string' && <span className="ml-2 text-zinc-400">{s.data.model}</span>}
        </span>
      </li>
    );
  }
  const bad = s.kind === 'error' || s.kind === 'permission_denied' || (s.result && !s.result.ok);
  return (
    <li className={`flex items-start gap-2 text-sm ${bad ? 'text-red-700 dark:text-red-400' : 'text-zinc-600 dark:text-zinc-400'}`}>
      <span className="w-4 shrink-0 text-center" aria-hidden>
        {bad ? '!' : '·'}
      </span>
      <span className="min-w-0 break-words">{s.text || s.kind.replace(/_/g, ' ')}</span>
    </li>
  );
}

/**
 * The moment the user's idea is about: the droid was parked on a predicate, a row changed in
 * SpacetimeDB, and the resume bridge woke it. Shown as a band between the turns.
 */
function ResumeBand({ turn, mark, nowMs }: { turn: Turn; mark?: ResumeMark; nowMs: number }) {
  const bySpacetime = !!mark?.bySpacetime;
  const fresh = mark ? nowMs / 1000 - mark.tsUs / 1e6 < 90 : false;
  const met = mark?.predicate ? predicateWords(mark.predicate).replace(/^waiting (for|until) /, '') : undefined;
  const restart = turn.protocol === 'restart' || mark?.mode === 'restart';
  const how = restart
    ? `New Factory session ${shortId(turn.factorySessionId ?? mark?.factorySessionId)}, seeded from the saved resume payload (Protocol B)${turn.restartedFrom || mark?.previousFactorySessionId ? `, after ${shortId(turn.restartedFrom || mark?.previousFactorySessionId)} was gone` : ''}.`
    : `Same Factory session ${shortId(turn.factorySessionId ?? mark?.factorySessionId)}, picked up where it stopped (Protocol A).`;

  if (!bySpacetime) {
    return (
      <div className="mx-4 my-2 flex items-start gap-3 rounded-md border border-dashed border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700">
        <span className="mt-0.5 text-zinc-400">↻</span>
        <div>
          <p className="font-medium text-zinc-700 dark:text-zinc-300">Resumed{mark?.reason ? ` (${mark.reason.slice(0, 120)})` : ''}</p>
          <p className="text-xs text-zinc-500">{how} Not woken by the resume bridge, so no database condition is claimed here.</p>
        </div>
      </div>
    );
  }
  return (
    <div
      className={`relative mx-3 my-3 overflow-hidden rounded-lg border border-violet-400/60 bg-violet-50 px-4 py-3 text-violet-950 dark:border-violet-500/50 dark:bg-violet-950/50 dark:text-violet-100 ${fresh ? 'motion-safe:animate-[tdm76-wake_1.6s_ease-out_1]' : ''}`}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-violet-600 text-white">
          <WakeGlyph className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <p className="text-[15px] font-semibold leading-snug">
            Woken by a SpacetimeDB update{fresh && <span className="ml-2 rounded bg-violet-600 px-1.5 py-px text-[11px] font-semibold text-white">just now</span>}
          </p>
          <p className="mt-0.5 text-sm">
            {met ? <>Its condition came true: {met}.</> : 'The row it was waiting on changed.'}
            {mark?.waitedS !== undefined && <> It had waited {duration(mark.waitedS)}.</>}
          </p>
          <p className="mt-1 text-xs text-violet-800/80 dark:text-violet-200/70">{how}</p>
        </div>
      </div>
    </div>
  );
}

