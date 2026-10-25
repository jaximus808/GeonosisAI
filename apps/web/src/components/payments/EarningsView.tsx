'use client';

// TDM-64: the earnings page body. Live from SpacetimeDB: payment, episode, review,
// task, ticket and contributor rows. Stripe TEST MODE only.

import Link from 'next/link';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useContributor } from '@/lib/contributor/hooks';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents } from '@/lib/format';
import {
  EARNED_STATUSES,
  PAY_STATUS,
  Tdm64ApiError,
  payStatus,
  paymentKind,
  refreshPayouts,
  startOnboarding,
  taskIdFromBatchRef,
  totalsByStatus,
  workLabel,
  type Contributor,
  type Episode,
  type Payment,
  type PayStatus,
  type Review,
} from '@/lib/payments/tdm64';
import { TestModeTag } from './TestModeBand';

const WORK_KINDS = ['clean_teleop', 'recovery_takeover', 'purchased'];

export function EarningsView({ contributorParam }: { contributorParam?: string }) {
  const own = useContributor();
  const [all, allReady] = useRows('contributor');
  const viewingId = contributorParam ? BigInt(contributorParam) : undefined;
  const contributor = viewingId != null ? all.find((c) => c.id === viewingId) : own.contributor;
  const isSelf = contributor != null && own.contributor?.id === contributor.id;

  if (viewingId != null && allReady && !contributor) {
    return (
      <Shell>
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          There is no contributor #{contributorParam}. <Link className="text-blue-600 hover:underline" href="/earnings">Pick another one</Link>.
        </p>
      </Shell>
    );
  }
  if (!contributor) {
    const loading = viewingId != null ? !allReady : !own.ready;
    return (
      <Shell>
        {loading ? (
          <p className="text-sm text-zinc-500">Connecting…</p>
        ) : (
          <NoContributor others={all} />
        )}
      </Shell>
    );
  }
  return (
    <Shell>
      <Earnings contributor={contributor} isSelf={isSelf} />
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="mx-auto max-w-4xl">
      {children}
    </div>
  );
}

function NoContributor({ others }: { others: readonly Contributor[] }) {
  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-semibold tracking-tight">Earnings</h1>
      <p className="mt-2 text-zinc-600 dark:text-zinc-400">
        This browser has not joined yet. Join to pick up paid robot tasks, and your payouts will show up here.
      </p>
      <Link
        href="/join"
        className="mt-4 inline-flex rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        Join the factory
      </Link>
      {others.length > 0 && (
        <div className="mt-10">
          <h2 className="text-sm font-medium text-zinc-500">Open someone&apos;s earnings (dashboard)</h2>
          <ul className="mt-2 flex flex-wrap gap-2">
            {[...others]
              .sort((a, b) => Number(a.id - b.id))
              .map((c) => (
                <li key={c.id.toString()}>
                  <Link
                    href={`/earnings?contributor=${c.id}`}
                    className="inline-flex rounded border border-zinc-300 px-2 py-1 text-sm hover:border-zinc-500 dark:border-zinc-700"
                  >
                    {c.name} <span className="ml-1 text-zinc-400">#{c.id.toString()}</span>
                  </Link>
                </li>
              ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- the page for one contributor

interface LedgerRow {
  key: string;
  at: Date;
  kind: string;
  ticketId: bigint;
  episode?: Episode;
  review?: Review;
  payment?: Payment;
  amountHint?: number; // task price when no payment exists yet
}

function Earnings({ contributor, isSelf }: { contributor: Contributor; isSelf: boolean }) {
  const cid = contributor.id;
  const [payments] = useRows('payment', (r) => r.contributorId.eq(cid));
  const [episodes] = useRows('episode', (r) => r.contributorId.eq(cid));
  const [tasks] = useRows('task', (r) => r.assigneeId.eq(cid));
  const [reviews] = useRows('review');
  const [tickets] = useRows('ticket');

  const rows = useMemo(() => {
    const epById = new Map(episodes.map((e) => [e.id, e]));
    const taskById = new Map(tasks.map((t) => [t.id, t]));
    const latestReview = new Map<bigint, Review>();
    for (const r of reviews) {
      const cur = latestReview.get(r.episodeId);
      if (!cur || r.id > cur.id) latestReview.set(r.episodeId, r);
    }
    const payByEp = new Map(payments.filter((p) => p.episodeId !== 0n).map((p) => [p.episodeId, p]));
    const out: LedgerRow[] = [];
    for (const e of episodes) {
      if (!WORK_KINDS.includes(e.kind)) continue;
      const task = e.taskId ? taskById.get(e.taskId) : undefined;
      out.push({
        key: `e${e.id}`,
        at: e.createdAt.toDate(),
        kind: e.kind,
        ticketId: e.ticketId,
        episode: e,
        review: latestReview.get(e.id),
        payment: payByEp.get(e.id),
        amountHint: task ? Number(task.priceCents) : undefined,
      });
    }
    for (const p of payments) {
      if (p.episodeId !== 0n && epById.has(p.episodeId)) continue;
      out.push({ key: `p${p.id}`, at: p.createdAt.toDate(), kind: paymentKind(p, epById, taskById), ticketId: p.ticketId, payment: p });
    }
    return out.sort((a, b) => b.at.getTime() - a.at.getTime());
  }, [episodes, tasks, reviews, payments]);

  const totals = totalsByStatus(payments);
  const earned = EARNED_STATUSES.reduce((n, s) => n + (totals[s]?.cents ?? 0), 0);
  const paid = totals.paid?.cents ?? 0;
  const held = totals.awaiting_onboarding?.cents ?? 0;
  const ticketDesc = new Map(tickets.map((t) => [t.id, t.taskDescription]));

  return (
    <>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">{isSelf ? 'Your earnings' : `${contributor.name}'s earnings`}</h1>
        <span className="text-sm text-zinc-500">
          {contributor.name} #{contributor.id.toString()}
          {contributor.kind === 'dataset_seller' ? ', dataset seller' : ''}
        </span>
        {!isSelf && (
          <Link href="/earnings" className="ml-auto text-sm text-blue-600 hover:underline">
            All contributors
          </Link>
        )}
      </div>

      <Summary paid={paid} earned={earned} totals={totals} />

      {!contributor.payoutsEnabled && (
        <OnboardingCallout contributor={contributor} heldCents={held} isSelf={isSelf} />
      )}

      <section className="mt-10" aria-labelledby="tdm64-ledger">
        <h2 id="tdm64-ledger" className="text-lg font-semibold tracking-tight">
          Work and payouts
        </h2>
        <p className="mt-1 text-sm text-zinc-500">
          Paid when the reviewer accepts an episode.
        </p>
        {rows.length === 0 ? (
          <p className="mt-4 rounded-md border border-dashed border-zinc-300 p-5 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
            Nothing yet. <Link className="text-blue-600 hover:underline" href="/marketplace">Pick up a paid task</Link> and your
            first payout appears here once the reviewer accepts it.
          </p>
        ) : (
          <ol className="mt-4 divide-y divide-zinc-200 border-y border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {rows.map((r) => (
              <LedgerItem key={r.key} row={r} ticketDesc={ticketDesc.get(r.ticketId)} />
            ))}
          </ol>
        )}
      </section>
    </>
  );
}

const BAR_ORDER: PayStatus[] = ['paid', 'pending', 'awaiting_onboarding'];
const LEGEND_ORDER: PayStatus[] = ['paid', 'pending', 'awaiting_onboarding', 'failed', 'blocked_budget', 'reversed'];

function Summary({ paid, earned, totals }: { paid: number; earned: number; totals: ReturnType<typeof totalsByStatus> }) {
  return (
    <div className="mt-6">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <div>
          <p className="text-sm text-zinc-500">Paid out</p>
          <p className="flex items-center gap-3 text-5xl font-light tabular-nums tracking-tight">
            {fmtCents(paid)} <TestModeTag />
          </p>
        </div>
        <div className="pb-1.5 text-sm text-zinc-600 dark:text-zinc-400">
          of <span className="font-medium tabular-nums text-zinc-900 dark:text-zinc-100">{fmtCents(earned)}</span> earned
          from accepted work
        </div>
      </div>

      <div
        className="mt-4 flex h-3 w-full overflow-hidden rounded-sm bg-zinc-200 dark:bg-zinc-800"
        role="img"
        aria-label={BAR_ORDER.map((s) => `${PAY_STATUS[s].label} ${fmtCents(totals[s]?.cents ?? 0)}`).join(', ')}
      >
        {earned > 0 &&
          BAR_ORDER.map((s) =>
            totals[s]?.cents ? (
              <div key={s} className={PAY_STATUS[s].swatch} style={{ width: `${(totals[s].cents / earned) * 100}%` }} />
            ) : null,
          )}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
        {LEGEND_ORDER.filter((s) => BAR_ORDER.includes(s) || totals[s]).map((s) => (
          <div key={s} className="flex items-start gap-2" title={PAY_STATUS[s].help}>
            <span aria-hidden className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${PAY_STATUS[s].swatch}`} />
            <div>
              <dt className="text-zinc-600 dark:text-zinc-400">{PAY_STATUS[s].label}</dt>
              <dd className="tabular-nums">
                {fmtCents(totals[s]?.cents ?? 0)}
                <span className="ml-1 text-zinc-400">
                  ({totals[s]?.count ?? 0})
                </span>
              </dd>
            </div>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ---------------------------------------------------------------- onboarding call-to-action

type OnboardState =
  | { kind: 'idle' }
  | { kind: 'busy'; what: string }
  | { kind: 'blocked'; message: string }
  | { kind: 'error'; message: string }
  | { kind: 'info'; message: string };

function OnboardingCallout({ contributor, heldCents, isSelf }: { contributor: Contributor; heldCents: number; isSelf: boolean }) {
  const [state, setState] = useState<OnboardState>({ kind: 'idle' });
  const autoChecked = useRef(false);
  const hasAccount = !!contributor.stripeAccountId;

  // Back from Stripe-hosted onboarding (or a reload after it): ask the orchestrator to re-check once.
  useEffect(() => {
    if (!hasAccount || autoChecked.current) return;
    autoChecked.current = true;
    refreshPayouts(contributor.id).catch(() => {});
  }, [hasAccount, contributor.id]);

  const onSetup = async () => {
    setState({ kind: 'busy', what: 'Opening Stripe…' });
    try {
      const r = await startOnboarding(contributor.id, window.location.href);
      window.location.assign(r.onboarding_url);
    } catch (e) {
      if (e instanceof Tdm64ApiError && e.status === 409) {
        setState({
          kind: 'blocked',
          message:
            'Payout setup is not open yet. Your accepted work is held for you.',
        });
      } else {
        setState({ kind: 'error', message: `Could not start payout setup: ${(e as Error).message}` });
      }
    }
  };

  const onCheck = async () => {
    setState({ kind: 'busy', what: 'Checking…' });
    try {
      const r = await refreshPayouts(contributor.id);
      setState(
        r.payouts_enabled
          ? { kind: 'info', message: 'Payouts are on. Held payouts are being sent.' }
          : { kind: 'info', message: `Stripe still needs a few details (status: ${r.transfers_status ?? 'no account'}). Finish setup and check again.` },
      );
    } catch (e) {
      setState({ kind: 'error', message: `Could not check payout setup: ${(e as Error).message}` });
    }
  };

  const busy = state.kind === 'busy';
  return (
    <section
      aria-labelledby="tdm64-onboard"
      className="mt-8 rounded-md border border-amber-300 bg-amber-50/70 p-4 dark:border-amber-700/60 dark:bg-amber-950/20 sm:p-5"
    >
      <h2 id="tdm64-onboard" className="font-semibold">
        {heldCents > 0
          ? `Set up payouts to receive ${fmtCents(heldCents)}`
          : 'Set up payouts before your first accepted task'}
      </h2>
      <p className="mt-1 max-w-prose text-sm text-zinc-700 dark:text-zinc-300">
        {hasAccount
          ? 'Finish the Stripe form, then check again.'
          : 'Stripe collects your details on its own page.'}
        {!isSelf && ' You are viewing someone else’s earnings, so only they should complete this.'}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onSetup}
          disabled={busy || state.kind === 'blocked'}
          className="rounded-md bg-zinc-900 px-3.5 py-2 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {hasAccount ? 'Continue payout setup' : 'Set up payouts'}
        </button>
        {hasAccount && (
          <button
            type="button"
            onClick={onCheck}
            disabled={busy}
            className="rounded-md border border-zinc-400 px-3.5 py-2 text-sm font-medium hover:border-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900 disabled:opacity-50 dark:border-zinc-600"
          >
            Check setup again
          </button>
        )}
      </div>
      {state.kind === 'busy' && <p className="mt-2 text-sm text-zinc-500">{state.what}</p>}
      {state.kind === 'blocked' && (
        <p role="status" className="mt-3 border-l-2 border-amber-600 pl-3 text-sm text-amber-900 dark:text-amber-200">
          {state.message}
        </p>
      )}
      {state.kind === 'error' && (
        <p role="alert" className="mt-3 border-l-2 border-rose-600 pl-3 text-sm text-rose-800 dark:text-rose-300">
          {state.message}
        </p>
      )}
      {state.kind === 'info' && (
        <p role="status" className="mt-3 text-sm text-zinc-700 dark:text-zinc-300">
          {state.message}
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- one ledger line

const REVIEW_TEXT: Record<string, { label: string; cls: string }> = {
  accepted: { label: 'Accepted', cls: 'text-teal-700 dark:text-teal-300' },
  rejected: { label: 'Rejected', cls: 'text-rose-700 dark:text-rose-300' },
  pending: { label: 'Waiting for review', cls: 'text-zinc-500' },
  not_reviewed: { label: 'Not reviewed', cls: 'text-zinc-500' },
  done: { label: 'Completed', cls: 'text-teal-700 dark:text-teal-300' },
  qa_accepted: { label: 'Passed QA', cls: 'text-teal-700 dark:text-teal-300' },
};

function LedgerItem({ row, ticketDesc }: { row: LedgerRow; ticketDesc?: string }) {
  const { episode, review, payment } = row;
  // Resets have no episode or review: the task was completed. Dataset batches passed QA.
  const reviewState = episode ? episode.reviewStatus : payment?.datasetOfferId ? 'qa_accepted' : payment ? 'done' : 'pending';
  const rv = REVIEW_TEXT[reviewState] ?? { label: reviewState, cls: 'text-zinc-500' };
  const amount = payment ? Number(payment.amountCents) : row.amountHint;
  const meta = payment ? payStatus(payment.status) : undefined;
  const resetTask = payment ? taskIdFromBatchRef(payment.batchRef) : undefined;
  const ref = episode
    ? `episode #${episode.id}`
    : payment?.datasetOfferId
      ? `offer #${payment.datasetOfferId}${payment.batchRef ? `, ${payment.batchRef}` : ''}`
      : resetTask != null
        ? `task #${resetTask}`
        : `payment #${payment?.id}`;

  return (
    <li className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 py-3 sm:grid-cols-[9rem_1fr_9rem_auto]">
      <time className="text-sm tabular-nums text-zinc-500 sm:pt-px" dateTime={row.at.toISOString()}>
        {row.at.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
      </time>
      <div className="col-span-2 row-start-2 min-w-0 sm:col-span-1 sm:row-start-1 sm:col-start-2">
        <p className="font-medium">
          {workLabel(row.kind)}{' '}
          <span className="font-normal text-zinc-500">
            {ref}, <Link className="hover:underline" href={`/tickets/${row.ticketId}`}>ticket #{row.ticketId.toString()}</Link>
          </span>
        </p>
        {ticketDesc && <p className="truncate text-sm text-zinc-500">{ticketDesc}</p>}
        {reviewState === 'rejected' && (
          <p className="mt-1 text-sm text-rose-800 dark:text-rose-300">
            Reviewer: {review?.reason || 'no reason recorded'}
          </p>
        )}
        {payment?.error && payment.status !== 'paid' && payment.status !== 'awaiting_onboarding' && (
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{payment.error}</p>
        )}
      </div>
      <p className={`col-span-2 row-start-3 text-sm sm:col-span-1 sm:row-start-1 sm:col-start-3 ${rv.cls}`}>{rv.label}</p>
      <div className="col-start-2 row-start-1 text-right sm:col-start-4">
        <p className={`tabular-nums ${reviewState === 'rejected' ? 'text-zinc-400 line-through' : ''}`}>
          {amount != null ? fmtCents(amount) : '–'}
        </p>
        {meta ? (
          <span
            title={meta.help}
            className={`mt-0.5 inline-flex whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${meta.pill}`}
          >
            {meta.label}
          </span>
        ) : (
          <span className="mt-0.5 block text-xs text-zinc-500">{reviewState === 'rejected' ? 'Not paid' : 'No payout yet'}</span>
        )}
      </div>
    </li>
  );
}
