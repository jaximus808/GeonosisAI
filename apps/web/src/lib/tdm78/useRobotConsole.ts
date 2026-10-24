'use client';

// TDM-78: everything the robot page shows, derived from live subscriptions.
//
//   robot          status (TDM-8 coarse enum) + last_heartbeat + camera_config_json
//   robot_command  queue + history (seq, ack, result, preempted)
//   event_log      rows from actor `robot-client:<robot.name>` (TDM-49): robot.state_changed (the 7
//                  fine states), robot.limit_hit (TDM-45 safety counters, rate-limited),
//                  robot.client_started, robot.estop_cleared, rollout.started (checkpoint)
//   task           a claimed, unexpired task on this robot = who holds arm control
//   ticket         deployed_checkpoint_uri of tickets on this robot

import { useEffect, useMemo, useState } from 'react';
import type { Timestamp } from 'spacetimedb';
import { useRobot, useRows, type RowOf } from '@/lib/spacetime/hooks';

export const TDM78_STATES = ['idle', 'homing', 'rollout', 'teleop', 'paused_for_takeover', 'waiting_reset', 'locked'] as const;
export type Tdm78State = (typeof TDM78_STATES)[number];

/** The TDM-49 mapping (services/robot-client/src/rdf_robot_client/state.py DB_STATUS). */
export const TDM78_STATE_STATUS: Record<Tdm78State, string> = {
  idle: 'online',
  homing: 'busy',
  rollout: 'busy',
  teleop: 'busy',
  paused_for_takeover: 'paused',
  waiting_reset: 'paused',
  locked: 'estop',
};

export const TDM78_SAFETY_KINDS = [
  ['clamped_joint_limit', 'Joint limit clamps'],
  ['clamped_speed', 'Speed clamps'],
  ['clamped_workspace', 'Workspace clamps'],
  ['clamped_gripper', 'Gripper clamps'],
  ['rejected_nonfinite', 'Rejected NaN / inf'],
  ['rejected_jump', 'Rejected jumps'],
  ['rejected_stopped', 'Rejected while stopped'],
] as const;

export type CommandRow = RowOf<'robotCommand'>;
export type EventRow = RowOf<'eventLog'>;

export function tdm78Ms(ts: Timestamp | undefined | null): number | null {
  return ts ? Number(ts.microsSinceUnixEpoch / 1000n) : null;
}

export function tdm78Json(s: string | undefined | null): Record<string, unknown> | null {
  if (!s) return null;
  try {
    const v = JSON.parse(s);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Re-render every `ms` (heartbeat age, lease countdown). */
export function useTdm78Now(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export interface StateEntry {
  id: bigint;
  at: number;
  from: string;
  to: string;
  reason: string;
  commandId: number | null;
}

export interface SafetyView {
  counts: Record<string, number | null>;
  lastKind: string | null;
  lastReason: string | null;
  lastAt: number | null;
  speedScale: number | null;
  profile: string | null;
  since: number | null; // the robot-client boot these counts belong to
}

export interface CheckpointView {
  uri: string | null;
  source: string;
  at: number | null;
}

export function useRobotConsole(robotId: bigint) {
  const [robot, robotReady] = useRobot(robotId);
  const [cmdRows, cmdReady] = useRows('robotCommand', (r) => r.robotId.eq(robotId));
  const actor = robot ? `robot-client:${robot.name}` : '\u0000none';
  const [evRows, evReady] = useRows('eventLog', (r) => r.actor.eq(actor));
  const [taskRows] = useRows('task', (r) => r.robotId.eq(robotId));
  const [ticketRows] = useRows('ticket', (r) => r.robotId.eq(robotId));
  const [contributors] = useRows('contributor');

  const commands = useMemo(() => [...cmdRows].sort((a, b) => (a.seq < b.seq ? 1 : a.seq > b.seq ? -1 : 0)), [cmdRows]);
  const events = useMemo(() => [...evRows].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)), [evRows]);

  const derived = useMemo(() => {
    let bootAt: number | null = null;
    let bootIdx = -1;
    const transitions: StateEntry[] = [];
    for (let i = 0; i < events.length; i++) {
      const e = events[i];
      if (e.type === 'robot.client_started') {
        bootAt = tdm78Ms(e.ts);
        bootIdx = i;
      }
      if (e.type === 'robot.state_changed') {
        const p = tdm78Json(e.payloadJson) ?? {};
        transitions.push({
          id: e.id,
          at: tdm78Ms(e.ts) ?? 0,
          from: String(p.from ?? ''),
          to: String(p.to ?? ''),
          reason: String(p.reason ?? ''),
          commandId: typeof p.command_id === 'number' ? p.command_id : null,
        });
      }
    }

    // Safety counters: newest `total` per kind since the last client boot (counters reset on boot).
    const safety: SafetyView = { counts: {}, lastKind: null, lastReason: null, lastAt: null, speedScale: null, profile: null, since: bootAt };
    for (const [k] of TDM78_SAFETY_KINDS) safety.counts[k] = null;
    for (let i = Math.max(0, bootIdx); i < events.length; i++) {
      const e = events[i];
      if (e.type !== 'robot.limit_hit') continue;
      const p = tdm78Json(e.payloadJson) ?? {};
      const kind = String(p.kind ?? '');
      if (typeof p.total === 'number') safety.counts[kind] = p.total;
      safety.lastKind = kind;
      safety.lastReason = typeof p.reason === 'string' ? p.reason : null;
      safety.lastAt = tdm78Ms(e.ts);
      if (typeof p.speed_scale === 'number') safety.speedScale = p.speed_scale;
      if (typeof p.profile === 'string') safety.profile = p.profile;
    }

    // Fine state: the newest of (state_changed event, command result_json.state); before any event
    // since boot, the boot event's own state.
    let state: { value: string; at: number; source: string } | null = null;
    const lastTr = transitions[transitions.length - 1];
    if (lastTr) state = { value: lastTr.to, at: lastTr.at, source: 'robot.state_changed' };
    if (bootIdx >= 0) {
      const p = tdm78Json(events[bootIdx].payloadJson) ?? {};
      const at = tdm78Ms(events[bootIdx].ts) ?? 0;
      if (typeof p.state === 'string' && (!state || at > state.at)) state = { value: p.state, at, source: 'robot.client_started' };
    }
    for (const c of commands) {
      const r = tdm78Json(c.resultJson);
      const at = tdm78Ms(c.finishedAt);
      if (r && typeof r.state === 'string' && at && (!state || at > state.at)) {
        state = { value: r.state, at, source: `command #${c.seq} result` };
      }
      break; // newest command only
    }

    // Active checkpoint.
    let checkpoint: CheckpointView = { uri: null, source: 'none', at: null };
    for (const c of commands) {
      if (c.type !== 'load_checkpoint' || c.status !== 'done') continue;
      const r = tdm78Json(c.resultJson);
      const uri = (r?.checkpoint_uri as string) ?? (tdm78Json(c.payloadJson)?.checkpoint_uri as string);
      if (uri) {
        checkpoint = { uri, source: `load_checkpoint #${c.seq}`, at: tdm78Ms(c.finishedAt) };
        break;
      }
    }
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.type !== 'rollout.started') continue;
      const p = tdm78Json(e.payloadJson) ?? {};
      const at = tdm78Ms(e.ts);
      if (typeof p.checkpoint_uri === 'string' && p.checkpoint_uri && at && (!checkpoint.at || at > checkpoint.at)) {
        checkpoint = { uri: p.checkpoint_uri, source: 'last rollout', at };
      }
      break;
    }
    if (bootAt && checkpoint.at && checkpoint.at < bootAt) {
      checkpoint = { ...checkpoint, source: `${checkpoint.source}, before the last client restart` };
    }
    const deployed = ticketRows
      .filter((t) => t.deployedCheckpointUri)
      .sort((a, b) => (a.updatedAt.microsSinceUnixEpoch < b.updatedAt.microsSinceUnixEpoch ? 1 : -1))
      .map((t) => ({ ticketId: Number(t.id), uri: t.deployedCheckpointUri as string }));

    const lastClear = [...events].reverse().find((e) => e.type === 'robot.estop_cleared');
    return { transitions, safety, state, checkpoint, deployed, bootAt, lastClearAt: lastClear ? tdm78Ms(lastClear.ts) : null };
  }, [events, commands, ticketRows]);

  const nameById = useMemo(() => new Map(contributors.map((c) => [c.id, c.name])), [contributors]);
  const tickets = useMemo(
    () =>
      [...ticketRows]
        .filter((t) => !['shipped', 'failed', 'cancelled'].includes(t.status))
        .sort((a, b) => (a.id < b.id ? 1 : -1))
        .map((t) => ({ id: Number(t.id), label: `#${t.id} ${t.taskDescription.slice(0, 48)}`, status: t.status })),
    [ticketRows],
  );

  return {
    ready: robotReady,
    robot,
    commandsReady: cmdReady,
    eventsReady: evReady,
    commands,
    tasks: taskRows,
    contributorName: (id: bigint) => nameById.get(id),
    tickets,
    ...derived,
  };
}
