// TDM-75 small labels for the ticket page.
import type { ReactNode } from 'react';

/** Rows written by the fake-robot harness (TDM-28). TDM-109: a dashed slag stencil tag, never colour alone. */
export function FakeBadge({ title }: { title?: string }) {
  return (
    <span
      title={title ?? 'Written by the fake-robot harness, not the real arm. Do not read these numbers as results.'}
      className="gx-tag gx-tag-sim"
      style={{ background: 'var(--slag-soft)', color: 'var(--slag)', boxShadow: 'inset 0 0 0 1px var(--slag)' }}
    >
      <i className="gx-rivet" aria-hidden />
      FAKE
    </span>
  );
}

/** TDM-107: recorded on the MuJoCo sim robot, never a real-arm result. TDM-109: steel stencil tag. */
export function SimBadge({ title }: { title?: string }) {
  return (
    <span
      title={title ?? 'Recorded on the SIM robot (MuJoCo simulation of the YAM). Never counted in real-robot numbers.'}
      className="gx-tag gx-tag-sim"
      data-gx-sim
    >
      <i className="gx-rivet" aria-hidden />
      SIM
    </span>
  );
}

/** Stripe test mode marker: no real money moves. TDM-109: ochre money tag. */
export function TestModeBadge() {
  return (
    <span title="Stripe test mode. No real money moves." className="gx-tag gx-tag-money">
      <i className="gx-rivet" aria-hidden />
      Test mode
    </span>
  );
}

// TDM-109: brand tag tones. good = arc (pass, shipped), bad = slag, busy = arc-live, wait = steel, warn = ochre.
// Melt is kept for the running station of the line, so busy rows here do not compete with it.
const TONE: Record<string, string> = {
  good: 'gx-tag-shipped',
  bad: 'gx-tag-failed',
  busy: 'gx-tag-busy',
  wait: '',
  warn: 'gx-tag-money',
};

const STATUS_TONE: Record<string, keyof typeof TONE> = {
  done: 'good', paid: 'good', completed: 'good', accepted: 'good', shipped: 'good', approved: 'good',
  failed: 'bad', rejected: 'bad', reversed: 'bad', blocked_budget: 'bad', cancelled: 'bad', expired: 'bad',
  running: 'busy', claimed: 'busy', executing: 'busy', ingesting: 'busy', qa: 'busy', submitted: 'busy',
  pending: 'wait', queued: 'wait', open: 'wait', proposed: 'wait', not_reviewed: 'wait',
  awaiting_onboarding: 'warn',
};

export function StatusPill({ status, tone }: { status: string; tone?: keyof typeof TONE }) {
  const t = tone ?? STATUS_TONE[status] ?? 'wait';
  return (
    <span className={`gx-tag ${TONE[t]}`} data-tone={t}>
      <i className="gx-rivet" aria-hidden />
      {status.replace(/_/g, ' ')}
    </span>
  );
}

export function Block({ title, aside, children, id }: { title: string; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="gx-plate mb-6 scroll-mt-6">
      <div className="gx-plate-head flex-wrap">
        <h2 className="gx-heading">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-sm border border-dashed border-line-strong px-4 py-3 text-sm text-ink-muted">{children}</p>
  );
}
