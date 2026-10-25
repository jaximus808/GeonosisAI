'use client';

// TDM-115: /factory. One isometric cell per robot (its current ticket and its queue), with the
// TDM-112 lanes kept as the "List view". Live from SpacetimeDB.

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
  useEffect(() => {
    setCompact(new URLSearchParams(window.location.search).get('compact') === '1');
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
      {view === 'list' ? <FactoryLanes /> : <Cells compact={compact} />}
    </div>
  );
}

function Cells({ compact }: { compact: boolean }) {
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
  return (
    <>
      <header className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <h1 className="gx-display-l m-0">Factory floor</h1>
        <span className="gx-small">{shown.length ? `${shown.length} ${shown.length === 1 ? 'robot' : 'robots'}` : ready ? 'No robot online' : 'Connecting'}</span>
      </header>
      {ready && shown.length === 0 && <p className="gx-small m-0">No robot is online. Start a robot client to put it on the floor.</p>}
      {shown.map((r) => (
        <FactoryIso key={String(r.id)} robotId={r.id} compact={compact} />
      ))}
    </>
  );
}
