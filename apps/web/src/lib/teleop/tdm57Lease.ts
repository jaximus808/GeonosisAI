'use client';

// TDM-57: exclusive arm control for the teleop page, on the TDM-10 claim lock.
//
//   const lease = useTeleopLease(taskId);
//   lease.state        'connecting' | 'not_joined' | 'no_task' | 'claiming' | 'held' | 'view_only'
//                      | 'available' | 'lost' | 'closed'
//   lease.canControl   true only while this browser holds a live lease AND the tab is visible AND the
//                      SpacetimeDB connection is up AND more than LEASE_MARGIN_MS is left.
//                      TDM-56 ANDs this with feed health and must release the clutch the moment it
//                      turns false.
//   lease.stamp(msg)   the command gate: returns msg + {task_id, claimed_at_us}, or null when control
//                      is not allowed at this instant (checked from refs at call time, so a 30 Hz send
//                      loop never sends on a stale render).
//   lease.reclaim()    claim_task again (after a loss, or "Take control" when the task is free).
//   lease.release()    give control back (release_task).
//
// Lifecycle:
//   - On open: claim_task once (autoClaim). Losing that race shows "Controlled by <name>" (view only).
//   - While held and visible: heartbeat_claim every 20 s via TDM-61's useTdm61ClaimHeartbeat, plus a
//     beat as soon as the tab is visible again. Hidden tab: no beats and no control; the 60 s lease
//     lapses on its own unless the tab comes back.
//   - Loss (latched until reclaim): lease ran out, released (here or by an operator), revoked
//     (cancel_task), completed, or someone else claimed after a lapse. Controls disable on the row
//     update; the robot's /control guard (rdf_robot_client.tdm57_lease) re-reads the row within 2 s
//     and the stream/control token can no longer be refreshed.
//   - pagehide / unmount while held: best-effort release_task so the next person can claim at once.
//
// Clock: lease_expires_at is server time. Each new lease_expires_at we observe tells us the server's
// clock at that moment (expires - TTL), so secondsLeft is computed against that offset, not the
// phone's own clock.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useReducer, useSpacetimeDB } from 'spacetimedb/react';
import { reducers } from '@/module_bindings';
import { useRows, type RowOf } from '@/lib/spacetime/hooks';
import { useContributor } from '@/lib/contributor/hooks';
import {
  actionErrorMessage,
  claimErrorMessage,
  leaseTtlMs,
  tsMs,
  useTdm61ClaimHeartbeat,
  type MarketTask,
} from '@/lib/marketplace/tdm61';

/** Stop accepting control this long before the lease runs out (heartbeat races, clock offset). */
export const LEASE_MARGIN_MS = 3_000;

export type TeleopLeaseState =
  | 'connecting'
  | 'not_joined'
  | 'no_task'
  | 'claiming'
  | 'held'
  | 'view_only'
  | 'available'
  | 'lost'
  | 'closed';

export type LostReason =
  | 'expired' // lease ran out (tab hidden / offline / no heartbeat)
  | 'released' // this page released it
  | 'released_by_operator' // an operator released it (POST /tasks/{id}/revoke)
  | 'revoked' // cancel_task
  | 'completed' // task finished
  | 'taken'; // someone else claimed it after a lapse

export const LOST_TEXT: Record<LostReason, string> = {
  expired: 'Your control lease ran out.',
  released: 'You gave control back.',
  released_by_operator: 'An operator took control back.',
  revoked: 'An operator revoked this task.',
  completed: 'This task is finished.',
  taken: 'Someone else took control after your lease ran out.',
};

export interface LeaseToken {
  taskId: bigint;
  /** task.claimed_at in micros: the claim generation. (task_id, claimed_at) is the TDM-10 lease token. */
  claimedAtUs: bigint;
  contributorId: bigint;
}

export interface TeleopLease {
  state: TeleopLeaseState;
  canControl: boolean;
  /** Why canControl is false while state is 'held'. */
  blockedBy?: 'hidden' | 'offline' | 'lease_low';
  secondsLeft?: number;
  holderName?: string;
  lostReason?: LostReason;
  lostWhileHidden?: boolean;
  /** Claim or heartbeat error, in words. */
  error?: string;
  busy: boolean;
  task?: MarketTask;
  leaseToken?: LeaseToken;
  reclaim: () => Promise<void>;
  release: () => Promise<void>;
  stamp: <T extends object>(msg: T) => (T & { task_id: number; claimed_at_us: number }) | null;
}

export interface TeleopLeaseOptions {
  /** Claim on open (default true). After a loss, reclaim is always explicit. */
  autoClaim?: boolean;
  /** release_task on pagehide / unmount while held (default true). */
  releaseOnLeave?: boolean;
}

function useVisible(): boolean {
  const [visible, setVisible] = useState(() =>
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  );
  useEffect(() => {
    const on = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}

function useTick(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

const CLOSED = new Set(['completed', 'cancelled', 'expired', 'submitted']);

export function useTeleopLease(taskIdIn: bigint | number | string, opts: TeleopLeaseOptions = {}): TeleopLease {
  const autoClaim = opts.autoClaim ?? true;
  const releaseOnLeave = opts.releaseOnLeave ?? true;
  const taskId = useMemo(() => {
    try {
      return BigInt(taskIdIn);
    } catch {
      return 0n;
    }
  }, [taskIdIn]);

  const { isActive, getConnection } = useSpacetimeDB();
  const { contributor, ready } = useContributor();
  const me = contributor?.id;
  const [rows, rowsReady] = useRows('task', (r) => r.id.eq(taskId));
  const [contributors] = useRows('contributor');
  const task = rows[0];
  const claimTask = useReducer(reducers.claimTask);
  const releaseTask = useReducer(reducers.releaseTask);
  const visible = useVisible();
  const tick = useTick(250);

  // Server clock offset: serverNow ~= Date.now() + skew. Learned from lease_expires_at values that
  // change while we watch (a fresh claim/heartbeat was written just now, at expires - TTL). The first
  // snapshot is skipped: it may be a stale row from a lease that lapsed long ago.
  const skew = useRef(0);
  const seenExp = useRef<{ v: number | undefined } | undefined>(undefined);
  const exp = tsMs(task?.leaseExpiresAt);
  if (rowsReady) {
    if (seenExp.current === undefined) seenExp.current = { v: exp };
    else if (task && exp !== undefined && exp !== seenExp.current.v) {
      seenExp.current = { v: exp };
      skew.current = exp - leaseTtlMs(task.type) - Date.now();
    } else if (exp !== seenExp.current.v) seenExp.current = { v: exp };
  }
  const serverNow = tick + skew.current;

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [lost, setLost] = useState<{ reason: LostReason; hidden: boolean } | undefined>();
  const held = useRef<{ gen: number; exp?: number } | undefined>(undefined);
  const releasedGen = useRef<number | undefined>(undefined);
  const autoTried = useRef(false);
  const hiddenSince = useRef<number | undefined>(undefined);
  if (!visible && hiddenSince.current === undefined) hiddenSince.current = Date.now();
  if (visible) hiddenSince.current = undefined;

  const gen = tsMs(task?.claimedAt);
  const liveOnServer = task?.status === 'claimed' && exp !== undefined && exp > serverNow;
  const mine = liveOnServer && me != null && task?.assigneeId === me && gen !== undefined;

  // Track the generation we hold, and detect the moment we stop holding it.
  let lostNow: LostReason | undefined;
  if (mine) {
    if (held.current?.gen !== gen) held.current = { gen: gen!, exp };
    else held.current.exp = exp;
  } else if (held.current && task) {
    const h = held.current;
    if (task.status === 'cancelled') lostNow = 'revoked';
    else if (task.status === 'completed' || task.status === 'submitted') lostNow = 'completed';
    else if (task.status === 'claimed' && (task.assigneeId !== me || gen !== h.gen)) lostNow = 'taken';
    else if (task.status === 'claimed') lostNow = 'expired';
    else if (releasedGen.current === h.gen) lostNow = 'released';
    else if (h.exp !== undefined && h.exp <= serverNow + 1_000) lostNow = 'expired';
    else lostNow = 'released_by_operator';
  }
  useEffect(() => {
    if (lostNow === undefined) return;
    held.current = undefined;
    setLost({ reason: lostNow, hidden: hiddenSince.current !== undefined || !visible });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lostNow]);
  useEffect(() => {
    if (mine) setLost(undefined);
  }, [mine, gen]);

  // Heartbeat only while held and visible (TDM-61 hook; undefined stops it).
  const hb = useTdm61ClaimHeartbeat(mine && visible ? task : undefined);
  const beatNow = hb.beatNow;
  useEffect(() => {
    if (mine && visible) beatNow();
  }, [visible, mine, beatNow]);

  const msLeft = mine && exp !== undefined ? exp - serverNow : undefined;
  let state: TeleopLeaseState;
  if (!ready || !rowsReady) state = 'connecting';
  else if (!contributor || contributor.kind !== 'contributor') state = 'not_joined';
  else if (!task) state = 'no_task';
  else if (mine) state = 'held';
  else if (busy) state = 'claiming';
  else if (lost || lostNow) state = 'lost';
  else if (CLOSED.has(task.status)) state = 'closed';
  else if (liveOnServer) state = 'view_only';
  else state = 'available';

  let blockedBy: TeleopLease['blockedBy'];
  if (state === 'held') {
    if (!visible) blockedBy = 'hidden';
    else if (!isActive) blockedBy = 'offline';
    else if ((msLeft ?? 0) <= LEASE_MARGIN_MS) blockedBy = 'lease_low';
  }
  const canControl = state === 'held' && blockedBy === undefined;

  const holder =
    task && liveOnServer ? contributors.find((c) => c.id === task.assigneeId) : undefined;
  const holderName = holder ? holder.name || `contributor ${holder.id}` : undefined;

  const reclaim = useCallback(async () => {
    if (me == null || taskId === 0n) return;
    setBusy(true);
    setError(undefined);
    try {
      await claimTask({ taskId, contributorId: me });
      setLost(undefined);
    } catch (e) {
      setError(claimErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [claimTask, me, taskId]);

  const release = useCallback(async () => {
    if (!held.current) return;
    releasedGen.current = held.current.gen;
    setBusy(true);
    try {
      await releaseTask({ taskId });
    } catch (e) {
      setError(actionErrorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [releaseTask, taskId]);

  // Claim once on open, if the task is free and the page is in front. Only the first settled state
  // counts: a viewer who opened while someone else drove gets a "Take control" button when it frees
  // up, never a surprise auto-claim.
  useEffect(() => {
    if (autoTried.current || state === 'connecting' || state === 'not_joined') return;
    if (!autoClaim || state !== 'available') {
      autoTried.current = true;
      return;
    }
    if (!visible || !isActive) return; // opened in a background tab: claim once it is in front
    autoTried.current = true;
    void reclaim();
  }, [autoClaim, state, visible, isActive, reclaim]);

  // Leaving the page: give control back right away instead of waiting out the lease.
  const leaveRef = useRef<() => void>(() => {});
  leaveRef.current = () => {
    if (!releaseOnLeave || !held.current) return;
    releasedGen.current = held.current.gen;
    getConnection()?.reducers.releaseTask({ taskId }).catch(() => {});
  };
  // Unmount releases on the next tick, not synchronously: React StrictMode (next dev) unmounts and
  // re-mounts every effect once, and a synchronous release there gave the claim back the moment the
  // teleop page opened. A re-mount within the tick cancels it; a real unmount still releases.
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    if (leaveTimer.current !== undefined) {
      clearTimeout(leaveTimer.current);
      leaveTimer.current = undefined;
    }
    const onHide = () => leaveRef.current();
    window.addEventListener('pagehide', onHide);
    return () => {
      window.removeEventListener('pagehide', onHide);
      leaveTimer.current = setTimeout(() => {
        leaveTimer.current = undefined;
        leaveRef.current();
      }, 0);
    };
  }, []);

  // The command gate reads refs at call time (no stale render in a 30 Hz loop).
  const genUs = mine ? task?.claimedAt?.microsSinceUnixEpoch : undefined;
  const gate = useRef({ canControl, exp, genUs });
  gate.current = { canControl, exp, genUs };
  const stamp = useCallback(
    <T extends object>(msg: T) => {
      const g = gate.current;
      if (!g.canControl || g.exp === undefined || g.genUs === undefined) return null;
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return null;
      if (g.exp - (Date.now() + skew.current) <= LEASE_MARGIN_MS) return null;
      return { ...msg, task_id: Number(taskId), claimed_at_us: Number(g.genUs) };
    },
    [taskId],
  );

  const leaseToken: LeaseToken | undefined =
    mine && task?.claimedAt && me != null
      ? { taskId, claimedAtUs: task.claimedAt.microsSinceUnixEpoch, contributorId: me }
      : undefined;

  return {
    state,
    canControl,
    blockedBy,
    secondsLeft: msLeft === undefined ? undefined : Math.max(0, Math.ceil(msLeft / 1000)),
    holderName,
    lostReason: state === 'lost' ? (lost?.reason ?? lostNow) : undefined,
    lostWhileHidden: state === 'lost' ? lost?.hidden : undefined,
    error: error ?? (mine ? hb.error : undefined),
    busy,
    task,
    leaseToken,
    reclaim,
    release,
    stamp,
  };
}

/** Live arm-control holders per robot, for the dashboard robot page. */
export function useControlHolders(robotId: bigint) {
  const [tasks] = useRows('task', (r) => r.robotId.eq(robotId));
  const [contributors] = useRows('contributor');
  const now = useTick(500);
  return useMemo(() => {
    const out: { task: MarketTask; holder?: RowOf<'contributor'>; msLeft: number }[] = [];
    for (const t of tasks) {
      const e = tsMs(t.leaseExpiresAt);
      if (t.status !== 'claimed' || e === undefined || e <= now) continue;
      if (t.type !== 'clean_teleop' && t.type !== 'recovery_takeover') continue;
      out.push({ task: t, holder: contributors.find((c) => c.id === t.assigneeId), msLeft: e - now });
    }
    return out;
  }, [tasks, contributors, now]);
}
