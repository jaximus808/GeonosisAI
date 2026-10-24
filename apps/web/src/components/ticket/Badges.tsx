// TDM-75 small labels for the ticket page.
import type { ReactNode } from 'react';

/** Rows written by the fake-robot harness (TDM-28). Magenta like its placeholder video. */
export function FakeBadge({ title }: { title?: string }) {
  return (
    <span
      title={title ?? 'Written by the fake-robot harness, not the real arm. Do not read these numbers as results.'}
      className="inline-flex items-center rounded-sm bg-fuchsia-600 px-1.5 py-px text-[11px] font-bold tracking-wide text-white"
    >
      FAKE
    </span>
  );
}

/** TDM-107: recorded on the MuJoCo sim robot, never a real-arm result. */
export function SimBadge({ title }: { title?: string }) {
  return (
    <span
      title={title ?? 'Recorded on the SIM robot (MuJoCo simulation of the YAM). Never counted in real-robot numbers.'}
      className="inline-flex items-center rounded-sm bg-violet-600 px-1.5 py-px text-[11px] font-bold tracking-wide text-white"
    >
      SIM
    </span>
  );
}

/** Stripe test mode marker: no real money moves. */
export function TestModeBadge() {
  return (
    <span
      title="Stripe test mode. No real money moves."
      className="inline-flex items-center rounded-sm border border-orange-500 px-1.5 py-px text-[11px] font-bold tracking-wide text-orange-600 dark:text-orange-400"
    >
      TEST MODE
    </span>
  );
}

const TONE: Record<string, string> = {
  good: 'bg-emerald-50 text-emerald-800 ring-emerald-600/25 dark:bg-emerald-950/40 dark:text-emerald-300',
  bad: 'bg-red-50 text-red-800 ring-red-600/25 dark:bg-red-950/40 dark:text-red-300',
  busy: 'bg-sky-50 text-sky-800 ring-sky-600/25 dark:bg-sky-950/40 dark:text-sky-300',
  wait: 'bg-zinc-100 text-zinc-700 ring-zinc-500/20 dark:bg-zinc-800 dark:text-zinc-300',
  warn: 'bg-amber-50 text-amber-800 ring-amber-600/25 dark:bg-amber-950/40 dark:text-amber-300',
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
    <span className={`inline-flex items-center whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${TONE[t]}`}>
      {status.replace(/_/g, ' ')}
    </span>
  );
}

export function Block({ title, aside, children, id }: { title: string; aside?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="mb-10 scroll-mt-6">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-500 dark:border-zinc-700">{children}</p>
  );
}
