'use client';

// TDM-116: /dashboard as the same app shell. Robots with their current job across the top, the tickets
// down the left (status, the line in miniature, spend), and the selected ticket's cockpit on the right.
// `?ticket=N` selects one; without it, the newest ticket still on the line.

import Link from 'next/link';
import { useCallback, useEffect, useMemo } from 'react';
import { ButtonLink } from '@/components/brand/Button';
import { gxTicketStations } from '@/components/brand/Line';
import { StatusTag, Tag, gxStationOf, type GxTone } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents } from '@/lib/format';
import { TDM101_ENDED, tdm101ParseTicketParam, tdm101PickTicket } from '@/lib/tdm101/live';
import { tdm107IsSimRobot } from '@/lib/tdm107/robotMode';
import { TDM116_PREF_KEYS, tdm116IsListToggleKey, tdm116ListTickets, tdm116ParseFlag } from '@/lib/tdm116/cockpit';
import { useTdm116Stored } from './hooks';
import { TicketCockpit } from './TicketCockpit';
import './cockpit.css';

const ROBOT_TONE: Record<string, GxTone> = { busy: 'running', online: 'shipped', paused: 'money', estop: 'failed', offline: 'steel' };
const USES_ROBOT = ['baseline', 'eval', 'dispatch'];
const tkt = (id: bigint) => `TKT-${id.toString().padStart(4, '0')}`;
const STAGE_WORD: Record<string, string> = {
  created: 'queued', onboarding: 'queued', onboard: 'queued', baseline: 'baseline eval', diagnose: 'diagnose', plan_data: 'plan data',
  dispatch: 'collect', review: 'review', train: 'train', eval: 'eval',
};

export function Tdm116DashboardShell({ ticketParam }: { ticketParam?: string | null }) {
  const [tickets, ready] = useRows('ticket');
  const [robots] = useRows('robot');
  const pinned = tdm101ParseTicketParam(ticketParam ?? null);
  const pick = useMemo(() => tdm101PickTicket(tickets, pinned), [tickets, pinned]);
  const sorted = useMemo(() => [...tickets].sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch) || Number(b.id - a.id)), [tickets]);
  const selected = pinned ?? pick.ticket?.id ?? null;
  const robotName = useMemo(() => new Map(robots.map((r) => [r.id, r.name])), [robots]);
  const visibleRobots = useMemo(() => robots.filter((r) => !r.name.startsWith('test.')).sort((a, b) => Number(a.id - b.id)), [robots]);

  // The ticket list collapses to a 44 px rail (chevron or `[`) and hides cancelled/test tickets by
  // default. Both are per-viewer prefs in localStorage. Below 900 px the list stays the stacked drawer.
  const [collapsedRaw, setCollapsedRaw] = useTdm116Stored(TDM116_PREF_KEYS.ticketsCollapsed);
  const [hideRaw, setHideRaw] = useTdm116Stored(TDM116_PREF_KEYS.hideCancelled);
  const collapsed = tdm116ParseFlag(collapsedRaw, false);
  const hide = tdm116ParseFlag(hideRaw, true);
  const toggleList = useCallback(() => setCollapsedRaw(collapsed ? '0' : '1'), [collapsed, setCollapsedRaw]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!tdm116IsListToggleKey(e)) return;
      e.preventDefault();
      toggleList();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggleList]);
  const list = useMemo(() => tdm116ListTickets(sorted, hide, selected), [sorted, hide, selected]);
  const current = sorted.find((t) => t.id === selected);

  return (
    <div className="tdm116-dash tdm116-fill" data-testid="dashboard-shell" data-collapsed={collapsed || undefined}>
      <div className="tdm116-robots" aria-label="Robots">
        <span className="gx-station-label text-ink-muted">Robots</span>
        {visibleRobots.length === 0 && <span className="gx-small">No robots registered. Run the robot-client to put one on the floor.</span>}
        {visibleRobots.map((r) => {
          const active = tickets.filter((t) => t.robotId === r.id && !TDM101_ENDED.has(t.status));
          const job = active.find((t) => USES_ROBOT.includes(t.status)) ?? active[0];
          return (
            <Link key={r.id.toString()} href={job ? `/dashboard?ticket=${job.id}` : `/robots/${r.id}`} className="tdm116-robot-chip no-underline" title={`${r.embodiment}, ${r.status}`}>
              <Tag tone={ROBOT_TONE[r.status] ?? 'steel'}>{r.status}</Tag>
              <span className="gx-id">{r.name}</span>
              {tdm107IsSimRobot(r) && <span className="gx-id">SIM</span>}
              <span className="gx-small">
                {job ? (
                  <>
                    TKT-{job.id.toString().padStart(4, '0')} · {STAGE_WORD[job.status] ?? job.status}
                    {active.length > 1 ? ` · +${active.length - 1} waiting` : ''}
                  </>
                ) : (
                  'no job'
                )}
              </span>
            </Link>
          );
        })}
      </div>

      {collapsed && (
        <button
          type="button"
          className="tdm116-tickets-rail"
          onClick={toggleList}
          aria-expanded={false}
          aria-controls="tdm116-tickets"
          aria-label={`Show tickets${current ? ` (showing ${tkt(current.id)}, ${gxStationOf(current.status).label})` : ''}. Shortcut [`}
          title="Show tickets  ["
          data-testid="dashboard-tickets-rail"
        >
          <span className="tdm116-chev" aria-hidden>
            ›
          </span>
          {current && (
            <>
              <i className={`gx-rivet tdm116-rail-rivet is-${gxStationOf(current.status).tone}`} aria-hidden />
              <span className="tdm116-rail-id gx-id">{tkt(current.id)}</span>
            </>
          )}
        </button>
      )}

      <aside className="tdm116-tickets" id="tdm116-tickets" aria-label="Tickets">
        <div className="tdm116-tickets-head">
          <button
            type="button"
            className="tdm116-collapse"
            onClick={toggleList}
            aria-expanded
            aria-controls="tdm116-tickets"
            aria-label="Hide tickets. Shortcut ["
            title="Hide tickets  ["
            data-testid="dashboard-tickets-collapse"
          >
            <span aria-hidden>‹</span>
          </button>
          <h1>Tickets</h1>
          <ButtonLink href="/tickets/new" variant={selected ? 'default' : 'primary'} small>
            File a ticket
          </ButtonLink>
        </div>
        <label className="tdm116-tickets-filter" data-testid="dashboard-hide-cancelled">
          <input type="checkbox" checked={hide} onChange={(e) => setHideRaw(e.target.checked ? '1' : '0')} />
          Hide cancelled and test
          {hide && list.hidden > 0 && <span className="gx-id ml-auto">{list.hidden} hidden</span>}
        </label>
        {!ready ? (
          <p className="gx-small m-4">Subscribing to tickets…</p>
        ) : sorted.length === 0 ? (
          <p className="gx-small m-4">No tickets yet. File one to put a robot on the line.</p>
        ) : list.shown.length === 0 ? (
          <p className="gx-small m-4">Every ticket here is cancelled or a test. Untick the filter to see them.</p>
        ) : (
          <ul data-testid="dashboard-tickets">
            {list.shown.map((t) => {
              const stations = gxTicketStations(t.status);
              const budget = Number(t.budgetCents);
              return (
                <li key={t.id.toString()}>
                  <Link href={`/dashboard?ticket=${t.id}`} className="tdm116-tkt" aria-current={t.id === selected ? 'page' : undefined} scroll={false}>
                    <span className="flex items-center gap-2">
                      <span className="gx-id shrink-0 whitespace-nowrap text-ink">TKT-{t.id.toString().padStart(4, '0')}</span>
                      <StatusTag status={t.status} />
                      <span className="tdm116-money ml-auto whitespace-nowrap text-xs">
                        {fmtCents(t.spentCents)}
                        <span className="text-ink-muted"> / {fmtCents(budget)}</span>
                      </span>
                    </span>
                    <span className="tdm116-tkt-title">{t.taskDescription}</span>
                    <span className="tdm116-mini" aria-hidden>
                      {stations.map((s) => (
                        <i key={s.key} className={gxCx(s.state === 'done' && 'is-done', s.state === 'current' && 'is-working', s.state === 'failed' && 'is-failed')} />
                      ))}
                    </span>
                    <span className="gx-id">{robotName.get(t.robotId) ?? `robot #${t.robotId}`}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      <div className="tdm116-dash-main">
        {selected ? (
          <TicketCockpit key={selected.toString()} id={selected.toString()} embedded />
        ) : (
          <div className="tdm116-empty m-6">
            <strong>{ready ? 'No ticket selected' : 'Connecting to the foundry'}</strong>
            {ready ? 'Pick a ticket on the left, or file one to put a robot on the line.' : ''}
          </div>
        )}
      </div>
    </div>
  );
}
