'use client';

// TDM-64: ticket spend and budget tracking. Mountable anywhere:
//   <TicketSpendPanel ticketId={id} />
// Live from SpacetimeDB (ticket, payment, episode, task, training_run, event_log).
// Stripe TEST MODE only.
//
// Numbers:
//   budget    ticket.budget_cents, the hard cap
//   approved  ticket.approved_cents, the sum approve_spend set on data plans
//   payouts   ticket.spent_cents = pending + awaiting_onboarding + paid payments (the reducer's guard)
//   GPU       sum(training_run.gpu_cost_cents); the flywheel counts it against the same budget
//   remaining budget - payouts - GPU
// The "paused" banner comes from the dispatcher's dispatch.budget_exhausted events (latest per
// plan, cleared by a later plan_dispatched / target_reached / reset_requested) and from any
// payment stored as blocked_budget.

import { useMemo, useState } from 'react';
import { useRows, useTicket } from '@/lib/spacetime/hooks';
import { fmtCents } from '@/lib/format';
import {
  PAY_STATUS,
  Tdm64ApiError,
  parseBudgetEvent,
  paymentKind,
  raiseBudget,
  workLabel,
  type BudgetEvent,
  type Payment,
} from '@/lib/payments/tdm64';

const HATCH_APPROVED = 'repeating-linear-gradient(-45deg, rgb(161 161 170 / .45) 0 3px, transparent 3px 7px)';
const HATCH_BLOCKED = 'repeating-linear-gradient(-45deg, rgb(194 65 12 / .75) 0 3px, transparent 3px 7px)';
const KIND_ORDER = ['clean_teleop', 'recovery_takeover', 'reset', 'purchased', 'other'];

export function TicketSpendPanel({ ticketId, className = '' }: { ticketId: bigint; className?: string }) {
  const [ticket, ticketReady] = useTicket(ticketId);
  const [payments] = useRows('payment', (r) => r.ticketId.eq(ticketId));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(ticketId));
  const [tasks] = useRows('task', (r) => r.ticketId.eq(ticketId));
  const [runs] = useRows('trainingRun', (r) => r.ticketId.eq(ticketId));
  const [exhausted] = useRows('eventLog', (r) => r.type.eq('dispatch.budget_exhausted'));
  const [dispatched] = useRows('eventLog', (r) => r.type.eq('dispatch.plan_dispatched'));
  const [reached] = useRows('eventLog', (r) => r.type.eq('dispatch.target_reached'));
  const [resets] = useRows('eventLog', (r) => r.type.eq('dispatch.reset_requested'));

  const pause = useMemo(() => {
    const latest = new Map<string, BudgetEvent>();
    for (const row of [...exhausted, ...dispatched, ...reached, ...resets]) {
      const ev = parseBudgetEvent(row);
      if (!ev || ev.ticketId !== ticketId) continue;
      const key = ev.type === 'dispatch.reset_requested' || ev.taskType === 'reset' ? 'reset' : `plan:${ev.dataPlanId}`;
      const cur = latest.get(key);
      if (!cur || ev.id > cur.id) latest.set(key, ev);
    }
    return [...latest.values()].filter((e) => e.type === 'dispatch.budget_exhausted').sort((a, b) => Number(b.id - a.id));
  }, [exhausted, dispatched, reached, resets, ticketId]);

  const breakdown = useMemo(() => {
    const epById = new Map(episodes.map((e) => [e.id, e]));
    const taskById = new Map(tasks.map((t) => [t.id, t]));
    const by = new Map<string, { paid: number; reserved: number; blocked: number; failed: number; n: number }>();
    for (const p of payments) {
      const k = paymentKind(p, epById, taskById);
      const b = by.get(k) ?? { paid: 0, reserved: 0, blocked: 0, failed: 0, n: 0 };
      const c = Number(p.amountCents);
      if (p.status === 'paid') b.paid += c;
      else if (p.status === 'pending' || p.status === 'awaiting_onboarding') b.reserved += c;
      else if (p.status === 'blocked_budget') b.blocked += c;
      else b.failed += c;
      b.n += 1;
      by.set(k, b);
    }
    return [...by.entries()].sort((a, b) => KIND_ORDER.indexOf(a[0]) - KIND_ORDER.indexOf(b[0]));
  }, [payments, episodes, tasks]);

  if (!ticket) {
    return (
      <section className={className}>
        <p className="text-sm text-zinc-500">{ticketReady ? `Ticket #${ticketId} not found.` : 'Connecting…'}</p>
      </section>
    );
  }

  const sum = (pred: (p: Payment) => boolean) => payments.filter(pred).reduce((n, p) => n + Number(p.amountCents), 0);
  const budget = Number(ticket.budgetCents);
  const approved = Number(ticket.approvedCents);
  const payouts = Number(ticket.spentCents);
  const paid = sum((p) => p.status === 'paid');
  const sending = sum((p) => p.status === 'pending');
  const waiting = sum((p) => p.status === 'awaiting_onboarding');
  const blockedRows = payments.filter((p) => p.status === 'blocked_budget');
  const blocked = blockedRows.reduce((n, p) => n + Number(p.amountCents), 0);
  const failedCount = payments.filter((p) => p.status === 'failed').length;
  const gpu = runs.reduce((n, r) => n + Number(r.gpuCostCents), 0);
  const remaining = budget - payouts - gpu;
  const used = payouts + gpu;
  const scale = Math.max(budget, used + blocked, 1);
  const pct = (c: number) => `${(Math.max(c, 0) / scale) * 100}%`;
  const approvedHeadroom = Math.max(Math.min(approved, budget) - payouts, 0);
  // The dispatcher only logs again when it next runs. Treat a ticket-budget pause as resolved once
  // the budget left after open tasks covers another task of that price (e.g. right after a raise).
  const openTaskCents = tasks
    .filter((t) => t.status === 'open' || t.status === 'claimed' || t.status === 'submitted')
    .reduce((n, t) => n + Number(t.priceCents), 0);
  const dispatchHeadroom = budget - payouts - openTaskCents;
  const activePause = pause.filter(
    (e) => e.limit === 'approved_cents' || dispatchHeadroom < Math.max(e.unitPriceCents ?? 1, 1),
  );
  const paused = activePause.length > 0 || blockedRows.length > 0;
  const terminal = ['shipped', 'failed', 'cancelled'].includes(ticket.status);

  // Suggested new budget: enough for what is blocked plus what the dispatcher said it needs.
  const need = activePause.reduce((n, e) => n + (e.need ?? 0) * (e.unitPriceCents ?? 0), 0);
  const shortfall = Math.max(need + blocked - dispatchHeadroom, 0);
  const suggested = budget + Math.max(shortfall, 500);

  return (
    <section className={className} aria-labelledby={`tdm64-spend-${ticketId}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={`tdm64-spend-${ticketId}`} className="text-lg font-semibold tracking-tight">
          Spend
        </h2>
      </div>

      {paused && !terminal && (
        <PausedBanner ticketId={ticketId} events={activePause} blockedRows={blockedRows} budget={budget} suggested={suggested} />
      )}

      <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
        <Figure label="Budget" value={budget} />
        <Figure label="Approved for data" value={approved} hint="Set by approving the planner's data plans" />
        <Figure label="Spent" value={used} hint={`${fmtCents(payouts)} payouts, ${fmtCents(gpu)} GPU`} />
        <Figure
          label="Remaining"
          value={remaining}
          tone={remaining <= 0 ? 'bad' : undefined}
          hint={openTaskCents > 0 ? `${dispatchHeadroom < 0 ? '−' : ''}${fmtCents(Math.abs(dispatchHeadroom))} for new tasks after ${fmtCents(openTaskCents)} of open tasks` : undefined}
        />
      </dl>

      <div
        className="relative mt-4 h-4 w-full overflow-hidden rounded-sm bg-zinc-200 dark:bg-zinc-800"
        role="img"
        aria-label={`Paid ${fmtCents(paid)}, sending ${fmtCents(sending)}, waiting for payout setup ${fmtCents(waiting)}, GPU ${fmtCents(gpu)}, approved but unspent ${fmtCents(approvedHeadroom)}, over budget ${fmtCents(blocked)}, budget ${fmtCents(budget)}`}
      >
        <div className="flex h-full">
          <div className={PAY_STATUS.paid.swatch} style={{ width: pct(paid) }} />
          <div className={PAY_STATUS.pending.swatch} style={{ width: pct(sending) }} />
          <div className={PAY_STATUS.awaiting_onboarding.swatch} style={{ width: pct(waiting) }} />
          <div className="bg-violet-500 dark:bg-violet-400" style={{ width: pct(gpu) }} />
          <div style={{ width: pct(approvedHeadroom), backgroundImage: HATCH_APPROVED }} />
          <div style={{ width: pct(Math.max(budget - used - approvedHeadroom, 0)) }} />
          <div style={{ width: pct(blocked), backgroundImage: HATCH_BLOCKED }} />
        </div>
        {scale > budget && (
          <div
            aria-hidden
            className="absolute inset-y-0 w-0.5 bg-zinc-900 dark:bg-zinc-100"
            style={{ left: pct(budget) }}
            title="Budget cap"
          />
        )}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-600 dark:text-zinc-400">
        <Key swatch={PAY_STATUS.paid.swatch} label={`Paid ${fmtCents(paid)}`} />
        <Key swatch={PAY_STATUS.pending.swatch} label={`Sending ${fmtCents(sending)}`} />
        <Key swatch={PAY_STATUS.awaiting_onboarding.swatch} label={`Waiting for payout setup ${fmtCents(waiting)}`} />
        <Key swatch="bg-violet-500 dark:bg-violet-400" label={`GPU ${fmtCents(gpu)}`} />
        <Key hatch={HATCH_APPROVED} label={`Approved, not spent ${fmtCents(approvedHeadroom)}`} />
        {blocked > 0 && <Key hatch={HATCH_BLOCKED} label={`Over budget ${fmtCents(blocked)}`} />}
        {failedCount > 0 && <li>{failedCount} failed transfer{failedCount === 1 ? '' : 's'} (released back to the budget)</li>}
      </ul>

      <div className="mt-6 overflow-x-auto">
        <table className="w-full min-w-[34rem] text-sm">
          <caption className="sr-only">Spend by work type</caption>
          <thead className="text-left text-zinc-500">
            <tr className="border-b border-zinc-200 dark:border-zinc-800">
              <th scope="col" className="py-2 pr-4 font-normal">Work</th>
              <th scope="col" className="py-2 pr-4 text-right font-normal">Paid</th>
              <th scope="col" className="py-2 pr-4 text-right font-normal">Reserved</th>
              <th scope="col" className="py-2 pr-4 text-right font-normal">Over budget</th>
              <th scope="col" className="py-2 pr-4 text-right font-normal">Failed or reversed</th>
              <th scope="col" className="py-2 text-right font-normal">Payouts</th>
            </tr>
          </thead>
          <tbody className="tabular-nums">
            {breakdown.length === 0 && (
              <tr className="border-b border-zinc-100 dark:border-zinc-900">
                <td colSpan={6} className="py-3 text-zinc-500">
                  No payouts yet. A contributor is paid only after the reviewer accepts their work.
                </td>
              </tr>
            )}
            {breakdown.map(([k, b]) => (
              <tr key={k} className="border-b border-zinc-100 dark:border-zinc-900">
                <th scope="row" className="py-2 pr-4 text-left font-medium">{workLabel(k)}</th>
                <td className="py-2 pr-4 text-right">{fmtCents(b.paid)}</td>
                <td className="py-2 pr-4 text-right">{fmtCents(b.reserved)}</td>
                <td className={`py-2 pr-4 text-right ${b.blocked ? 'text-orange-800 dark:text-orange-300' : ''}`}>{fmtCents(b.blocked)}</td>
                <td className="py-2 pr-4 text-right text-zinc-500">{fmtCents(b.failed)}</td>
                <td className="py-2 text-right text-zinc-500">{b.n}</td>
              </tr>
            ))}
            <tr className="border-b border-zinc-100 dark:border-zinc-900">
              <th scope="row" className="py-2 pr-4 text-left font-medium">
                GPU training <span className="font-normal text-zinc-500">({runs.length} run{runs.length === 1 ? '' : 's'}, Modal)</span>
              </th>
              <td className="py-2 pr-4 text-right">{fmtCents(gpu)}</td>
              <td colSpan={4} className="py-2 text-right text-xs text-zinc-500">
                Counted against the budget, not a Stripe payout
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {!terminal && <BudgetEditor ticketId={ticketId} budget={budget} floor={Math.max(payouts, approved)} />}
    </section>
  );
}

function Figure({ label, value, hint, tone }: { label: string; value: number; hint?: string; tone?: 'bad' }) {
  return (
    <div>
      <dt className="text-sm text-zinc-500">{label}</dt>
      <dd className={`text-2xl tabular-nums tracking-tight ${tone === 'bad' ? 'text-rose-700 dark:text-rose-300' : ''}`}>
        {value < 0 ? `−${fmtCents(-value)}` : fmtCents(value)}
      </dd>
      {hint && <dd className="text-xs text-zinc-500">{hint}</dd>}
    </div>
  );
}

function Key({ swatch, hatch, label }: { swatch?: string; hatch?: string; label: string }) {
  return (
    <li className="flex items-center gap-1.5">
      <span
        aria-hidden
        className={`h-2.5 w-2.5 rounded-[2px] ring-1 ring-inset ring-zinc-400/40 ${swatch ?? ''}`}
        style={hatch ? { backgroundImage: hatch } : undefined}
      />
      {label}
    </li>
  );
}

// ---------------------------------------------------------------- paused banner + budget form

function PausedBanner({
  ticketId,
  events,
  blockedRows,
  budget,
  suggested,
}: {
  ticketId: bigint;
  events: BudgetEvent[];
  blockedRows: Payment[];
  budget: number;
  suggested: number;
}) {
  const blockedCents = blockedRows.reduce((n, p) => n + Number(p.amountCents), 0);
  const capByApproval = events.some((e) => e.limit === 'approved_cents');
  return (
    <div role="alert" className="mt-4 rounded-md border border-orange-300 bg-orange-50 p-4 text-orange-950 dark:border-orange-800 dark:bg-orange-950/30 dark:text-orange-100">
      <p className="font-semibold">
        New paid tasks are paused: {capByApproval && events.every((e) => e.limit === 'approved_cents') ? 'the approved spend is used up' : 'the budget is used up'}
      </p>
      <ul className="mt-1 space-y-1 text-sm">
        {events.map((e) => (
          <li key={e.id.toString()}>
            {e.taskType ? `${workLabel(e.taskType)}: ` : ''}
            {e.reason ?? 'budget exhausted'}
            <span className="ml-1 text-orange-800/70 dark:text-orange-200/60">
              ({e.ts.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })})
            </span>
          </li>
        ))}
        {blockedRows.length > 0 && (
          <li>
            {blockedRows.length} accepted payout{blockedRows.length === 1 ? '' : 's'} ({fmtCents(blockedCents)}) {blockedRows.length === 1 ? 'is' : 'are'} on hold
            until there is room: {blockedRows[0].error ?? 'over budget'}
          </li>
        )}
      </ul>
      {capByApproval && (
        <p className="mt-2 text-sm">
          The plan&apos;s approved spend is the limit. Approve more spend to continue.
        </p>
      )}
      <BudgetForm ticketId={ticketId} budget={budget} initial={suggested} cta="Approve more spend" />
    </div>
  );
}

function BudgetEditor({ ticketId, budget, floor }: { ticketId: bigint; budget: number; floor: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-sm text-blue-600 hover:underline focus-visible:outline-2 focus-visible:outline-blue-600"
      >
        {open ? 'Close budget' : 'Change budget'}
      </button>
      {open && (
        <div className="mt-2 max-w-xl">
          <p className="text-sm text-zinc-500">
            The budget cannot go below what is already approved or spent ({fmtCents(floor)}). Lowering it until the next task no
            longer fits pauses new paid tasks.
          </p>
          <BudgetForm ticketId={ticketId} budget={budget} initial={budget} cta="Set budget" />
        </div>
      )}
    </div>
  );
}

function BudgetForm({ ticketId, budget, initial, cta }: { ticketId: bigint; budget: number; initial: number; cta: string }) {
  const [dollars, setDollars] = useState((initial / 100).toFixed(2));
  const [state, setState] = useState<{ kind: 'idle' | 'busy' | 'ok' | 'error'; msg?: string }>({ kind: 'idle' });
  const cents = Math.round(Number(dollars) * 100);
  const valid = dollars.trim() !== '' && Number.isFinite(cents) && cents >= 0;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setState({ kind: 'busy' });
    try {
      await raiseBudget(ticketId, cents);
      setState({ kind: 'ok', msg: `Budget set to ${fmtCents(cents)}. The dispatcher picks it up and resumes paid tasks if they fit.` });
    } catch (err) {
      const m = err instanceof Tdm64ApiError ? err.message : (err as Error).message;
      setState({ kind: 'error', msg: `Budget not changed: ${m}` });
    }
  };

  const id = `tdm64-budget-${ticketId}-${cta.replace(/\W/g, '')}`;
  return (
    <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-2">
      <div>
        <label htmlFor={id} className="block text-xs text-zinc-600 dark:text-zinc-400">
          New budget (USD)
        </label>
        <div className="mt-1 flex items-center rounded-md border border-zinc-400 bg-white focus-within:outline-2 focus-within:outline-blue-600 dark:border-zinc-600 dark:bg-zinc-900">
          <span className="pl-2 text-zinc-500">$</span>
          <input
            id={id}
            inputMode="decimal"
            value={dollars}
            onChange={(e) => setDollars(e.target.value.replace(/[^0-9.]/g, ''))}
            className="w-28 bg-transparent px-1.5 py-1.5 tabular-nums outline-none"
          />
        </div>
      </div>
      <button
        type="submit"
        disabled={!valid || state.kind === 'busy' || cents === budget}
        className="rounded-md bg-zinc-900 px-3.5 py-2 text-sm font-medium text-white hover:bg-zinc-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-900 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
      >
        {state.kind === 'busy' ? 'Saving…' : cta}
      </button>
      <p className="basis-full text-xs text-zinc-500">Logged in the event log as a human change (human:web-dashboard).</p>
      {state.kind === 'ok' && <p role="status" className="basis-full text-sm text-teal-800 dark:text-teal-300">{state.msg}</p>}
      {state.kind === 'error' && <p role="alert" className="basis-full text-sm text-rose-800 dark:text-rose-300">{state.msg}</p>}
    </form>
  );
}
