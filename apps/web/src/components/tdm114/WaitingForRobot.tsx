'use client';

// TDM-114: honest robot status, mountable anywhere (ticket cockpit, station drawer, robot page).
//
//   <WaitingForRobot floor={floor} />            the ochre "Waiting for <robot>" card + operator buttons
//                                                (renders nothing unless floor.waitingFor is set)
//   <RobotQueueList queue={floor.robotQueue} />  what the robot runs now and what's queued next
//   <EvalProgressLine phase={floor.evalPhase} /> trial x/N · phase · last trial · ETA (SEEDED label)
//   <Tdm114ClearEstop robotId sim />             sim robots only: operator "Clear e-stop"
//
// Buttons need the operator login (the API routes check it too); each action is logged in event_log by
// the orchestrator (robot.reassigned / robot.claim_now / robot.resume_requested / robot.estop_clear_requested).

import { useState } from 'react';
import { Button, ButtonLink } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import type { Tdm112Floor, Tdm114EvalPhase, Tdm114QueueEntry, Tdm114WaitingFor } from '@/lib/tdm112/floor';

type Phase = 'idle' | 'busy' | 'done' | 'error';

function useTdm114Action() {
  const [st, setSt] = useState<{ phase: Phase; msg: string }>({ phase: 'idle', msg: '' });
  const run = async (url: string, okMsg: (j: Record<string, unknown>) => string) => {
    setSt({ phase: 'busy', msg: '' });
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
      const j = (await r.json().catch(() => ({}))) as Record<string, unknown>;
      if (r.ok) setSt({ phase: 'done', msg: okMsg(j) });
      else {
        const d = j.detail as { message?: string } | string | undefined;
        setSt({ phase: 'error', msg: (typeof d === 'string' ? d : d?.message) || `HTTP ${r.status}` });
      }
    } catch (e) {
      setSt({ phase: 'error', msg: (e as Error).message });
    }
  };
  return { ...st, run };
}

const CLAIM_OUTCOME: Record<string, string> = {
  preempted: 'The other ticket is stopping. This eval starts as soon as the robot is reset.',
  queued: "This ticket is first in line; it starts when the robot's current step finishes.",
  started: 'Eval starting.',
};

export function WaitingForRobot({ floor, waitingFor, ticketId, className }: { floor?: Tdm112Floor | null; waitingFor?: Tdm114WaitingFor; ticketId?: number; className?: string }) {
  const w = waitingFor ?? floor?.waitingFor;
  const tid = ticketId ?? floor?.ticketId ?? 0;
  const { operator } = useOperator();
  const act = useTdm114Action();
  if (!w) return null;
  const h = w.holder;
  const anyAction = w.actions.claimNow || w.actions.resume || w.actions.clearEstop;
  const here = typeof window !== 'undefined' ? window.location.pathname : '/';
  return (
    <section
      className={gxCx('rounded-sm border-l-4 border-ochre bg-ochre-soft px-3 py-2', className)}
      data-testid="tdm114-waiting"
      data-reason={w.reason}
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone="money">{w.reason === 'robot_reassigned' ? 'Paused' : 'Waiting for robot'}</Tag>
        {h?.ticketRef && h.ticketId !== tid && <span className="gx-id">held by {h.ticketRef}</span>}
      </div>
      <p className="m-0 mt-1 text-[15px] text-ink" data-testid="tdm114-waiting-text">
        {w.text}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {w.actions.takeOverTaskId ? (
          <ButtonLink small href={`/teleop/${w.actions.takeOverTaskId}`}>
            Take over task #{w.actions.takeOverTaskId}
          </ButtonLink>
        ) : null}
        {anyAction && operator === false && (
          <a className="gx-small underline" href={operatorLoginHref(here)}>
            Log in as operator to act
          </a>
        )}
        {operator && w.actions.claimNow && (
          <Button
            small
            variant="primary"
            disabled={act.phase === 'busy' || act.phase === 'done'}
            onClick={() => act.run(`/api/tdm114/tickets/${tid}/robot/claim_now`, (j) => CLAIM_OUTCOME[String(j.outcome)] ?? 'Done.')}
          >
            {act.phase === 'busy' ? 'Asking the robot…' : `Pause ${h?.ticketRef || 'the other job'}, run this now`}
          </Button>
        )}
        {operator && w.actions.resume && (
          <Button small disabled={act.phase === 'busy' || act.phase === 'done'} onClick={() => act.run(`/api/tdm114/tickets/${tid}/robot/resume`, () => 'Resumed. Collection starts again once the robot is free.')}>
            Resume {floor ? `TKT-${String(tid).padStart(4, '0')}` : 'this ticket'} later
          </Button>
        )}
        {operator && w.actions.clearEstop && (
          <Button small variant="danger" disabled={act.phase === 'busy' || act.phase === 'done'} onClick={() => act.run(`/api/tdm114/robots/${w.robotId}/estop/clear`, () => 'Clear sent. The sim robot unlocks in a second.')}>
            Clear e-stop (sim)
          </Button>
        )}
      </div>
      {act.phase === 'done' && <p className="gx-small m-0 mt-1">{act.msg}</p>}
      {act.phase === 'error' && <p className="gx-small m-0 mt-1 text-slag">Not done: {act.msg}</p>}
    </section>
  );
}

const STATE_WORD: Record<Tdm114QueueEntry['state'], string> = { now: 'Now', queued: 'Next', paused: 'Paused' };
const PHASE_WORD: Record<string, string> = {
  running: 'running',
  paused_for_takeover: 'paused for a takeover',
  taken_over: 'a person is driving',
  teleop: 'teleop',
  waiting_reset: 'waiting for a reset',
};

export function RobotQueueList({ queue, dark = false, className }: { queue: readonly Tdm114QueueEntry[]; dark?: boolean; className?: string }) {
  if (!queue.length) return <p className={gxCx('m-0 text-sm', dark ? 'text-zinc-400' : 'gx-small', className)}>The robot is free. Nothing queued.</p>;
  let nextShown = false;
  return (
    <ol className={gxCx('m-0 grid list-none gap-1 p-0 text-sm', className)} data-testid="tdm114-queue">
      {queue.map((q, i) => {
        const word = q.state === 'queued' ? (nextShown ? 'Queued' : ((nextShown = true), STATE_WORD.queued)) : STATE_WORD[q.state];
        return (
          <li key={`${q.ticketId}-${q.job}-${i}`} className="flex flex-wrap items-baseline gap-x-2" data-state={q.state}>
            <span className={gxCx('w-14 shrink-0 text-xs font-semibold uppercase tracking-wide', dark ? 'text-zinc-400' : 'text-steel')}>{word}</span>
            <span className={gxCx('font-medium', q.isThisTicket && 'underline decoration-dotted')}>{q.ticketRef || 'robot'}</span>
            <span>{q.jobLabel}</span>
            {q.phase && <span className={dark ? 'text-zinc-400' : 'gx-small'}>{PHASE_WORD[q.phase] ?? q.phase}</span>}
            {q.state !== 'now' && q.note && <span className={dark ? 'text-zinc-500' : 'gx-small'}>({q.note})</span>}
          </li>
        );
      })}
    </ol>
  );
}

export function EvalProgressLine({ phase, className }: { phase?: Tdm114EvalPhase; className?: string }) {
  if (!phase) return null;
  return (
    <p className={gxCx('m-0 flex flex-wrap items-center gap-2 text-sm', className)} data-testid="tdm114-eval-progress">
      {phase.seeded && <Tag tone="failed">SEEDED</Tag>}
      <span className="tabular-nums">{phase.label}</span>
    </p>
  );
}

/** Sim robots only: the operator "Clear e-stop" button (real arms are cleared by a person at the arm). */
export function Tdm114ClearEstop({ robotId, sim, className }: { robotId: number | bigint | string; sim: boolean; className?: string }) {
  const { operator } = useOperator();
  const act = useTdm114Action();
  if (!sim) return null;
  const here = typeof window !== 'undefined' ? window.location.pathname : '/';
  if (operator === false)
    return (
      <p className={gxCx('m-0 text-sm', className)}>
        This is a simulated robot: <a className="underline" href={operatorLoginHref(here)}>log in as operator</a> to clear the e-stop from here.
      </p>
    );
  if (!operator) return null;
  return (
    <div className={gxCx('flex flex-wrap items-center gap-2', className)}>
      <button
        type="button"
        data-testid="tdm114-clear-estop"
        disabled={act.phase === 'busy'}
        onClick={() => act.run(`/api/tdm114/robots/${robotId}/estop/clear`, () => 'Clear sent; the sim robot unlocks in a second.')}
        className="rounded bg-white px-3 py-1.5 text-sm font-semibold text-zinc-950 disabled:opacity-50"
      >
        {act.phase === 'busy' ? 'Clearing…' : 'Clear e-stop (sim robot)'}
      </button>
      {act.msg && <span className={act.phase === 'error' ? 'text-sm text-red-300' : 'text-sm text-zinc-300'}>{act.phase === 'error' ? `Not cleared: ${act.msg}` : act.msg}</span>}
    </div>
  );
}
