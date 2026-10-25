'use client';

// TDM-57: exclusive-control UI.
//   <ControlLeaseBanner lease={useTeleopLease(taskId)} />   teleop page (TDM-56 mounts it): who has
//       control, time left, "you lost control" + Reclaim, view-only when someone else drives.
//   <ControlHolderPanel robotId={id} />                    dashboard robot page: the live holder's
//       name and lease, with Revoke (release_task) and Revoke + cancel (cancel_task).

import { useState } from 'react';
import Link from 'next/link';
import { OneLine } from '@/components/teleop/TeleopOverlay';
import { fmtCountdown, typeInfo } from '@/lib/marketplace/tdm61';
import { LOST_TEXT, useControlHolders, type TeleopLease } from '@/lib/teleop/tdm57Lease';

type Tone = 'good' | 'warn' | 'bad' | 'muted';
const TONE: Record<Tone, string> = {
  good: 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-100',
  warn: 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100',
  bad: 'border-red-300 bg-red-50 text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-100',
  muted: 'border-zinc-300 bg-zinc-50 text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200',
};
const BTN =
  'min-h-11 rounded-md px-4 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50';

/** `variant="bar"`: one fixed-height row (title + one truncated line, "More" for the rest), for the teleop top bar,
 *  where a message that grows would push the viewer down. */
export function ControlLeaseBanner({ lease, className = '', variant = 'banner' }: { lease: TeleopLease; className?: string; variant?: 'banner' | 'bar' }) {
  const { state, canControl, blockedBy, secondsLeft, holderName, lostReason, lostWhileHidden, error, busy } = lease;
  let tone: Tone = 'muted';
  let title = '';
  let body: string | undefined;
  let action: { label: string; run: () => void; disabled?: boolean } | undefined;

  switch (state) {
    case 'connecting':
      title = 'Connecting…';
      break;
    case 'not_joined':
      tone = 'warn';
      title = 'Join as a contributor to drive the arm.';
      break;
    case 'no_task':
      tone = 'bad';
      title = 'This task does not exist.';
      break;
    case 'claiming':
      title = 'Taking control…';
      break;
    case 'held':
      if (canControl) {
        tone = 'good';
        title = `You have control · ${fmtCountdown((secondsLeft ?? 0) * 1000)}`;
        body = 'Your hold renews itself while this page stays open and in front.';
      } else {
        tone = 'warn';
        title = 'Controls paused';
        body =
          blockedBy === 'hidden'
            ? `This tab is in the background. Come back within ${secondsLeft ?? 0} s to keep control.`
            : blockedBy === 'offline'
              ? 'Reconnecting to the server. Controls are off until it is back.'
              : 'Renewing your hold…';
      }
      action = { label: 'Give back control', run: () => void lease.release(), disabled: busy };
      break;
    case 'view_only':
      title = `Controlled by ${holderName ?? 'someone else'}`;
      body = 'You are watching. Controls unlock if they give it back or their hold runs out.';
      break;
    case 'available':
      tone = 'warn';
      title = 'Nobody is driving right now.';
      action = { label: 'Take control', run: () => void lease.reclaim(), disabled: busy };
      break;
    case 'lost': {
      tone = 'bad';
      title = 'You lost control';
      const why = lostReason ? LOST_TEXT[lostReason] : '';
      body = lostWhileHidden && lostReason === 'expired' ? `${why} The tab was in the background.` : why;
      const closed = lostReason === 'revoked' || lostReason === 'completed';
      const othersLive = lease.task?.status === 'claimed' && holderName !== undefined;
      if (!closed) {
        action = {
          label: othersLive ? `Controlled by ${holderName}` : 'Reclaim control',
          run: () => void lease.reclaim(),
          disabled: busy || othersLive,
        };
      }
      break;
    }
    case 'closed':
      title = `This task is ${lease.task?.status ?? 'closed'}.`;
      break;
  }

  if (variant === 'bar') {
    const line = error ?? body;
    return (
      <div
        role="status"
        aria-live="polite"
        data-tdm57-state={state}
        data-tdm57-can-control={canControl ? 'true' : 'false'}
        data-tdm57-lost={lostReason ?? ''}
        data-variant="bar"
        className={`flex h-14 min-w-0 items-center gap-3 rounded-lg border px-3 ${TONE[tone]} ${className}`}
      >
        <div className="min-w-0 flex-1 leading-tight">
          <p className="truncate text-sm font-semibold" title={title}>
            {title}
          </p>
          {state === 'not_joined' ? (
            <Link href="/join" className="text-xs font-semibold underline">
              Join
            </Link>
          ) : (
            line && <OneLine text={line} className={`text-xs ${error ? 'font-medium text-red-700 dark:text-red-300' : 'opacity-90'}`} testId="tdm57-bar-line" />
          )}
        </div>
        {action && (
          <button
            type="button"
            onClick={action.run}
            disabled={action.disabled}
            className={`h-10 shrink-0 whitespace-nowrap rounded-md px-3 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${tone === 'good' ? 'border border-current bg-transparent' : 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'}`}
          >
            {action.label}
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      role="status"
      aria-live="polite"
      data-tdm57-state={state}
      data-tdm57-can-control={canControl ? 'true' : 'false'}
      data-tdm57-lost={lostReason ?? ''}
      className={`flex flex-wrap items-center gap-3 rounded-lg border px-4 py-3 ${TONE[tone]} ${className}`}
    >
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {body && <p className="text-sm opacity-90">{body}</p>}
        {state === 'not_joined' && (
          <Link href="/join" className="text-sm font-semibold underline">
            Join
          </Link>
        )}
        {error && <p className="text-sm font-medium text-red-700 dark:text-red-300">{error}</p>}
      </div>
      {action && (
        <button
          type="button"
          onClick={action.run}
          disabled={action.disabled}
          className={`${BTN} ${tone === 'good' ? 'border border-current bg-transparent' : 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'}`}
        >
          {action.label}
        </button>
      )}
    </div>
  );
}

export function ControlHolderPanel({ robotId }: { robotId: bigint | number | string }) {
  let id = 0n;
  try {
    id = BigInt(robotId);
  } catch {
    /* bad id: shows nobody */
  }
  const holders = useControlHolders(id);
  const [busy, setBusy] = useState<bigint | undefined>();
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | undefined>();

  async function revoke(taskId: bigint, cancel: boolean) {
    const verb = cancel ? 'Revoke control and cancel task' : 'Revoke control of task';
    if (!window.confirm(`${verb} #${taskId}? The operator's controls stop within a few seconds.`)) return;
    setBusy(taskId);
    setMsg(undefined);
    try {
      const res = await fetch(`/api/tdm57/tasks/${taskId}/revoke`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason: cancel ? 'operator revoke + cancel (robot page)' : 'operator revoke (robot page)', cancel }),
      });
      const body = (await res.json().catch(() => ({}))) as { detail?: unknown; action?: string };
      if (res.ok) setMsg({ text: `Task #${taskId} ${body.action ?? 'revoked'}.`, bad: false });
      else setMsg({ text: typeof body.detail === 'string' ? body.detail : `Revoke failed (${res.status})`, bad: true });
    } catch (e) {
      setMsg({ text: (e as Error).message, bad: true });
    } finally {
      setBusy(undefined);
    }
  }

  return (
    <div data-tdm57-holders={holders.length} className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      {holders.length === 0 ? (
        <p className="text-sm text-zinc-500">Nobody holds control of this arm.</p>
      ) : (
        <ul className="space-y-3">
          {holders.map(({ task, holder, msLeft }) => (
            <li key={String(task.id)} className="flex flex-wrap items-center gap-3">
              <span aria-hidden className="h-2.5 w-2.5 rounded-full bg-emerald-500" />
              <div className="min-w-0 flex-1">
                <p className="font-semibold" data-tdm57-holder-name>
                  {holder?.name || `contributor ${task.assigneeId}`}
                </p>
                <p className="text-xs text-zinc-500">
                  {typeInfo(task.type).label} · task #{String(task.id)} · lease {fmtCountdown(msLeft)}
                </p>
              </div>
              <button
                type="button"
                className={`${BTN} border border-red-400 text-red-700 dark:text-red-300`}
                disabled={busy === task.id}
                onClick={() => void revoke(task.id, false)}
              >
                Revoke
              </button>
              <button
                type="button"
                className={`${BTN} bg-red-600 text-white`}
                disabled={busy === task.id}
                onClick={() => void revoke(task.id, true)}
              >
                Revoke + cancel
              </button>
            </li>
          ))}
        </ul>
      )}
      {msg && <p className={`mt-3 text-sm ${msg.bad ? 'text-red-700 dark:text-red-300' : 'text-emerald-700 dark:text-emerald-300'}`}>{msg.text}</p>}
    </div>
  );
}
