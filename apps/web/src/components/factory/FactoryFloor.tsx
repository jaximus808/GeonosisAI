'use client';

// TDM-112: the factory floor. Every agent in the pipeline as a station on an assembly line (Eval ->
// Diagnose -> Plan data -> Collect -> Review -> Train -> Eval -> Ship), the long-running Factory
// Droids riding an overhead rail to the station they serve, and a part that travels the conveyor
// whenever work hands off. Live from SpacetimeDB (lib/tdm112/useFloor.ts), plain SVG/DOM.
//
//   <FactoryFloor ticketId={id} />                 ticket page (replaces the Line: same stations, more detail)
//   <FactoryFloor ticketId={id} variant="band" />  /live kiosk top band
//   <FactoryFloor ticketId={id} variant="lane" />  one lane of /factory
//   <FactoryFloor />                               follows the newest active ticket
//
// Clicking a station opens a drawer with that station's events and the E11 agents panel / decision trail.

import './factory.css';
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Tag, type GxTone } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { useRows } from '@/lib/spacetime/hooks';
import { tdm101PickTicket } from '@/lib/tdm101/live';
import { tdm112Elapsed, tdm112Handoff, type Tdm112DroidView, type Tdm112Floor, type Tdm112StationView } from '@/lib/tdm112/floor';
import { useTdm112Floor, useTdm112Now } from '@/lib/tdm112/useFloor';
import { StationDrawer } from './StationDrawer';

export type FactoryFloorVariant = 'full' | 'band' | 'lane';

function toId(v: bigint | number | string | undefined | null): bigint | null {
  if (v === undefined || v === null || v === '') return null;
  try {
    const b = BigInt(v);
    return b > 0n ? b : null;
  } catch {
    return null;
  }
}

export function FactoryFloor({ ticketId, variant = 'full', className }: { ticketId?: bigint | number | string | null; variant?: FactoryFloorVariant; className?: string }) {
  const id = toId(ticketId);
  if (id !== null) return <FloorForTicket ticketId={id} variant={variant} className={className} />;
  return <FloorFollowing variant={variant} className={className} />;
}

function FloorFollowing({ variant, className }: { variant: FactoryFloorVariant; className?: string }) {
  const [tickets, ready] = useRows('ticket');
  const pick = useMemo(() => tdm101PickTicket(tickets, null), [tickets]);
  if (!pick.ticket) {
    return (
      <section className={gxCx('tdm112', className)} data-variant={variant}>
        <p className="gx-small m-0">{ready ? 'No tickets yet. File one to put a robot on the line.' : 'Connecting to the foundry.'}</p>
      </section>
    );
  }
  return <FloorForTicket ticketId={pick.ticket.id} variant={variant} className={className} />;
}

const STATE_TAG: Record<Tdm112StationView['state'], { tone: GxTone | 'ink'; label: string }> = {
  idle: { tone: 'steel', label: 'Idle' },
  working: { tone: 'running', label: 'Working' },
  waiting: { tone: 'money', label: 'Waiting' },
  done: { tone: 'ink', label: 'Done' },
  failed: { tone: 'failed', label: 'Failed' },
};

function StateTag({ st, robotWait }: { st: Tdm112StationView; robotWait?: boolean }) {
  if (st.key === 'ship' && st.state === 'done') return <Tag tone="shipped">Shipped</Tag>;
  if (robotWait && st.state === 'waiting') return <Tag tone="money">Waiting for robot</Tag>; // TDM-114
  const t = STATE_TAG[st.state];
  if (t.tone === 'ink') return <Tag className="tdm112-tag-ink">{t.label}</Tag>;
  return <Tag tone={t.tone}>{t.label}</Tag>;
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const m = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(m.matches);
    const on = () => setReduced(m.matches);
    m.addEventListener('change', on);
    return () => m.removeEventListener('change', on);
  }, []);
  return reduced;
}

function FloorForTicket({ ticketId, variant, className }: { ticketId: bigint; variant: FactoryFloorVariant; className?: string }) {
  const { floor, ready, events } = useTdm112Floor(ticketId);
  const [open, setOpen] = useState<number | null>(null);
  if (!floor) {
    return (
      <section className={gxCx('tdm112', className)} data-variant={variant}>
        <p className="gx-small m-0">{ready ? `There is no ticket #${ticketId} yet. Its floor appears as soon as it is created.` : 'Connecting to the floor.'}</p>
      </section>
    );
  }
  const st = open !== null ? floor.stations[open] : null;
  return (
    <>
      <FloorView floor={floor} variant={variant} className={className} onOpen={setOpen} />
      {st && <StationDrawer ticketId={ticketId} floor={floor} station={st} events={events} onClose={() => setOpen(null)} />}
    </>
  );
}

/** The floor itself, presentational: a model in, DOM out. */
export function FloorView({ floor, variant = 'full', className, onOpen }: { floor: Tdm112Floor; variant?: FactoryFloorVariant; className?: string; onOpen?: (index: number) => void }) {
  const nowMs = useTdm112Now(1000);
  const nowUs = nowMs * 1000;
  const reduced = useReducedMotion();
  const scrollRef = useRef<HTMLDivElement>(null);
  const partRef = useRef<HTMLSpanElement>(null);
  const prevActive = useRef<number | null>(null);
  const active = floor.activeIndex;

  // The part: a 160 ms ease-out lift, then a mechanical slide to the next station (backwards on a loop-back).
  useLayoutEffect(() => {
    const prev = prevActive.current;
    prevActive.current = active;
    const el = partRef.current;
    if (!el || prev === null || reduced) return;
    const h = tdm112Handoff(prev, active);
    if (!h || typeof el.animate !== 'function') return;
    const dist = Math.abs(h.to - h.from);
    const slide = Math.min(2400, 420 * dist);
    const total = 160 + slide + 120;
    const x = (c: number) => `translateX(${c * 100}%)`;
    el.animate(
      [
        { transform: `${x(h.from)} translateY(0)`, offset: 0, easing: 'ease-out' },
        { transform: `${x(h.from)} translateY(-5px)`, offset: 160 / total, easing: 'cubic-bezier(0.45, 0, 0.2, 1)' },
        { transform: `${x(h.to)} translateY(-5px)`, offset: (160 + slide) / total, easing: 'ease-out' },
        { transform: `${x(h.to)} translateY(0)`, offset: 1 },
      ],
      { duration: total },
    );
  }, [active, reduced]);

  // Phones: keep the running station in view inside the horizontal scroller.
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc || active < 0 || sc.scrollWidth <= sc.clientWidth + 4) return;
    const col = sc.scrollWidth / floor.stations.length;
    const target = Math.max(0, col * active - col * 0.25);
    if (Math.abs(sc.scrollLeft - target) > col / 2) sc.scrollTo({ left: target, behavior: reduced ? 'auto' : 'smooth' });
  }, [active, floor.stations.length, reduced]);

  const compact = variant !== 'full';
  const partAt = active >= 0 ? active : floor.ended === 'shipped' ? floor.stations.length - 1 : -1;

  return (
    <section
      className={gxCx('tdm112', className)}
      data-variant={variant}
      data-testid="factory-floor"
      data-ticket={floor.ticketId}
      data-active={active}
      aria-label={`Factory floor for ticket ${floor.ticketId}`}
    >
      {variant !== 'lane' && (
        <div className="tdm112-head">
          <span className="gx-station-label">Factory floor</span>
          <span className="gx-id">TKT-{String(floor.ticketId).padStart(4, '0')}</span>
          {floor.iterationLabel && <Tag tone="steel">{floor.iterationLabel}</Tag>}
          {floor.queued && <span className="gx-small">Queued. The baseline eval starts once the robot is onboarded.</span>}
          {floor.ended === 'failed' && <Tag tone="failed">Ticket failed</Tag>}
          {floor.endedNote && <span className="gx-small text-slag">{floor.endedNote}</span>}
          {floor.ended === 'cancelled' && <Tag tone="cancelled">Cancelled</Tag>}
          {floor.ended === 'shipped' && <Tag tone="shipped">Shipped</Tag>}
          {!compact && <span className="gx-small tdm112-hint hidden sm:inline">Select a station for its events and reasoning.</span>}
        </div>
      )}
      <div ref={scrollRef} className="tdm112-scroll">
        <div className="tdm112-grid" style={{ '--n': floor.stations.length } as CSSProperties}>
          {variant !== 'lane' && <DroidRail droids={floor.droids} nowUs={nowUs} />}
          <div className="tdm112-belt" aria-hidden>
            {partAt >= 0 && (
              <span ref={partRef} className="tdm112-part" style={{ '--x': partAt } as CSSProperties}>
                <i />
              </span>
            )}
          </div>
          <ol className="tdm112-stations">
            {floor.stations.map((st) => (
              <li key={st.key} className="tdm112-st" data-station={st.key} data-state={st.state} aria-current={st.index === active ? 'step' : undefined}>
                <button type="button" className="tdm112-card" onClick={() => onOpen?.(st.index)} aria-label={`${st.label}${st.sub ? ` ${st.sub}` : ''}: ${STATE_TAG[st.state].label}. ${st.activity}`}>
                  <span className="tdm112-name">
                    {st.label}
                    {st.sub && <small>{st.sub}</small>}
                  </span>
                  <span className="flex flex-wrap items-center gap-1">
                    <StateTag st={st} robotWait={floor.waitingFor?.stationIndex === st.index} />
                    {st.computeTag && (
                      <Tag tone={st.computeTag === 'REAL' ? 'shipped' : 'failed'} title={COMPUTE_TITLE[st.computeTag]}>
                        {st.computeTag}
                      </Tag>
                    )}
                  </span>
                  {!compact && <span className="tdm112-agent" title={st.agent}>{st.agent}</span>}
                  {st.facts.length > 0 && (
                    <span className="tdm112-facts">
                      {st.facts.slice(0, compact ? 2 : 4).map((f, i) => (
                        <span key={i}>{f}</span>
                      ))}
                    </span>
                  )}
                  {st.chips && st.chipsTotal ? <Chips chips={st.chips} total={st.chipsTotal} max={compact ? 10 : 20} /> : null}
                  <span className={gxCx('tdm112-activity', st.activityIsError && 'is-error')} title={st.activity}>
                    {st.activity}
                  </span>
                  <span className="tdm112-foot">
                    {st.llm && (
                      <span className="tdm112-llm" title={`LLM backend ${st.llm.backend}${st.llm.model ? `, model ${st.llm.model}` : ''} (newest llm.call)`}>
                        <span>{st.llm.backend}</span>
                        {st.llm.model && <span>{st.llm.model}</span>}
                      </span>
                    )}
                    <Elapsed st={st} nowUs={nowUs} />
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}

const COMPUTE_TITLE: Record<string, string> = {
  STUB: 'Stub training run, no GPU.',
  REAL: 'Real Modal training run.',
  SEEDED: 'Seeded outcomes, not run on a robot.',
};

function Elapsed({ st, nowUs }: { st: Tdm112StationView; nowUs: number }) {
  if (!st.startedUs || !nowUs) return null;
  const running = st.state === 'working' || st.state === 'waiting';
  const to = running ? nowUs : st.endedUs;
  if (!to) return null;
  const t = tdm112Elapsed(st.startedUs, to);
  if (!t) return null;
  return (
    <span className="tdm112-time" title={running ? 'Time at this station so far' : 'Time this station took (last run)'}>
      {running ? t : `took ${t}`}
      {st.runs > 1 ? ` ×${st.runs}` : ''}
    </span>
  );
}

function Chips({ chips, total, max }: { chips: { index: number; pass: boolean }[]; total: number; max: number }) {
  const by = new Map(chips.map((c) => [c.index, c]));
  const n = Math.min(total, max);
  return (
    <span className="tdm112-chips" aria-label={`${chips.filter((c) => c.pass).length} passed, ${chips.filter((c) => !c.pass).length} failed of ${total}`}>
      {Array.from({ length: n }, (_, i) => {
        const c = by.get(i);
        return (
          <i key={i} className={gxCx('gx-trial', c ? (c.pass ? 'is-pass' : 'is-fail') : 'tdm112-chip-todo')} title={c ? `Trial ${i + 1}: ${c.pass ? 'Pass' : 'Fail'}` : `Trial ${i + 1}: not run yet`}>
            {c ? (c.pass ? 'P' : 'F') : ''}
          </i>
        );
      })}
      {total > n && <span className="gx-id">+{total - n}</span>}
    </span>
  );
}

// ------------------------------------------------------------------------------ droids

function DroidFigure() {
  return (
    <svg className="tdm112-droid-fig" width="22" height="34" viewBox="0 0 22 34" aria-hidden>
      {/* trolley on the rail, hanger, head with a visor slit, body, two arms */}
      <rect x="6" y="0" width="10" height="5" fill="currentColor" />
      <rect x="10" y="5" width="2" height="5" fill="currentColor" />
      <rect x="4" y="10" width="14" height="9" fill="none" stroke="currentColor" strokeWidth="2" />
      <rect className="eye" x="7" y="13.5" width="8" height="2.5" />
      <rect x="6" y="21" width="10" height="9" fill="currentColor" />
      <rect x="1" y="22" width="3" height="7" fill="currentColor" />
      <rect x="18" y="22" width="3" height="7" fill="currentColor" />
    </svg>
  );
}

/** The overhead rail. On /factory one rail is shared by every lane, so each droid names its ticket. */
export function DroidRail({ droids, nowUs, showTicket = false, empty }: { droids: Tdm112DroidView[]; nowUs: number; showTicket?: boolean; empty?: string }) {
  // Several droids at one column stand side by side.
  const slots = new Map<number, number>();
  const placed = droids.map((d) => {
    const col = d.stationIndex >= 0 ? d.stationIndex : 0;
    const slot = slots.get(col) ?? 0;
    slots.set(col, slot + 1);
    return { d, col, slot };
  });
  return (
    <div className="tdm112-rail" aria-label="Factory Droids">
      {droids.length === 0 && <span className="tdm112-rail-empty gx-small">{empty ?? 'No Factory Droid on this ticket yet. One rides in when the orchestrator starts it.'}</span>}
      {placed.map(({ d, col, slot }) => (
        <Droid key={d.id} d={d} col={col} slot={slot} nowUs={nowUs} showTicket={showTicket} />
      ))}
    </div>
  );
}

function Droid({ d, col, slot, nowUs, showTicket }: { d: Tdm112DroidView; col: number; slot: number; nowUs: number; showTicket?: boolean }) {
  const [moving, setMoving] = useState(false);
  const prev = useRef(col);
  useEffect(() => {
    if (prev.current === col) return;
    prev.current = col;
    setMoving(true);
    const t = setTimeout(() => setMoving(false), 1100);
    return () => clearTimeout(t);
  }, [col]);
  const wokeRecent = d.woke && nowUs - d.woke.tsUs < 10_000_000;
  let note: string;
  if (d.state === 'asleep') note = d.predicate?.raw ? `WAITING_FOR ${d.predicate.raw}` : d.predicate?.words ?? 'asleep';
  else if (wokeRecent && d.woke) note = `woke on ${d.woke.on}`;
  else if (d.state === 'docked' || d.state === 'done') note = d.state === 'done' ? 'done, in the bay' : 'in the bay';
  else if (d.state === 'failed') note = 'failed';
  else note = d.step || 'working';
  const title = [
    `${d.label} #${d.id}${d.name ? ` (${d.name})` : ''}`,
    d.state === 'asleep' ? `Asleep: ${d.predicate?.words ?? ''}` : `State: ${d.state}`,
    d.step ? `Step: ${d.step}` : '',
    d.woke ? `Last woke on ${d.woke.on}` : '',
  ]
    .filter(Boolean)
    .join('\n');
  return (
    <div
      className="tdm112-droid"
      data-state={wokeRecent && d.state !== 'asleep' ? 'waking' : d.state}
      data-moving={moving ? '1' : undefined}
      style={{ '--x': col, '--slot': slot } as CSSProperties}
    >
      <div className="tdm112-droid-body" title={title}>
        <DroidFigure />
        <span className="tdm112-droid-text">
          <span className="tdm112-droid-name">
            {d.label.replace(/ Droid$/, '')}
            {d.state === 'asleep' && <span className="tdm112-droid-state"> asleep</span>}
            {showTicket && d.ticketId > 0 ? <span className="gx-id"> TKT-{String(d.ticketId).padStart(4, '0')}</span> : null}
          </span>
          <span className={gxCx('tdm112-droid-note', wokeRecent && d.state !== 'asleep' && 'tdm112-woke')}>{note}</span>
        </span>
      </div>
    </div>
  );
}
