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
//   TDM-51         eval.trial_awaiting_outcome (robot) -> the open Pass / Fail prompt; trial_outcome
//                  commands -> the robot's answer (done, or failed = refused: late / duplicate / wrong trial)
//   TDM-69         an open run_baseline with purpose collect_recovery = a collection rollout ("Failing");
//                  intervention.* rows from actor orchestrator:interventions for this robot

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

export const TDM78_IV_ACTOR = 'orchestrator:interventions';
const TDM78_OPEN = new Set(['issued', 'acked']);
// a later robot event of one of these types means the awaited trial is no longer waiting
const TDM78_TRIAL_CLOSERS = new Set([
  'eval.trial_recorded',
  'eval.trial_interrupted',
  'eval.trial_started',
  'eval.trials_started',
  'collect.trial_ended',
  'robot.client_started',
]);

export interface TrialPromptView {
  evalRunId: number;
  index: number;
  trials: number | null;
  commandId: number | null;
  ticketId: number;
  ended: string;
  suggested: boolean | null;
  askedAt: number;
  deadline: number | null; // ms; null = waits for ever
}

export interface VerdictView {
  id: bigint;
  seq: number;
  status: string;
  success: boolean | null;
  note: string;
  evalRunId: number;
  index: number | null;
  issuedAt: number;
  finishedAt: number | null;
  error: string | null;
}

export interface CollectRolloutView {
  commandId: number;
  seq: number;
  status: string;
  ticketId: number;
  issuedAt: number;
  dataPlanId: number;
}

export interface InterventionView {
  ref: string;
  commandId: number;
  taskId: number | null;
  phase: 'awaiting_claim' | 'taken_over' | 'handed_back' | 'closed' | 'declined';
  detector: string;
  reason: string;
  contributorId: number | null;
  outcome: string | null;
  at: number;
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
  const [ivRows] = useRows('eventLog', (r) => r.actor.eq(TDM78_IV_ACTOR));

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

    // TDM-51: the trial the robot is waiting on (newest eval.trial_awaiting_outcome, nothing closing it since)
    let trial: TrialPromptView | null = null;
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (TDM78_TRIAL_CLOSERS.has(e.type)) break;
      if (e.type !== 'eval.trial_awaiting_outcome') continue;
      const p = tdm78Json(e.payloadJson) ?? {};
      const commandId = typeof p.command_id === 'number' ? p.command_id : null;
      const cmd = commandId != null ? commands.find((c) => Number(c.id) === commandId) : undefined;
      if (cmd && !TDM78_OPEN.has(cmd.status)) break; // the run ended (done / failed / preempted)
      let trials = typeof p.trials === 'number' ? p.trials : null;
      if (trials == null) {
        const started = [...events].reverse().find((x) => x.type === 'eval.trials_started' && tdm78Json(x.payloadJson)?.command_id === commandId);
        const t = tdm78Json(started?.payloadJson)?.trials;
        trials = typeof t === 'number' ? t : null;
      }
      const askedAt = tdm78Ms(e.ts) ?? 0;
      const timeoutS = typeof p.timeout_s === 'number' ? p.timeout_s : 0;
      trial = {
        evalRunId: Number(p.eval_run_id ?? 0),
        index: Number(p.index ?? 0),
        trials,
        commandId,
        ticketId: Number(p.ticket_id ?? 0),
        ended: String(p.ended ?? ''),
        suggested: typeof p.suggested === 'boolean' ? p.suggested : null,
        askedAt,
        deadline: timeoutS > 0 ? askedAt + timeoutS * 1000 : null,
      };
      break;
    }

    // TDM-51: every trial_outcome command (newest first), with the robot's answer
    const verdicts: VerdictView[] = commands
      .filter((c) => c.type === 'trial_outcome')
      .map((c) => {
        const p = tdm78Json(c.payloadJson) ?? {};
        const r = tdm78Json(c.resultJson);
        return {
          id: c.id,
          seq: Number(c.seq),
          status: c.status,
          success: typeof p.success === 'boolean' ? p.success : null,
          note: typeof p.note === 'string' ? p.note : '',
          evalRunId: Number(p.eval_run_id ?? 0),
          index: typeof p.index === 'number' ? p.index : null,
          issuedAt: tdm78Ms(c.issuedAt) ?? 0,
          finishedAt: tdm78Ms(c.finishedAt),
          error: c.status === 'failed' ? String(r?.error ?? c.resultJson ?? 'refused') : c.status === 'preempted' ? 'preempted by the e-stop' : null,
        };
      });

    // TDM-69: an open collection rollout (the orchestrator's run_baseline purpose collect_recovery)
    let collect: CollectRolloutView | null = null;
    for (const c of commands) {
      if (!TDM78_OPEN.has(c.status) || (c.type !== 'run_baseline' && c.type !== 'run_eval')) continue;
      const p = tdm78Json(c.payloadJson) ?? {};
      if (p.purpose !== 'collect_recovery') continue;
      collect = {
        commandId: Number(c.id),
        seq: Number(c.seq),
        status: c.status,
        ticketId: Number(c.ticketId),
        issuedAt: tdm78Ms(c.issuedAt) ?? 0,
        dataPlanId: Number(p.data_plan_id ?? 0),
      };
      break;
    }

    return {
      transitions,
      safety,
      state,
      checkpoint,
      deployed,
      bootAt,
      lastClearAt: lastClear ? tdm78Ms(lastClear.ts) : null,
      trial,
      verdicts,
      collect,
    };
  }, [events, commands, ticketRows]);

  // TDM-69: the newest intervention on this robot, from the orchestrator's intervention.* trail
  const intervention = useMemo(() => {
    const prefix = `iv:${robotId}:`;
    const rows = [...ivRows].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const byRef = new Map<string, InterventionView>();
    let last: string | null = null;
    for (const e of rows) {
      const p = tdm78Json(e.payloadJson) ?? {};
      const ref = typeof p.ref === 'string' ? p.ref : '';
      if (!ref.startsWith(prefix)) continue;
      const at = tdm78Ms(e.ts) ?? 0;
      const cur: InterventionView = byRef.get(ref) ?? {
        ref,
        commandId: Number(p.command_id ?? ref.split(':')[2] ?? 0),
        taskId: null,
        phase: 'awaiting_claim',
        detector: String(p.detector ?? ''),
        reason: String(p.reason ?? ''),
        contributorId: null,
        outcome: null,
        at,
      };
      if (typeof p.task_id === 'number' && p.task_id > 0) cur.taskId = p.task_id;
      if (typeof p.contributor_id === 'number' && p.contributor_id > 0) cur.contributorId = p.contributor_id;
      if (e.type === 'intervention.started') cur.phase = 'awaiting_claim';
      else if (e.type === 'intervention.claimed') cur.phase = 'taken_over';
      else if (e.type === 'intervention.claim_lapsed') cur.phase = 'awaiting_claim';
      else if (e.type === 'intervention.handback') cur.phase = 'handed_back';
      else if (e.type === 'intervention.closed') {
        cur.phase = 'closed';
        cur.outcome = String(p.outcome ?? '');
      } else if (e.type === 'intervention.declined') {
        cur.phase = 'declined';
        cur.outcome = String(p.why ?? '');
      } else continue;
      cur.at = at;
      byRef.set(ref, cur);
      last = ref;
    }
    return last ? (byRef.get(last) ?? null) : null;
  }, [ivRows, robotId]);

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
    intervention,
    ...derived,
  };
}
