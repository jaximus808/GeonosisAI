'use client';

// TDM-112: the drawer a station opens. Its own events (newest first, plain words with the raw type), then
// the E11 pieces reused as they are: the agents panel's orchestrator-agent rows and droid cards (TDM-76)
// for the agents of this station, and the decision evidence trail (TDM-68) for Diagnose and Plan data.

import { useEffect, useRef } from 'react';
import { AgentCard } from '@/components/agents/AgentCard';
import { OrchestratorAgents } from '@/components/agents/OrchestratorAgents';
import { DecisionTrail } from '@/components/ticket/DecisionTrail';
import { Button } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { useNow, usePanelModel } from '@/lib/tdm76/feed';
import { tdm112Elapsed, tdm112Words, type Tdm112Event, type Tdm112Floor, type Tdm112StationView } from '@/lib/tdm112/floor';

/** Which TDM-35 orchestrator services (and droid roles) work each station. */
const SERVICES: Record<Tdm112StationView['key'], string[]> = {
  baseline: ['eval'],
  diagnose: ['clustering'],
  plan_data: ['planner'],
  collect: ['dispatcher'],
  review: ['reviewer', 'qa'],
  train: [],
  eval: ['eval'],
  ship: ['flywheel'],
};
const DROIDS: Record<Tdm112StationView['key'], string[]> = {
  baseline: ['onboarding_droid'],
  diagnose: [],
  plan_data: [],
  collect: ['data_droid'],
  review: ['data_droid'],
  train: ['training_droid'],
  eval: [],
  ship: [],
};
const EVENTS_SHOWN = 25;

function clock(us: number): string {
  return new Date(us / 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function StationDrawer({
  ticketId,
  floor,
  station,
  events,
  onClose,
}: {
  ticketId: bigint;
  floor: Tdm112Floor;
  station: Tdm112StationView;
  events: Tdm112Event[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const nowMs = useNow(1000);
  const model = usePanelModel({ ticketId: Number(ticketId) });

  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      prev?.focus?.();
    };
  }, [onClose]);

  const ids = new Set(station.eventIds);
  const mine = events.filter((e) => ids.has(e.id)).slice(-EVENTS_SHOWN).reverse();
  const services = model.orchestrator.filter((a) => SERVICES[station.key].includes(a.key));
  const droids = model.agents.filter((a) => a.isDroid && DROIDS[station.key].includes(a.session.role));
  const running = station.state === 'working' || station.state === 'waiting';
  const elapsed = station.startedUs ? tdm112Elapsed(station.startedUs, running ? nowMs * 1000 : station.endedUs ?? 0) : '';

  return (
    <>
      <div className="tdm112-scrim" onClick={onClose} aria-hidden />
      <div ref={ref} className="tdm112-drawer" role="dialog" aria-modal="true" aria-label={`${station.label} station`} tabIndex={-1}>
        <header className="grid gap-2">
          <div className="flex items-center gap-3">
            <span className="gx-id">
              TKT-{String(floor.ticketId).padStart(4, '0')} · station {station.index + 1} of {floor.stations.length}
            </span>
            <Button variant="ghost" className="gx-btn-sm ml-auto" onClick={onClose}>
              Close
            </Button>
          </div>
          <h2 className="gx-display-l">
            {station.label}
            {station.sub ? ` · ${station.sub}` : ''}
          </h2>
          <p className="gx-small m-0">{station.agent}</p>
          <div className="flex flex-wrap items-center gap-2">
            <Tag tone={station.state === 'working' ? 'running' : station.state === 'waiting' ? 'money' : station.state === 'failed' ? 'failed' : 'steel'}>
              {station.state === 'done' ? 'Done' : station.state === 'working' ? 'Working' : station.state === 'waiting' ? 'Waiting' : station.state === 'failed' ? 'Failed' : 'Idle'}
            </Tag>
            {station.computeTag && <Tag tone={station.computeTag === 'REAL' ? 'shipped' : 'failed'}>{station.computeTag}</Tag>}
            {elapsed && <span className="gx-data">{running ? `${elapsed} so far` : `took ${elapsed}`}</span>}
            {station.runs > 1 && <span className="gx-id">ran {station.runs} times</span>}
            {station.llm && (
              <span className="gx-id">
                {station.llm.backend}
                {station.llm.model ? ` · ${station.llm.model}` : ''}
              </span>
            )}
          </div>
          {station.facts.length > 0 && <p className="gx-data m-0">{station.facts.join(' · ')}</p>}
          <p className={gxCx('m-0 text-[15px]', station.activityIsError ? 'text-slag' : 'text-ink')}>{station.activity}</p>
          {station.state === 'idle' && <p className="gx-small m-0">{station.waitsFor}</p>}
        </header>

        <section className="grid gap-2" aria-label="Events">
          <h3 className="gx-heading">Last events</h3>
          {mine.length === 0 ? (
            <p className="gx-small m-0">No event_log rows for this station on this ticket yet. {station.waitsFor}</p>
          ) : (
            <ol className="tdm112-events">
              {mine.map((e) => (
                <li key={e.id} className={/(error|failed|aborted|blocked)$/.test(e.type) ? 'is-error' : undefined}>
                  <span className="gx-id">{clock(e.tsUs)}</span>
                  <span>
                    {tdm112Words(e)}
                    <span className="gx-id block" title={`event_log #${e.id}, actor ${e.actor}`}>
                      {e.type} · {e.actor}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </section>

        {(services.length > 0 || droids.length > 0) && (
          <section className="grid gap-3" aria-label="Agents at this station">
            <h3 className="gx-heading">Agents at this station</h3>
            {droids.map((a) => (
              <AgentCard key={a.session.id} a={a} nowMs={nowMs} live={model.mode === 'live'} defaultOpen={false} />
            ))}
            {services.length > 0 && <OrchestratorAgents agents={services} nowMs={nowMs} />}
          </section>
        )}

        {(station.key === 'diagnose' || station.key === 'plan_data') && (
          <section className="grid gap-3" aria-label="Decision trail">
            <h3 className="gx-heading">Reasoning</h3>
            <p className="gx-small m-0">What failed, what we are buying, why: the planner&apos;s own words, with the rows behind them.</p>
            <DecisionTrail ticketId={String(ticketId)} refreshKey={events.length} />
          </section>
        )}
      </div>
    </>
  );
}
