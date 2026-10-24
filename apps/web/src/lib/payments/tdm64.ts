'use client';

// TDM-64: shared logic for the earnings page and the ticket spend panel.
//
// Everything here is Stripe TEST MODE. No real money moves (docs/stripe-plan.md),
// and every surface that shows an amount also shows the test-mode badge.
//
// Writes go through the Next server proxy at /api/tdm64/* (app/api/tdm64), which
// forwards a short allow-list of orchestrator routes. The browser cannot call the
// orchestrator directly for POST/PATCH (its CORS allows GET only), and it cannot
// call the payment or settings reducers itself (they need a service role).

import { useEffect, useState } from 'react';
import type { RowOf } from '@/lib/spacetime/hooks';

export type Payment = RowOf<'payment'>;
export type Episode = RowOf<'episode'>;
export type Review = RowOf<'review'>;
export type Task = RowOf<'task'>;
export type Contributor = RowOf<'contributor'>;

export const TDM64_TEST_BADGE_FALLBACK = 'Stripe TEST MODE: no real money';

// ---------------------------------------------------------------- payment statuses

export type PayStatus = 'paid' | 'pending' | 'awaiting_onboarding' | 'failed' | 'blocked_budget' | 'reversed';

export interface StatusMeta {
  label: string;
  /** What it means for the person who did the work. */
  help: string;
  /** Tailwind classes for the dot / bar segment. */
  swatch: string;
  /** Tailwind classes for the inline pill. */
  pill: string;
}

export const PAY_STATUS: Record<PayStatus, StatusMeta> = {
  paid: {
    label: 'Paid',
    help: 'A Stripe test transfer was sent to your connected account.',
    swatch: 'bg-teal-600 dark:bg-teal-400',
    pill: 'text-teal-800 bg-teal-50 ring-teal-600/30 dark:text-teal-200 dark:bg-teal-950/50',
  },
  pending: {
    label: 'Sending',
    help: 'Approved and reserved from the ticket budget. The transfer is on its way.',
    swatch: 'bg-indigo-500 dark:bg-indigo-400',
    pill: 'text-indigo-800 bg-indigo-50 ring-indigo-600/30 dark:text-indigo-200 dark:bg-indigo-950/50',
  },
  awaiting_onboarding: {
    label: 'Waiting for payout setup',
    help: 'Approved and held for you. It is sent as soon as your payout account is set up.',
    swatch: 'bg-amber-500 dark:bg-amber-400',
    pill: 'text-amber-900 bg-amber-50 ring-amber-600/40 dark:text-amber-200 dark:bg-amber-950/50',
  },
  failed: {
    label: 'Failed',
    help: 'Stripe refused the transfer. The amount went back to the ticket budget and can be retried.',
    swatch: 'bg-rose-600 dark:bg-rose-400',
    pill: 'text-rose-800 bg-rose-50 ring-rose-600/30 dark:text-rose-200 dark:bg-rose-950/50',
  },
  blocked_budget: {
    label: 'Over budget',
    help: 'Accepted, but the ticket budget or approved spend could not cover it yet.',
    swatch: 'bg-orange-700 dark:bg-orange-500',
    pill: 'text-orange-900 bg-orange-50 ring-orange-700/40 dark:text-orange-200 dark:bg-orange-950/50',
  },
  reversed: {
    label: 'Reversed',
    help: 'The transfer was taken back after the episode was found bad.',
    swatch: 'bg-zinc-400 dark:bg-zinc-500',
    pill: 'text-zinc-700 bg-zinc-100 ring-zinc-500/30 dark:text-zinc-300 dark:bg-zinc-800',
  },
};

export function payStatus(s: string): StatusMeta {
  return PAY_STATUS[s as PayStatus] ?? {
    label: s.replace(/_/g, ' '),
    help: '',
    swatch: 'bg-zinc-400',
    pill: 'text-zinc-700 bg-zinc-100 ring-zinc-500/30 dark:text-zinc-300 dark:bg-zinc-800',
  };
}

/** Statuses that count as money owed to (or already sent to) the contributor. */
export const EARNED_STATUSES: readonly PayStatus[] = ['paid', 'pending', 'awaiting_onboarding'];
/** Statuses that hold ticket budget (the reducer's reserved set: counted in ticket.spent_cents). */
export const RESERVED_STATUSES: readonly string[] = ['pending', 'awaiting_onboarding', 'paid'];

export function sumCents(rows: readonly Payment[], statuses: readonly string[]): number {
  let n = 0;
  for (const p of rows) if (statuses.includes(p.status)) n += Number(p.amountCents);
  return n;
}

export function totalsByStatus(rows: readonly Payment[]): Record<string, { cents: number; count: number }> {
  const out: Record<string, { cents: number; count: number }> = {};
  for (const p of rows) {
    const t = (out[p.status] ??= { cents: 0, count: 0 });
    t.cents += Number(p.amountCents);
    t.count += 1;
  }
  return out;
}

// ---------------------------------------------------------------- what a payment was for

export const WORK_LABEL: Record<string, string> = {
  clean_teleop: 'Clean demo',
  recovery_takeover: 'Recovery takeover',
  reset: 'Scene reset',
  purchased: 'Dataset batch',
};

export function workLabel(kind: string): string {
  return WORK_LABEL[kind] ?? kind.replace(/_/g, ' ');
}

/** Task id from a reset payment's batch_ref ("task:<id>"), else undefined. */
export function taskIdFromBatchRef(ref: string | undefined): bigint | undefined {
  if (!ref || !ref.startsWith('task:')) return undefined;
  try {
    return BigInt(ref.slice(5));
  } catch {
    return undefined;
  }
}

/** The work type a payment pays for: episode kind, the task's type, or a dataset batch. */
export function paymentKind(p: Payment, episodes: Map<bigint, Episode>, tasks: Map<bigint, Task>): string {
  if (p.datasetOfferId !== 0n) return 'purchased';
  if (p.episodeId !== 0n) {
    const e = episodes.get(p.episodeId);
    if (e) return e.kind;
  }
  const tid = taskIdFromBatchRef(p.batchRef);
  if (tid != null) return tasks.get(tid)?.type ?? 'reset';
  return 'other';
}

// ---------------------------------------------------------------- dispatcher budget events

export interface BudgetEvent {
  id: bigint;
  ts: Date;
  type: string;
  ticketId: bigint;
  dataPlanId: bigint;
  limit?: 'ticket_budget' | 'approved_cents' | string;
  reason?: string;
  need?: number;
  unitPriceCents?: number;
  budgetRemainingCents?: number;
  taskType?: string;
}

export function parseBudgetEvent(row: RowOf<'eventLog'>): BudgetEvent | undefined {
  try {
    const p = JSON.parse(row.payloadJson) as Record<string, unknown>;
    if (p.ticket_id == null) return undefined;
    const big = (v: unknown) => (v == null ? 0n : BigInt(String(v)));
    const num = (v: unknown) => (v == null ? undefined : Number(v));
    return {
      id: row.id,
      ts: row.ts.toDate(),
      type: row.type,
      ticketId: big(p.ticket_id),
      dataPlanId: big(p.data_plan_id),
      limit: p.limit as string | undefined,
      reason: p.reason as string | undefined,
      need: num(p.need),
      unitPriceCents: num(p.unit_price_cents),
      budgetRemainingCents: num(p.budget_remaining_cents),
      taskType: (p.task_type as string | undefined) ?? (p.data_type as string | undefined),
    };
  } catch {
    return undefined;
  }
}

// ---------------------------------------------------------------- orchestrator calls (via /api/tdm64)

export class Tdm64ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function detailText(body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === 'object' && 'msg' in x ? String(x.msg) : String(x))).join('; ');
  }
  return typeof body === 'string' ? body : JSON.stringify(body);
}

async function tdm64Call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/tdm64/${path}`, {
    ...init,
    headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // keep the raw text
  }
  if (!res.ok) throw new Tdm64ApiError(res.status, detailText(body) || `HTTP ${res.status}`);
  return body as T;
}

export function startOnboarding(contributorId: bigint, returnUrl: string) {
  return tdm64Call<{ onboarding_url: string; stripe_account_id: string }>(
    `payments/contributors/${contributorId}/onboarding`,
    { method: 'POST', body: JSON.stringify({ return_url: returnUrl, refresh_url: returnUrl }) },
  );
}

export function refreshPayouts(contributorId: bigint) {
  return tdm64Call<{ payouts_enabled: boolean; transfers_status: string | null }>(
    `payments/contributors/${contributorId}/refresh`,
    { method: 'POST', body: '{}' },
  );
}

/** PATCH /tickets/{id}/settings {budget_cents}; the orchestrator logs it as actor human:<humanId>. */
export function raiseBudget(ticketId: bigint, budgetCents: number, humanId = 'web-dashboard') {
  return tdm64Call<unknown>(`tickets/${ticketId}/settings`, {
    method: 'PATCH',
    headers: { 'x-human-id': humanId },
    body: JSON.stringify({ budget_cents: budgetCents }),
  });
}

/** Badge text from GET /payments/mode, or the fixed fallback when the orchestrator is not reachable. */
export function useTestModeBadge(): { text: string; live: boolean } {
  const [state, setState] = useState<{ text: string; live: boolean }>({ text: TDM64_TEST_BADGE_FALLBACK, live: false });
  useEffect(() => {
    let alive = true;
    tdm64Call<{ mode: string; badge: string }>('payments/mode')
      .then((r) => {
        // Never show anything that reads as live money: only accept a test-mode answer.
        if (alive && r.mode === 'test' && r.badge) setState({ text: r.badge, live: true });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return state;
}
