// TDM-115: the isometric cell's pure model (lib/tdm115/iso.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm112Floor } = (await import(new URL('../src/lib/tdm112/floor.ts', import.meta.url).href)) as typeof import('../src/lib/tdm112/floor');
const { tdm115Cell, tdm115Holder, tdm115RobotQueue, tdm115FocusTicket } = (await import(
  new URL('../src/lib/tdm115/iso.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm115/iso');

const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(s * 1_000_000) });
const T0 = 1_800_000_000;
let nextId = 1;
const ev = (s: number, type: string, payload: Record<string, unknown>) => ({ id: nextId++, tsUs: (T0 + s) * 1e6, actor: 'orchestrator', type, payload: { ticket_id: 4, ...payload } });
const moved = (s: number, from: string, to: string) => ev(s, 'ticket.status_changed', { from, to });
const ticket = (status: string, id = 4n) => ({ id, robotId: 3n, status, iteration: 0, targetSuccess: 0.8, createdAt: ts(T0 + Number(id)), taskDescription: 'Put the cube on the plate' });
const robot = { id: 3n, name: 'sim-yam-mujoco', status: 'paused', cameraConfigJson: '{"sim":true}' };
const cmd = (id: bigint, ticketId: bigint, type: string, status: string, payload = {}) => ({ id, robotId: 3n, ticketId, type, status, payloadJson: JSON.stringify(payload), issuedAt: ts(T0 + Number(id)) });
const now = (T0 + 500) * 1e6;

function cellFor(status: string, extra: Partial<Parameters<typeof tdm115Cell>[0]> = {}, events = [moved(0, 'created', 'baseline')]) {
  const t = ticket(status);
  const floor = tdm112Floor({ ticket: { ...t, deployedCheckpointUri: null }, events, nowUs: now });
  return tdm115Cell({ floor, ticket: t, robot, events, nowUs: now, ...extra });
}

describe('tdm115 robot holder and queue', () => {
  it('names the ticket on the arm and says when it is paused for a person', () => {
    const h = tdm115Holder(robot, [cmd(95n, 3n, 'run_baseline', 'acked', { purpose: 'collect_recovery' }), cmd(90n, 2n, 'run_eval', 'done')]);
    assert.equal(h?.ticketId, 3);
    assert.equal(h?.paused, true);
    assert.match(h!.words, /TKT-0003 has it, paused for a person to take over/);
  });
  it('ignores closed commands and non-job commands', () => {
    assert.equal(tdm115Holder(robot, [cmd(1n, 3n, 'run_eval', 'done'), cmd(2n, 3n, 'soft_estop', 'issued')]), null);
  });
  it('puts the ticket on the arm first, then the rest oldest first, without ended tickets', () => {
    const h = tdm115Holder(robot, [cmd(95n, 3n, 'run_baseline', 'acked')]);
    const q = tdm115RobotQueue([ticket('baseline', 4n), ticket('dispatch', 3n), ticket('shipped', 2n), ticket('created', 5n)], h);
    assert.deepEqual(q.map((x) => [x.ticketId, x.now]), [[3, true], [4, false], [5, false]]);
    assert.equal(tdm115FocusTicket([ticket('baseline', 4n), ticket('dispatch', 3n)], h)?.id, 3n);
  });
});

describe('tdm115Cell', () => {
  it('shows waiting for robot, not working, when another ticket holds the arm', () => {
    const c = cellFor('baseline', { commands: [cmd(95n, 3n, 'run_baseline', 'acked', { purpose: 'collect_recovery' })] });
    assert.equal(c.waitingForRobot, true);
    assert.equal(c.machines.eval.state, 'waiting_robot');
    const runner = c.agents.find((a) => a.key === 'eval_runner')!;
    assert.equal(runner.state, 'waiting_robot');
    assert.match(runner.say, /Waiting for the robot\. TKT-0003 has it/);
  });
  it('counts trials on the eval bench and says which trial runs', () => {
    const run = { id: 7n, kind: 'baseline', status: 'running', trials: 10, completed: 3, successes: 1, createdAt: ts(T0 + 1) };
    const trials = [0, 1, 2].map((i) => ({ evalRunId: 7n, trialIndex: i, success: i === 1 }));
    const c = cellFor('baseline', { evalRuns: [run], evalTrials: trials, commands: [cmd(96n, 4n, 'run_baseline', 'acked')] });
    assert.equal(c.waitingForRobot, false);
    assert.deepEqual(c.lights.baseline?.marks.slice(0, 5), ['fail', 'pass', 'fail', 'now', 'todo']);
    assert.equal(c.agents[0].say, 'Running trial 4 of 10 on the sim arm.');
  });
  it('asks for approval with the price at the plan desk', () => {
    const plan = { id: 2n, iteration: 0, dataType: 'recovery_takeover', amount: 6, estCostCents: 1750n, approvedCents: 0n, status: 'proposed', createdAt: ts(T0 + 9) };
    const events = [moved(0, 'created', 'baseline'), moved(10, 'baseline', 'diagnose'), moved(20, 'diagnose', 'plan_data')];
    const c = cellFor('plan_data', { plans: [plan], clusters: [{ iteration: 0, label: 'slips at the rim', count: 4 }, { iteration: 0, label: 'misses the cube', count: 2 }] }, events);
    const planner = c.agents.find((a) => a.key === 'planner')!;
    assert.equal(planner.state, 'waiting_person');
    assert.equal(planner.say, 'Waiting for you to approve $17.50.');
    assert.equal(c.plan?.waiting, true);
    assert.equal(c.agents.find((a) => a.key === 'clusterer')!.say, 'I grouped 6 failures into 2 causes.');
    assert.deepEqual(c.bins.map((b) => b.count), [4, 2]);
  });
  it('always shows the three Factory Droids; a sleeping one says its predicate', () => {
    const t = ticket('dispatch');
    const sessions = [{ id: 11n, ticketId: 4n, role: 'data_droid', name: 'data', status: 'waiting_for', currentStep: 'parked', waitingForJson: '{"raw":"episode:count>=8"}', createdAt: ts(T0), lastUpdate: ts(T0) }];
    const floor = tdm112Floor({ ticket: { ...t, deployedCheckpointUri: null }, sessions, events: [], nowUs: now });
    const c = tdm115Cell({ floor, ticket: t, robot, nowUs: now });
    const droids = c.agents.filter((a) => a.kind === 'droid');
    assert.deepEqual(droids.map((d) => d.key), ['onboarding_droid', 'data_droid', 'training_droid']);
    assert.equal(droids[1].state, 'asleep');
    assert.equal(droids[1].say, 'zz WAITING_FOR episode:count>=8');
    assert.equal(droids[1].at, 'collect');
    assert.equal(droids[0].at, 'bay');
    assert.equal(droids[0].say, 'Not started for this ticket.');
  });
});
