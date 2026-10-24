'use client';

// TDM-99: the robot view's HUD. <RobotHud robotId ticketId? state /> goes in RobotView's `hud` slot
// (docs/WORKER_GUIDE.md, "E15 Robot view contract"):
//
//   const live = useRobotLive(robotId);
//   <RobotView robotId={robotId} hud={<RobotHud robotId={robotId} ticketId={ticketId} state={live.state} stale={live.stale} />} />
//
// What it shows, and where each value comes from (nothing is made up; missing data reads as missing):
//   * who's driving       state.control_source (POLICY / HUMAN / IDLE) + state.robot_state
//   * the policy's plan   state.chunk_preview (the robot's downsampled current action chunk, TDM-50
//                         rollout -> /ws/state), drawn as each joint's offset from where the arm is now.
//                         The twin draws the same rows as its ghost arm (RobotView passes ghost=).
//   * gripper             state.gripper (0 closed .. 1 open)
//   * safety              state.safety.clamps_1s (flashes when a clamp count rises) and a latched
//                         e-stop banner from state.estop / robot_state 'estop'
//   * trial counter       eval_run (newest running, for the ticket or else the robot) + its eval_trial rows
//   * ticket + checkpoint ticket.task_description; base vs fine-tuned from state.checkpoint_uri (else the
//                         eval run's checkpoint_uri) matched against training_run rows
//
// Dark only: it lives inside RobotView's dark frame. Colours follow the twin (RobotTwin3D's palette):
// intent blue for the policy and its plan, envelope amber for safety clamps, e-stop red.

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import {
  DRIVER_LABEL,
  activeClamps,
  classifyCheckpoint,
  gripperText,
  pickEvalRun,
  planSummary,
  shortCheckpoint,
  trialCounter,
  type CheckpointInfo,
  type PlanSummary,
  type TrialCounter,
} from '@/lib/robotview/hud';
import type { ControlSource, RobotLiveState } from '@/lib/robotview/types';

export interface RobotHudProps {
  robotId: number | bigint;
  /** Count this ticket's eval runs (the ticket page). Default: the robot's newest eval run. */
  ticketId?: number | bigint | null;
  /** The live state (useRobotLive(robotId).state). null = no state yet. */
  state: RobotLiveState | null;
  /** useRobotLive(...).stale: the state readouts dim and say so. */
  stale?: boolean;
  className?: string;
}

const C = {
  intent: '#8cc2ff', // the twin's ghost / intent colour: the policy and its plan
  human: '#7fe0b0',
  idle: '#8b929c',
  envelope: '#f0a63a', // the twin's workspace envelope: safety clamps
  estop: '#ff4d4f',
  pass: '#7fe0b0',
  fail: '#ff6b6b',
};

const DRIVER_COLOR: Record<ControlSource, string> = { policy: C.intent, human: C.human, idle: C.idle };
const DRIVER_NOTE: Record<ControlSource, string> = {
  policy: 'The policy is moving the arm',
  human: 'A person has control',
  idle: 'Nobody is moving the arm',
};

const STYLE = `
@keyframes rdf-hud-flash { 0% { background-color: rgba(240,166,58,.55); } 100% { background-color: rgba(240,166,58,.08); } }
@keyframes rdf-hud-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
.rdf-hud-flash { animation: rdf-hud-flash 700ms ease-out both; }
.rdf-hud-pulse { animation: rdf-hud-pulse 1.4s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) { .rdf-hud-flash, .rdf-hud-pulse { animation: none; } }
`;

function toBig(v: number | bigint | null | undefined): bigint | null {
  return v == null ? null : BigInt(v);
}

export function RobotHud({ robotId, ticketId, state, stale = false, className = '' }: RobotHudProps) {
  const rid = BigInt(robotId);
  const tidProp = toBig(ticketId);
  const [runs] = useRows('evalRun', tidProp != null ? (r) => r.ticketId.eq(tidProp) : (r) => r.robotId.eq(rid));
  const run = pickEvalRun(runs, tidProp);
  const runId = run ? BigInt(run.id) : 0n;
  const [trials] = useRows('evalTrial', (r) => r.evalRunId.eq(runId));
  const tid = tidProp ?? (run ? BigInt(run.ticketId) : null);
  const [tickets] = useRows('ticket', (r) => r.id.eq(tid ?? 0n));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid ?? 0n));

  const counter = run ? trialCounter(run, trials) : null;
  const ckpt = classifyCheckpoint(state?.checkpoint_uri, run, trainingRuns);
  const ticket = tid != null ? tickets.find((t) => t.id === tid) : undefined;
  const estop = !!state && (state.estop || state.robot_state === 'estop');

  return (
    <div
      className={`overflow-hidden rounded-lg bg-black/40 text-zinc-100 ring-1 ring-white/10 ${className}`}
      data-testid="robot-hud"
      data-estop={estop ? '1' : '0'}
    >
      <style>{STYLE}</style>
      {estop && <EstopBanner state={state!} />}
      <div className={`flex flex-wrap items-stretch ${stale ? 'opacity-70' : ''}`}>
        <Driver state={state} stale={stale} />
        <Plan plan={planSummary(state)} driving={state?.control_source === 'policy'} />
        <Gripper state={state} />
        <Safety state={state} />
        <Trials counter={counter} />
        <TicketCheckpoint ticketId={tid} title={ticket?.taskDescription} ckpt={ckpt} />
      </div>
    </div>
  );
}

export default RobotHud;

// ---------------------------------------------------------------------------------------------

function Segment({ caption, children, className = '', grow = false, testid }: { caption: string; children: ReactNode; className?: string; grow?: boolean; testid?: string }) {
  return (
    <div data-testid={testid} className={`flex min-w-0 flex-col justify-center gap-1 border-white/10 px-3 py-2 [&:not(:first-child)]:border-l ${grow ? 'flex-1 basis-56' : 'shrink-0'} ${className}`}>
      <span className="text-[11px] leading-none text-zinc-400">{caption}</span>
      {children}
    </div>
  );
}

function EstopBanner({ state }: { state: RobotLiveState }) {
  return (
    <div
      role="alert"
      data-testid="hud-estop"
      data-t-ms={state.t_ms}
      className="flex items-center gap-3 px-3 py-2 text-white"
      style={{ backgroundColor: C.estop, backgroundImage: 'repeating-linear-gradient(135deg, rgba(0,0,0,.16) 0 10px, transparent 10px 20px)' }}
    >
      <svg aria-hidden viewBox="0 0 20 20" className="h-5 w-5 shrink-0">
        <polygon points="6,1 14,1 19,6 19,14 14,19 6,19 1,14 1,6" fill="white" />
        <rect x="5" y="9" width="10" height="2.4" fill={C.estop} />
      </svg>
      <p className="text-sm leading-tight">
        <b className="font-semibold">E-stop latched.</b> The arm is held where it stopped and will not move until an operator clears it.
      </p>
    </div>
  );
}

function Driver({ state, stale }: { state: RobotLiveState | null; stale: boolean }) {
  if (!state)
    return (
      <Segment caption="Driving" testid="hud-driver">
        <span className="text-sm text-zinc-400">No live state</span>
      </Segment>
    );
  const src = state.control_source;
  const color = DRIVER_COLOR[src] ?? C.idle;
  return (
    <Segment caption="Driving" testid="hud-driver" className="relative pl-4">
      <span aria-hidden className="absolute inset-y-0 left-0 w-1.5" style={{ backgroundColor: color }} />
      <span className="flex items-baseline gap-2" title={DRIVER_NOTE[src]}>
        <span className="text-lg font-bold leading-none tracking-wide" style={{ color }} data-testid="hud-driver-source">
          {DRIVER_LABEL[src] ?? src}
        </span>
        <span className="text-xs text-zinc-400">{stale ? 'state stalled' : state.robot_state}</span>
      </span>
    </Segment>
  );
}

const PLAN_W = 112;
const PLAN_H = 30;
const PLAN_MIN_SCALE = 0.05; // rad: a near-still plan stays near the midline instead of filling the box

function Plan({ plan, driving }: { plan: PlanSummary | null; driving: boolean }) {
  if (!plan)
    return (
      <Segment caption="Planned motion" testid="hud-plan">
        <span className="text-sm text-zinc-500">{driving ? 'Holding, no chunk' : 'No plan'}</span>
      </Segment>
    );
  const scale = Math.max(plan.maxDelta, PLAN_MIN_SCALE);
  const n = plan.deltas.length;
  const x = (k: number) => 2 + (k / (n - 1)) * (PLAN_W - 4);
  const y = (d: number) => PLAN_H / 2 - (d / scale) * (PLAN_H / 2 - 2);
  return (
    <Segment caption="Planned motion" testid="hud-plan">
      <span className="flex items-center gap-2">
        <svg
          viewBox={`0 0 ${PLAN_W} ${PLAN_H}`}
          width={PLAN_W}
          height={PLAN_H}
          role="img"
          aria-label={`Policy plan: ${plan.points} points, joints move up to ${plan.maxDelta.toFixed(2)} rad`}
        >
          <line x1="0" x2={PLAN_W} y1={PLAN_H / 2} y2={PLAN_H / 2} stroke="white" strokeOpacity=".12" />
          {Array.from({ length: 6 }, (_, j) => (
            <polyline
              key={j}
              fill="none"
              stroke={C.intent}
              strokeOpacity={1 - j * 0.12}
              strokeWidth={j < 3 ? 1.6 : 1}
              strokeLinejoin="round"
              points={plan.deltas.map((r, k) => `${x(k).toFixed(1)},${y(r[j]).toFixed(1)}`).join(' ')}
            />
          ))}
          <circle cx={x(0)} cy={PLAN_H / 2} r="2" fill="white" />
        </svg>
        <span className="text-xs tabular-nums text-zinc-300" title="Largest joint change the current chunk plans from here">
          up to {plan.maxDelta.toFixed(2)} rad
        </span>
      </span>
    </Segment>
  );
}

function Gripper({ state }: { state: RobotLiveState | null }) {
  if (!state)
    return (
      <Segment caption="Gripper" testid="hud-gripper">
        <span className="text-sm text-zinc-500">–</span>
      </Segment>
    );
  const g = gripperText(state.gripper);
  const gap = 2 + (g.pct / 100) * 10; // jaw opening in the glyph
  return (
    <Segment caption="Gripper" testid="hud-gripper">
      <span className="flex items-center gap-2">
        <svg aria-hidden viewBox="0 0 28 20" className="h-5 w-7">
          <rect x={14 - gap / 2 - 4} y="2" width="4" height="13" rx="1" fill="#cfd4da" />
          <rect x={14 + gap / 2} y="2" width="4" height="13" rx="1" fill="#cfd4da" />
          <rect x="4" y="15" width="20" height="3" rx="1" fill="#7c838d" />
        </svg>
        <span className="text-sm font-medium tabular-nums" data-testid="hud-gripper-text">
          {g.word} <span className="text-zinc-400">{g.pct}%</span>
        </span>
      </span>
    </Segment>
  );
}

function Safety({ state }: { state: RobotLiveState | null }) {
  const clamps = activeClamps(state?.safety?.clamps_1s);
  const total = clamps.reduce((s, [, n]) => s + n, 0);
  // Flash on every rise of the last-second clamp count (a new clamp), not on every frame it stays up.
  const prev = useRef(0);
  const [flash, setFlash] = useState(0);
  useEffect(() => {
    if (total > prev.current) setFlash((f) => f + 1);
    prev.current = total;
  }, [total]);
  return (
    <Segment caption="Safety, last second" testid="hud-safety" className="relative">
      {flash > 0 && total > 0 && <span key={flash} aria-hidden className="rdf-hud-flash pointer-events-none absolute inset-0" />}
      {!state ? (
        <span className="text-sm text-zinc-500">–</span>
      ) : clamps.length === 0 ? (
        <span className="text-sm text-zinc-300">No clamps</span>
      ) : (
        <span className="relative flex flex-wrap gap-x-2 text-sm" style={{ color: C.envelope }} data-testid="hud-clamps">
          {clamps.slice(0, 3).map(([label, n]) => (
            <span key={label} className="tabular-nums">
              {label} <b className="font-semibold">{n}</b>
            </span>
          ))}
        </span>
      )}
    </Segment>
  );
}

function Trials({ counter }: { counter: TrialCounter | null }) {
  if (!counter)
    return (
      <Segment caption="Eval" testid="hud-trials">
        <span className="text-sm text-zinc-500">No eval run yet</span>
      </Segment>
    );
  const kind = counter.kind === 'baseline' ? 'Baseline eval' : counter.kind === 'candidate' ? 'Fine-tuned eval' : 'A/B eval';
  const status = counter.running ? '' : counter.status === 'done' ? ', finished' : `, ${counter.status}`;
  return (
    <Segment caption={`${kind} #${counter.runId}${status}`} testid="hud-trials">
      <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-lg font-bold leading-none tabular-nums" data-testid="hud-trial-headline">
          {counter.headline}
        </span>
        <span className="flex gap-[3px]" aria-hidden>
          {counter.pips.map((p, i) => (
            <span
              key={i}
              className={`h-3 w-2 rounded-[2px] ${p === 'running' ? 'rdf-hud-pulse' : ''}`}
              style={
                p === 'pass'
                  ? { backgroundColor: C.pass }
                  : p === 'fail'
                    ? { backgroundColor: C.fail }
                    : p === 'running'
                      ? { backgroundColor: 'white' }
                      : { boxShadow: 'inset 0 0 0 1px rgba(255,255,255,.28)' }
              }
            />
          ))}
        </span>
        <span className="text-sm tabular-nums" data-testid="hud-trial-tally">
          <span style={{ color: C.pass }}>{counter.passed} ✓</span> <span style={{ color: C.fail }}>{counter.failed} ✗</span>
        </span>
        <span className="sr-only">
          {counter.passed} passed, {counter.failed} failed of {counter.planned} planned trials
        </span>
        {counter.simulated && <span className="text-xs text-violet-300">simulated trials</span>}
      </span>
    </Segment>
  );
}

function TicketCheckpoint({ ticketId, title, ckpt }: { ticketId: bigint | null; title?: string; ckpt: CheckpointInfo }) {
  const badge =
    ckpt.kind === 'finetuned'
      ? { bg: C.intent, fg: '#0b1a2b', ring: 'transparent' }
      : ckpt.kind === 'base'
        ? { bg: 'transparent', fg: '#e4e7eb', ring: 'rgba(255,255,255,.45)' }
        : { bg: 'transparent', fg: ckpt.kind === 'unknown' ? C.envelope : '#9aa1ab', ring: ckpt.kind === 'unknown' ? C.envelope : 'rgba(255,255,255,.2)' };
  const tip = ckpt.uri ? `${ckpt.uri}\n(${ckpt.from === 'robot' ? 'reported by the robot' : 'from the eval run'})` : undefined;
  return (
    <Segment caption={ticketId != null ? `Ticket ${ticketId}` : 'Ticket'} testid="hud-ticket" grow>
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 max-w-full truncate text-sm font-medium" title={title}>
          {title ?? (ticketId != null ? 'Loading…' : 'No ticket on this robot')}
        </span>
        <span
          className="shrink-0 rounded px-1.5 py-0.5 text-xs font-semibold"
          style={{ backgroundColor: badge.bg, color: badge.fg, boxShadow: `inset 0 0 0 1px ${badge.ring}` }}
          title={tip}
          data-testid="hud-checkpoint"
          data-kind={ckpt.kind}
        >
          {ckpt.label}
          {ckpt.kind === 'unknown' && ckpt.uri ? <span className="font-normal"> {shortCheckpoint(ckpt.uri)}</span> : null}
        </span>
      </span>
    </Segment>
  );
}
