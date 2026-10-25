// AgentWorkbench model (lib/agentwork/workbench.ts): liveness and transcript. `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm112Floor, tdm112Words } = (await import(new URL('../src/lib/tdm112/floor.ts', import.meta.url).href)) as typeof import('../src/lib/tdm112/floor');
const aw = (await import(new URL('../src/lib/agentwork/workbench.ts', import.meta.url).href)) as typeof import('../src/lib/agentwork/workbench');

const T0 = 1_800_000_000;
const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(s * 1_000_000) });
let nextId = 1;
const ev = (s: number, type: string, payload: Record<string, unknown>) => ({ id: nextId++, tsUs: (T0 + s) * 1e6, actor: 'orchestrator', type, payload: { ticket_id: 4, ...payload } });
const moved = (s: number, from: string, to: string) => ev(s, 'ticket.status_changed', { from, to });
const step = (s: number, station: string, text: string, extra: Record<string, unknown> = {}) => ev(s, 'agent.step', { station, agent: 'x', text, ...extra });
const thought = (s: number, station: string, text: string, extra: Record<string, unknown> = {}) => ev(s, 'agent.thought', { station, agent: 'x', text, ...extra });
const ticket = (status: string) => ({ id: 4n, status, iteration: 0, targetSuccess: 0.8, createdAt: ts(T0) });

function bench(status: string, events: ReturnType<typeof ev>[], atS: number, key: string, extra: Record<string, unknown> = {}) {
  const nowUs = (T0 + atS) * 1e6;
  const floor = tdm112Floor({ ticket: ticket(status), events, nowUs, ...extra });
  const st = floor.stations.find((s) => s.key === key)!;
  return aw.awWorkbench(st, events, nowUs, tdm112Words);
}

describe('awWorkbench', () => {
  it('a Claude call in flight is thinking, with a live ticking line, and stays fresh on heartbeats', () => {
    const events = [
      moved(0, 'baseline', 'diagnose'),
      step(1, 'diagnose', 'Reading 10 baseline trials: 6 failed'),
      step(2, 'diagnose', 'Asking Claude (Claude API · claude-opus-5-5)', { phase: 'llm_start' }),
      thought(12, 'diagnose', 'Still thinking… 10s', { phase: 'llm_wait' }),
      thought(22, 'diagnose', 'Still thinking… 20s', { phase: 'llm_wait' }),
    ];
    const w = bench('diagnose', events, 25, 'diagnose');
    assert.equal(w.mode, 'thinking');
    assert.equal(w.freshness, 'fresh');
    assert.equal(w.agent.key, 'clusterer');
    const last = w.lines[w.lines.length - 1];
    assert.equal(last.live, true);
    assert.equal(last.text, 'Thinking… 23s');
    // the two heartbeats collapsed into the one live line
    assert.equal(w.lines.filter((l) => l.glyph === 'thought').length, 1);
    assert.deepEqual(w.lines.map((l) => aw.AW_GLYPH[l.glyph]), ['●', '●', '✻']);
  });

  it('after the answer: thoughts and the decision; narration hides the rows it covers', () => {
    const events = [
      moved(0, 'baseline', 'diagnose'),
      ev(1, 'diagnose.started', { failed: 6 }),
      step(2, 'diagnose', 'Asking Claude', { phase: 'llm_start' }),
      step(30, 'diagnose', 'Claude answered in 28s', { phase: 'llm_end' }),
      thought(31, 'diagnose', 'Six of ten failures are grasp slips at the bin edge.', { phase: 'reasoning' }),
      ev(32, 'diagnose.clusters', { clusters: [{}, {}] }),
      step(33, 'diagnose', 'Grouped 6 failures into 2 causes: grasp slip ×4, timeout ×2', { decision: true }),
    ];
    const w = bench('diagnose', events, 35, 'diagnose');
    assert.equal(w.mode, 'working');
    assert.deepEqual(
      w.lines.map((l) => `${aw.AW_GLYPH[l.glyph]} ${l.text}`),
      ['● Asking Claude', '● Claude answered in 28s', '✻ Thinking: Six of ten failures are grasp slips at the bin edge.', '→ Grouped 6 failures into 2 causes: grasp slip ×4, timeout ×2'],
    );
    assert.equal(w.lines[w.lines.length - 1].live, true);
  });

  it('turns amber, then stalled (red), when nothing happens', () => {
    const events = [moved(0, 'baseline', 'diagnose'), step(1, 'diagnose', 'Reading 10 baseline trials')];
    assert.equal(bench('diagnose', events, 10, 'diagnose').freshness, 'fresh');
    assert.equal(bench('diagnose', events, 45, 'diagnose').freshness, 'slow');
    const w = bench('diagnose', events, 200, 'diagnose');
    assert.equal(w.mode, 'stalled');
    assert.equal(w.freshness, 'stale');
    assert.match(w.lines[w.lines.length - 1].text, /No activity for 3m 19s/);
  });

  it('a proposed plan waits for a human with the reason; collect silence is waiting, not stuck', () => {
    const plan = { id: 1n, iteration: 0, dataType: 'recovery_takeover', amount: 8, estCostCents: 1750n, approvedCents: 0n, status: 'proposed', createdAt: ts(T0 + 5) };
    const w = bench('plan_data', [moved(0, 'diagnose', 'plan_data')], 600, 'plan_data', { plans: [plan] });
    assert.equal(w.mode, 'waiting');
    assert.equal(w.waitingFor, 'Waiting for operator approval of $17.50');
    assert.equal(aw.AW_GLYPH[w.lines[w.lines.length - 1].glyph], '⏸');
    const c = bench('dispatch', [moved(0, 'plan_data', 'dispatch')], 2000, 'collect');
    assert.equal(c.mode, 'waiting');
  });

  it('done and idle stations do not animate', () => {
    const events = [moved(0, 'baseline', 'diagnose'), moved(50, 'diagnose', 'plan_data')];
    assert.equal(bench('plan_data', events, 60, 'diagnose').mode, 'done');
    assert.equal(bench('plan_data', events, 60, 'train').mode, 'idle');
  });

  it('a stale llm_start (lost end row) is not thinking forever', () => {
    const evs = [step(0, 'diagnose', 'Asking Claude', { phase: 'llm_start' })];
    assert.equal(aw.awLlmInFlight(evs, (T0 + 60) * 1e6)?.sinceUs, T0 * 1e6);
    assert.equal(aw.awLlmInFlight(evs, (T0 + 600) * 1e6), null);
  });
});

describe('FactoryIso uses the same liveness', async () => {
  const { tdm115Cell } = (await import(new URL('../src/lib/tdm115/iso.ts', import.meta.url).href)) as typeof import('../src/lib/tdm115/iso');
  const cellAt = (atS: number, events: ReturnType<typeof ev>[]) => {
    const nowUs = (T0 + atS) * 1e6;
    const floor = tdm112Floor({ ticket: ticket('diagnose'), events, nowUs });
    const t = { ...ticket('diagnose'), robotId: 3n, taskDescription: 'x' };
    return tdm115Cell({ floor, ticket: t, events, nowUs, liveness: aw.awFloorLiveness(floor.stations, events, nowUs) });
  };
  it('a quiet station is stalled, an in-flight Claude call is thinking with its narration', () => {
    const quiet = [moved(0, 'baseline', 'diagnose'), step(1, 'diagnose', 'Reading 10 baseline trials')];
    const a = cellAt(300, quiet).agents.find((x) => x.key === 'clusterer')!;
    assert.equal(a.state, 'stalled');
    assert.equal(cellAt(300, quiet).machines.diagnose.state, 'stalled');
    const busy = [...quiet, step(2, 'diagnose', 'Asking Claude', { phase: 'llm_start' })];
    const b = cellAt(20, busy).agents.find((x) => x.key === 'clusterer')!;
    assert.equal(b.state, 'working');
    assert.equal(b.thinking, true);
  });
});
