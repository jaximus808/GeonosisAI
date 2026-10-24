// TDM-99: the pure half of <RobotHud/> (lib/robotview/hud.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { activeClamps, classifyCheckpoint, gripperText, pickEvalRun, planSummary, trialCounter, BASE_CHECKPOINT } = (await import(
  new URL('../src/lib/robotview/hud.ts', import.meta.url).href
)) as typeof import('../src/lib/robotview/hud');

const ts = (us: number) => ({ microsSinceUnixEpoch: BigInt(us) });
const run = (o: Record<string, unknown> = {}) => ({
  id: 7n, ticketId: 3n, robotId: 1n, trainingRunId: 0n, kind: 'baseline', checkpointUri: BASE_CHECKPOINT,
  status: 'running', trials: 10, createdAt: ts(1000), ...o,
});
const trial = (i: number, success: boolean, o: Record<string, unknown> = {}) => ({ evalRunId: 7n, trialIndex: i, success, failureNote: null, ...o });

describe('pickEvalRun', () => {
  it('prefers the newest running run, then queued, then the newest finished one', () => {
    const done = run({ id: 1n, status: 'done', createdAt: ts(5000) });
    const live = run({ id: 2n, status: 'running', createdAt: ts(100) });
    const queued = run({ id: 3n, status: 'queued', createdAt: ts(9000) });
    assert.equal(pickEvalRun([done, live, queued])?.id, 2n);
    assert.equal(pickEvalRun([done, queued])?.id, 3n);
    assert.equal(pickEvalRun([done, run({ id: 4n, status: 'cancelled', createdAt: ts(4000) })])?.id, 1n);
    assert.equal(pickEvalRun([]), null);
  });
  it('filters by ticket when given one', () => {
    const a = run({ id: 1n, ticketId: 3n, status: 'done' });
    const b = run({ id: 2n, ticketId: 4n, status: 'running' });
    assert.equal(pickEvalRun([a, b], 3)?.id, 1n);
    assert.equal(pickEvalRun([a, b], 9n), null);
  });
});

describe('trialCounter', () => {
  it('counts eval_trial rows: "Trial 4/10" with 2 passed and 1 failed', () => {
    const c = trialCounter(run(), [trial(0, true), trial(1, false), trial(2, true)]);
    assert.equal(c.headline, 'Trial 4/10');
    assert.deepEqual([c.current, c.done, c.passed, c.failed, c.planned], [4, 3, 2, 1, 10]);
    assert.deepEqual(c.pips.slice(0, 5), ['pass', 'fail', 'pass', 'running', 'pending']);
    assert.equal(c.pips.length, 10);
  });
  it('ignores other runs and duplicate rows; a finished run shows its result', () => {
    const rows = [trial(0, true), trial(0, true), trial(1, false), { ...trial(2, true), evalRunId: 8n }];
    const c = trialCounter(run({ status: 'done', trials: 3 }), rows);
    assert.deepEqual([c.done, c.passed, c.failed, c.current], [2, 1, 1, null]);
    assert.equal(c.headline, '1/3 passed');
    assert.deepEqual(c.pips, ['pass', 'fail', 'pending']);
  });
  it('a gap (an interrupted trial) is the trial in progress, and [SIM] notes flag simulation', () => {
    const c = trialCounter(run({ trials: 3 }), [trial(0, true), trial(2, false, { failureNote: '[SIM] outcome not confirmed' })]);
    assert.equal(c.current, 2);
    assert.equal(c.simulated, true);
  });
  it('no rows yet: trial 1 of N', () => {
    assert.equal(trialCounter(run({ trials: 5 }), []).headline, 'Trial 1/5');
  });
});

describe('classifyCheckpoint', () => {
  const trs = [{ id: 11n, ticketId: 3n, iteration: 2, status: 'done', baseCheckpointUri: BASE_CHECKPOINT, checkpointUri: 'modal-vol://rdf-checkpoints/runs/t3/it2/' }];
  it('fine-tuned only when a training_run produced that exact URI', () => {
    const c = classifyCheckpoint('modal-vol://rdf-checkpoints/runs/t3/it2', null, trs);
    assert.deepEqual([c.kind, c.label, c.trainingRunId, c.from], ['finetuned', 'Fine-tuned, iteration 2', 11, 'robot']);
  });
  it('base for the pi0.5 base checkpoint and its aliases', () => {
    assert.equal(classifyCheckpoint('base', null, []).kind, 'base');
    assert.equal(classifyCheckpoint(`${BASE_CHECKPOINT}/`, null, []).label, 'Base π0.5');
  });
  it('falls back to the eval run row, and says unknown rather than guessing', () => {
    assert.equal(classifyCheckpoint(undefined, run(), trs).from, 'eval_run');
    assert.equal(classifyCheckpoint(undefined, run({ checkpointUri: 'sim://scripted' }), []).label, 'Base (baseline eval)');
    assert.equal(classifyCheckpoint('modal-vol://x/other', run({ kind: 'candidate', trainingRunId: 4n }), trs).kind, 'unknown');
    assert.equal(classifyCheckpoint(null, null, trs).kind, 'none');
  });
});

describe('state readouts', () => {
  it('clamps: only non-zero, named, biggest first', () => {
    assert.deepEqual(activeClamps({ clamped_speed: 2, clamped_workspace: 5, estops: 0 }), [['workspace', 5], ['speed', 2]]);
    assert.deepEqual(activeClamps(undefined), []);
  });
  it('gripper: 0 closed .. 1 open', () => {
    assert.deepEqual(gripperText(0.82), { word: 'Open', pct: 82 });
    assert.deepEqual(gripperText(-0.2), { word: 'Closed', pct: 0 });
  });
  it('plan: deltas from the current joints; none without a preview', () => {
    const p = planSummary({ joints: [0, 0, 0, 0, 0, 0], chunk_preview: [[0.1, 0, 0, 0, 0, 0, 1], [0.3, -0.1, 0, 0, 0, 0, 0.2]] });
    assert.ok(p);
    assert.equal(p.points, 2);
    assert.ok(Math.abs(p.maxDelta - 0.3) < 1e-9);
    assert.equal(p.gripperEnd, 0.2);
    assert.equal(planSummary({ joints: [0, 0, 0, 0, 0, 0], chunk_preview: undefined }), null);
    assert.equal(planSummary({ joints: [0, 0, 0, 0, 0, 0], chunk_preview: [[0, 0, 0, 0, 0, 0]] }), null);
  });
});
