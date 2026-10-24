import type { ReactNode } from 'react';
import { gxCx } from './cx';

/** Wilson score 95% interval for k successes in n trials, as fractions 0..1. */
export function gxWilson95(k: number, n: number): [number, number] {
  if (n <= 0) return [0, 1];
  const z = 1.96;
  const p = k / n;
  const z2 = z * z;
  const denom = 1 + z2 / n;
  const centre = p + z2 / (2 * n);
  const half = z * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n));
  return [Math.max(0, (centre - half) / denom), Math.min(1, (centre + half) / denom)];
}

const pct = (x: number) => `${Math.round(x * 100)}%`;
const clamp01 = (x: number) => Math.max(0, Math.min(1, x));

export type GxTrialChip = { ok: boolean; title?: string; href?: string; onClick?: () => void };

/**
 * TDM-109: an eval result. Big mono percentage with the count, a track with the Wilson 95% band
 * (steel; arc only when it passes), the point in ink and the target as a dashed melt tick, then one
 * P/F chip per trial. Always shows the denominator and the interval.
 */
export function Readout({
  label,
  successes,
  trials,
  target,
  lo,
  hi,
  pass,
  trialChips,
  footer,
  pending,
  className,
  testId,
}: {
  label: ReactNode;
  successes: number;
  trials: number;
  /** Target success rate, 0..1. */
  target?: number;
  /** Wilson bounds 0..1; computed from successes/trials when absent. */
  lo?: number;
  hi?: number;
  /** Override the pass rule (default: lower bound clears the target). */
  pass?: boolean;
  trialChips?: GxTrialChip[];
  footer?: ReactNode;
  /** No trials yet: show the empty track and a dash. */
  pending?: boolean;
  className?: string;
  testId?: string;
}) {
  const [wlo, whi] = gxWilson95(successes, trials);
  const l = clamp01(lo ?? wlo);
  const h = clamp01(hi ?? whi);
  const p = trials > 0 ? successes / trials : 0;
  const passes = pass ?? (target != null && trials > 0 && l >= target);
  const empty = pending || trials === 0;
  return (
    <div className={gxCx('gx-readout', className)} data-testid={testId}>
      <span className="gx-label">{label}</span>
      <div className="gx-readout-num">
        {empty ? '—' : pct(p)}
        <small>
          {successes}/{trials}
        </small>
      </div>
      <div className="gx-track" role="img" aria-label={empty ? 'No trials yet' : `${pct(p)}, 95% interval ${pct(l)} to ${pct(h)}${target != null ? `, target ${pct(target)}` : ''}`}>
        {!empty && <div className={gxCx('gx-track-ci', passes && 'is-pass')} style={{ left: pct(l), width: pct(h - l) }} />}
        {!empty && <div className="gx-track-point" style={{ left: pct(p) }} />}
        {target != null && <div className="gx-track-target" style={{ left: pct(target) }} />}
      </div>
      <span className="gx-id">
        {empty ? 'No trials yet' : `${pct(l)}–${pct(h)}, n=${trials}`}
        {target != null && ` · target ${pct(target)}`}
      </span>
      {trialChips && trialChips.length > 0 && (
        <div className="gx-trials">
          {trialChips.map((t, i) => {
            const cls = gxCx('gx-trial', t.ok ? 'is-pass' : 'is-fail');
            const txt = t.ok ? 'P' : 'F';
            const title = t.title ?? `Trial ${i + 1}: ${t.ok ? 'Pass' : 'Fail'}`;
            if (t.href)
              return (
                <a key={i} href={t.href} className={cls} title={title}>
                  {txt}
                </a>
              );
            if (t.onClick)
              return (
                <button key={i} type="button" onClick={t.onClick} className={gxCx(cls, 'cursor-pointer border-0 p-0')} title={title}>
                  {txt}
                </button>
              );
            return (
              <span key={i} className={cls} title={title}>
                {txt}
              </span>
            );
          })}
        </div>
      )}
      {footer}
    </div>
  );
}
