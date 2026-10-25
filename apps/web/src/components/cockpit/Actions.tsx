'use client';

// TDM-116: the cockpit's action buttons. One primary (melt) per view, the step that moves the ticket:
// Approve plan, Take over, View report. Approve goes through the operator-gated server proxy
// POST /api/spend/plans/{id}/approve (approve_spend at the plan's estimate). The robot-wait actions
// (claim now, resume, clear e-stop) are TDM-114's WaitingForRobot, mounted by the stage views.

import { useState } from 'react';
import { Button, ButtonLink, type GxButtonVariant } from '@/components/brand/Button';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import type { Tdm116Action } from '@/lib/tdm116/cockpit';

type PostState = { state: 'idle' | 'busy' | 'done' | 'error'; msg: string };

function useOperatorPost() {
  const [s, setS] = useState<PostState>({ state: 'idle', msg: '' });
  const post = async (url: string, body: unknown) => {
    setS({ state: 'busy', msg: '' });
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
      const j = (await r.json().catch(() => ({}))) as { ok?: boolean; detail?: unknown };
      if (r.ok && j.ok !== false) setS({ state: 'done', msg: '' });
      else {
        const d = j.detail as { message?: string } | string | undefined;
        setS({ state: 'error', msg: (typeof d === 'string' ? d : d?.message) || `HTTP ${r.status}` });
      }
    } catch (e) {
      setS({ state: 'error', msg: (e as Error).message });
    }
  };
  return { ...s, post };
}

function here() {
  return typeof window === 'undefined' ? '/dashboard' : `${window.location.pathname}${window.location.search}`;
}

/** The top bar's one primary action (see tdm116PrimaryAction), or the TDM-114 robot wait's own action. */
export function Tdm116PrimaryButton({ action, ticketId, size = 'md', variant = 'primary' }: {
  action: Tdm116Action;
  ticketId: number;
  size?: 'md' | 'lg';
  variant?: GxButtonVariant;
}) {
  const { operator } = useOperator();
  const req = useOperatorPost();
  if (!action) return null;
  const cls = size === 'lg' ? 'tdm116-btn-lg' : undefined;
  if (action.kind === 'takeover' || action.kind === 'report') {
    return (
      <ButtonLink href={action.href} variant={variant} className={cls} data-testid="tdm116-primary">
        {action.label}
      </ButtonLink>
    );
  }
  // approve: operator only
  if (operator === false) {
    return (
      <ButtonLink href={operatorLoginHref(here())} variant="default" className={cls} title="Approving spend needs the operator login" data-testid="tdm116-primary">
        Log in to approve
      </ButtonLink>
    );
  }
  return (
    <span className="inline-flex flex-col items-start gap-1">
      <Button
        variant={variant}
        className={cls}
        data-testid="tdm116-primary"
        disabled={operator === null || req.state === 'busy' || req.state === 'done'}
        onClick={() => req.post(`/api/spend/plans/${action.planId}/approve`, { ticket_id: ticketId })}
      >
        {req.state === 'done' ? 'Approved' : req.state === 'busy' ? 'Approving…' : action.label}
      </Button>
      {req.state === 'error' && (
        <span className="text-xs text-slag" role="alert">
          Not approved: {req.msg}
        </span>
      )}
    </span>
  );
}
