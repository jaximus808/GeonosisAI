// TDM-114: who holds the robot, waiting-for-robot, the robot queue and live eval progress, in the floor
// model (lib/tdm112/floor.ts). `pnpm --filter @rdf/web test`. The fixture is the live TKT-0004 situation:
// sim robot 3 held by TKT-0003's collection rollout #95, paused for a takeover of task #12.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm112Floor, tdm114Holder, tdm114Queue, tdm114EvalPhase } = (await import(new URL('../src/lib/tdm112/floor.ts', import.meta.url).href)) as typeof import('../src/lib/tdm112/floor');

const T0 = 1_800_000_000;
const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(Math.round((T0 + s) * 1_000_000)) });
let nid = 1;
const ev = (s: number, type: string, payload: Record<string, unknown>, actor = 'orchestrator:interventions') => ({ id: nid++, tsUs: (T0 + s) * 1e6, actor, type, payload });

const robot = { id: 3n, name: 'sim-yam-mujoco', status: 'paused', cameraConfigJson: JSON.stringify({ sim: true, capabilities: ['interventions'] }) };
const rollout = {
  id: 95n, robotId: 3n, ticketId: 3n, type: 'run_baseline', status: 'acked',
  payloadJson: JSON.stringify({ purpose: 'collect_recovery', eval_run_id: 0, data_plan_id: 1 }), issuedAt: ts(0), ackedAt: ts(1),
};
const started = ev(50, 'intervention.started', { ref: 'iv:3:95:0', robot_id: 3, ticket_id: 3, command_id: 95, task_id: 12, phase: 'awaiting_claim', started_us: (T0 + 50) * 1e6 });
const tickets = [{ id: 3n, status: 'dispatch', robotId: 3n }, { id: 4n, status: 'baseline', robotId: 3n }];
const t4 = { id: 4n, status: 'baseline', iteration: 0, targetSuccess: 0.8, createdAt: ts(0) };
const moved = ev(60, 'ticket.status_changed', { ticket_id: 4, from: 'created', to: 'baseline' }, 'orchestrator');
const flywheel = ev(60, 'flywheel.transition', { ticket_id: 4, to: 'baseline', reason: 'robot 3 onboarded (adapter mujoco)' }, 'orchestrator');

function liveFloor(extra: Partial<Parameters<typeof tdm112Floor>[0]> = {}) {
  return tdm112Floor({
    ticket: t4,
    events: [moved, flywheel],
    robot,
    robotCommands: [rollout],
    robotTickets: tickets,
    robotEvalRuns: [],
    robotEvents: [started],
    nowUs: (T0 + 158) * 1e6,
    ...extra,
  });
}

describe('tdm114 waiting for the robot', () => {
  it('reads the live TKT-0004 situation as Waiting (not WORKING) and says who holds the robot', () => {
    const f = liveFloor();
    const st = f.stations[0];
    assert.equal(st.state, 'waiting');
    assert.match(st.activity, /^Waiting for sim-yam-mujoco\. It's paused for a person to take over TKT-0003 task #12 \(since \d{1,2}:\d{2} [AP]M\)\.$/);
    assert.doesNotMatch(st.activity, /onboarded/);
    assert.equal(f.holder?.ticketId, 3);
    assert.equal(f.holder?.state, 'paused_for_takeover');
    assert.equal(f.holder?.job, 'collect');
    assert.equal(f.holder?.taskId, 12);
    assert.equal(f.waitingFor?.reason, 'robot_busy');
    assert.deepEqual(f.waitingFor?.actions, { takeOverTaskId: 12, claimNow: true, resume: false, clearEstop: false });
  });

  it('lists the robot queue: TKT-0003 now, TKT-0004 baseline queued', () => {
    const f = liveFloor();
    assert.deepEqual(
      f.robotQueue.map((q) => [q.ticketRef, q.job, q.state, q.isThisTicket]),
      [['TKT-0003', 'collect', 'now', false], ['TKT-0004', 'baseline', 'queued', true]],
    );
  });

  it('a person driving is not preemptable', () => {
    const f = liveFloor({ robotEvents: [started, ev(70, 'intervention.claimed', { ref: 'iv:3:95:0', ticket_id: 3, contributor_id: 7 })] });
    assert.equal(f.holder?.state, 'taken_over');
    assert.equal(f.holder?.human, true);
    assert.equal(f.waitingFor?.actions.claimNow, false);
    assert.match(f.waitingFor?.text ?? '', /A person is driving it for TKT-0003 task #12/);
  });

  it('a held collection ticket reads "Paused: robot reassigned" with a resume action', () => {
    const t3 = { id: 3n, status: 'dispatch', iteration: 0, targetSuccess: 0.8, createdAt: ts(0) };
    const hold = ev(200, 'robot.reassigned', { robot_id: 3, ticket_id: 3, to_ticket_id: 4, by: 'op', auto: false }, 'orchestrator:arbitration');
    const f = tdm112Floor({
      ticket: t3,
      events: [ev(0, 'ticket.status_changed', { ticket_id: 3, from: 'plan_data', to: 'dispatch' }, 'orchestrator')],
      robot: { ...robot, status: 'online' },
      robotCommands: [],
      robotTickets: tickets,
      robotEvalRuns: [],
      robotEvents: [started, ev(201, 'intervention.closed', { ref: 'iv:3:95:0', ticket_id: 3, outcome: 'preempted' }), hold],
      nowUs: (T0 + 210) * 1e6,
    });
    const st = f.stations[3];
    assert.equal(st.state, 'waiting');
    assert.match(st.activity, /^Paused: robot reassigned\. sim-yam-mujoco is running TKT-0004's eval/);
    assert.equal(f.waitingFor?.actions.resume, true);
    assert.equal(f.robotQueue.find((q) => q.ticketId === 3)?.state, 'paused');
  });

  it('estop on a sim robot offers Clear e-stop', () => {
    const f = liveFloor({ robot: { ...robot, status: 'estop' }, robotCommands: [] });
    assert.equal(f.waitingFor?.reason, 'robot_estop');
    assert.equal(f.waitingFor?.actions.clearEstop, true);
  });
});

describe('tdm114 eval progress', () => {
  const run = { id: 9n, ticketId: 4n, robotId: 3n, kind: 'baseline', status: 'running', trials: 10, completed: 2, successes: 1, trainingRunId: 0n, createdAt: ts(100) };
  const trials = [
    { evalRunId: 9n, trialIndex: 0, success: true, failureNote: '[SIM] auto outcome from sim state: the cube is resting in the plate', createdAt: ts(130) },
    { evalRunId: 9n, trialIndex: 1, success: false, failureNote: '[SIM] missed grasp: gripper closed on nothing', createdAt: ts(160) },
  ];

  it('shows trial x/N, the robot phase, the last trial with its note, and a rough ETA', () => {
    const p = tdm114EvalPhase({ run, trials, live: { phase: 'lift', eval_run_id: 9 }, nowUs: (T0 + 170) * 1e6 });
    assert.equal(p?.trial, 3);
    assert.equal(p?.phase, 'lift');
    assert.equal(p?.phaseSource, 'robot');
    assert.deepEqual(p?.lastTrial, { index: 1, pass: false, note: 'missed grasp: gripper closed on nothing' });
    assert.equal(p?.etaS, 230); // 30 s per trial x 8 left, 10 s into the next
    assert.equal(p?.label, 'Trial 3 of 10 · lift · last: trial 2 fail, missed grasp: gripper closed on nothing · about 3m 50s left');
  });

  it('the floor station shows the progress, not the transition reason', () => {
    const f = liveFloor({ evalRuns: [run], evalTrials: trials, robotEvalRuns: [run], robotCommands: [], robot: { ...robot, status: 'busy' }, nowUs: (T0 + 170) * 1e6 });
    assert.equal(f.stations[0].state, 'working');
    assert.match(f.stations[0].activity, /^Trial 3 of 10 · last: trial 2 fail/);
    assert.equal(f.evalPhase?.evalRunId, 9);
    assert.equal(f.waitingFor, undefined);
  });

  it('a seeded eval lands all N trials at once with the SEEDED label', () => {
    const seeded = Array.from({ length: 10 }, (_, i) => ({ evalRunId: 9n, trialIndex: i, success: i % 3 !== 0, failureNote: '[SEEDED] not run: drawn', createdAt: ts(101) }));
    const p = tdm114EvalPhase({ run: { ...run, completed: 10 }, trials: seeded, nowUs: (T0 + 102) * 1e6 });
    assert.equal(p?.seeded, true);
    assert.equal(p?.completed, 10);
    assert.match(p?.label ?? '', /^SEEDED: 10 of 10 trials drawn at once/);
    assert.equal(p?.etaS, undefined);
  });

  it('waiting_reset between trials reads as the reset phase', () => {
    const p = tdm114EvalPhase({ run, trials, robotState: 'waiting_reset', nowUs: (T0 + 170) * 1e6 });
    assert.equal(p?.phase, 'reset');
  });
});

describe('tdm114 holder words', () => {
  it('names a running eval of another ticket', () => {
    const h = tdm114Holder({ robot: { ...robot, status: 'busy' }, commands: [{ ...rollout, ticketId: 5n, type: 'run_eval', payloadJson: JSON.stringify({ kind: 'baseline', eval_run_id: 2 }) }] });
    assert.equal(h?.job, 'baseline');
    assert.match(h?.words ?? '', /^It's running TKT-0005's baseline eval/);
    const q = tdm114Queue({ holder: h, tickets, runs: [], robotEvents: [], thisTicketId: 4 });
    assert.equal(q[0].ticketRef, 'TKT-0005');
  });
});
