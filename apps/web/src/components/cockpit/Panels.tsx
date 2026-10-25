'use client';

// TDM-116: the rail's deep-dive tabs. Each is one full-height panel; long lists scroll inside it, the
// page never does. Existing components are mounted as they are (TDM-64 spend, TDM-68 decision trail,
// TDM-76 agents, TDM-79 run settings, TDM-104 scene, TDM-112 floor, TDM-115 isometric cell).

import dynamic from 'next/dynamic';
import { useMemo, useState } from 'react';
import { ButtonLink } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { TicketAgentsPanel } from '@/components/agents/TicketAgentsPanel';
import { FactoryFloor } from '@/components/factory/FactoryFloor';
import { TicketSpendPanel } from '@/components/payments/TicketSpendPanel';
import { Tdm104ScenePanel } from '@/components/scene/Tdm104ScenePanel';
import { DecisionTrail } from '@/components/ticket/DecisionTrail';
import { EpisodesGrid } from '@/components/ticket/EpisodesGrid';
import { EvalCards } from '@/components/ticket/EvalCards';
import { OfferCard } from '@/components/ticket/OfferCard';
import { RunSettingsButton } from '@/components/ticket/RunSettingsDrawer';
import { PaymentsTable } from '@/components/ticket/TasksTable';
import { TestModeTag } from '@/components/payments/TestModeBand';
import { DATA_TYPE_LABEL, parseObj, shortUri } from '@/components/ticket/util';
import { fmtCents, fmtTs } from '@/lib/format';
import { tdm112Words } from '@/lib/tdm112/floor';
import type { Tdm116StageKey, Tdm116TabKey } from '@/lib/tdm116/cockpit';
import { TDM116_STAGE_KEYS } from '@/lib/tdm116/cockpit';
import { RobotQueueList, WaitingForRobot } from '@/components/tdm114/WaitingForRobot';
import { Tdm116CancelBlock } from './CancelTicket';
import { Tdm125CollectPicker } from '@/components/tdm125/CollectRobot';
import { Tdm116RobotWell } from './RobotWell';
import type { Tdm116Data } from './useCockpitData';

// TDM-115's isometric cell (plain SVG, no WebGL). Loaded only when the Agents tab opens.
const FactoryIso = dynamic(() => import('@/components/factory-iso/FactoryIso').then((m) => m.FactoryIso), {
  ssr: false,
  loading: () => <div className="tdm116-empty">Loading the factory cell…</div>,
});

const pct = (x: number) => `${Math.round(x * 100)}%`;

export function Tdm116Panel({ d, tab, onPin }: { d: Tdm116Data; tab: Exclude<Tdm116TabKey, 'overview'>; onPin: (k: Tdm116StageKey) => void }) {
  switch (tab) {
    case 'robot':
      return <RobotPanel d={d} />;
    case 'data':
      return <DataPanel d={d} />;
    case 'money':
      return <MoneyPanel d={d} />;
    case 'agents':
      return <AgentsPanel d={d} onPin={onPin} />;
    case 'events':
      return <EventsPanel d={d} />;
    case 'settings':
      return <SettingsPanel d={d} />;
    case 'report':
      return <ReportPanel d={d} />;
  }
}

function PanelHead({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="tdm116-head">
      <div className="tdm116-head-title">
        <h2>{title}</h2>
      </div>
      {children && <div className="tdm116-follow">{children}</div>}
    </div>
  );
}

export function Tdm116PanelFrame({ title, aside, children, testId }: { title: string; aside?: React.ReactNode; children: React.ReactNode; testId?: string }) {
  return (
    <>
      <PanelHead title={title}>{aside}</PanelHead>
      <div className="tdm116-stage" data-testid={testId}>
        {children}
      </div>
    </>
  );
}

// ------------------------------------------------------------------------------ Robot

function RobotPanel({ d }: { d: Tdm116Data }) {
  const r = d.robot;
  const floor = d.floor;
  return (
    <Tdm116PanelFrame title="Robot" testId="panel-robot" aside={r ? <span className="gx-id">{r.name} · {r.status}</span> : undefined}>
      <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(280px, 1fr)' }}>
        <div className="tdm116-col">
          <Tdm116RobotWell d={d} />
        </div>
        <div className="tdm116-col tdm116-scroll">
          <section className="tdm116-plate is-tight" aria-label="Robot">
            <p className="gx-label m-0 mb-2">This ticket&apos;s robot</p>
            {r ? (
              <dl className="tdm116-kv">
                <dt>Name</dt>
                <dd>{r.name}</dd>
                <dt>Embodiment</dt>
                <dd>{r.embodiment}</dd>
                <dt>Status</dt>
                <dd>{r.status}</dd>
                <dt>Mode</dt>
                <dd>{d.mode === 'sim' ? 'sim' : d.mode === 'real' ? 'real arm' : '—'}</dd>
                <dt>Heartbeat</dt>
                <dd>{r.lastHeartbeat ? fmtTs(r.lastHeartbeat) : 'never'}</dd>
              </dl>
            ) : (
              <p className="gx-small m-0">No robot assigned.</p>
            )}
            {r && (
              <div className="mt-3">
                <ButtonLink href={`/robots/${r.id}`} small>
                  Robot page
                </ButtonLink>
              </div>
            )}
          </section>
          {floor?.holder && (
            <section className="tdm116-plate is-tight" aria-label="Holder">
              <p className="gx-label m-0 mb-1">Holding the robot</p>
              <p className="m-0 text-sm">{floor.holder.words}</p>
            </section>
          )}
          {floor && (
            <section className="tdm116-plate is-tight" aria-label="Robot queue">
              <p className="gx-label m-0 mb-1">Queue</p>
              <RobotQueueList queue={floor.robotQueue} />
            </section>
          )}
          {floor?.waitingFor && <WaitingForRobot floor={floor} />}
        </div>
      </div>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Data

function DataPanel({ d }: { d: Tdm116Data }) {
  const ds = d.dataset;
  const plans = [...d.plans].sort((a, b) => b.iteration - a.iteration || Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
  return (
    <Tdm116PanelFrame title="Data" testId="panel-data" aside={<span className="gx-id">{d.episodes.length} episodes</span>}>
      <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(260px, 340px) minmax(0, 1fr)' }}>
        <div className="tdm116-col tdm116-scroll">
          <section className="tdm116-plate is-tight" aria-label="Training set">
            <p className="gx-label m-0 mb-2">Training set</p>
            <div className="tdm116-big">
              {ds?.acceptedCount ?? 0}
              <small>accepted of {ds?.totalCount ?? 0}</small>
            </div>
            {d.settings?.targetEpisodes ? <p className="gx-small mt-1 mb-0">Trains at {d.settings.targetEpisodes} accepted.</p> : null}
            {ds?.lerobotUri && (
              <p className="gx-id mt-2 mb-0 break-all" title={ds.lerobotUri}>
                {shortUri(ds.lerobotUri, 3)}
              </p>
            )}
          </section>
          <section className="tdm116-plate is-tight" aria-label="Data plans">
            <p className="gx-label m-0 mb-1">Data plans</p>
            {plans.length === 0 ? (
              <p className="gx-small m-0">None yet.</p>
            ) : (
              <ul className="tdm116-rows">
                {plans.map((p) => (
                  <li key={p.id.toString()}>
                    <div className="tdm116-row-top">
                      <span className="tdm116-row-main text-sm">
                        {p.amount} {(DATA_TYPE_LABEL[p.dataType] ?? p.dataType).toLowerCase()}
                      </span>
                      <span className="tdm116-money">{fmtCents(p.estCostCents)}</span>
                    </div>
                    <span className="gx-small">
                      iteration {p.iteration} · {p.status}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="tdm116-plate is-tight" aria-label="Dataset offers">
            <p className="gx-label m-0 mb-1">Dataset offers</p>
            {d.offers.length === 0 ? <p className="gx-small m-0">No offers on this ticket.</p> : <OfferCard offers={d.offers} people={d.people} />}
          </section>
        </div>
        <div className="tdm116-scroll" id="episodes">
          <EpisodesGrid episodes={d.episodes} reviews={d.reviews} payments={d.payments} people={d.people} simRobotIds={d.simRobotIds} />
        </div>
      </div>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Money

function MoneyPanel({ d }: { d: Tdm116Data }) {
  return (
    <Tdm116PanelFrame title="Money" testId="panel-money" aside={<TestModeTag />}>
      <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)' }}>
        <div className="tdm116-scroll">
          <TicketSpendPanel ticketId={d.tid} />
        </div>
        <section className="tdm116-plate tdm116-scroll" aria-label="Payouts">
          <div className="tdm116-plate-head">
            <h3>Payouts</h3>
            <ButtonLink href={`/tickets/${d.tid}/spend`} small>
              Spend ledger
            </ButtonLink>
          </div>
          <PaymentsTable payments={d.payments} people={d.people} />
        </section>
      </div>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Agents

function AgentsPanel({ d, onPin }: { d: Tdm116Data; onPin: (k: Tdm116StageKey) => void }) {
  const [view, setView] = useState<'cell' | 'line'>('cell');
  return (
    <Tdm116PanelFrame
      title="Agents"
      testId="panel-agents"
      aside={
        <span className="inline-flex" role="tablist" aria-label="Agents view">
          <button type="button" role="tab" aria-selected={view === 'cell'} className={`gx-btn gx-btn-sm ${view === 'cell' ? '' : 'gx-btn-ghost'}`} onClick={() => setView('cell')}>
            Factory cell
          </button>
          <button type="button" role="tab" aria-selected={view === 'line'} className={`gx-btn gx-btn-sm ${view === 'line' ? '' : 'gx-btn-ghost'}`} onClick={() => setView('line')}>
            Line and droids
          </button>
        </span>
      }
    >
      <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 1.5fr) minmax(320px, 1fr)' }}>
        <div className="tdm116-col tdm116-scroll">
          {view === 'cell' ? (
            <FactoryIso ticketId={Number(d.tid)} compact onStationClick={(i: number) => onPin(TDM116_STAGE_KEYS[i] ?? 'baseline')} />
          ) : (
            <section className="gx-plate" aria-label="Factory floor">
              <FactoryFloor ticketId={d.tid} />
            </section>
          )}
          <TicketAgentsPanel ticketId={d.tid.toString()} />
        </div>
        <section className="tdm116-plate tdm116-scroll" aria-label="Decision trail">
          <div className="tdm116-plate-head">
            <h3>Decision trail</h3>
            <span className="gx-small">What failed, what we bought, why</span>
          </div>
          <DecisionTrail ticketId={d.tid.toString()} refreshKey={d.events.length} />
        </section>
      </div>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Events

function EventsPanel({ d }: { d: Tdm116Data }) {
  const [q, setQ] = useState('');
  const rows = useMemo(() => {
    const all = [...d.events].reverse();
    const needle = q.trim().toLowerCase();
    return needle ? all.filter((e) => e.row.type.toLowerCase().includes(needle) || e.row.actor.toLowerCase().includes(needle)) : all;
  }, [d.events, q]);
  return (
    <Tdm116PanelFrame
      title="Events"
      testId="panel-events"
      aside={
        <>
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter by type or actor"
            aria-label="Filter events"
            className="h-8 w-56 rounded-sm border border-line-strong bg-surface-sunken px-2 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-arc"
          />
          <span className="gx-id">{rows.length} rows · append-only</span>
        </>
      }
    >
      <section className="tdm116-plate tdm116-scroll" style={{ padding: 0 }} aria-label="Ticket events">
        <table className="tdm116-table">
          <thead className="sticky top-0 bg-surface">
            <tr>
              <th className="pl-3 pt-2">#</th>
              <th className="pt-2">Time</th>
              <th className="pt-2">Type</th>
              <th className="pt-2">Actor</th>
              <th className="pt-2">What happened</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.row.id.toString()}>
                <td className="pl-3 text-ink-muted">{e.row.id.toString()}</td>
                <td className="whitespace-nowrap">{e.row.ts.toDate().toLocaleTimeString()}</td>
                <td className="whitespace-nowrap">{e.row.type}</td>
                <td className="whitespace-nowrap text-ink-muted">{e.row.actor}</td>
                <td style={{ fontFamily: 'var(--font-sans)' }}>{tdm112Words({ type: e.row.type, payload: e.payload })}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="gx-small m-3">No events{q ? ' match' : ' yet'}.</p>}
      </section>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Settings

function SettingsPanel({ d }: { d: Tdm116Data }) {
  const s = d.settings;
  const hp = parseObj(s?.hyperparamsJson) ?? {};
  return (
    <Tdm116PanelFrame title="Settings" testId="panel-settings">
      <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(300px, 420px) minmax(0, 1fr)' }}>
        <div className="tdm116-col tdm116-scroll">
          <section className="tdm116-plate" aria-label="Run settings">
            <div className="tdm116-plate-head">
              <h3>Run settings</h3>
              <RunSettingsButton ticketId={d.tid.toString()} loop={d.loop} liveUpdatedAtMicros={s?.updatedAt.microsSinceUnixEpoch} />
            </div>
            {s ? (
              <dl className="tdm116-kv">
                <dt>Eval trials</dt>
                <dd>{s.evalTrials}</dd>
                <dt>Target</dt>
                <dd>
                  {pct(s.successThreshold)} on {s.requireCiLow ? 'CI lower bound' : 'point estimate'}
                </dd>
                <dt>Budget</dt>
                <dd className="tdm116-money">{fmtCents(s.budgetCents)}</dd>
                <dt>Episodes to train</dt>
                <dd>{s.targetEpisodes}</dd>
                <dt>Max iterations</dt>
                <dd>{s.maxIterations}</dd>
                <dt>Data type</dt>
                <dd>{s.dataTypeOverride ? DATA_TYPE_LABEL[s.dataTypeOverride] ?? s.dataTypeOverride : 'planner decides'}</dd>
                {Object.entries(hp)
                  .filter(([k]) => k !== 'prompt')
                  .slice(0, 8)
                  .map(([k, v]) => (
                    <div key={k} className="contents">
                      <dt>{k.replace(/_/g, ' ')}</dt>
                      <dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd>
                    </div>
                  ))}
                <dt>Updated</dt>
                <dd>{fmtTs(s.updatedAt)}</dd>
              </dl>
            ) : (
              <p className="gx-small m-0">No run settings row yet.</p>
            )}
          </section>
          <Tdm125CollectPicker ticketId={d.tid} split={d.split} robots={d.allRobots} frozen={['shipped', 'failed', 'cancelled'].includes(d.ticket?.status ?? '')} />
          <Tdm116CancelBlock ticketId={Number(d.tid)} status={d.ticket?.status ?? 'created'} />
        </div>
        <div className="tdm116-scroll">
          <Tdm104ScenePanel ticketId={d.tid.toString()} />
        </div>
      </div>
    </Tdm116PanelFrame>
  );
}

// ------------------------------------------------------------------------------ Report

function ReportPanel({ d }: { d: Tdm116Data }) {
  const t = d.ticket!;
  return (
    <Tdm116PanelFrame
      title="Report"
      testId="panel-report"
      aside={
        <ButtonLink href={`/tickets/${t.id}/report`} small>
          Open the full report
        </ButtonLink>
      }
    >
      <section className="tdm116-plate tdm116-scroll" aria-label="Results">
        <div className="tdm116-plate-head">
          <h3>Before and after</h3>
          <span className="gx-id">Wilson 95% · live as trials finish</span>
        </div>
        <EvalCards runs={d.evalRuns} events={d.events} settings={d.settings} robotFake={d.robotFake} iterationOf={d.iterationOf} simRobotIds={d.simRobotIds} mode={d.mode} />
        {d.split.line && (
          <p className="mt-3 mb-0 text-sm font-semibold" data-testid="tdm125-report-split">
            {d.split.line}
          </p>
        )}
        <p className="gx-small mt-4 mb-0">
          The full report prints and shares without a login.
        </p>
      </section>
    </Tdm116PanelFrame>
  );
}
