'use client';

// TDM-115: /factory. One isometric cell per robot (its current ticket and its queue), with the
// TDM-112 lanes kept as the "List view". Live from SpacetimeDB.
// `?ticket=N` focuses that ticket: its robot's cell comes first, showing ticket N, with a link back to
// its cockpit; the other robots follow. Without it, the all-robots overview.

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import { FactoryLanes } from '@/components/factory/FactoryLanes';
import { useRows } from '@/lib/spacetime/hooks';
import { FactoryIso } from './FactoryIso';
import './factory-iso.css';

const VIEW_KEY = 'tdm115.factoryView';
const ENDED = new Set(['shipped', 'failed', 'cancelled']);

export function FactoryIsoPage() {
  const [view, setView] = useState<'cells' | 'list'>('cells');
  // ?compact=1 previews the compact cell other pages mount.
  const [compact, setCompact] = useState(false);
  const [focusTicket, setFocusTicket] = useState<bigint | null>(null);
  useEffect(() => {
    const sp = new URLSearchParams(window.location.search);
    setCompact(sp.get('compact') === '1');
    try {
      const n = BigInt(sp.get('ticket') ?? '');
      setFocusTicket(n > 0n ? n : null);
    } catch {
      /* no or bad ?ticket */
    }
    try {
      if (window.localStorage.getItem(VIEW_KEY) === 'list') setView('list');
    } catch {
      /* storage blocked */
    }
  }, []);
  const pick = (v: 'cells' | 'list') => {
    setView(v);
    try {
      window.localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* storage blocked */
    }
  };
  return (
    <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-4 px-4 py-4 sm:px-8 sm:py-6" data-testid="factory-page">
      <div className="fiso-seg" data-overlap={view === 'cells' ? '' : undefined} role="group" aria-label="Factory view">
        <button type="button" aria-pressed={view === 'cells'} onClick={() => pick('cells')}>
          Floor view
        </button>
        <button type="button" aria-pressed={view === 'list'} onClick={() => pick('list')}>
          List view
        </button>
      </div>
      {view === 'list' ? <FactoryLanes /> : <Cells compact={compact} focusTicket={focusTicket} />}
    </div>
  );
}

function Cells({ compact, focusTicket }: { compact: boolean; focusTicket: bigint | null }) {
  const [robots, ready] = useRows('robot');
  const [tickets] = useRows('ticket');
  const shown = useMemo(() => {
    const real = robots.filter((r) => !r.name.startsWith('test.'));
    const live = (id: bigint) => tickets.some((t) => t.robotId === id && !ENDED.has(t.status));
    const used = (id: bigint) => tickets.some((t) => t.robotId === id);
    return real
      .filter((r) => used(r.id) || r.status !== 'offline')
      .sort((a, b) => Number(live(b.id)) - Number(live(a.id)) || Number(used(b.id)) - Number(used(a.id)) || Number(a.id - b.id));
  }, [robots, tickets]);
  const focus = focusTicket !== null ? tickets.find((t) => t.id === focusTicket) : undefined;
  const tktLabel = (id: bigint) => `TKT-${id.toString().padStart(4, '0')}`;
  const rest = focus ? shown.filter((r) => r.id !== focus.robotId) : shown;
  return (
    <>
      <header className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <h1 className="gx-display-l m-0">Factory floor</h1>
        <span className="gx-small">{shown.length ? `${shown.length} ${shown.length === 1 ? 'robot' : 'robots'}` : ready ? 'No robot online' : 'Connecting'}</span>
        {focusTicket !== null && (
          <span className="flex items-center gap-4 text-sm" data-testid="factory-focus">
            <Link href={`/dashboard?ticket=${focusTicket}`}>Back to {tktLabel(focusTicket)}</Link>
            <Link href="/factory">All robots</Link>
          </span>
        )}
      </header>
      {ready && focusTicket !== null && !focus && <p className="gx-small m-0">There is no ticket #{focusTicket.toString()} in this database. Showing every robot.</p>}
      {ready && shown.length === 0 && !focus && <p className="gx-small m-0">No robot is online. Start a robot client to put it on the floor.</p>}
      {focus && <FactoryIso key={`t${focus.id}`} ticketId={focus.id} compact={compact} />}
      {rest.map((r) => (
        <FactoryIso key={String(r.id)} robotId={r.id} compact={compact} />
      ))}
    </>
  );
}
