'use client';

// TDM-116: Cancel ticket. Operator only, with a confirm step (a real <dialog>: it is destructive and
// cannot be undone). POST /api/tdm116/tickets/{id}/cancel -> set_ticket_status(id, "cancelled", note).
// TDM-114 releases the ticket's robot job on the status change; the cockpit says "Robot released" once
// the robot is no longer busy for this ticket.

import { useEffect, useRef, useState } from 'react';
import { Button, ButtonLink } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import { TDM116_TERMINAL } from '@/lib/tdm116/cockpit';

export function Tdm116CancelDialog({ ticketId, open, onClose }: { ticketId: number; open: boolean; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [reason, setReason] = useState('');
  const [state, setState] = useState<'idle' | 'busy' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  const { operator } = useOperator();

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      setState('idle');
      setMsg('');
      d.showModal();
    } else if (!open && d.open) d.close();
  }, [open]);

  const submit = async () => {
    setState('busy');
    try {
      const r = await fetch(`/api/tdm116/tickets/${ticketId}/cancel`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason }),
      });
      const j = (await r.json().catch(() => ({}))) as { ok?: boolean; detail?: unknown };
      if (r.ok && j.ok) {
        setReason('');
        onClose();
        return;
      }
      setState('error');
      setMsg(typeof j.detail === 'string' ? j.detail : `HTTP ${r.status}`);
    } catch (e) {
      setState('error');
      setMsg((e as Error).message);
    }
  };

  const ref4 = `TKT-${String(ticketId).padStart(4, '0')}`;
  return (
    <dialog ref={ref} className="tdm116-dialog" onClose={onClose} aria-labelledby="tdm116-cancel-title">
      <h2 id="tdm116-cancel-title">Cancel {ref4}?</h2>
      <p className="gx-small m-0">
        The line stops for this ticket and its robot job is released. A cancelled ticket cannot be restarted.
      </p>
      {operator === false ? (
        <div className="tdm116-dialog-actions">
          <Button onClick={onClose}>Keep running</Button>
          <ButtonLink href={operatorLoginHref(typeof window === 'undefined' ? '/dashboard' : window.location.pathname + window.location.search)} variant="primary">
            Log in as operator
          </ButtonLink>
        </div>
      ) : (
        <>
          <label>
            Reason (logged with the status change)
            <textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} placeholder="Wrong task, robot needed elsewhere, ..." />
          </label>
          {state === 'error' && (
            <p className="mt-3 mb-0 text-sm text-slag" role="alert">
              Not cancelled: {msg}
            </p>
          )}
          <div className="tdm116-dialog-actions">
            <Button onClick={onClose} disabled={state === 'busy'}>
              Keep running
            </Button>
            <Button variant="danger" onClick={submit} disabled={state === 'busy' || operator === null} data-testid="tdm116-cancel-confirm">
              {state === 'busy' ? 'Cancelling…' : 'Cancel ticket'}
            </Button>
          </div>
        </>
      )}
    </dialog>
  );
}

/** After a cancel: is the robot free yet? TDM-114 ends the robot job; the robot row goes back to online. */
export function Tdm116RobotReleased({ status, robotStatus, holderTicketId, ticketId }: {
  status: string;
  robotStatus?: string;
  /** TDM-114 floor.holder.ticketId (0 / undefined when nothing holds the robot). */
  holderTicketId?: number;
  ticketId: number;
}) {
  if (status !== 'cancelled' || !robotStatus) return null;
  const stillOurs = holderTicketId === ticketId;
  if (!stillOurs && robotStatus !== 'busy') return <Tag tone="shipped" title={`Robot status: ${robotStatus}`}>Robot released</Tag>;
  return <Tag tone="steel" title={`Robot status: ${robotStatus}`}>Releasing robot</Tag>;
}

/** The Settings tab's cancel block. */
export function Tdm116CancelBlock({ ticketId, status }: { ticketId: number; status: string }) {
  const [open, setOpen] = useState(false);
  const ended = TDM116_TERMINAL.has(status);
  return (
    <section className="tdm116-plate" aria-labelledby="tdm116-cancel-h">
      <div className="tdm116-plate-head">
        <h3 id="tdm116-cancel-h">Cancel ticket</h3>
      </div>
      <p className="gx-small mt-0 mb-3">Stops the line for this ticket and releases its robot. Operator only. Cannot be undone.</p>
      <Button variant="danger" onClick={() => setOpen(true)} disabled={ended} disabledReason={`This ticket is already ${status}.`} data-testid="tdm116-cancel-open">
        Cancel ticket
      </Button>
      {ended && <p className="gx-small mt-2 mb-0">This ticket is {status}.</p>}
      <Tdm116CancelDialog ticketId={ticketId} open={open} onClose={() => setOpen(false)} />
    </section>
  );
}
