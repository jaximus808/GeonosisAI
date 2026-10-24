'use client';

// TDM-61: the contributor marketplace (/marketplace). This is what a judge's
// phone opens after scanning the /join QR, so it is phone-first.
//
// - Open tasks, live from the `task` table: a claimable task is `open`, or
//   `claimed` with a lapsed lease (TDM-10 expiry is lazy).
// - Claim calls claim_task(task_id, contributor_id) as this browser's own
//   identity. The reducer is atomic; the loser gets "already claimed by <name>".
// - A held claim shows its lease countdown and is kept alive with
//   heartbeat_claim (60 s arm-control leases, 10 min resets).
// - Teleop and takeover tasks are worked at /teleop/[taskId] (TDM-56/57).
//   Resets are physical, so they are finished here with complete_task.
// - "My tasks" follows each claim through review and payout (Stripe TEST MODE).

import Link from 'next/link';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useReducer, useSpacetimeDB } from 'spacetimedb/react';
import { reducers } from '@/module_bindings';
import { useRows, type RowOf } from '@/lib/spacetime/hooks';
import { useContributor } from '@/lib/contributor/hooks';
import {
  actionErrorMessage,
  claimErrorMessage,
  cleanInstructions,
  fmtCountdown,
  fmtPrice,
  isArmControl,
  isClaimable,
  leaseLeftMs,
  leaseLive,
  leaseTtlMs,
  typeInfo,
  useNow,
  useTdm61ClaimHeartbeat,
  workHref,
  type MarketTask,
  type TaskType,
} from '@/lib/marketplace/tdm61';
import { Tdm59ClaimGate } from '@/components/teleop/Tdm59ClaimGate'; // TDM-59 practice unlocks arm-control claims
import { resetHref } from '@/lib/tdm62/reset'; // TDM-62 reset task page
import { ShareTask } from '@/components/tdm108/ShareTask'; // TDM-108 share card + link

type Ticket = RowOf<'ticket'>;
type Robot = RowOf<'robot'>;
type Payment = RowOf<'payment'>;
type Episode = RowOf<'episode'>;

type Filter = 'all' | TaskType;
type Tab = 'open' | 'mine';
type Toast = { id: number; text: string; tone: 'bad' | 'good' };

// TDM-109: every task kind is money (ochre) on the brand; the kind is told apart by its label, not colour.
const BRAND_TYPE_STYLE = {
  rail: 'bg-ochre',
  text: 'text-ochre',
  chip: 'data-[on=true]:bg-ink data-[on=true]:text-ground data-[on=true]:border-ink',
};
const TYPE_STYLE: Record<string, { rail: string; text: string; chip: string }> = {
  clean_teleop: BRAND_TYPE_STYLE,
  recovery_takeover: BRAND_TYPE_STYLE,
  reset: BRAND_TYPE_STYLE,
};
const styleOf = (type: string) => TYPE_STYLE[type] ?? TYPE_STYLE.reset;

const HAZARD = 'repeating-linear-gradient(-45deg, var(--ochre) 0 6px, var(--surface-sunken) 6px 12px)';

const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'clean_teleop', label: 'Teleop demo' },
  { id: 'recovery_takeover', label: 'Takeover' },
  { id: 'reset', label: 'Reset' },
];

function TestStripe({ className = '' }: { className?: string }) {
  return (
    <span
      title="Stripe test mode. No real money moves."
      className={`inline-flex items-center gap-1 text-[11px] font-semibold text-ochre ${className}`}
    >
      <span aria-hidden className="h-2.5 w-4 rounded-[2px]" style={{ backgroundImage: HAZARD }} />
      Test mode
    </span>
  );
}

// ------------------------------------------------------------------ view

export function MarketplaceView({ onlyTasks }: { onlyTasks?: string[] } = {}) {
  // TDM-108: a visitor who came in on a job-drop invite sees that job's task(s) only
  const only = useMemo(() => (onlyTasks?.length ? new Set(onlyTasks) : null), [onlyTasks]);
  const { isActive } = useSpacetimeDB();
  const { contributor, ready } = useContributor();
  const me = contributor?.id;
  const canClaim = contributor != null && contributor.kind === 'contributor';

  const [tasks, tasksReady] = useRows('task');
  const [tickets] = useRows('ticket');
  const [robots] = useRows('robot');
  const [payments] = useRows('payment', (r) => r.contributorId.eq(me ?? 0n));
  const [episodes] = useRows('episode', (r) => r.contributorId.eq(me ?? 0n));

  const claimTask = useReducer(reducers.claimTask);
  const releaseTask = useReducer(reducers.releaseTask);
  const completeTask = useReducer(reducers.completeTask);

  const now = useNow(500);
  const [tab, setTab] = useState<Tab>('open');
  const [filter, setFilter] = useState<Filter>('all');
  const [busy, setBusy] = useState<bigint | undefined>();
  const [toast, setToast] = useState<Toast | undefined>();

  const ticketById = useMemo(() => new Map<bigint, Ticket>(tickets.map((t) => [t.id, t])), [tickets]);
  const robotById = useMemo(() => new Map<bigint, Robot>(robots.map((r) => [r.id, r])), [robots]);

  const open = useMemo(
    () =>
      tasks
        .filter((t) => isClaimable(t, now) && !(me != null && t.assigneeId === me && leaseLive(t, now)))
        .filter((t) => only == null || only.has(String(t.id)))
        .sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch)),
    [tasks, now, me, only],
  );
  const held = useMemo(
    () => (me == null ? [] : tasks.filter((t) => t.assigneeId === me && leaseLive(t, now))),
    [tasks, now, me],
  );
  const mine = useMemo(
    () =>
      me == null
        ? []
        : tasks
            .filter((t) => t.assigneeId === me && t.status !== 'open')
            .sort((a, b) => Number((b.claimedAt?.microsSinceUnixEpoch ?? 0n) - (a.claimedAt?.microsSinceUnixEpoch ?? 0n))),
    [tasks, me],
  );
  const shown = filter === 'all' ? open : open.filter((t) => t.type === filter);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: open.length };
    for (const t of open) c[t.type] = (c[t.type] ?? 0) + 1;
    return c;
  }, [open]);

  // New tasks arriving while the page is open: highlight them and buzz the phone once.
  const seen = useRef<Set<bigint> | null>(null);
  const [fresh, setFresh] = useState<Set<bigint>>(new Set());
  useEffect(() => {
    if (!tasksReady) return;
    const openIds = tasks.filter((t) => t.status === 'open').map((t) => t.id);
    if (seen.current == null) {
      seen.current = new Set(openIds);
      return;
    }
    const arrived = openIds.filter((id) => !seen.current!.has(id));
    for (const id of arrived) seen.current.add(id);
    if (arrived.length === 0) return;
    setFresh((cur) => new Set([...cur, ...arrived]));
    try {
      navigator.vibrate?.([60, 40, 60]);
    } catch {
      // not supported
    }
    // Not cleared on the next table update (that would strand the highlight); unmount is harmless.
    window.setTimeout(() => setFresh((cur) => new Set([...cur].filter((id) => !arrived.includes(id)))), 6000);
  }, [tasks, tasksReady]);

  useEffect(() => {
    document.title = open.length ? `(${open.length}) Paid tasks · GeonosisAI` : 'Paid tasks · GeonosisAI';
  }, [open.length]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(undefined), 5000);
    return () => window.clearTimeout(timer);
  }, [toast]);
  const say = (text: string, tone: Toast['tone']) => setToast({ id: Date.now(), text, tone });

  async function onClaim(task: MarketTask) {
    if (me == null) return;
    setBusy(task.id);
    try {
      await claimTask({ taskId: task.id, contributorId: me });
      say(`Claimed task #${task.id}. It's yours for ${isArmControl(task.type) ? '60 seconds at a time' : '10 minutes'}.`, 'good');
    } catch (e) {
      say(claimErrorMessage(e), 'bad');
    } finally {
      setBusy(undefined);
    }
  }

  async function onRelease(task: MarketTask) {
    setBusy(task.id);
    try {
      await releaseTask({ taskId: task.id });
      say(`Released task #${task.id}. It's open for others again.`, 'good');
    } catch (e) {
      say(actionErrorMessage(e), 'bad');
    } finally {
      setBusy(undefined);
    }
  }

  async function onResetDone(task: MarketTask) {
    setBusy(task.id);
    try {
      await completeTask({ taskId: task.id, resultJson: JSON.stringify({ source: 'marketplace', done: 'reset' }) });
      say(`Reset marked done. ${fmtPrice(task.priceCents)} (test mode) is on its way.`, 'good');
      setTab('mine');
    } catch (e) {
      say(actionErrorMessage(e), 'bad');
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <div className="mx-auto max-w-xl">
      <style>{`
        @keyframes tdm61-arrive { from { box-shadow: 0 0 0 3px var(--ochre); } to { box-shadow: 0 0 0 0 transparent; } }
        @keyframes tdm61-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
        @media (prefers-reduced-motion: reduce) { .tdm61-motion { animation: none !important; } }
      `}</style>

      {/* TDM-109: PhoneTask layout. Earnings in ochre mono are the only big number on the screen. */}
      {contributor && (
        <div className="mb-4 flex items-baseline justify-between" data-testid="gx-earned">
          <span className="gx-label">Paid so far</span>
          <strong className="font-mono text-[28px] leading-8 font-medium tabular-nums text-ochre">{fmtPrice(paidTotalOf(mine, payments, episodes))}</strong>
        </div>
      )}
      <div className="mb-4">
        <h1 className="font-display text-xl leading-6 font-bold">Open tasks</h1>
        <p className="gx-small mt-1 mb-0">Claim one, do it, and get paid once it passes review.</p>
      </div>

      <p className="mb-4 flex items-center gap-2 rounded-sm bg-ochre-soft px-3 py-2 text-sm text-ink">
        <span aria-hidden className="h-4 w-5 shrink-0" style={{ backgroundImage: HAZARD }} />
        <span>
          <span className="font-semibold text-ochre">Test mode.</span> Prices are paid as Stripe test transfers. No real money moves.
        </span>
      </p>

      {!ready && <p className="gx-small py-8 text-center">{isActive ? 'Loading tasks…' : 'Connecting to the factory…'}</p>}

      {ready && contributor && !canClaim && (
        <p className="mb-4 rounded-sm bg-surface-sunken px-3 py-2 text-sm text-ink-muted">
          You joined as a dataset seller, so you can watch tasks here but not claim them.
        </p>
      )}

      {held.map((t) => (
        <HeldClaim
          key={`${t.id}-${t.claimedAt?.microsSinceUnixEpoch}`}
          task={t}
          now={now}
          ticket={ticketById.get(t.ticketId)}
          busy={busy === t.id}
          onRelease={() => void onRelease(t)}
          onResetDone={() => void onResetDone(t)}
        />
      ))}

      {ready && (
        <>
          <div role="tablist" aria-label="Task lists" className="mb-3 grid grid-cols-2 rounded-sm border border-line bg-surface-sunken p-1 text-sm font-medium">
            {(['open', 'mine'] as Tab[]).map((id) => (
              <button
                key={id}
                role="tab"
                type="button"
                aria-selected={tab === id}
                onClick={() => setTab(id)}
                className="rounded-sm px-3 py-2 text-ink-muted aria-selected:bg-surface aria-selected:text-ink aria-selected:shadow-[inset_0_-2px_0_var(--melt)]"
              >
                {id === 'open' ? `Open tasks (${open.length})` : `My tasks (${mine.length})`}
              </button>
            ))}
          </div>

          {tab === 'open' && (
            <>
              <div className="-mx-4 mb-3 flex gap-2 overflow-x-auto px-4 pb-1" role="group" aria-label="Filter by type">
                {FILTERS.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    data-on={filter === f.id}
                    aria-pressed={filter === f.id}
                    onClick={() => setFilter(f.id)}
                    className={`shrink-0 rounded-sm border border-line-strong px-3 py-1.5 text-sm text-ink-muted ${
                      f.id === 'all'
                        ? BRAND_TYPE_STYLE.chip
                        : styleOf(f.id).chip
                    }`}
                  >
                    {f.label} <span className="tabular-nums opacity-70">{counts[f.id] ?? 0}</span>
                  </button>
                ))}
              </div>

              {shown.length === 0 ? (
                <div className="rounded-lg border border-dashed border-line-strong px-4 py-10 text-center">
                  <p className="font-medium">No open {filter === 'all' ? '' : `${typeInfo(filter).label.toLowerCase()} `}tasks right now.</p>
                  <p className="gx-small mt-1">
                    Keep this page open. New tasks show up here the moment the factory posts them.
                  </p>
                </div>
              ) : (
                <ul className="m-0 grid list-none gap-4 p-0">
                  {shown.map((t, i) => (
                    <TaskTicket
                      key={t.id}
                      task={t}
                      ticket={ticketById.get(t.ticketId)}
                      robot={robotById.get(t.robotId)}
                      fresh={fresh.has(t.id)}
                      busy={busy === t.id}
                      action={
                        canClaim ? (
                          <Tdm59ClaimGate taskType={t.type} taskId={t.id}><button
                            type="button"
                            disabled={busy !== undefined}
                            onClick={() => void onClaim(t)}
                            className={`gx-btn gx-btn-block ${i === 0 && held.length === 0 ? 'gx-btn-primary' : ''}`}
                          >
                            {busy === t.id ? 'Claiming…' : `Claim for ${fmtPrice(t.priceCents)}`}
                          </button></Tdm59ClaimGate>
                        ) : contributor ? null : (
                          <Link
                            href="/join"
                            className={`gx-btn gx-btn-block ${i === 0 ? 'gx-btn-primary' : ''}`}
                          >
                            Join to claim
                          </Link>
                        )
                      }
                    />
                  ))}
                </ul>
              )}
            </>
          )}

          {tab === 'mine' && (
            <MyTasks
              tasks={mine}
              now={now}
              ticketById={ticketById}
              payments={payments}
              episodes={episodes}
              guest={!contributor}
            />
          )}
          <p className="gx-small mt-6 mb-0">Test mode. No real money moves.</p>
        </>
      )}

      {toast && (
        <div className="pointer-events-none fixed inset-x-0 bottom-0 z-50 flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <div
            key={toast.id}
            role={toast.tone === 'bad' ? 'alert' : 'status'}
            className={`pointer-events-auto flex w-full max-w-md items-start gap-3 rounded-sm border bg-surface px-4 py-3 text-sm font-medium text-ink shadow-overlay ${
              toast.tone === 'bad' ? 'border-slag' : 'border-line'
            }`}
          >
            <span className="flex-1">{toast.text}</span>
            <button type="button" onClick={() => setToast(undefined)} className="-my-1 rounded-sm px-1 text-ink-muted hover:text-ink" aria-label="Dismiss">
              ✕
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ task ticket

function TaskTicket({
  task,
  ticket,
  robot,
  fresh,
  busy,
  action,
}: {
  task: MarketTask;
  ticket?: Ticket;
  robot?: Robot;
  fresh: boolean;
  busy: boolean;
  action: React.ReactNode;
}) {
  const info = typeInfo(task.type);
  const s = styleOf(task.type);
  const text = cleanInstructions(task.instructions);
  return (
    <li
      id={`task-${task.id}`} // TDM-65 push deep link (/marketplace?task=N)
      className={`tdm61-motion gx-task ${busy ? 'opacity-80' : ''}`}
      style={fresh ? { animation: 'tdm61-arrive 2.4s ease-out' } : undefined}
    >
      {/* TDM-109: TaskCard. Kind as a money tag, price in ochre mono top right. */}
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2">
          <span className="gx-tag gx-tag-money">
            <i className="gx-rivet" aria-hidden />
            {info.label}
          </span>
          {fresh && <span className="gx-tag gx-tag-busy">New</span>}
        </span>
        <span className="text-right">
          <span className="gx-price block">{fmtPrice(task.priceCents)}</span>
          <span className="gx-id block">{task.type === 'reset' ? 'per reset' : 'per accepted episode'}</span>
        </span>
      </div>
      <h3 className="gx-task-title line-clamp-2">{ticket?.taskDescription ?? `Ticket ${task.ticketId}`}</h3>
      <p className="m-0 line-clamp-3">{text || info.blurb}</p>
      <dl className="gx-facts m-0">
        <div>
          <dt className="sr-only">Time</dt>
          <dd className="m-0">About {info.minutes} min</dd>
        </div>
        <div>
          <dt className="sr-only">Robot</dt>
          <dd className="m-0 font-mono">{robot?.name ?? `Robot ${task.robotId}`}</dd>
        </div>
        <div>
          <dt className="sr-only">Where</dt>
          <dd className="m-0">{info.where === 'remote' ? 'From your phone' : 'At the robot'}</dd>
        </div>
        <div>
          <dt className="sr-only">Task id</dt>
          <dd className="m-0 font-mono">#{String(task.id)}</dd>
        </div>
      </dl>
      {task.type === 'reset' && (
        <Link href={resetHref(task.id)} className="text-[13px] font-semibold text-arc underline underline-offset-2">
          Steps and start photo
        </Link>
      )}
      <ShareTask taskId={task.id} />
      {action}
      <TestStripe />
    </li>
  );
}

// ------------------------------------------------------------------ held claim

function HeldClaim({
  task,
  now,
  ticket,
  busy,
  onRelease,
  onResetDone,
}: {
  task: MarketTask;
  now: number;
  ticket?: Ticket;
  busy: boolean;
  onRelease: () => void;
  onResetDone: () => void;
}) {
  const { lastBeatAt, error } = useTdm61ClaimHeartbeat(task);
  const info = typeInfo(task.type);
  const left = Math.max(0, leaseLeftMs(task, now) ?? 0);
  const ttl = leaseTtlMs(task.type);
  const frac = Math.min(1, left / ttl);
  const low = left < Math.min(15_000, ttl / 4);
  const href = workHref(task);
  const beatAgo = lastBeatAt ? Math.max(0, Math.round((now - lastBeatAt) / 1000)) : undefined;

  return (
    <section
      aria-label={`Task #${task.id} you hold`}
      className="mb-5 overflow-hidden rounded-lg border border-arc bg-surface text-ink"
    >
      <div className="px-4 pb-3 pt-4">
        <p className="text-sm text-ink-muted">
          You hold <span className="font-semibold text-ink">{info.label.toLowerCase()} #{String(task.id)}</span>
        </p>
        <p className="mt-1 line-clamp-2 font-display text-2xl font-extrabold leading-tight">{ticket?.taskDescription ?? `Ticket ${task.ticketId}`}</p>

        <div className="mt-4 flex items-end justify-between gap-3">
          <div>
            <p className="gx-label">Hold time left</p>
            <p className={`font-mono text-4xl font-medium leading-none tabular-nums ${low ? 'text-slag' : ''}`} aria-live="off">
              {fmtCountdown(left)}
            </p>
          </div>
          <p className="flex items-center gap-1.5 pb-0.5 text-right font-mono text-xs text-ink-muted">
            <span
              aria-hidden
              className={`tdm61-motion inline-block h-1.5 w-1.5 ${error ? 'bg-slag' : 'bg-arc'}`}
              style={error ? undefined : { animation: 'tdm61-pulse 2s ease-in-out infinite' }}
            />
            {error ? 'Keep-alive failed' : beatAgo === undefined ? 'Keeping your hold alive' : `Kept alive ${beatAgo} s ago`}
          </p>
        </div>
        <div
          className="mt-2 h-2.5 overflow-hidden bg-surface-sunken"
          role="progressbar"
          aria-label="Hold time left"
          aria-valuemin={0}
          aria-valuemax={Math.round(ttl / 1000)}
          aria-valuenow={Math.round(left / 1000)}
        >
          <div
            className={`h-full transition-[width] duration-500 ease-linear ${low ? 'bg-slag' : 'bg-arc'}`}
            style={{ width: `${frac * 100}%` }}
          />
        </div>
        <p className="gx-small mt-2 mb-0">
          {error
            ? error
            : isArmControl(task.type)
              ? 'You control the arm while this page or the teleop page stays open. The hold renews every 20 s and lapses 60 s after the last renewal.'
              : 'Your hold renews while this page is open. Mark it done when the scene is back at its start.'}
        </p>
        {task.type === 'reset' && (
          <Link href={resetHref(task.id)} className="mt-2 inline-block text-sm font-semibold text-arc underline underline-offset-2">
            Open the reset page: steps, start photo, robot camera
          </Link>
        )}
      </div>

      <div className="grid grid-cols-[1fr_auto] gap-2 border-t border-line bg-surface-sunken p-3">
        {href ? (
          <Link
            href={href}
            className="gx-btn gx-btn-primary gx-btn-block"
          >
            Open controls
          </Link>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={onResetDone}
            className="gx-btn gx-btn-primary gx-btn-block"
          >
            {busy ? 'Saving…' : 'Mark reset done'}
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={onRelease}
          className="gx-btn gx-btn-ghost h-11"
        >
          Release
        </button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ my tasks

/** Sum of paid payouts for this contributor's tasks (teleop via episode, resets via batch_ref "task:<id>"). */
function paidTotalOf(tasks: readonly MarketTask[], payments: readonly Payment[], episodes: readonly Episode[]): number {
  const epByTask = new Map<bigint, Episode>();
  for (const e of episodes) if (e.taskId !== 0n) epByTask.set(e.taskId, e);
  const payByEpisode = new Map<bigint, Payment>();
  const payByRef = new Map<string, Payment>();
  for (const p of payments) {
    if (p.episodeId !== 0n) payByEpisode.set(p.episodeId, p);
    if (p.batchRef) payByRef.set(p.batchRef, p);
  }
  return tasks.reduce((sum, t) => {
    const ep = epByTask.get(t.id);
    const pay = (ep && payByEpisode.get(ep.id)) ?? payByRef.get(`task:${t.id}`);
    return pay?.status === 'paid' ? sum + Number(pay.amountCents) : sum;
  }, 0);
}

type Tone = 'good' | 'bad' | 'busy' | 'wait' | 'warn';
const TONE: Record<Tone, string> = {
  good: 'text-emerald-700 dark:text-emerald-400',
  bad: 'text-red-700 dark:text-red-400',
  busy: 'text-sky-700 dark:text-sky-400',
  wait: 'text-zinc-500',
  warn: 'text-amber-700 dark:text-amber-400',
};

function myTaskState(task: MarketTask, now: number, pay: Payment | undefined, ep: Episode | undefined): [string, Tone] {
  if (task.status === 'claimed') return leaseLive(task, now) ? ['Holding now', 'busy'] : ['Hold ran out', 'warn'];
  if (task.status === 'submitted') return ['Submitted', 'busy'];
  if (task.status === 'cancelled') return ['Withdrawn by the factory', 'wait'];
  if (task.status === 'expired') return ['Expired', 'wait'];
  if (task.status !== 'completed') return [task.status, 'wait'];
  if (pay) {
    switch (pay.status) {
      case 'paid':
        return ['Paid', 'good'];
      case 'pending':
        return ['Payout processing', 'wait'];
      case 'awaiting_onboarding':
        return ['Set up payouts to receive it', 'warn'];
      case 'blocked_budget':
        return ['Held: ticket budget reached', 'bad'];
      case 'failed':
        return ['Payout failed', 'bad'];
      case 'reversed':
        return ['Payout reversed', 'bad'];
    }
  }
  if (ep) {
    if (ep.reviewStatus === 'rejected') return ['Not accepted in review', 'bad'];
    if (ep.reviewStatus === 'accepted') return ['Accepted, payout coming', 'good'];
    return ['In review', 'wait'];
  }
  return isArmControl(task.type) ? ['Done, waiting for the recording', 'wait'] : ['Done, payout coming', 'wait'];
}

function MyTasks({
  tasks,
  now,
  ticketById,
  payments,
  episodes,
  guest,
}: {
  tasks: MarketTask[];
  now: number;
  ticketById: Map<bigint, Ticket>;
  payments: readonly Payment[];
  episodes: readonly Episode[];
  guest: boolean;
}) {
  if (guest) {
    return (
      <div className="rounded-lg border border-dashed border-line-strong px-4 py-10 text-center">
        <p className="font-medium">Join to keep track of your tasks.</p>
        <Link href="/join" className="gx-btn gx-btn-primary mt-3">
          Join as a contributor
        </Link>
      </div>
    );
  }
  if (tasks.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line-strong px-4 py-10 text-center">
        <p className="font-medium">You haven&apos;t claimed a task yet.</p>
        <p className="gx-small mt-1">Tasks you claim stay here through review and payout.</p>
      </div>
    );
  }
  // Teleop/takeover tasks are paid through their episode; resets through batch_ref "task:<id>".
  const epByTask = new Map<bigint, Episode>();
  for (const e of episodes) if (e.taskId !== 0n) epByTask.set(e.taskId, e);
  const payByEpisode = new Map<bigint, Payment>();
  const payByRef = new Map<string, Payment>();
  for (const p of payments) {
    if (p.episodeId !== 0n) payByEpisode.set(p.episodeId, p);
    if (p.batchRef) payByRef.set(p.batchRef, p);
  }
  const paidTotal = tasks.reduce((sum, t) => {
    const ep = epByTask.get(t.id);
    const pay = (ep && payByEpisode.get(ep.id)) ?? payByRef.get(`task:${t.id}`);
    return pay?.status === 'paid' ? sum + Number(pay.amountCents) : sum;
  }, 0);

  return (
    <>
      <p className="mb-2 flex items-center justify-between text-sm text-ink-muted">
        <span>
          Paid so far <span className="font-mono font-medium tabular-nums text-ochre">{fmtPrice(paidTotal)}</span>
        </span>
        <TestStripe />
      </p>
      <ul className="m-0 list-none divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface p-0">
        {tasks.map((t) => {
          const ep = epByTask.get(t.id);
          const pay = (ep && payByEpisode.get(ep.id)) ?? payByRef.get(`task:${t.id}`);
          const [label, tone] = myTaskState(t, now, pay, ep);
          const href = t.status === 'claimed' && leaseLive(t, now) ? workHref(t) : undefined;
          return (
            <li key={t.id} className="relative flex items-center gap-3 py-3 pl-5 pr-4">
              <span aria-hidden className={`absolute inset-y-0 left-0 w-1 ${styleOf(t.type).rail}`} />
              <div className="min-w-0 flex-1">
                <p className="flex items-baseline gap-2 text-sm">
                  <span className={`font-semibold ${styleOf(t.type).text}`}>{typeInfo(t.type).label}</span>
                  <span className="gx-id">#{String(t.id)}</span>
                </p>
                <p className="truncate text-sm">{ticketById.get(t.ticketId)?.taskDescription ?? `Ticket ${t.ticketId}`}</p>
                <p className={`mt-0.5 text-[13px] font-medium ${TONE[tone]}`}>
                  {label}
                  {href && (
                    <>
                      {' '}
                      <Link href={href} className="underline">
                        Open teleop
                      </Link>
                    </>
                  )}
                </p>
              </div>
              <span className="gx-price text-right">{fmtPrice(pay?.amountCents ?? t.priceCents)}</span>
            </li>
          );
        })}
      </ul>
    </>
  );
}
