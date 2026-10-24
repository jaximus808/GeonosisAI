'use client';

// TDM-112: /factory. Every active ticket as its own lane of agent stations, all lanes on one column
// grid, and the shared Factory Droids on one overhead rail above them (each names its ticket).

import './factory.css';
import Link from 'next/link';
import { useMemo, type CSSProperties } from 'react';
import { StatusTag } from '@/components/brand/StatusTag';
import { Wordmark } from '@/components/brand/Wordmark';
import { useConnectionStatus, useRows } from '@/lib/spacetime/hooks';
import { parsePredicate, predicateWords } from '@/lib/tdm76/predicate';
import { TDM112_STATIONS, tdm112Droids, tdm112IsDroidRole, type Tdm112Event } from '@/lib/tdm112/floor';
import { useTdm112Now } from '@/lib/tdm112/useFloor';
import { DroidRail, FactoryFloor } from './FactoryFloor';

const ENDED = new Set(['shipped', 'failed', 'cancelled']);
const describe = (json: string) => predicateWords(parsePredicate(json));

export function FactoryLanes() {
  const [tickets, ready] = useRows('ticket');
  const [sessions] = useRows('agentSession');
  const [eventRows] = useRows('eventLog');
  const { status } = useConnectionStatus();
  const nowMs = useTdm112Now(1000);

  const { active, last } = useMemo(() => {
    const real = tickets.filter((t) => !t.createdBy.startsWith('test.'));
    const pool = real.length ? real : tickets;
    const newest = [...pool].sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
    return { active: newest.filter((t) => !ENDED.has(t.status)), last: newest.find((t) => ENDED.has(t.status)) };
  }, [tickets]);

  const lanes = active.length ? active : last ? [last] : [];
  const laneIds = useMemo(() => new Set(lanes.map((t) => t.id)), [lanes]);

  const resumes = useMemo<Tdm112Event[]>(() => {
    const out: Tdm112Event[] = [];
    for (const r of eventRows) {
      if (r.type !== 'agent.resumed') continue;
      try {
        out.push({ id: Number(r.id), tsUs: Number(r.ts.microsSinceUnixEpoch), actor: r.actor, type: r.type, payload: JSON.parse(r.payloadJson) });
      } catch {}
    }
    return out.sort((a, b) => a.tsUs - b.tsUs);
  }, [eventRows]);

  const droids = useMemo(
    () =>
      tdm112Droids(
        sessions.filter((x) => tdm112IsDroidRole(x.role) && laneIds.has(x.ticketId) && !x.name.startsWith('test.')),
        resumes,
        nowMs * 1000,
        describe,
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessions, resumes, laneIds, Math.floor(nowMs / 2000)],
  );

  return (
    <div className="tdm112-page" data-testid="factory-page">
      <header className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Link href="/dashboard" className="no-underline" aria-label="GeonosisAI dashboard">
          <Wordmark size="sm" />
        </Link>
        <h1 className="gx-display-l">Factory floor</h1>
        <span className="gx-small">
          {active.length ? `${active.length} ${active.length === 1 ? 'ticket' : 'tickets'} on the line` : ready ? 'No ticket on the line right now' : 'Connecting'}
        </span>
        <span className={`gx-live ml-auto ${status === 'connected' ? '' : status === 'connecting' ? 'is-connecting' : 'is-down'}`}>
          <i className="gx-rivet" aria-hidden />
          {status === 'connected' ? 'Live from SpacetimeDB' : status === 'connecting' ? 'Connecting to SpacetimeDB' : 'SpacetimeDB disconnected, retrying'}
        </span>
        <nav className="gx-nav" aria-label="Pages">
          <Link href="/live">Live kiosk</Link>
          <Link href="/agents">Agents</Link>
          <Link href="/dashboard">Tickets</Link>
        </nav>
      </header>

      {/* The shared overhead rail: one column per station, aligned with every lane below. */}
      <section className="tdm112 tdm112-overhead" data-variant="lane" aria-label="Factory Droids overhead">
        <div className="tdm112-head">
          <span className="gx-station-label">Factory Droids</span>
          <span className="gx-small">ride to the station they serve, sleep there on a WAITING_FOR predicate, wake on a SpacetimeDB update</span>
        </div>
        <div className="tdm112-scroll">
          <div className="tdm112-grid" style={{ '--n': TDM112_STATIONS.length } as CSSProperties}>
            <DroidRail droids={droids} nowUs={nowMs * 1000} showTicket empty="No Factory Droid on the line right now." />
            <ol className="tdm112-stations" aria-hidden>
              {TDM112_STATIONS.map((s) => (
                <li key={s.key} className="gx-id" style={{ scrollSnapAlign: 'start' }}>
                  {s.label}
                  {s.sub ? ` · ${s.sub}` : ''}
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>

      {lanes.length === 0 && ready && <p className="gx-small m-0">No tickets yet. File one to put a robot on the line.</p>}
      {lanes.map((t) => (
        <section key={String(t.id)} className="tdm112-lane" aria-label={`Ticket ${t.id}`}>
          <div className="tdm112-lane-title">
            <span className="gx-id">TKT-{String(t.id).padStart(4, '0')}</span>
            <StatusTag status={t.status} />
            {!active.length && <span className="gx-small">Last ticket, nothing in flight</span>}
            <Link href={`/tickets/${t.id}`} className="min-w-0 truncate">
              {t.taskDescription || 'Untitled ticket'}
            </Link>
            {t.iteration > 0 && <span className="gx-small">Iteration {t.iteration + 1}</span>}
          </div>
          <FactoryFloor ticketId={t.id} variant="lane" />
        </section>
      ))}
    </div>
  );
}
