'use client';

// The robot page's eval / collection controls, shown above everything else while they matter:
//
//   TDM-51  "Trial N: Pass / Fail (+note)" while the robot waits for the operator's verdict
//           (eval.trial_awaiting_outcome). Sends trial_outcome {success, note, eval_run_id, index}
//           through /api/tdm78/robots/{id}/commands. The robot is the judge of whether it still
//           counts: a late, duplicate or wrong-trial verdict fails the command, and that refusal is
//           shown here in red (also when someone else sent it).
//   TDM-69  "Failing" during a collection rollout (run_baseline purpose collect_recovery): pauses
//           the policy and routes a paid takeover task, via /api/tdm78/robots/{id}/interventions/flag
//           (orchestrator POST /robots/{id}/interventions/flag, operator key added server side).
//
// Both need an operator login (the routes answer 401 without one); the buttons are disabled until then.

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import type { CollectRolloutView, InterventionView, TrialPromptView, VerdictView } from '@/lib/tdm78/useRobotConsole';

const REFUSAL_SHOWN_MS = 5 * 60_000;

interface SendState {
  ok: boolean;
  text: string;
}

function secs(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return s < 120 ? `${s} s` : `${Math.ceil(s / 60)} min`;
}

function LoginNote({ robotId, what }: { robotId: string; what: string }) {
  return (
    <p className="text-sm">
      {what} needs an operator.{' '}
      <Link href={operatorLoginHref(`/robots/${robotId}`)} className="font-medium text-sky-700 underline dark:text-sky-400">
        Log in as operator
      </Link>
      .
    </p>
  );
}

export function Tdm78TrialPanel({
  robotId,
  trial,
  verdicts,
  collect,
  intervention,
  now,
  contributorName,
}: {
  robotId: string;
  trial: TrialPromptView | null;
  verdicts: readonly VerdictView[];
  collect: CollectRolloutView | null;
  intervention: InterventionView | null;
  now: number;
  contributorName: (id: bigint) => string | undefined;
}) {
  const op = useOperator();
  const notOperator = op.operator !== true;

  // Refusals: the newest trial_outcome the robot failed, unless a later verdict went through or a
  // newer trial has asked for its verdict since (then the prompt is what matters).
  const newest = verdicts[0];
  const refusal =
    newest &&
    newest.status !== 'issued' &&
    newest.status !== 'acked' &&
    newest.error &&
    now - (newest.finishedAt ?? newest.issuedAt) < REFUSAL_SHOWN_MS &&
    !(trial && newest.issuedAt < trial.askedAt)
      ? newest
      : null;

  if (!trial && !collect && !refusal) return null;
  return (
    <div className="space-y-3">
      {trial && <TrialPrompt robotId={robotId} trial={trial} verdicts={verdicts} now={now} notOperator={notOperator} onUnauthorized={op.recheck} checking={op.operator === null} />}
      {refusal && <Refusal v={refusal} />}
      {collect && (
        <FailingPanel
          robotId={robotId}
          collect={collect}
          intervention={intervention && intervention.commandId === collect.commandId ? intervention : null}
          notOperator={notOperator}
          checking={op.operator === null}
          onUnauthorized={op.recheck}
          now={now}
          contributorName={contributorName}
        />
      )}
    </div>
  );
}

function Refusal({ v }: { v: VerdictView }) {
  return (
    <div role="alert" className="rounded-lg border-2 border-red-600 bg-red-50 px-4 py-3 text-red-900 dark:border-red-500 dark:bg-red-950 dark:text-red-100">
      <p className="font-semibold">
        The robot refused verdict #{v.seq}
        {v.index != null ? ` (trial ${v.index + 1}: ${v.success ? 'PASS' : 'FAIL'})` : ''}
      </p>
      <p className="mt-1 text-sm">{v.error}</p>
      <p className="mt-1 text-xs opacity-80">Recorded trials are never changed. A verdict only counts for the trial the robot is waiting on right now.</p>
    </div>
  );
}

function TrialPrompt({
  robotId,
  trial,
  verdicts,
  now,
  notOperator,
  checking,
  onUnauthorized,
}: {
  robotId: string;
  trial: TrialPromptView;
  verdicts: readonly VerdictView[];
  now: number;
  notOperator: boolean;
  checking: boolean;
  onUnauthorized: () => void;
}) {
  const [note, setNote] = useState('');
  const [pending, setPending] = useState<'pass' | 'fail' | null>(null);
  const [sent, setSent] = useState<SendState | null>(null);
  const key = `${trial.evalRunId}:${trial.index}:${trial.askedAt}`;
  useEffect(() => {
    setNote('');
    setSent(null);
  }, [key]);

  // the robot's answer to a verdict for THIS trial (since it asked)
  const mine = verdicts.filter((v) => v.evalRunId === trial.evalRunId && v.index === trial.index && v.issuedAt >= trial.askedAt - 60_000);
  const accepted = mine.find((v) => v.status === 'done');
  const inFlight = mine.find((v) => v.status === 'issued' || v.status === 'acked');
  const left = trial.deadline != null ? trial.deadline - now : null;
  const n = trial.index + 1;
  const of = trial.trials ? ` of ${trial.trials}` : '';
  const title = `Trial ${n}${of}: did it succeed?`;

  async function send(success: boolean) {
    setPending(success ? 'pass' : 'fail');
    setSent(null);
    try {
      const res = await fetch(`/api/tdm78/robots/${robotId}/commands`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          type: 'trial_outcome',
          payload: { success, note: note.trim(), eval_run_id: trial.evalRunId, index: trial.index },
          ticket_id: trial.ticketId,
        }),
        cache: 'no-store',
      });
      const b = (await res.json().catch(() => ({}))) as { ok?: boolean; seq?: number | null; detail?: string };
      if (res.status === 401) onUnauthorized();
      setSent(
        res.ok && b.ok
          ? { ok: true, text: `${success ? 'Pass' : 'Fail'} sent${b.seq ? ` as #${b.seq}` : ''}; waiting for the robot to take it…` }
          : { ok: false, text: `Not sent: ${b.detail ?? `HTTP ${res.status}`}` },
      );
    } catch (e) {
      setSent({ ok: false, text: `Not sent: ${(e as Error).message}` });
    } finally {
      setPending(null);
    }
  }

  const disabled = notOperator || pending != null || Boolean(inFlight) || Boolean(accepted);
  return (
    <section
      aria-labelledby="tdm78-trial-title"
      className="rounded-lg border-2 border-amber-400 bg-amber-50 p-4 text-zinc-900 shadow-sm dark:border-amber-500 dark:bg-amber-950/60 dark:text-zinc-100"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="tdm78-trial-title" className="text-xl font-semibold tracking-tight">
          {title}
        </h2>
        <span className="text-sm tabular-nums text-zinc-700 dark:text-zinc-300">
          Eval run #{trial.evalRunId}
          {trial.ended ? `, rollout ended: ${trial.ended}` : ''}
        </span>
      </div>
      <p className="mt-1 text-sm text-zinc-700 dark:text-zinc-300">
        {trial.suggested != null && <>The success check suggests {trial.suggested ? 'PASS' : 'FAIL'}; you decide. </>}
        {left == null
          ? 'The robot waits for your answer.'
          : left > 0
            ? `No answer in ${secs(left)} records this trial as a FAILURE (outcome not confirmed).`
            : 'Time is up; the robot is recording it without your answer.'}
      </p>

      {accepted ? (
        <p className="mt-3 text-base font-semibold text-emerald-800 dark:text-emerald-300" role="status">
          The robot took verdict #{accepted.seq}: trial {n} {accepted.success ? 'PASS' : 'FAIL'}
          {accepted.note ? ` (“${accepted.note}”)` : ''}. Recording it now…
        </p>
      ) : (
        <>
          <label htmlFor="tdm78-trial-note" className="mt-3 block text-xs text-zinc-700 dark:text-zinc-300">
            Note (optional; saved with the trial)
          </label>
          <input
            id="tdm78-trial-note"
            value={note}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. cube slipped out of the gripper"
            className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          />
          <div className="mt-3 grid grid-cols-2 gap-3">
            <button
              type="button"
              disabled={disabled}
              onClick={() => void send(true)}
              className="min-h-12 rounded-md bg-emerald-600 px-4 text-lg font-semibold text-white outline-none hover:bg-emerald-500 focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {pending === 'pass' ? 'Sending…' : 'Pass'}
            </button>
            <button
              type="button"
              disabled={disabled}
              onClick={() => void send(false)}
              className="min-h-12 rounded-md bg-red-600 px-4 text-lg font-semibold text-white outline-none hover:bg-red-500 focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {pending === 'fail' ? 'Sending…' : 'Fail'}
            </button>
          </div>
        </>
      )}
      <div className="mt-2 min-h-5 text-sm" aria-live="polite">
        {notOperator && !checking ? (
          <LoginNote robotId={robotId} what="Giving a verdict" />
        ) : inFlight && !accepted ? (
          <span>Verdict #{inFlight.seq} is with the robot ({inFlight.status})…</span>
        ) : sent && !accepted ? (
          <span className={sent.ok ? '' : 'font-medium text-red-700 dark:text-red-400'}>{sent.text}</span>
        ) : null}
      </div>
    </section>
  );
}

const PHASE_TEXT: Record<InterventionView['phase'], string> = {
  awaiting_claim: 'Policy paused. Takeover task posted, waiting for someone to claim it.',
  taken_over: 'A person has the arm.',
  handed_back: 'Handed back; the policy is finishing the trial.',
  closed: 'Intervention closed',
  declined: 'Not paused',
};

function FailingPanel({
  robotId,
  collect,
  intervention,
  notOperator,
  checking,
  onUnauthorized,
  now,
  contributorName,
}: {
  robotId: string;
  collect: CollectRolloutView;
  intervention: InterventionView | null;
  notOperator: boolean;
  checking: boolean;
  onUnauthorized: () => void;
  now: number;
  contributorName: (id: bigint) => string | undefined;
}) {
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<SendState | null>(null);
  useEffect(() => setRes(null), [collect.commandId]);
  const live = intervention != null && intervention.phase !== 'closed' && intervention.phase !== 'declined';

  async function flag() {
    setBusy(true);
    setRes(null);
    try {
      const r = await fetch(`/api/tdm78/robots/${robotId}/interventions/flag`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ note: 'operator pressed Failing (robot page)' }),
        cache: 'no-store',
      });
      const b = (await r.json().catch(() => ({}))) as { outcome?: string; detail?: unknown; intervention?: { task_id?: number } | null };
      if (r.status === 401) onUnauthorized();
      if (!r.ok) setRes({ ok: false, text: `Not flagged: ${typeof b.detail === 'string' ? b.detail : `HTTP ${r.status}`}` });
      else if (b.outcome === 'started')
        setRes({ ok: true, text: `Paused. Takeover task #${b.intervention?.task_id ?? '?'} posted for a person to recover the trial.` });
      else if (b.outcome === 'duplicate') setRes({ ok: true, text: 'Already flagged for this trial.' });
      else setRes({ ok: false, text: `The orchestrator did not pause it: ${String(b.outcome ?? 'unknown').replace(/^declined:/, '')}` });
    } catch (e) {
      setRes({ ok: false, text: `Not flagged: ${(e as Error).message}` });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900/60">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold">Collection rollout #{collect.seq} running</h2>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            The policy is collecting recovery data{collect.ticketId ? ` for ticket #${collect.ticketId}` : ''}. If it is going wrong, press
            Failing: it pauses and a paid takeover task goes out.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void flag()}
          disabled={notOperator || busy || live || collect.status !== 'acked'}
          title={collect.status !== 'acked' ? 'The robot has not started this rollout yet' : 'Pause the policy and post a takeover task'}
          className="min-h-11 rounded-md bg-amber-500 px-5 text-base font-semibold text-zinc-950 outline-none hover:bg-amber-400 focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-45"
        >
          {busy ? 'Flagging…' : 'Failing'}
        </button>
      </div>
      {intervention && (
        <p className="mt-2 text-sm" role="status">
          <span className="font-medium">{PHASE_TEXT[intervention.phase]}</span>
          {intervention.phase === 'taken_over' && intervention.contributorId != null && (
            <> ({contributorName(BigInt(intervention.contributorId)) ?? `contributor #${intervention.contributorId}`})</>
          )}
          {intervention.outcome ? `: ${intervention.outcome.replace(/^interventions disabled: /, '')}` : ''}
          {intervention.taskId ? ` Task #${intervention.taskId}.` : ''}
          <span className="text-zinc-500"> {intervention.detector || 'operator'}, {secs(now - intervention.at)} ago.</span>
        </p>
      )}
      <div className="mt-1 min-h-5 text-sm" aria-live="polite">
        {notOperator && !checking ? (
          <LoginNote robotId={robotId} what="Flagging a rollout" />
        ) : res ? (
          <span className={res.ok ? 'text-emerald-800 dark:text-emerald-300' : 'font-medium text-red-700 dark:text-red-400'}>{res.text}</span>
        ) : null}
      </div>
    </section>
  );
}
