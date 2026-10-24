'use client';

// TDM-61: marketplace helpers (task labels, lease math, claim errors, and the
// claim heartbeat). The lease rules mirror spacetime/spacetimedb/src/reducers/
// marketplace.ts (TDM-10): a claim is live only while status = 'claimed' AND
// now < lease_expires_at. Expiry is lazy on the server, so a 'claimed' row with
// a lapsed lease is claimable again and the UI treats it as open.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import type { Timestamp } from 'spacetimedb';
import type { RowOf } from '@/lib/spacetime/hooks';

export type MarketTask = RowOf<'task'>;
export type TaskType = 'clean_teleop' | 'recovery_takeover' | 'reset';

/** Arm-control types: 60 s lease, heartbeat every 30 s or faster. */
export const ARM_CONTROL_TYPES: readonly string[] = ['clean_teleop', 'recovery_takeover'];
export const CONTROL_LEASE_MS = 60_000;
export const RESET_LEASE_MS = 10 * 60_000;
/** Heartbeat well inside the lease so one dropped beat never loses the arm. */
export const CONTROL_HEARTBEAT_MS = 20_000;
export const RESET_HEARTBEAT_MS = 60_000;

export interface TaskTypeInfo {
  label: string;
  /** What the contributor actually does, in one line. */
  blurb: string;
  /** Rough duration shown as "about N min". An estimate by type, not a measurement. */
  minutes: number;
  /** Where the work happens. */
  where: 'remote' | 'on site';
}

export const TASK_TYPE_INFO: Record<TaskType, TaskTypeInfo> = {
  clean_teleop: {
    label: 'Teleop demo',
    blurb: 'Drive the arm from your phone through one clean, full run of the task.',
    minutes: 3,
    where: 'remote',
  },
  recovery_takeover: {
    label: 'Recovery takeover',
    blurb: 'The robot is stuck. Take over, finish the task, then hand control back.',
    minutes: 2,
    where: 'remote',
  },
  reset: {
    label: 'Scene reset',
    blurb: 'Put the objects back where they started so the robot can try again.',
    minutes: 1,
    where: 'on site',
  },
};

export function typeInfo(type: string): TaskTypeInfo {
  return TASK_TYPE_INFO[type as TaskType] ?? { label: type, blurb: '', minutes: 0, where: 'remote' };
}

export function isArmControl(type: string): boolean {
  return ARM_CONTROL_TYPES.includes(type);
}

export function leaseTtlMs(type: string): number {
  return isArmControl(type) ? CONTROL_LEASE_MS : RESET_LEASE_MS;
}

export function tsMs(ts: Timestamp | undefined | null): number | undefined {
  return ts ? Number(ts.microsSinceUnixEpoch / 1000n) : undefined;
}

/** Milliseconds left on the lease (<= 0 means lapsed), or undefined when not claimed. */
export function leaseLeftMs(task: MarketTask, now: number): number | undefined {
  if (task.status !== 'claimed') return undefined;
  const exp = tsMs(task.leaseExpiresAt);
  return exp === undefined ? 0 : exp - now;
}

export function leaseLive(task: MarketTask, now: number): boolean {
  const left = leaseLeftMs(task, now);
  return left !== undefined && left > 0;
}

/** Claimable right now: open, or claimed with a lapsed lease (lazy expiry). */
export function isClaimable(task: MarketTask, now: number): boolean {
  return task.status === 'open' || (task.status === 'claimed' && !leaseLive(task, now));
}

/** Where a held task is worked. Teleop and takeover go to the phone teleop page (TDM-56/57). */
export function workHref(task: MarketTask): string | undefined {
  return isArmControl(task.type) ? `/teleop/${task.id}` : undefined;
}

/** Instructions without the dispatcher's internal `[ref ...]` tag. */
export function cleanInstructions(text: string): string {
  return text.replace(/\s*\[ref [^\]]+\]/g, '').trim();
}

export function fmtCountdown(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

export function fmtPrice(cents: bigint | number): string {
  const n = Number(cents);
  return n % 100 === 0 ? `$${n / 100}` : `$${(n / 100).toFixed(2)}`;
}

function errText(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

/**
 * Turns a claim_task SenderError into words for the person who lost.
 * The reducer says: "task 7 is already claimed by Sam (contributor 3)".
 */
export function claimErrorMessage(e: unknown): string {
  const msg = errText(e);
  const taken = /already claimed by (.+?) \(contributor \d+\)/.exec(msg);
  if (taken) return `Already claimed by ${taken[1]}.`;
  if (/already under control via task/.test(msg)) return 'Someone else is driving this robot right now. Try again in a minute.';
  if (/in estop/.test(msg)) return 'The robot is in emergency stop. Control tasks are paused.';
  if (/is (completed|cancelled|expired|submitted), not open/.test(msg)) return 'This task is no longer open.';
  if (/cannot claim tasks/.test(msg)) return 'Dataset seller accounts cannot claim robot tasks.';
  if (/contributor \d+ not found|not contributor/.test(msg)) return 'Join as a contributor first, then claim.';
  return `Claim failed: ${msg}`;
}

export function actionErrorMessage(e: unknown): string {
  const msg = errText(e);
  if (/has expired; claim it again/.test(msg)) return 'Your hold ran out. Claim the task again if it is still open.';
  if (/not claimed/.test(msg)) return 'You no longer hold this task.';
  return msg;
}

/** A clock that re-renders every `everyMs`. */
export function useNow(everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

export interface HeartbeatState {
  /** Local time of the last accepted heartbeat (or the claim itself). */
  lastBeatAt?: number;
  error?: string;
  beatNow: () => void;
}

/**
 * Keeps a held claim alive with heartbeat_claim: every 20 s for arm control
 * (60 s lease), every 60 s for resets (10 min lease), and right away when the
 * tab becomes visible again. Pass the task only while this browser holds a live
 * lease on it; pass undefined to stop. Reusable by the teleop page (TDM-57).
 */
export function useTdm61ClaimHeartbeat(task: MarketTask | undefined): HeartbeatState {
  const { getConnection } = useSpacetimeDB();
  const [lastBeatAt, setLastBeatAt] = useState<number | undefined>();
  const [error, setError] = useState<string | undefined>();
  const taskId = task?.id;
  const type = task?.type;
  // A new claim generation (claimed_at) counts as a fresh beat.
  const claimedAt = tsMs(task?.claimedAt);
  const inflight = useRef(false);

  useEffect(() => {
    setError(undefined);
    setLastBeatAt(claimedAt);
  }, [taskId, claimedAt]);

  const beatNow = useCallback(() => {
    if (taskId === undefined || inflight.current) return;
    const conn = getConnection();
    if (!conn) return;
    inflight.current = true;
    conn.reducers
      .heartbeatClaim({ taskId })
      .then(() => {
        setLastBeatAt(Date.now());
        setError(undefined);
      })
      .catch((e: unknown) => setError(actionErrorMessage(e)))
      .finally(() => {
        inflight.current = false;
      });
  }, [taskId, getConnection]);

  useEffect(() => {
    if (taskId === undefined || type === undefined) return;
    const every = isArmControl(type) ? CONTROL_HEARTBEAT_MS : RESET_HEARTBEAT_MS;
    const timer = window.setInterval(beatNow, every);
    const onVisible = () => {
      if (document.visibilityState === 'visible') beatNow();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [taskId, type, beatNow]);

  return { lastBeatAt, error, beatNow };
}
