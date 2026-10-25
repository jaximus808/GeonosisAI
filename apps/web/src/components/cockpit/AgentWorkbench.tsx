'use client';

// AgentWorkbench: the agent at the shown station, visibly at work. A droid at its console (WorkbenchDroid)
// in one of seven modes, a heartbeat ("last activity 4s ago", amber then red when nothing happens), and a
// Claude-Code-style transcript of the station's latest run with a spinner on the current line.
//
// Fixed height, the transcript scrolls inside itself, so nothing on the page moves when a line arrives.
// Data: the ticket's event_log rows (agent.step / agent.thought plus the station's ordinary rows) through
// the pure model in lib/agentwork/workbench.ts. Motion is CSS only, paused offscreen and in a hidden tab.

import './agent-workbench.css';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { gxCx } from '@/components/brand/cx';
import { useInView, useStillness } from '@/components/factory-iso/kit/react/live';
import { tdm112Words, type Tdm112Event, type Tdm112StationView } from '@/lib/tdm112/floor';
import { AW_GLYPH, AW_MODE_WORDS, awAgo, awWorkbench, type AwLine } from '@/lib/agentwork/workbench';
import { useTdm116Now, useTdm116PageVisible } from './hooks';
import { WorkbenchDroid } from './WorkbenchDroid';

export function AgentWorkbench({ station, events, ended }: { station: Tdm112StationView | undefined; events: readonly Tdm112Event[]; ended?: string }) {
  const ref = useRef<HTMLElement>(null);
  const inView = useInView(ref);
  const visible = useTdm116PageVisible();
  const still = useStillness();
  const nowMs = useTdm116Now(1000);
  const nowUs = nowMs * 1000;
  const w = useMemo(() => (station && nowUs ? awWorkbench(station, events, nowUs, tdm112Words) : null), [station, events, nowUs]);

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
          {mode === 'waiting' ? 'Waiting' : mode === 'idle' && ended ? `Ticket ${ended}` : AW_MODE_WORDS[mode]}
        </div>
        <p className="aw-headline" title={w?.headline}>
          {mode === 'waiting' && w?.waitingFor ? w.waitingFor : w?.headline ?? ''}
        </p>
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
      </div>

      <Transcript lines={w?.lines ?? []} busy={busy} />
    </section>
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
