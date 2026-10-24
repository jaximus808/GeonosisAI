'use client';

// TDM-78: operator commands. Anything that moves the arm asks for a second press to confirm.
// Sent through /api/tdm78/robots/{id}/commands (Next server -> orchestrator, operator key added
// server side). The SpacetimeDB reducer is the real gate (busy / estop latch); the disabled states
// here only mirror it. The commands route needs an operator login (/ops/login); without one every
// button here is disabled. The soft e-stop (Tdm78SoftEstop) never needs it.

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { operatorLoginHref, useOperator } from '@/lib/operator/useOperator';
import type { CommandRow } from '@/lib/tdm78/useRobotConsole';

type CmdType = 'home' | 'stop' | 'run_baseline' | 'load_checkpoint' | 'start_teleop' | 'stop_teleop';
const JOB_TYPES = new Set(['run_baseline', 'run_eval', 'start_teleop', 'reset_request', 'home', 'load_checkpoint']);

interface Outcome {
  ok: boolean;
  text: string;
}

export function Tdm78CommandPanel({
  robotId,
  status,
  commands,
  tickets,
}: {
  robotId: string;
  status: string | undefined;
  commands: readonly CommandRow[];
  tickets: { id: number; label: string }[];
}) {
  const [armed, setArmed] = useState<CmdType | null>(null);
  const [pending, setPending] = useState<CmdType | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [ticketId, setTicketId] = useState(0);
  const [prompt, setPrompt] = useState('');
  const [ckpt, setCkpt] = useState('');
  const op = useOperator();
  const notOperator = op.operator !== true;

  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(null), 5000);
    return () => clearTimeout(t);
  }, [armed]);

  const locked = status === 'estop';
  const offline = !status || status === 'offline';
  const openJob = commands.find((c) => (c.status === 'issued' || c.status === 'acked') && JOB_TYPES.has(c.type));
  const openEstop = commands.find((c) => (c.status === 'issued' || c.status === 'acked') && c.type === 'soft_estop');
  const blocked = locked || Boolean(openEstop);

  async function send(type: CmdType, payload: Record<string, unknown> = {}) {
    setArmed(null);
    setPending(type);
    setOutcome(null);
    const t0 = performance.now();
    try {
      const res = await fetch(`/api/tdm78/robots/${robotId}/commands`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ type, payload, ticket_id: ticketId }),
        cache: 'no-store',
      });
      const b = (await res.json().catch(() => ({}))) as { ok?: boolean; seq?: number | null; detail?: string };
      const ms = Math.round(performance.now() - t0);
      if (res.status === 401) op.recheck(); // session expired or logged out elsewhere
      setOutcome(
        res.ok && b.ok
          ? { ok: true, text: `${LABEL[type]} sent${b.seq ? ` as #${b.seq}` : ''} in ${ms} ms.` }
          : { ok: false, text: `${LABEL[type]} not sent: ${b.detail ?? `HTTP ${res.status}`}` },
      );
    } catch (e) {
      setOutcome({ ok: false, text: `${LABEL[type]} not sent: ${(e as Error).message}` });
    } finally {
      setPending(null);
    }
  }

  function motion(type: CmdType, payload?: Record<string, unknown>) {
    if (armed === type) void send(type, payload);
    else setArmed(type);
  }

  const jobDisabled = notOperator || blocked || offline || Boolean(openJob) || pending != null;
  const stopDisabled = notOperator || blocked || offline || pending != null;
  const why = locked
    ? 'Locked by the soft e-stop.'
    : openEstop
      ? `E-stop #${openEstop.seq} is still open.`
      : offline
        ? 'The robot is offline.'
        : openJob
          ? `Busy with ${openJob.type} #${openJob.seq}.`
          : null;

  return (
    <div className="space-y-4">
      {notOperator && (
        <p className="rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
          {op.operator === null ? (
            'Checking operator login…'
          ) : (
            <>
              Commands need an operator.{' '}
              <Link href={operatorLoginHref(`/robots/${robotId}`)} className="font-medium text-sky-700 underline dark:text-sky-400">
                Log in as operator
              </Link>
              . The soft e-stop works without it.
            </>
          )}
        </p>
      )}
      {why && <p className="text-sm text-zinc-600 dark:text-zinc-400">{why}</p>}

      <div className="grid grid-cols-2 gap-2">
        <Tdm78Btn
          label={armed === 'home' ? 'Confirm: home' : 'Home'}
          hint="Moves the arm to its home pose"
          tone={armed === 'home' ? 'confirm' : 'default'}
          disabled={jobDisabled}
          busy={pending === 'home'}
          onClick={() => motion('home')}
        />
        <Tdm78Btn
          label="Stop job"
          hint="Ends the current job and holds the arm (no lock)"
          disabled={stopDisabled}
          busy={pending === 'stop'}
          onClick={() => void send('stop')}
        />
        <Tdm78Btn
          label={armed === 'start_teleop' ? 'Confirm: teleop' : 'Start teleop'}
          hint="Hands the arm to a human driver"
          tone={armed === 'start_teleop' ? 'confirm' : 'default'}
          disabled={jobDisabled}
          busy={pending === 'start_teleop'}
          onClick={() => motion('start_teleop')}
        />
        <Tdm78Btn
          label="Stop teleop"
          hint="Ends teleop and holds the arm"
          disabled={stopDisabled}
          busy={pending === 'stop_teleop'}
          onClick={() => void send('stop_teleop')}
        />
      </div>

      <fieldset className="space-y-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
        <legend className="sr-only">Run baseline</legend>
        <label className="block text-xs text-zinc-600 dark:text-zinc-400" htmlFor="tdm78-prompt">
          Baseline prompt (optional)
        </label>
        <input
          id="tdm78-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="put the cup in the bin"
          className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
        <Tdm78Btn
          wide
          label={armed === 'run_baseline' ? 'Confirm: run baseline' : 'Run baseline'}
          hint="The policy drives the arm for one episode"
          tone={armed === 'run_baseline' ? 'confirm' : 'default'}
          disabled={jobDisabled}
          busy={pending === 'run_baseline'}
          onClick={() => motion('run_baseline', prompt.trim() ? { prompt: prompt.trim() } : {})}
        />
      </fieldset>

      <fieldset className="space-y-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
        <legend className="sr-only">Load checkpoint</legend>
        <label className="block text-xs text-zinc-600 dark:text-zinc-400" htmlFor="tdm78-ckpt">
          Checkpoint URI
        </label>
        <input
          id="tdm78-ckpt"
          value={ckpt}
          onChange={(e) => setCkpt(e.target.value)}
          placeholder="modal-vol://rdf-checkpoints/runs/…"
          className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-900"
        />
        <Tdm78Btn
          wide
          label="Load checkpoint"
          hint="Points the policy at this checkpoint; the arm does not move"
          disabled={jobDisabled || !ckpt.trim()}
          busy={pending === 'load_checkpoint'}
          onClick={() => void send('load_checkpoint', { checkpoint_uri: ckpt.trim() })}
        />
      </fieldset>

      {tickets.length > 0 && (
        <div className="border-t border-zinc-200 pt-3 dark:border-zinc-800">
          <label className="block text-xs text-zinc-600 dark:text-zinc-400" htmlFor="tdm78-ticket">
            Attach commands to ticket
          </label>
          <select
            id="tdm78-ticket"
            value={ticketId}
            onChange={(e) => setTicketId(Number(e.target.value))}
            className="mt-1 w-full rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value={0}>None</option>
            {tickets.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
      )}

      <p aria-live="polite" className={`min-h-5 text-sm ${outcome?.ok === false ? 'text-red-700 dark:text-red-400' : 'text-zinc-700 dark:text-zinc-300'}`}>
        {armed ? `Press "${LABEL[armed]}" again within 5 s to move the arm.` : outcome?.text}
      </p>
    </div>
  );
}

const LABEL: Record<CmdType, string> = {
  home: 'Home',
  stop: 'Stop job',
  run_baseline: 'Run baseline',
  load_checkpoint: 'Load checkpoint',
  start_teleop: 'Start teleop',
  stop_teleop: 'Stop teleop',
};

function Tdm78Btn({
  label,
  hint,
  onClick,
  disabled,
  busy,
  tone = 'default',
  wide,
}: {
  label: string;
  hint: string;
  onClick: () => void;
  disabled?: boolean;
  busy?: boolean;
  tone?: 'default' | 'confirm';
  wide?: boolean;
}) {
  return (
    <button
      type="button"
      title={hint}
      onClick={onClick}
      disabled={disabled}
      className={`${wide ? 'w-full' : ''} rounded-md px-3 py-2 text-sm font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-sky-500 disabled:cursor-not-allowed disabled:opacity-45 ${
        tone === 'confirm'
          ? 'bg-amber-400 text-zinc-950 hover:bg-amber-300'
          : 'border border-zinc-300 bg-white text-zinc-900 hover:bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100 dark:hover:bg-zinc-800'
      }`}
    >
      {busy ? 'Sending…' : label}
    </button>
  );
}
