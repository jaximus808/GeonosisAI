'use client';

import { useMemo } from 'react';
import { Rivet, StatusTag, Tag } from '@/components/brand/StatusTag';
import { useConnectionStatus, useRows } from '@/lib/spacetime/hooks';
import { fmtCents } from '@/lib/format';
import { tdm111Stats } from '@/lib/tdm111/landing';

/**
 * TDM-111: the landing page's live numbers, read-only from the anonymous SpacetimeDB subscription (the same
 * one every page uses). A number appears only once its table has synced; nothing is filled in for it.
 */
export function LandingLiveStrip() {
  const conn = useConnectionStatus();
  const [robots, robotsReady] = useRows('robot');
  const [tasks, tasksReady] = useRows('task');
  const [tickets, ticketsReady] = useRows('ticket');
  const s = useMemo(
    () => tdm111Stats(robotsReady ? robots : null, tasksReady ? tasks : null, ticketsReady ? tickets : null),
    [robots, robotsReady, tasks, tasksReady, tickets, ticketsReady],
  );
  const any = s.robotsOnline || s.openTasks || s.shipped != null || s.latest;

  return (
    <section aria-label="Live from the foundry" className="gx-plate" data-testid="tdm111-live">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="gx-heading">On the line now</h2>
        <span
          className={`gx-live ${conn.status === 'connected' ? '' : conn.status === 'connecting' ? 'is-connecting' : 'is-down'}`}
          data-testid="tdm111-live-status"
        >
          <Rivet />
          {conn.status === 'connected' ? 'Live from SpacetimeDB' : conn.status === 'connecting' ? 'Connecting' : 'Live data unavailable'}
        </span>
      </div>
      {!any ? (
        <p className="gx-small">
          {conn.status === 'disconnected' ? 'The live numbers could not load here.' : 'Loading the live numbers.'}
        </p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-5 lg:grid-cols-4">
          {s.robotsOnline && (
            <div data-testid="tdm111-robots">
              <dt className="gx-label">Robots online</dt>
              <dd className="gx-readout-num">{s.robotsOnline.total}</dd>
              <dd className="gx-id mt-1 flex flex-wrap gap-2">
                {s.robotsOnline.real > 0 && <Tag tone="shipped">{s.robotsOnline.real} real</Tag>}
                {s.robotsOnline.sim > 0 && <Tag>{s.robotsOnline.sim} sim</Tag>}
                {s.robotsOnline.total === 0 && 'None right now'}
              </dd>
            </div>
          )}
          {s.openTasks && (
            <div data-testid="tdm111-tasks">
              <dt className="gx-label">Open paid tasks</dt>
              <dd className="gx-readout-num">{s.openTasks.count}</dd>
              <dd className="gx-id mt-1">
                <span className="text-ochre">{fmtCents(s.openTasks.payCents)}</span> total · test mode
              </dd>
            </div>
          )}
          {s.shipped != null && (
            <div data-testid="tdm111-shipped">
              <dt className="gx-label">Tickets shipped</dt>
              <dd className="gx-readout-num">{s.shipped}</dd>
              <dd className="gx-id mt-1">{tickets.length} filed</dd>
            </div>
          )}
          {s.latest && (
            <div data-testid="tdm111-latest">
              <dt className="gx-label">Latest ticket</dt>
              <dd className="gx-readout-num">#{s.latest.id}</dd>
              <dd className="mt-1">
                <StatusTag status={s.latest.status} />
              </dd>
            </div>
          )}
        </dl>
      )}
    </section>
  );
}
