'use client';

// TDM-62: everything the reset task page (/reset/[taskId]) shows, from live subscriptions plus the
// orchestrator's /resets API (routine steps, start-state photo, scene photos).
//
//   task           the reset task (status, claim, lease)
//   robot          coarse status (paused = waiting_reset) and name
//   event_log      robot-client:<name>  robot.state_changed (fine state), eval.trial_started
//                  orchestrator:dispatcher  dispatch.reset_requested (eval run + trial), dispatch.reset_escalated,
//                                           dispatch.resume_issued
//   robot_command  resume_policy whose payload names this task (the dispatcher's release)
//   payment        batch_ref "task:<id>" (reserve_task_payment, Stripe TEST MODE)
//   eval_run       progress of the eval this reset belongs to

import { useCallback, useEffect, useMemo, useState } from 'react';
import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';
import { useRows, type RowOf } from '@/lib/spacetime/hooks';
import { tsMs } from '@/lib/marketplace/tdm61';

export type ResetTask = RowOf<'task'>;
type EventRow = RowOf<'eventLog'>;

export const resetHref = (taskId: bigint | number | string) => `/reset/${taskId}`;

export type ArmSafety = 'safe' | 'moving' | 'stopped' | 'unknown';

/** Fine robot states (TDM-49) in the words a person at the table needs. */
export const STATE_WORDS: Record<string, { safety: ArmSafety; text: string }> = {
  waiting_reset: { safety: 'safe', text: 'Arm is holding still, waiting for you' },
  idle: { safety: 'safe', text: 'Arm is idle' },
  paused_for_takeover: { safety: 'moving', text: 'Arm is paused for a remote driver. It may move.' },
  homing: { safety: 'moving', text: 'Arm is moving to its home pose' },
  rollout: { safety: 'moving', text: 'Robot is running its policy' },
  teleop: { safety: 'moving', text: 'Someone is driving the arm remotely' },
  locked: { safety: 'stopped', text: 'Emergency stop is on' },
};

export function parseJson(s: string | undefined | null): Record<string, unknown> {
  if (!s) return {};
  try {
    const v = JSON.parse(s);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

const num = (v: unknown) => (typeof v === 'number' ? v : typeof v === 'string' && v !== '' ? Number(v) : undefined);

export interface RoutineInfo {
  steps: string[];
  source: 'default' | 'onboarding' | 'stored' | string;
  reference_url: string | null;
  /** TDM-32 onboarding operator_prompt: one line above the steps. */
  prompt?: string | null;
  est_seconds?: number | null;
}
export interface Evidence {
  storage_uri: string;
  url: string;
  source: string;
  at_ms: number;
}
export interface ResetInfo {
  routine: RoutineInfo;
  evidence: Evidence[];
}

/** GET /api/tdm62/resets/{id}: routine + photos. `reload()` after attaching a photo. */
export function useResetInfo(taskId: bigint) {
  const [info, setInfo] = useState<ResetInfo | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    fetch(`/api/tdm62/resets/${taskId}`, { cache: 'no-store' })
      .then(async (r) => {
        const b = (await r.json().catch(() => ({}))) as ResetInfo & { detail?: string };
        if (!live) return;
        if (r.ok) {
          setInfo(b);
          setError(undefined);
        } else setError(typeof b.detail === 'string' ? b.detail : `HTTP ${r.status}`);
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [taskId, n]);
  return { info, error, reload: useCallback(() => setN((x) => x + 1), []) };
}

export class PhotoError extends Error {}

/** Attach a scene photo to the claimed reset: from the robot camera, or a phone photo (data URL). */
export async function attachPhoto(taskId: bigint, identityHex: string, src: { camera: true } | { dataUrl: string }) {
  let token: string | null = null;
  try {
    token = window.localStorage.getItem(BROWSER_TOKEN_KEY);
  } catch {
    /* storage blocked */
  }
  if (!token) throw new PhotoError('This browser has no factory identity yet.');
  const body =
    'camera' in src
      ? { identity: identityHex, spacetime_token: token, source: 'camera' }
      : { identity: identityHex, spacetime_token: token, source: 'upload', image_b64: src.dataUrl };
  const res = await fetch(`/api/tdm62/resets/${taskId}/photo`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  const out = (await res.json().catch(() => ({}))) as { storage_uri?: string; url?: string; detail?: string };
  if (!res.ok || !out.storage_uri) throw new PhotoError(typeof out.detail === 'string' ? out.detail : `HTTP ${res.status}`);
  return out as { storage_uri: string; url: string };
}

/** Shrink a phone photo to <= 1600 px JPEG before upload (phones shoot 4-12 MB). */
export async function downscalePhoto(file: File, maxPx = 1600): Promise<string> {
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, maxPx / Math.max(bmp.width, bmp.height));
  const c = document.createElement('canvas');
  c.width = Math.round(bmp.width * k);
  c.height = Math.round(bmp.height * k);
  c.getContext('2d')!.drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.82);
}

export interface ResetView {
  task?: ResetTask;
  ready: boolean;
  robot?: RowOf<'robot'>;
  ticket?: RowOf<'ticket'>;
  assignee?: RowOf<'contributor'>;
  /** Fine robot state (TDM-49), from the newest robot.state_changed; falls back to robot.status. */
  armState?: string;
  armSafety: ArmSafety;
  armText: string;
  /** The dispatcher's reset_requested context: which eval trial this reset follows. */
  context: { evalRunId?: number; trialIndex?: number; reason?: string; trialSuccess?: boolean };
  evalRun?: RowOf<'evalRun'>;
  escalations: { level: number; final: boolean; note: string; atMs: number }[];
  resume?: RowOf<'robotCommand'>;
  resumeIssuedAtMs?: number;
  payment?: RowOf<'payment'>;
  /** Next trial started after this reset completed (eval.trial_started from the robot). */
  nextTrial?: { index: number; atMs: number };
}

export function useResetView(taskId: bigint): ResetView {
  const [taskRows, taskReady] = useRows('task', (r) => r.id.eq(taskId));
  const task = taskRows[0];
  const robotId = task?.robotId ?? 0n;
  const ticketId = task?.ticketId ?? 0n;
  const [robots] = useRows('robot', (r) => r.id.eq(robotId));
  const robot = robots[0];
  const [tickets] = useRows('ticket', (r) => r.id.eq(ticketId));
  const [contributors] = useRows('contributor', (r) => r.id.eq(task?.assigneeId ?? 0n));
  const [robotEvents] = useRows('eventLog', (r) => r.actor.eq(robot ? `robot-client:${robot.name}` : '-'));
  const [dispatchEvents] = useRows('eventLog', (r) => r.actor.eq('orchestrator:dispatcher'));
  const [commands] = useRows('robotCommand', (r) => r.robotId.eq(robotId));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(ticketId));

  const mine = useMemo(() => {
    const out: (EventRow & { p: Record<string, unknown> })[] = [];
    for (const e of dispatchEvents) {
      if (!e.type.startsWith('dispatch.reset') && e.type !== 'dispatch.resume_issued') continue;
      const p = parseJson(e.payloadJson);
      if (num(p.task_id) === Number(taskId)) out.push({ ...e, p });
    }
    return out.sort((a, b) => Number(a.id - b.id));
  }, [dispatchEvents, taskId]);

  const requested = mine.find((e) => e.type === 'dispatch.reset_requested')?.p ?? {};
  const context = {
    evalRunId: num(requested.eval_run_id),
    trialIndex: num(requested.trial_index),
    reason: typeof requested.reason === 'string' ? requested.reason : undefined,
    trialSuccess: typeof requested.trial_success === 'boolean' ? requested.trial_success : undefined,
  };
  const [runs] = useRows('evalRun', (r) => r.id.eq(BigInt(context.evalRunId ?? 0)));

  const armState = useMemo(() => {
    let best: EventRow | undefined;
    for (const e of robotEvents) if (e.type === 'robot.state_changed' && (!best || e.id > best.id)) best = e;
    const to = best ? parseJson(best.payloadJson).to : undefined;
    if (typeof to === 'string') return to;
    if (!robot) return undefined;
    return robot.status === 'estop' ? 'locked' : robot.status === 'paused' ? 'waiting_reset' : undefined;
  }, [robotEvents, robot]);
  const words = armState ? STATE_WORDS[armState] : undefined;

  const doneMs = tsMs(task?.completedAt);
  const nextTrial = useMemo(() => {
    if (doneMs === undefined || context.evalRunId === undefined) return undefined;
    for (const e of [...robotEvents].sort((a, b) => Number(a.id - b.id))) {
      if (e.type !== 'eval.trial_started') continue;
      const p = parseJson(e.payloadJson);
      const at = tsMs(e.ts) ?? 0;
      if (num(p.eval_run_id) === context.evalRunId && (num(p.index) ?? -1) > (context.trialIndex ?? -1) && at >= doneMs - 1000) {
        return { index: num(p.index)!, atMs: at };
      }
    }
    return undefined;
  }, [robotEvents, doneMs, context.evalRunId, context.trialIndex]);

  const resume = commands.find((c) => c.type === 'resume_policy' && num(parseJson(c.payloadJson).task_id) === Number(taskId));
  const payment = payments.find((p) => p.episodeId === 0n && p.batchRef === `task:${taskId}`);

  return {
    task,
    ready: taskReady,
    robot,
    ticket: tickets[0],
    assignee: task && task.assigneeId !== 0n ? contributors[0] : undefined,
    armState,
    armSafety: words?.safety ?? (robot?.status === 'online' ? 'safe' : 'unknown'),
    armText: words?.text ?? (robot ? `Robot is ${robot.status}` : 'Waiting for the robot'),
    context,
    evalRun: runs[0],
    escalations: mine
      .filter((e) => e.type === 'dispatch.reset_escalated')
      .map((e) => ({ level: num(e.p.level) ?? 1, final: e.p.final === true, note: String(e.p.note ?? ''), atMs: tsMs(e.ts) ?? 0 })),
    resume,
    resumeIssuedAtMs: resume ? tsMs(resume.issuedAt) : undefined,
    payment,
    nextTrial,
  };
}
