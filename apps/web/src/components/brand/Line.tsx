import type { CSSProperties } from 'react';
import { gxCx } from './cx';
import { Tag } from './StatusTag';

export type GxStationState = 'done' | 'current' | 'failed' | 'upcoming';
export type GxStation = { key: string; name: string; state: GxStationState; note?: string };

/** The stations of the line, in order, keyed by the `ticket.status` that runs them. */
export const GX_LINE_STATIONS: ReadonlyArray<{ key: string; name: string }> = [
  { key: 'baseline', name: 'Baseline' },
  { key: 'diagnose', name: 'Diagnose' },
  { key: 'plan_data', name: 'Plan data' },
  { key: 'dispatch', name: 'Collect' },
  { key: 'review', name: 'Review' },
  { key: 'train', name: 'Train' },
  { key: 'eval', name: 'Eval' },
  { key: 'shipped', name: 'Ship' },
];

/**
 * Map a ticket.status onto the line. Stations before the running one are done, the running one is
 * current, later ones upcoming. `shipped` marks every station done. On `failed`, pass `stoppedAt`
 * (the status the ticket failed in) to mark that station slag. An eval loop-back simply moves the
 * current station back: later stations read as upcoming again (never a second line).
 */
export function gxTicketStations(
  status: string,
  opts: { notes?: Partial<Record<string, string>>; stoppedAt?: string } = {},
): GxStation[] {
  const idx = GX_LINE_STATIONS.findIndex((s) => s.key === status);
  const stopIdx = opts.stoppedAt ? GX_LINE_STATIONS.findIndex((s) => s.key === opts.stoppedAt) : -1;
  return GX_LINE_STATIONS.map((s, i) => {
    let state: GxStationState = 'upcoming';
    if (status === 'shipped') state = 'done';
    else if (status === 'failed' || status === 'cancelled') {
      if (stopIdx >= 0 && i < stopIdx) state = 'done';
      else if (stopIdx === i) state = status === 'failed' ? 'failed' : 'upcoming';
    } else if (idx >= 0) state = i < idx ? 'done' : i === idx ? 'current' : 'upcoming';
    return { key: s.key, name: s.name, state, note: opts.notes?.[s.key] };
  });
}

/**
 * TDM-109: the line, the brand's signature element. Stations as square rivets on a track: done filled
 * `ink`, running `melt` (pulses every 2 s unless reduced motion), upcoming hollow `steel`, failed `slag`.
 * On phones it collapses to the running station's tag and a step count ("Eval · 7 of 8").
 */
export function Line({ stations, className, compactOnPhone = true }: { stations: GxStation[]; className?: string; compactOnPhone?: boolean }) {
  const n = stations.length;
  const curIdx = stations.findIndex((s) => s.state === 'current' || s.state === 'failed');
  const doneCount = stations.filter((s) => s.state === 'done').length;
  const cur = curIdx >= 0 ? stations[curIdx] : undefined;
  return (
    <div className={className} data-testid="gx-line">
      {compactOnPhone && (
        <div className="flex items-center gap-2 sm:hidden">
          {cur ? (
            <Tag tone={cur.state === 'failed' ? 'failed' : 'running'}>{cur.name}</Tag>
          ) : (
            <Tag tone={doneCount === n ? 'shipped' : 'steel'}>{doneCount === n ? 'Shipped' : 'Queued'}</Tag>
          )}
          <span className="gx-id">
            {cur ? `${cur.name} · ${curIdx + 1} of ${n}` : `${doneCount} of ${n} stations done`}
          </span>
          {cur?.note && <span className="gx-id">· {cur.note}</span>}
        </div>
      )}
      <div className={gxCx('gx-line-scroll', compactOnPhone && 'hidden sm:block')}>
        <ol className="gx-line" style={{ '--stations': n } as CSSProperties}>
          {stations.map((s) => (
            <li
              key={s.key}
              className={gxCx(
                'gx-station',
                s.state === 'done' && 'is-done',
                s.state === 'current' && 'is-current',
                s.state === 'failed' && 'is-failed',
              )}
              aria-current={s.state === 'current' ? 'step' : undefined}
              data-station={s.key}
              data-state={s.state}
            >
              <span className="gx-station-name">{s.name}</span>
              <span className="gx-station-note">{s.note ?? ' '}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
