// TDM-112: the factory floor's pure model (lib/tdm112/floor.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm112Floor, tdm112StationOfEvent, tdm112Words, tdm112WokeOn, tdm112Handoff, tdm112Elapsed, tdm112Droids } = (await import(
  new URL('../src/lib/tdm112/floor.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm112/floor');

const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(s * 1_000_000) });
const T0 = 1_800_000_000;
let nextId = 1;
const ev = (s: number, type: string, payload: Record<string, unknown>) => ({ id: nextId++, tsUs: (T0 + s) * 1e6, actor: 'orchestrator', type, payload: { ticket_id: 7, ...payload } });
const ticket = (status: string, iteration = 0) => ({ id: 7n, status, iteration, targetSuccess: 0.8, createdAt: ts(T0) });
const moved = (s: number, from: string, to: string) => ev(s, 'ticket.status_changed', { from, to });

describe('tdm112Floor stations', () => {
  it('marks stations before the running one done, the running one working, later ones idle', () => {
    const f = tdm112Floor({ ticket: ticket('plan_data'), events: [moved(0, 'created', 'baseline'), moved(60, 'baseline', 'diagnose'), moved(90, 'diagnose', 'plan_data')], nowUs: (T0 + 100) * 1e6 });
    assert.deepEqual(f.stations.map((s) => s.state), ['done', 'done', 'working', 'idle', 'idle', 'idle', 'idle', 'idle']);
    assert.equal(f.activeIndex, 2);
    assert.equal(f.stations[2].startedUs, (T0 + 90) * 1e6);
    assert.equal(f.stations[0].endedUs, (T0 + 60) * 1e6);
  });

  it('shows plan approval as waiting on a human (ochre), with the estimate', () => {
    const plan = { id: 3n, iteration: 0, dataType: 'recovery_takeover', amount: 5, estCostCents: 1750n, approvedCents: 0n, status: 'proposed', createdAt: ts(T0 + 95) };
    const f = tdm112Floor({ ticket: ticket('plan_data'), plans: [plan], events: [moved(90, 'diagnose', 'plan_data')], nowUs: (T0 + 100) * 1e6 });
    const st = f.stations[2];
    assert.equal(st.state, 'waiting');
    assert.match(st.activity, /Waiting for operator approval of \$17\.50/);
    assert.deepEqual(st.facts, ['5 recovery takeovers', 'est. $17.50']);
  });

  it('says what an idle station waits for when it has no rows', () => {
    const f = tdm112Floor({ ticket: ticket('baseline'), nowUs: T0 * 1e6 });
    assert.equal(f.stations[5].activity, 'Waits for enough accepted episodes.');
  });

  it('marks the station a failed ticket stopped at as failed', () => {
    const f = tdm112Floor({ ticket: ticket('failed'), events: [moved(0, 'created', 'baseline'), moved(10, 'baseline', 'diagnose'), moved(20, 'diagnose', 'failed')], nowUs: (T0 + 30) * 1e6 });
    assert.equal(f.stations[1].state, 'failed');
    assert.equal(f.stations[0].state, 'done');
    assert.equal(f.ended, 'failed');
    assert.equal(f.activeIndex, -1);
  });

  it('shipped: every station done', () => {
    const f = tdm112Floor({ ticket: ticket('shipped'), nowUs: T0 * 1e6 });
    assert.ok(f.stations.every((s) => s.state === 'done'));
  });

  it('eval chips and trial count come from the live trials', () => {
    const run = { id: 1n, kind: 'baseline', status: 'running', trials: 6, completed: 2, successes: 1, trainingRunId: 0n, createdAt: ts(T0) };
    const f = tdm112Floor({
      ticket: ticket('baseline'),
      evalRuns: [run],
      evalTrials: [{ evalRunId: 1n, trialIndex: 0, success: false }, { evalRunId: 1n, trialIndex: 1, success: true }],
      events: [moved(0, 'created', 'baseline'), ev(5, 'eval.trial', { eval_run_id: 1, index: 1, success: true, failure_note: '[SIM] auto outcome from sim state: the cube is resting in the plate' })],
      nowUs: (T0 + 10) * 1e6,
    });
    const st = f.stations[0];
    assert.deepEqual(st.chips, [{ index: 0, pass: false }, { index: 1, pass: true }]);
    assert.deepEqual(st.facts, ['trial 3/6', '1/2 pass']);
    // TDM-114: a running eval reads as live progress (trial x/N, last trial; its note comes from the trial row)
    assert.equal(st.activity, 'Trial 3 of 6 · last: trial 2 pass');
  });

  it('loop-back: Iteration 2 of 3, later stations idle again', () => {
    const f = tdm112Floor({ ticket: ticket('diagnose', 1), maxIterations: 3, events: [moved(0, 'eval', 'diagnose')], nowUs: T0 * 1e6 });
    assert.equal(f.iterationLabel, 'Iteration 2 of 3');
    assert.equal(f.stations[6].state, 'idle');
    assert.equal(f.stations[1].state, 'working');
  });

  it('names the LLM backend and model of the newest call for the station step', () => {
    const calls = [
      { id: 900, tsUs: (T0 + 1) * 1e6, actor: 'orchestrator:llm', type: 'llm.call', payload: { step: 'diagnose', backend: 'api', model: 'old' } },
      { id: 901, tsUs: (T0 + 70) * 1e6, actor: 'orchestrator:llm', type: 'llm.call', payload: { step: 'diagnose', backend: 'claude_code', model: 'claude-opus-5-5' } },
    ];
    const f = tdm112Floor({ ticket: ticket('diagnose'), events: [moved(60, 'baseline', 'diagnose')], llmCalls: calls, nowUs: (T0 + 80) * 1e6 });
    assert.deepEqual(f.stations[1].llm, { backend: 'claude_code', model: 'claude-opus-5-5' });
  });

  it('tags a stub training run STUB', () => {
    const tr = { id: 1n, iteration: 0, status: 'running', modalCallId: 'stub-ab12', hyperparamsJson: '{"steps":2000}', metricsJson: '{"fake":true}', createdAt: ts(T0) };
    const f = tdm112Floor({ ticket: ticket('train'), trainingRuns: [tr], nowUs: T0 * 1e6 });
    assert.equal(f.stations[5].computeTag, 'STUB');
    assert.deepEqual(f.stations[5].facts, ['2,000 steps', 'running']);
  });
});

describe('tdm112StationOfEvent', () => {
  const ctx = { statusAt: () => 'baseline', runKind: (id: number) => (id === 2 ? 'candidate' : id === 1 ? 'baseline' : undefined), taskType: () => 'reset' };
  it('routes eval rows by run kind, resets during an eval to that eval', () => {
    assert.equal(tdm112StationOfEvent({ type: 'eval.trial', payload: { eval_run_id: 2 }, tsUs: 0 }, ctx), 'eval');
    assert.equal(tdm112StationOfEvent({ type: 'eval.trial', payload: { eval_run_id: 1 }, tsUs: 0 }, ctx), 'baseline');
    assert.equal(tdm112StationOfEvent({ type: 'task.created', payload: { type: 'reset', task_id: 4 }, tsUs: 0 }, ctx), 'baseline');
    assert.equal(tdm112StationOfEvent({ type: 'task.created', payload: { type: 'recovery_takeover' }, tsUs: 0 }, ctx), 'collect');
    assert.equal(tdm112StationOfEvent({ type: 'planner.data_plan', payload: {}, tsUs: 0 }, ctx), 'plan_data');
    assert.equal(tdm112StationOfEvent({ type: 'robot.limit_hit', payload: {}, tsUs: 0 }, ctx), null);
    // a DAgger rollout while the ticket collects data is collection, not the baseline
    assert.equal(tdm112StationOfEvent({ type: 'rollout.started', payload: {}, tsUs: 0 }, { ...ctx, statusAt: () => 'dispatch' }), 'collect');
    // the reset after an eval's last trial lands once the ticket moved on, and still belongs to that eval
    assert.equal(
      tdm112StationOfEvent({ type: 'dispatch.paged', payload: { task_type: 'reset' }, tsUs: 0 }, { ...ctx, statusAt: () => 'diagnose', lastEvalStatusAt: () => 'baseline' }),
      'baseline',
    );
    assert.equal(tdm112StationOfEvent({ type: 'job_drop.preview', payload: { task_id: 21 }, tsUs: 0 }, { ...ctx, statusAt: () => 'dispatch' }), 'collect');
  });
});

describe('tdm112Words', () => {
  it('reads common rows in plain words', () => {
    assert.equal(tdm112Words({ type: 'planner.data_plan', payload: { amount: 5, data_type: 'recovery_takeover', est_cost_cents: 1750 } }), 'Plan: buy 5 recovery takeovers, est. $17.50');
    assert.equal(tdm112Words({ type: 'task.created', payload: { type: 'reset', task_id: 2, price_cents: 200 } }), 'Posted reset task #2 at $2.00');
    assert.equal(tdm112Words({ type: 'dispatch.teleop_started', payload: {} }), 'Dispatch: teleop started');
  });
});

describe('droids', () => {
  const session = (status: string, extra: Record<string, unknown> = {}) => ({
    id: 4n, ticketId: 7n, role: 'data_droid', name: 'data-7', status, currentStep: 'converting episodes', waitingForJson: null, createdAt: ts(T0), lastUpdate: ts(T0), ...extra,
  });
  it('a parked droid sleeps on its station with its predicate', () => {
    const [d] = tdm112Droids([session('waiting_for', { waitingForJson: '{"table":"episode","field":"accepted_count","op":">=","value":8,"raw":"episode:accepted_count>=8"}' })], [], T0 * 1e6, () => 'waiting for 8 accepted episodes');
    assert.equal(d.state, 'asleep');
    assert.equal(d.stationIndex, 3);
    assert.deepEqual(d.predicate, { raw: 'episode:accepted_count>=8', words: 'waiting for 8 accepted episodes' });
  });
  it('a SpacetimeDB wake-up reads woke on <table>:<field>, and pulses for 10 s', () => {
    const r = { id: 1, tsUs: T0 * 1e6, actor: 'orchestrator:resume', type: 'agent.resumed', payload: { session_id: 4, reason: 'predicate true: episode.accepted_count >= 8; ...', satisfied_predicate: { table: 'episode', field: 'accepted_count', op: '>=', value: 8 } } };
    assert.equal(tdm112WokeOn(r), 'episode:accepted_count');
    const [d] = tdm112Droids([session('running')], [r], (T0 + 3) * 1e6);
    assert.equal(d.state, 'waking');
    assert.equal(d.woke?.on, 'episode:accepted_count');
    const [later] = tdm112Droids([session('running')], [r], (T0 + 30) * 1e6);
    assert.equal(later.state, 'working');
  });
  it('a droid that is not acting waits in the bay', () => {
    const [d] = tdm112Droids([session('done')], [], T0 * 1e6);
    assert.equal(d.stationIndex, -1);
  });
});

describe('handoff and elapsed', () => {
  it('a part travels forward on a handoff and backwards on a loop-back', () => {
    assert.deepEqual(tdm112Handoff(0, 1), { from: 0, to: 1, back: false });
    assert.deepEqual(tdm112Handoff(6, 1), { from: 6, to: 1, back: true });
    assert.equal(tdm112Handoff(2, 2), null);
    assert.equal(tdm112Handoff(-1, 0), null);
  });
  it('formats elapsed time', () => {
    assert.equal(tdm112Elapsed(1e6, 39e6), '38s');
    assert.equal(tdm112Elapsed(0, 1e6), '');
    assert.equal(tdm112Elapsed(1e6, (1 + 252) * 1e6), '4m 12s');
  });
});
