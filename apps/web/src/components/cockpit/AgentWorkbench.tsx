'use client';

// AgentWorkbench: the agent at the shown station, visibly at work. A droid at its console (WorkbenchDroid)
// in one of seven modes, a heartbeat ("last activity 4s ago", amber then red when nothing happens), and a
// Claude-Code-style transcript of the station's latest run with a spinner on the current line.
//
// Fixed height, the transcript scrolls inside itself, so nothing on the page moves when a line arrives.
// Data: the ticket's event_log rows (agent.step / agent.thought plus the station's ordinary rows) through
// the pure model in lib/agentwork/workbench.ts. Motion is CSS only, paused offscreen and in a hidden tab.
//
// Reset ask: while a REAL robot's eval waits on a reset task (awResetAsk), the droid waits on "you": the
// headline says what to do and a big "Done, scene reset" button finishes the task as the operator
// (POST /api/tdm116/resets/{task}/done -> orchestrator reset_done.py), so the next trial starts.

import './agent-workbench.css';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { gxCx } from '@/components/brand/cx';
import { useInView, useStillness } from '@/components/factory-iso/kit/react/live';
import { tdm112Words, type Tdm112Event, type Tdm112StationView } from '@/lib/tdm112/floor';
import { AW_GLYPH, AW_MODE_WORDS, awAgo, awWorkbench, type AwLine, type AwResetAsk } from '@/lib/agentwork/workbench';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import { useTdm116Now, useTdm116PageVisible } from './hooks';
import { WorkbenchDroid } from './WorkbenchDroid';

export function AgentWorkbench({ station, events, ended, reset }: {
  station: Tdm112StationView | undefined;
  events: readonly Tdm112Event[];
  ended?: string;
  /** A real robot's open reset task the shown eval waits on (awResetAsk), else null. */
  reset?: AwResetAsk | null;
}) {
  const ref = useRef<HTMLElement>(null);
  const inView = useInView(ref);
  const visible = useTdm116PageVisible();
  const still = useStillness();
  const nowMs = useTdm116Now(1000);
  const nowUs = nowMs * 1000;
  const w = useMemo(() => (station && nowUs ? awWorkbench(station, events, nowUs, tdm112Words, reset) : null), [station, events, nowUs, reset]);

  const mode = w?.mode ?? 'idle';
  const paused = !inView || !visible || still;
  const busy = mode === 'working' || mode === 'thinking';
  const beat =
    !w || w.freshness === 'none'
      ? null
      : { tone: w.freshness, words: w.ageS !== undefined ? `last activity ${awAgo(w.ageS)} ago` : 'no activity yet' };

  return (
    <section
      ref={ref}
      className="aw"
      data-mode={mode}
      data-paused={paused ? '' : undefined}
      data-ask={w?.reset ? '' : undefined}
      data-testid="agent-workbench"
      aria-label={w ? `${w.agent.name}: ${AW_MODE_WORDS[mode]}` : 'Agent'}
    >
      <div className="aw-figure">
        <WorkbenchDroid mode={mode} claude={w?.agent.kind === 'claude'} />
      </div>

      <div className="aw-status">
        <div className="aw-who">
          <strong>{w?.agent.name ?? 'Agent'}</strong>
          {w?.agent.kind === 'claude' && <span className="aw-kind">Claude</span>}
        </div>
        <div className="aw-mode" data-mode={mode}>
          <i className="aw-mode-dot" aria-hidden />
          {w?.reset ? 'Waiting on you' : mode === 'waiting' ? 'Waiting' : mode === 'idle' && ended ? `Ticket ${ended}` : AW_MODE_WORDS[mode]}
        </div>
        <p className="aw-headline" title={w?.headline}>
          {w?.reset ? w.reset.ask : mode === 'waiting' && w?.waitingFor ? w.waitingFor : w?.headline ?? ''}
        </p>
        {w?.reset ? (
          <ResetDone key={w.reset.taskId} taskId={w.reset.taskId} />
        ) : (
        <div className="aw-beat" data-tone={beat?.tone ?? 'none'} aria-live="off">
          {beat ? (
            <>
              <i className="aw-beat-dot" aria-hidden data-busy={busy ? '' : undefined} />
              {beat.words}
            </>
          ) : (
            <span className="aw-beat-quiet">{mode === 'waiting' ? 'Not stuck: waiting on someone' : ' '}</span>
          )}
        </div>
        )}
      </div>

      <Transcript lines={w?.lines ?? []} busy={busy} />
    </section>
  );
}

/** "Done, scene reset": completes the open reset task as the operator; the eval then resumes. */
function ResetDone({ taskId }: { taskId: number }) {
  const { operator } = useOperator();
  const [s, setS] = useState<{ state: 'idle' | 'busy' | 'done' | 'error'; msg: string }>({ state: 'idle', msg: '' });
  if (operator === false) {
    const here = typeof window === 'undefined' ? '/dashboard' : `${window.location.pathname}${window.location.search}`;
    return (
      <a className="aw-done is-login" href={operatorLoginHref(here)} data-testid="aw-reset-login">
        Log in as operator to tap Done
      </a>
    );
  }
  const go = async () => {
    setS({ state: 'busy', msg: '' });
    try {
      const r = await fetch(`/api/tdm116/resets/${taskId}/done`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
      const j = (await r.json().catch(() => ({}))) as { detail?: unknown };
      if (r.ok) setS({ state: 'done', msg: '' });
      else setS({ state: 'error', msg: typeof j.detail === 'string' ? j.detail : `HTTP ${r.status}` });
    } catch (e) {
      setS({ state: 'error', msg: (e as Error).message });
    }
  };
  return (
    <button
      type="button"
      className="aw-done"
      data-state={s.state}
      onClick={go}
      disabled={s.state === 'busy' || s.state === 'done' || operator === null}
      title={s.state === 'error' ? s.msg : `Marks reset task #${taskId} done as the operator; the next trial starts`}
      data-testid="aw-reset-done"
    >
      {s.state === 'busy' ? 'Sending…' : s.state === 'done' ? 'Done. Resuming…' : s.state === 'error' ? 'Retry: Done, scene reset' : 'Done, scene reset'}
    </button>
  );
}

function Transcript({ lines, busy }: { lines: AwLine[]; busy: boolean }) {
  const box = useRef<HTMLOListElement>(null);
  const stick = useRef(true); // follow the bottom unless the reader scrolled up
  const [, force] = useState(0);
  const lastId = lines[lines.length - 1]?.id;
  const lastText = lines[lines.length - 1]?.text;

  useLayoutEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight; // scrolls the box only, never the page
  }, [lines.length, lastId, lastText]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const on = () => {
      const at = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      if (at !== stick.current) {
        stick.current = at;
        force((x) => x + 1);
      }
    };
    el.addEventListener('scroll', on, { passive: true });
    return () => el.removeEventListener('scroll', on);
  }, []);

  return (
    <div className="aw-log-wrap">
      <ol ref={box} className="aw-log" aria-label="What the agent is doing" aria-live="polite" aria-relevant="additions">
        {lines.map((l) => (
          <li key={l.id} className="aw-line" data-glyph={l.glyph} data-live={l.live && busy ? '' : undefined}>
            <span className={gxCx('aw-g', l.live && busy && 'is-spin')} aria-hidden>
              {l.live && busy ? '✻' : AW_GLYPH[l.glyph]}
            </span>
            <span className="aw-t">
              {l.text}
              {l.live && busy && <span className="aw-ellipsis" aria-hidden />}
            </span>
          </li>
        ))}
      </ol>
      {!stick.current && (
        <button
          type="button"
          className="aw-jump"
          onClick={() => {
            const el = box.current;
            if (el) el.scrollTop = el.scrollHeight;
            stick.current = true;
            force((x) => x + 1);
          }}
        >
          Latest ↓
        </button>
      )}
    </div>
  );
}
