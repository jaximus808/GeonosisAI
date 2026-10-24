import type { ReactNode } from 'react';
import { gxCx } from './cx';

export type GxTone = 'steel' | 'running' | 'shipped' | 'failed' | 'money' | 'cancelled';

/** The brand's bullet: a 6px square rivet in currentColor. Never round. */
export function Rivet({ className }: { className?: string }) {
  return <i className={gxCx('gx-rivet', className)} aria-hidden />;
}

/** A stamped tag with a rivet. The text always names the state, so it reads without colour. */
export function Tag({ tone = 'steel', children, className, title }: { tone?: GxTone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span
      className={gxCx(
        'gx-tag',
        tone === 'running' && 'gx-tag-running',
        tone === 'shipped' && 'gx-tag-shipped',
        tone === 'failed' && 'gx-tag-failed',
        tone === 'money' && 'gx-tag-money',
        tone === 'cancelled' && 'gx-tag-cancelled',
        className,
      )}
      title={title}
    >
      <Rivet />
      {children}
    </span>
  );
}

/** ticket.status -> station label and tag tone (the "words of the line" table in the brand book). */
export const GX_STATION_OF_STATUS: Record<string, { label: string; tone: GxTone }> = {
  created: { label: 'Queued', tone: 'steel' },
  onboarding: { label: 'Queued', tone: 'steel' },
  onboard: { label: 'Queued', tone: 'steel' },
  baseline: { label: 'Baseline', tone: 'running' },
  diagnose: { label: 'Diagnose', tone: 'running' },
  plan_data: { label: 'Plan data', tone: 'running' },
  dispatch: { label: 'Collect', tone: 'running' },
  review: { label: 'Review', tone: 'running' },
  train: { label: 'Train', tone: 'running' },
  eval: { label: 'Eval', tone: 'running' },
  shipped: { label: 'Shipped', tone: 'shipped' },
  failed: { label: 'Failed', tone: 'failed' },
  cancelled: { label: 'Cancelled', tone: 'cancelled' },
};

export function gxStationOf(status: string): { label: string; tone: GxTone } {
  return GX_STATION_OF_STATUS[status] ?? { label: status.replace(/_/g, ' '), tone: 'steel' };
}

/** Status tag for a ticket: pass `ticket.status`. */
export function StatusTag({ status, className }: { status: string; className?: string }) {
  const s = gxStationOf(status);
  return (
    <Tag tone={s.tone} className={className} title={`ticket.status = ${status}`}>
      {s.label}
    </Tag>
  );
}

/** The SIMULATED marker for sim robots and their views: a steel stencil tag. */
export function SimTag({ label = 'Simulated', className, title }: { label?: string; className?: string; title?: string }) {
  return (
    <span className={gxCx('gx-tag gx-tag-sim', className)} title={title ?? 'Simulated robot, not real hardware.'} data-gx-sim>
      <Rivet />
      {label}
    </span>
  );
}
