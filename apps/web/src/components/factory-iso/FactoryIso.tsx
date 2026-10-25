'use client';

// TDM-115: the factory as a physical place. One isometric robot cell: the arm on its table in the
// middle, seven machines around a conveyor, and every agent as a small droid at its machine with one
// short line of what it is doing, live from SpacetimeDB. Click a machine or a droid to see more.
//
//   <FactoryIso ticketId={4} />                    the cell of ticket 4's robot, focused on ticket 4
//   <FactoryIso robotId={3} />                     robot 3's cell, focused on the ticket on the arm now
//   <FactoryIso />                                 follows the newest live ticket
//   <FactoryIso ticketId={4} compact />            short header, one bubble (the agent that is acting)
//   <FactoryIso ticketId={4} onStationClick={(i) => …} />   i = TDM-112 station index 0..7
//                                                  (without it, a click opens the station drawer)
//
// Drawn with the vendored Anatomy kit (./kit, MIT, see kit/PROVENANCE). Plain SVG and DOM: no WebGL,
// no frame loop. CSS motion only, paused offscreen and in hidden tabs, and off under reduced motion.

import './kit/iso.css';
import './factory-iso.css';
import Link from 'next/link';
import { SampleBadgeFor } from '@/components/sample/SampleBadge';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { StationDrawer } from '@/components/factory/StationDrawer';
import { useRows } from '@/lib/spacetime/hooks';
import { useOperator } from '@/lib/operator/useOperator';
import {
  TDM115_STATE_WORDS,
  tdm115FocusTicket,
  tdm115Holder,
  tdm115Tkt,
  tdm115Usd,
  type Tdm115Agent,
  type Tdm115Cell,
  type Tdm115MachineKey,
  type Tdm115QueueItem,
} from '@/lib/tdm115/iso';
import { DROID_HEAD, MACHINE_ANCHOR, VIEW_H, VIEW_W, at, pct } from './geometry';
import { useInView, useStillness } from './kit/react/live';
import { IsoArt, spotOf } from './IsoArt';
import { AgentGuide, useAgentGuide } from './AgentGuide';
import { useIsoCell } from './useIsoCell';

export interface FactoryIsoProps {
  robotId?: bigint | number | string | null;
  ticketId?: bigint | number | string | null;
  compact?: boolean;
  /** Called with the TDM-112 station index (0 Eval baseline, 1 Diagnose, 2 Plan data, 3 Collect, 4 Review, 5 Train, 6 Eval fine-tuned, 7 Ship). */
  onStationClick?: (stationId: number) => void;
  className?: string;
}

function toId(v: bigint | number | string | undefined | null): bigint | null {
  if (v === undefined || v === null || v === '') return null;
  try {
    const b = BigInt(v);
    return b > 0n ? b : null;
  } catch {
    return null;
  }
}

export function FactoryIso({ robotId, ticketId, compact = false, onStationClick, className }: FactoryIsoProps) {
  const tid = toId(ticketId);
  const rid = toId(robotId);
  if (tid !== null) return <IsoCell ticketId={tid} compact={compact} onStationClick={onStationClick} className={className} />;
  if (rid !== null) return <IsoForRobot robotId={rid} compact={compact} onStationClick={onStationClick} className={className} />;
  return <IsoFollowing compact={compact} onStationClick={onStationClick} className={className} />;
}

type Inner = { compact: boolean; onStationClick?: (i: number) => void; className?: string };

function Empty({ className, children }: { className?: string; children: string }) {
  return (
    <section className={gxCx('fiso fiso-empty', className)}>
      <p className="gx-small m-0">{children}</p>
    </section>
  );
}

function IsoForRobot({ robotId, ...rest }: Inner & { robotId: bigint }) {
  const [tickets, ready] = useRows('ticket', (r) => r.robotId.eq(robotId));
  const [robots] = useRows('robot', (r) => r.id.eq(robotId));
  const [commands] = useRows('robotCommand', (r) => r.robotId.eq(robotId));
  const holder = useMemo(() => tdm115Holder(robots[0], commands), [robots, commands]);
  const [picked, setPicked] = useState<number | null>(null);
  const focus = useMemo(
    () => tickets.find((t) => Number(t.id) === picked) ?? tdm115FocusTicket(tickets, holder),
    [tickets, holder, picked],
  );
  if (!focus) return <Empty className={rest.className}>{ready ? `No ticket has used ${robots[0]?.name ?? `robot ${robotId}`} yet. File one to put it on the line.` : 'Connecting to the foundry.'}</Empty>;
  return <IsoCell ticketId={focus.id} onPickTicket={setPicked} {...rest} />;
}

function IsoFollowing(props: Inner) {
  const [tickets, ready] = useRows('ticket');
  const focus = useMemo(() => tdm115FocusTicket(tickets, null), [tickets]);
  if (!focus) return <Empty className={props.className}>{ready ? 'No tickets yet. File one to put a robot on the line.' : 'Connecting to the foundry.'}</Empty>;
  return <IsoCell ticketId={focus.id} {...props} />;
}

// ------------------------------------------------------------------------------ one cell

function useTabVisible() {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden');
    on();
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}

/** Bubble side per agent, so neighbours don't talk over each other. */
const SIDE: Record<string, 'l' | 'r'> = {
  eval_runner: 'r', clusterer: 'l', planner: 'l', dispatcher: 'l', data_droid: 'r', reviewer: 'l', dataset_qa: 'r', training_droid: 'r', onboarding_droid: 'l',
};
const TALKS = new Set(['working', 'waiting_robot', 'waiting_person', 'failed', 'asleep']);
const LOUD = new Set(['working', 'waiting_robot', 'waiting_person', 'failed']);
const LINE: Tdm115MachineKey[] = ['eval', 'diagnose', 'plan', 'collect', 'review', 'train', 'ship'];
/** The words of the line (brand book), shorter than the machine names. */
const LINE_WORD: Record<Tdm115MachineKey, string> = { eval: 'Eval', diagnose: 'Diagnose', plan: 'Plan data', collect: 'Collect', review: 'Review', train: 'Train', ship: 'Ship' };

export function IsoCell({ ticketId, compact, onStationClick, onPickTicket, className }: Inner & { ticketId: bigint; onPickTicket?: (id: number) => void }) {
  const { cell, floor, events, queue, ready, taskDescription } = useIsoCell(ticketId);
  const stageRef = useRef<HTMLDivElement>(null);
  const inView = useInView(stageRef);
  const tabVisible = useTabVisible();
  const reduced = useStillness();
  const [selected, setSelected] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<number | null>(null);
  const guide = useAgentGuide();
  const [hover, setHover] = useState<Tdm115MachineKey | null>(null);

  const openStation = useCallback(
    (i: number) => {
      if (onStationClick) onStationClick(i);
      else setDrawer(i);
    },
    [onStationClick],
  );
  const onMachine = useCallback(
    (k: Tdm115MachineKey) => {
      if (!cell) return;
      openStation(cell.machines[k].stationIndex);
    },
    [cell, openStation],
  );
  const onSelect = useCallback((k: string) => setSelected((s) => (s === k ? null : k)), []);

  if (!cell || !floor) {
    return <Empty className={className}>{ready ? `There is no ticket #${ticketId} yet. Its cell appears as soon as it is created.` : 'Connecting to the foundry.'}</Empty>;
  }

  const sel = cell.agents.find((a) => a.key === selected) ?? null;
  const talking = compact
    ? cell.agents.filter((a) => TALKS.has(a.state) && a.machine === cell.active && a.kind !== 'droid').slice(0, 1)
    : cell.agents.filter((a) => TALKS.has(a.state) || a.key === selected);
  const st = drawer !== null ? floor.stations[drawer] : null;
  const label = `${cell.robotName}${cell.sim ? ' (simulated)' : ''}, ${tdm115Tkt(cell.ticketId)}: ${cell.readout || 'idle'}`;

  return (
    <section
      className={gxCx('fiso iso', className)}
      data-compact={compact ? '' : undefined}
      data-paused={!inView || !tabVisible ? '' : undefined}
      data-testid="factory-iso"
      data-ticket={cell.ticketId}
      data-robot={cell.robotId}
      aria-label={`Robot cell ${cell.robotName}`}
    >
      <header className="fiso-head">
        <span className="fiso-robot-name">
          <span className="gx-station-label">{cell.robotName}</span>
          {cell.sim && <Tag tone="steel">Simulated</Tag>}
        </span>
        <Link href={`/dashboard?ticket=${cell.ticketId}`} className="fiso-ticket" title={taskDescription}>
          <span className="gx-id">{tdm115Tkt(cell.ticketId)}</span>
          <SampleBadgeFor description={taskDescription} />
          {!compact && taskDescription && <span className="fiso-task">{taskDescription}</span>}
        </Link>
        <span className="fiso-readout" data-st={cell.machines[cell.active ?? 'eval']?.state} aria-live="polite">
          {cell.readout}
        </span>
        <button type="button" className="fiso-guide-btn" aria-expanded={guide.open} onClick={guide.toggle}>
          What is an agent?
        </button>
      </header>

      {!compact && queue.length > 1 && <Queue queue={queue} ticketId={cell.ticketId} onPick={onPickTicket} />}
      {cell.waitingForRobot && cell.holder && (
        <p className="fiso-wait">
          <i className="gx-rivet" aria-hidden /> Waiting for {cell.robotName}. {cell.holder.words}.
        </p>
      )}

      <div className="fiso-body">
        <div ref={stageRef} className="fiso-stage" data-hover={hover ?? undefined}>
          <IsoArt cell={cell} selected={selected} onSelect={onSelect} onMachine={onMachine} reduced={reduced} label={label} />
          <div className="fiso-overlay">
            {(Object.keys(cell.machines) as Tdm115MachineKey[])
              .filter((k) => LOUD.has(cell.machines[k].state) || hover === k)
              .map((k) => (
                <MachinePlate key={k} cell={cell} k={k} compact={compact} onOpen={() => onMachine(k)} />
              ))}
            {!compact && (cell.active === 'collect' || cell.machines.collect.state === 'waiting_person') && <CollectCard cell={cell} />}
            {talking.map((a) => (
              <Bubble key={a.key} a={a} side={SIDE[a.key] ?? 'r'} onClick={() => onSelect(a.key)}>
                {a.key === 'planner' && cell.plan?.waiting && !compact ? <PlanAsk cell={cell} /> : null}
              </Bubble>
            ))}
          </div>
          {sel && (
            <AgentCard a={sel} onClose={() => setSelected(null)} onOpen={() => openStation(sel.stationIndex)}>
              {sel.key === 'planner' && cell.plan?.waiting ? <PlanAsk cell={cell} /> : null}
            </AgentCard>
          )}
          {guide.open && <AgentGuide agents={cell.agents} onClose={guide.close} />}
        </div>
      </div>

      <ol className="fiso-line" aria-label="Machines on the line">
        {LINE.map((k) => {
          const m = cell.machines[k];
          return (
            <li key={k} data-st={m.state} data-machine={k} aria-current={cell.active === k ? 'step' : undefined}>
              <button type="button" aria-label={`${m.label}: ${TDM115_STATE_WORDS[m.state]}${m.readout.length ? `, ${m.readout.join(', ')}` : ''}`} onClick={() => onMachine(k)} onMouseEnter={() => setHover(k)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(k)} onBlur={() => setHover(null)}>
                <span className="fiso-line-name" title={m.label}>{LINE_WORD[k]}</span>
                <span className="fiso-line-num">{m.readout.join(' · ') || TDM115_STATE_WORDS[m.state]}</span>
              </button>
            </li>
          );
        })}
      </ol>

      {/* Phones and narrow mounts: the bubbles become this list. */}
      <ol className="fiso-list" aria-label="What each agent is doing">
        {cell.agents.map((a) => (
          <li key={a.key} data-st={a.state}>
            <button type="button" onClick={() => onSelect(a.key)}>
              <i className="fiso-dot" aria-hidden />
              <span className="fiso-list-name">
                {a.name}
                {a.kind === 'claude' && <span className="fiso-badge">{a.model}</span>}
              </span>
              <span className="fiso-list-say">{a.say}</span>
            </button>
            {a.key === 'planner' && cell.plan?.waiting ? <PlanAsk cell={cell} /> : null}
          </li>
        ))}
      </ol>

      {st && <StationDrawer ticketId={ticketId} floor={floor} station={st} events={events} onClose={() => setDrawer(null)} />}
    </section>
  );
}

// ------------------------------------------------------------------------------ overlays

function Queue({ queue, ticketId, onPick }: { queue: Tdm115QueueItem[]; ticketId: number; onPick?: (id: number) => void }) {
  return (
    <ol className="fiso-queue" aria-label="Tickets on this robot">
      {queue.slice(0, 5).map((q) => (
        <li key={q.ticketId} data-now={q.now ? '' : undefined} data-focus={q.ticketId === ticketId ? '' : undefined}>
          {q.now ? <span className="fiso-queue-when">On the arm</span> : queue.some((x) => x.now) && (q.status === 'baseline' || q.status === 'eval') ? <span className="fiso-queue-when">Waiting</span> : null}
          {onPick ? (
            <button type="button" className="gx-id fiso-queue-pick" title={`Show ${q.task}`} aria-pressed={q.ticketId === ticketId} onClick={() => onPick(q.ticketId)}>
              {tdm115Tkt(q.ticketId)}
            </button>
          ) : (
            <Link href={`/dashboard?ticket=${q.ticketId}`} className="gx-id" title={q.task}>
              {tdm115Tkt(q.ticketId)}
            </Link>
          )}
          <span className="fiso-queue-stage">{q.label}</span>
        </li>
      ))}
      {queue.length > 5 && <li className="gx-id">+{queue.length - 5}</li>}
    </ol>
  );
}

function MachinePlate({ cell, k, compact, onOpen }: { cell: Tdm115Cell; k: Tdm115MachineKey; compact: boolean; onOpen: () => void }) {
  const m = cell.machines[k];
  const pos = pct(MACHINE_ANCHOR[k]);
  const words = TDM115_STATE_WORDS[m.state];
  return (
    <button
      type="button"
      className="fiso-plate"
      data-machine={k}
      data-st={m.state}
      style={pos as CSSProperties}
      onClick={onOpen}
      aria-label={`${m.label}: ${words}${m.readout.length ? `, ${m.readout.join(', ')}` : ''}. Open details.`}
    >
      <i className="gx-rivet" aria-hidden />
      <span className="fiso-plate-name">{m.label}</span>
      {!compact && m.readout.length > 0 && <span className="fiso-plate-num">{m.readout.join(' · ')}</span>}
    </button>
  );
}

function headPos(a: Tdm115Agent) {
  const [x, y] = at(spotOf(a));
  return { left: `${(((x + DROID_HEAD[0]) / VIEW_W) * 100).toFixed(2)}%`, top: `${(((y + DROID_HEAD[1]) / VIEW_H) * 100).toFixed(2)}%` } as CSSProperties;
}

function Bubble({ a, side, onClick, children }: { a: Tdm115Agent; side: 'l' | 'r'; onClick: () => void; children?: ReactNode }) {
  return (
    <div className="fiso-bubble" data-st={a.state} data-side={side} data-woke={a.woke ? '' : undefined} style={headPos(a)}>
      <button type="button" className="fiso-bubble-hit" onClick={onClick} aria-label={`${a.name}: ${a.say}`}>
        <span className="fiso-bubble-name">
          {a.name}
          {a.kind === 'claude' && <span className="fiso-badge">{a.model}</span>}
        </span>
        <span className="fiso-bubble-say">{a.say}</span>
      </button>
      {children}
    </div>
  );
}

function AgentCard({ a, onClose, onOpen, children }: { a: Tdm115Agent; onClose: () => void; onOpen: () => void; children?: ReactNode }) {
  return (
    <div className="fiso-agent" data-st={a.state} role="dialog" aria-label={a.name}>
      <div className="fiso-agent-head">
        <i className="fiso-dot" aria-hidden />
        <strong>{a.name}</strong>
        <span className="fiso-kind">{a.kind === 'claude' ? `Claude · ${a.model}` : a.kind === 'droid' ? 'Factory Droid' : 'Service'}</span>
        <span className="fiso-agent-state">{TDM115_STATE_WORDS[a.state]}</span>
        <button type="button" className="fiso-x" onClick={onClose} aria-label="Close">
          Close
        </button>
      </div>
      <p className="fiso-agent-say">{a.say}</p>
      <p className="gx-small m-0">{a.does} {a.wakes}</p>
      {a.woke && <p className="gx-small m-0 text-arc">Woke on {a.woke}</p>}
      {children}
      <button type="button" className="gx-btn gx-btn-sm" onClick={onOpen}>
        Show events
      </button>
    </div>
  );
}

function PlanAsk({ cell }: { cell: Tdm115Cell }) {
  const plan = cell.plan!;
  return (
    <span className="fiso-ask">
      <span className="fiso-ask-what">
        {plan.words}
      </span>
      <ApproveButton ticketId={cell.ticketId} planId={plan.id} cents={plan.priceCents} />
    </span>
  );
}

function ApproveButton({ ticketId, planId, cents }: { ticketId: number; planId: number; cents: number }) {
  const { operator } = useOperator();
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  if (operator === null) return null;
  if (!operator) {
    return (
      <a className="fiso-card-login" href="/ops/login">
        Log in to approve
      </a>
    );
  }
  const approve = async () => {
    setState('busy');
    try {
      const r = await fetch(`/api/spend/plans/${planId}/approve`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ticket_id: ticketId }),
      });
      const j = (await r.json().catch(() => ({}))) as { ok?: boolean; detail?: string };
      if (r.ok && j.ok) setState('done');
      else {
        setState('error');
        setMsg(j.detail || `HTTP ${r.status}`);
      }
    } catch (e) {
      setState('error');
      setMsg((e as Error).message);
    }
  };
  return (
    <>
      <button type="button" className="gx-btn gx-btn-primary gx-btn-sm" onClick={approve} disabled={state === 'busy' || state === 'done'}>
        {state === 'done' ? 'Approved' : state === 'busy' ? 'Approving' : `Approve ${tdm115Usd(cents)}`}
      </button>
      {state === 'error' && <span className="fiso-card-err">Not approved: {msg}</span>}
    </>
  );
}

function CollectCard({ cell }: { cell: Tdm115Cell }) {
  const c = cell.collect;
  if (!c.open && !c.claimed && !c.done && !c.discord && !c.text && !c.drafted) return null;
  const pos = pct([158, 17, 40]);
  return (
    <div className="fiso-card fiso-collectcard" style={pos as CSSProperties}>
      <span>
        Discord <b>{c.discord}</b>
      </span>
      <span>
        Text <b>{c.text}</b>
      </span>
      {c.drafted > 0 && (
        <span title="Dry run: drafted, not sent">
          Drafted <b>{c.drafted}</b>
        </span>
      )}
      <span>
        Claimed <b>{c.claimed}</b>
      </span>
    </div>
  );
}

