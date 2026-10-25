'use client';

// TDM-116: the cockpit's top bar. Who (ticket, task, sim/real, robot), the key numbers (before -> after
// with n, spend of budget, target), the one primary action, a small menu, and the line: eight stations,
// the current one lit, each a button that pins its view on the stage.

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { StatusTag, Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { fmtCents } from '@/lib/format';
import type { Tdm112Floor } from '@/lib/tdm112/floor';
import { TDM116_TERMINAL, type Tdm116Action, type Tdm116StageKey } from '@/lib/tdm116/cockpit';
import { FakeBadge, SimBadge } from '@/components/ticket/Badges';
import { SampleBadgeFor } from '@/components/sample/SampleBadge';
import { Button } from '@/components/brand/Button';
import { Tdm116PrimaryButton } from './Actions';
import { Tdm116CancelDialog, Tdm116RobotReleased } from './CancelTicket';
import type { Tdm116Data } from './useCockpitData';

const pct = (x: number) => `${Math.round(x * 100)}%`;

function RunNum({ run, label }: { run?: { status: string; trials: number; completed: number; successes: number }; label: string }) {
  if (!run) return <span className="text-ink-muted">—</span>;
  const n = run.status === 'done' ? run.trials : run.completed;
  return (
    <span title={`${label}: ${run.successes} of ${n} passed${run.status === 'done' ? '' : ` so far (${run.trials} planned)`}`}>
      {n > 0 ? pct(run.successes / n) : '—'}
      <small>
        {run.successes}/{n}
        {run.status !== 'done' ? ' so far' : ''}
      </small>
    </span>
  );
}

export function Tdm116TopBar({ d, shown, onPick, action, onTab, factory = false, onFactory }: {
  d: Tdm116Data;
  shown: Tdm116StageKey;
  onPick: (k: Tdm116StageKey) => void;
  action: Tdm116Action;
  onTab: (tab: 'settings' | 'report' | 'robot') => void;
  /** The Overview is showing the factory cell (TDM-115) instead of the stage. */
  factory?: boolean;
  onFactory?: () => void;
}) {
  const t = d.ticket!;
  const floor = d.floor;
  const budget = Number(t.budgetCents);
  const spent = Number(t.spentCents);
  const ref4 = `TKT-${t.id.toString().padStart(4, '0')}`;
  return (
    <header className="tdm116-top" data-testid="cockpit-top">
      <div className="tdm116-who">
        <div className="tdm116-who-meta">
          <span className="gx-id text-ink">{ref4}</span>
          <StatusTag status={t.status} />
          {d.mode === 'sim' && <SimBadge title="This ticket runs on the SIM robot (MuJoCo YAM). Its numbers are sim results, never real-arm success rates." />}
          {d.mode === 'real' && <Tag tone="cancelled">Real robot</Tag>}
          {d.robotFake && <FakeBadge />}
          <SampleBadgeFor description={t.taskDescription} />
          {d.robot ? (
            <Link href={`/robots/${d.robot.id}`} className="gx-id hover:text-ink" title={`${d.robot.embodiment}, ${d.robot.status}`}>
              {d.robot.name}
            </Link>
          ) : (
            <span className="gx-id">robot #{t.robotId.toString()}</span>
          )}
          {floor?.iterationLabel && <span className="gx-id">{floor.iterationLabel}</span>}
          <Tdm116RobotReleased status={t.status} robotStatus={d.robot?.status} holderTicketId={floor?.holder?.ticketId} ticketId={Number(t.id)} />
        </div>
        <h1 className="tdm116-title" title={t.taskDescription}>
          {t.taskDescription}
        </h1>
      </div>

      <div className="tdm116-right">
        <dl className="tdm116-nums m-0">
          <div className="tdm116-num">
            <dt className="gx-label">Before → after</dt>
            <dd className="tdm116-num-v m-0" data-testid="cockpit-before-after">
              <RunNum run={d.baseline} label="Baseline" />
              <span className="tdm116-arrow" aria-hidden>
                →
              </span>
              <span className="sr-only">after</span>
              <RunNum run={d.candidate} label="Fine-tuned" />
            </dd>
          </div>
          <div className="tdm116-num">
            <dt className="gx-label">Spend</dt>
            <dd className="tdm116-num-v m-0">
              <span className="tdm116-money">{fmtCents(spent)}</span>
              <small>of {fmtCents(budget)}</small>
            </dd>
            <div className="gx-meter" role="img" aria-label={`Spent ${fmtCents(spent)} of ${fmtCents(budget)}`}>
              <span style={{ width: budget > 0 ? `${Math.min(100, (spent / budget) * 100)}%` : '0%' }} />
            </div>
          </div>
          <div className="tdm116-num tdm116-num-target">
            <dt className="gx-label">Target</dt>
            <dd className="tdm116-num-v m-0">{pct(t.targetSuccess)}</dd>
          </div>
        </dl>
        <div className="tdm116-actions">
          {/* One melt button per view: when the stage itself shows the plan's Approve front and centre, the
              top bar's copy steps down to a plain button. */}
          <Tdm116PrimaryButton action={action} ticketId={Number(t.id)} variant={action?.kind === 'approve' && shown === 'plan_data' ? 'default' : 'primary'} />
          {onFactory && (
            <Button small aria-pressed={factory} onClick={onFactory} data-testid="cockpit-factory-toggle" title={factory ? 'Back to the stage' : 'This ticket’s deployment, as the factory cell: every agent at its machine'}>
              {factory ? 'Stage view' : 'Factory view'}
            </Button>
          )}
          <Tdm116Menu ticketId={Number(t.id)} status={t.status} robotId={Number(t.robotId)} onTab={onTab} />
        </div>
      </div>

      <Tdm116Line floor={floor} shown={shown} onPick={onPick} />
    </header>
  );
}

const STATE_WORD: Record<string, string> = { idle: 'Idle', working: 'Running', waiting: 'Waiting', done: 'Done', failed: 'Failed' };

/** The line, compact: one button per station. The station on the stage carries an ink bar. */
export function Tdm116Line({ floor, shown, onPick }: { floor: Tdm112Floor | null; shown: Tdm116StageKey; onPick: (k: Tdm116StageKey) => void }) {
  const ref = useRef<HTMLOListElement>(null);
  const active = floor?.activeIndex ?? -1;
  const hasFloor = !!floor;
  // Phones: keep the station on the stage in view inside the horizontal scroller.
  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>('[aria-pressed="true"]');
    const sc = ref.current?.parentElement;
    if (!el || !sc || sc.scrollWidth <= sc.clientWidth + 4) return;
    sc.scrollTo({ left: Math.max(0, el.offsetLeft - 24), behavior: 'smooth' });
  }, [shown, hasFloor]);
  if (!floor) return <div className="tdm116-line" />;
  return (
    <nav className="tdm116-line" aria-label="Stations">
      <ol ref={ref} data-testid="cockpit-line">
        {floor.stations.map((st) => {
          const note = st.state === 'waiting' ? 'needs a person' : st.facts[0] ?? (st.state === 'done' ? 'done' : '');
          return (
            <li key={st.key}>
              <button
                type="button"
                className="tdm116-st"
                data-station={st.key}
                data-state={st.state}
                aria-pressed={st.key === shown}
                aria-current={st.index === active ? 'step' : undefined}
                onClick={() => onPick(st.key as Tdm116StageKey)}
                title={`${st.label}${st.sub ? ` ${st.sub}` : ''}: ${STATE_WORD[st.state]}. ${st.activity}`}
              >
                <span className="tdm116-st-name">
                  {st.label}
                  {st.sub && <small>{st.sub}</small>}
                </span>
                <span className="tdm116-st-note">{note || ' '}</span>
                <span className="tdm116-st-shown" aria-hidden />
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function Tdm116Menu({ ticketId, status, robotId, onTab }: { ticketId: number; status: string; robotId: number; onTab: (tab: 'settings' | 'report' | 'robot') => void }) {
  const [open, setOpen] = useState(false);
  const [cancel, setCancel] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    window.addEventListener('mousedown', onDown);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('mousedown', onDown);
      window.removeEventListener('keydown', onKey);
    };
  }, [open]);
  const go = (tab: 'settings' | 'report' | 'robot') => {
    setOpen(false);
    onTab(tab);
  };
  const ended = TDM116_TERMINAL.has(status);
  return (
    <div className="tdm116-menu" ref={box}>
      <button type="button" className="gx-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)} data-testid="tdm116-menu">
        More
      </button>
      {open && (
        <div className="tdm116-menu-list" role="menu">
          <button type="button" role="menuitem" onClick={() => go('settings')}>
            Run settings
          </button>
          <button type="button" role="menuitem" onClick={() => go('robot')}>
            Robot view
          </button>
          <Link role="menuitem" href={`/tickets/${ticketId}/report`} onClick={() => setOpen(false)}>
            Open the report
          </Link>
          <Link role="menuitem" href={`/tickets/${ticketId}/spend`} onClick={() => setOpen(false)}>
            Spend ledger
          </Link>
          {robotId > 0 && (
            <Link role="menuitem" href={`/robots/${robotId}`} onClick={() => setOpen(false)}>
              Robot page
            </Link>
          )}
          <hr />
          <button
            type="button"
            role="menuitem"
            className={gxCx(!ended && 'is-danger')}
            disabled={ended}
            title={ended ? `This ticket is already ${status}.` : undefined}
            onClick={() => {
              setOpen(false);
              setCancel(true);
            }}
            data-testid="tdm116-menu-cancel"
          >
            {ended ? `Ticket ${status}` : 'Cancel ticket…'}
          </button>
        </div>
      )}
      <Tdm116CancelDialog ticketId={ticketId} open={cancel} onClose={() => setCancel(false)} />
    </div>
  );
}
