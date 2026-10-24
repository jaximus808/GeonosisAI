'use client';

// "Approve plan": operator-only button on a proposed data plan (POST /api/spend/plans/{id}/approve,
// which calls approve_spend at the plan's estimated cost). Hidden for anyone not logged in as operator.
import { useState } from 'react';
import { useOperator } from '@/lib/operator/useOperator';

export function ApprovePlanButton({ ticketId, planId, estCostCents, status }: {
  ticketId: number | string; planId: number; estCostCents: number; status: string;
}) {
  const { operator } = useOperator();
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  if (status !== 'proposed') return null;
  if (!operator) {
    return <p className="text-xs text-zinc-500"><a className="underline" href="/ops/login">Log in as operator</a> to approve this plan.</p>;
  }
  const approve = async () => {
    setState('busy');
    try {
      const r = await fetch(`/api/spend/plans/${planId}/approve`, {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ticket_id: Number(ticketId) }),
      });
      const j = (await r.json().catch(() => ({}))) as { ok?: boolean; detail?: string };
      if (r.ok && j.ok) setState('done');
      else { setState('error'); setMsg(j.detail || `HTTP ${r.status}`); }
    } catch (e) {
      setState('error');
      setMsg((e as Error).message);
    }
  };
  return (
    <div className="mt-1 flex flex-wrap items-center gap-3">
      <button type="button" onClick={approve} disabled={state === 'busy' || state === 'done'}
        className="rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">
        {state === 'done' ? 'Approved' : state === 'busy' ? 'Approving…' : `Approve plan (${(estCostCents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' })}, test mode)`}
      </button>
      {state === 'error' && <span className="text-xs text-red-700 dark:text-red-400">Not approved: {msg}</span>}
    </div>
  );
}
