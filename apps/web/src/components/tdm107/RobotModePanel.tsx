'use client';

// TDM-107: the ticket's robot mode inside the run-settings drawer (TDM-79).
//
//   * shows which robot the ticket runs on now: the SIM robot (MuJoCo YAM in its 3D scene) or the
//     REAL YAM, read from the orchestrator (GET /tickets/{id}/robot_mode via /api/tdm107);
//   * an operator switches it (POST, operator login; logged as ticket.robot_mode_changed). New tasks
//     and evals then go to the other robot; rows already recorded keep their own robot and label;
//   * "Train on sim data too" is run_settings hyperparams.include_sim_data (PATCH via /api/tdm79).
//     Unset = on for a sim ticket, off for a real one.

import { useCallback, useEffect, useState } from 'react';
import { patchSettings, type Tdm79Settings } from '@/lib/tdm79/settings';
import { TDM107_MODE_HINT, TDM107_MODE_LABEL, type Tdm107Mode } from '@/lib/tdm107/robotMode';

interface Tdm107Robot {
  id: number;
  name: string;
  status: string;
  robot_mode: Tdm107Mode | null;
}

interface Tdm107State {
  ticket_id: number;
  status: string;
  robot_mode: Tdm107Mode | null;
  robot: Tdm107Robot | null;
  include_sim_data: boolean;
  include_sim_data_explicit: boolean | null;
  robots: Record<Tdm107Mode, Tdm107Robot[]>;
  history: { event_id: number; from_mode?: string; to_mode?: string; to_robot_id?: number; by?: string; reason?: string }[];
  switchable: boolean;
  changed?: boolean;
  cancelled_task_ids?: number[];
}

function detailText(body: unknown, status: number): string {
  const d = (body as { detail?: unknown })?.detail;
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const o = d as { message?: string; code?: string };
    return o.message ?? o.code ?? `Error ${status}`;
  }
  if (Array.isArray(d)) return d.map((x: { msg?: string }) => x.msg ?? '').join('; ');
  return typeof d === 'string' ? d : `Error ${status}`;
}

export function Tdm107RobotModePanel({
  ticketId,
  humanId,
  frozen,
  onSettingsSaved,
}: {
  ticketId: string;
  humanId: string;
  frozen: boolean;
  onSettingsSaved?: (s: Tdm79Settings) => void;
}) {
  const [state, setState] = useState<Tdm107State>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string>();
  const [confirm, setConfirm] = useState<Tdm107Mode | null>(null);
  const [reason, setReason] = useState('');

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/tdm107/tickets/${ticketId}/robot_mode`, { cache: 'no-store' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(detailText(body, res.status));
      setState(body as Tdm107State);
      setError(undefined);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [ticketId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function switchTo(mode: Tdm107Mode) {
    setBusy(true);
    setNote(undefined);
    setError(undefined);
    try {
      const res = await fetch(`/api/tdm107/tickets/${ticketId}/robot_mode`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-human-id': humanId },
        body: JSON.stringify({ mode, reason: reason.trim() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(res.status === 401 ? 'Operator login required to switch robots.' : detailText(body, res.status));
      const s = body as Tdm107State;
      setState(s);
      setConfirm(null);
      setReason('');
      const cancelled = s.cancelled_task_ids?.length ?? 0;
      setNote(
        s.changed
          ? `Now on ${s.robot?.name ?? 'the robot'} (${s.robot_mode}). New tasks and evals go there.` +
              (cancelled ? ` ${cancelled} open task${cancelled === 1 ? '' : 's'} on the old robot were re-posted.` : '')
          : 'Already on that robot.',
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function setIncludeSim(v: boolean) {
    setBusy(true);
    setNote(undefined);
    const r = await patchSettings(ticketId, { hyperparams: { include_sim_data: v } }, humanId);
    setBusy(false);
    if (!r.ok) {
      setError(Object.values(r.errors).join('; '));
      return;
    }
    onSettingsSaved?.(r.settings);
    await load();
  }

  const mode = state?.robot_mode ?? null;
  const other: Tdm107Mode = mode === 'sim' ? 'real' : 'sim';
  const target = state?.robots[other] ?? [];
  const canSwitch = !!state && state.switchable && !frozen && target.length > 0;

  return (
    <section aria-labelledby="tdm107-title" className="mb-7">
      <h3 id="tdm107-title" className="text-sm font-semibold">
        Robot
      </h3>
      <p className="mb-3 mt-1 border-l-2 border-blue-500/60 pl-2 text-xs text-zinc-600 dark:text-zinc-400">
        <span className="font-medium text-zinc-800 dark:text-zinc-200">Takes effect: </span>
        new tasks and evals go to the new robot at once. Sim and real results are never pooled.
      </p>

      {!state && !error && <p className="text-sm text-zinc-500">Loading the robot mode…</p>}
      {state && (
        <div
          className={`rounded-lg border px-3 py-2.5 ${
            mode === 'sim'
              ? 'border-violet-400 bg-violet-50/70 dark:border-violet-700 dark:bg-violet-950/30'
              : 'border-zinc-200 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950'
          }`}
        >
          <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
            {mode ? TDM107_MODE_LABEL[mode] : 'Unknown robot'}
            {mode === 'sim' && (
              <span className="rounded bg-violet-600 px-1.5 py-0.5 text-[10px] font-semibold tracking-wider text-white uppercase">
                Simulated
              </span>
            )}
          </p>
          <p className="mt-0.5 text-xs text-zinc-500">
            {state.robot ? `${state.robot.name} (#${state.robot.id}, ${state.robot.status})` : 'no robot row'}
            {mode ? `. ${TDM107_MODE_HINT[mode]}` : ''}
          </p>
        </div>
      )}

      {state && (
        <div className="mt-3 space-y-2">
          {confirm ? (
            <div className="rounded-lg border border-amber-400 bg-amber-50 px-3 py-2.5 text-sm dark:border-amber-700 dark:bg-amber-950/30">
              <p className="font-medium text-amber-900 dark:text-amber-200">
                Switch to the {TDM107_MODE_LABEL[confirm].toLowerCase()} ({target.map((r) => r.name).join(', ')})?
              </p>
              <p className="mt-0.5 text-xs text-amber-900/80 dark:text-amber-200/80">
                The current stage re-runs its eval there; earlier {mode} results stay labelled {mode}.
              </p>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                maxLength={500}
                placeholder="Why (optional, goes in the event log)"
                className="mt-2 w-full rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-950"
              />
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void switchTo(confirm)}
                  className="rounded-md bg-amber-600 px-3 py-1 text-sm font-semibold text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  {busy ? 'Switching…' : 'Switch'}
                </button>
                <button type="button" onClick={() => setConfirm(null)} className="rounded-md px-3 py-1 text-sm text-zinc-600 dark:text-zinc-400">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              disabled={!canSwitch || busy}
              onClick={() => setConfirm(other)}
              className="rounded-md border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
            >
              Switch to the {TDM107_MODE_LABEL[other].toLowerCase()}
            </button>
          )}
          {state && !target.length && (
            <p className="text-xs text-zinc-500">No {other} robot is registered yet, so there is nothing to switch to.</p>
          )}
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              checked={state.include_sim_data}
              disabled={busy || frozen}
              onChange={(e) => void setIncludeSim(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-zinc-300 accent-blue-600"
            />
            <span>
              <span className="font-medium">Train on sim-robot episodes too</span>
              <span className="block text-xs text-zinc-500">
                {state.include_sim_data_explicit === null
                  ? `Default for a ${mode ?? 'real'} ticket (${state.include_sim_data ? 'on' : 'off'}).`
                  : 'Set on this ticket.'}{' '}
                Sim episodes never count toward real success numbers either way.
              </span>
            </span>
          </label>
          {state.history.length > 0 && (
            <ul className="text-xs text-zinc-500">
              {state.history.slice(-3).map((h) => (
                <li key={h.event_id}>
                  Event {h.event_id}: {h.from_mode} to {h.to_mode} (robot #{h.to_robot_id}) by {h.by}
                  {h.reason ? `: ${h.reason}` : ''}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {note && (
        <p role="status" className="mt-2 text-sm text-emerald-700 dark:text-emerald-400">
          {note}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </section>
  );
}
