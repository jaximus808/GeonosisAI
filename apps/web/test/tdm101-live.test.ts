// TDM-101: the pure half of the /live kiosk (lib/tdm101/live.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const { tdm101PickTicket, tdm101ParseTicketParam, tdm101OneLine, tdm101EvalPair, tdm101Ticker, tdm101Wake, tdm101KeyAction } = (await import(
  new URL('../src/lib/tdm101/live.ts', import.meta.url).href
)) as typeof import('../src/lib/tdm101/live');

const ts = (us: number) => ({ microsSinceUnixEpoch: BigInt(us) });
const tk = (id: number, status: string, at: number, createdBy = 'human:web') => ({ id: BigInt(id), status, createdAt: ts(at), createdBy });

describe('tdm101PickTicket', () => {
  const rows = [tk(1, 'shipped', 100), tk(2, 'baseline', 200), tk(3, 'failed', 300), tk(4, 'train', 50, 'test.tdm19')];
  it('follows the newest ticket still in flight, skipping test tickets', () => {
    assert.deepEqual(tdm101PickTicket(rows, null), { ticket: rows[1], how: 'active' });
  });
  it('falls back to the newest ticket when none is active', () => {
    const ended = [tk(1, 'shipped', 100), tk(3, 'failed', 300)];
    assert.equal(tdm101PickTicket(ended, null).ticket?.id, 3n);
    assert.equal(tdm101PickTicket(ended, null).how, 'latest');
  });
  it('honours ?ticket= even before the row exists', () => {
    assert.equal(tdm101PickTicket(rows, 1n).ticket?.id, 1n);
    assert.deepEqual(tdm101PickTicket(rows, 9n), { ticket: null, how: 'pinned' });
  });
  it('uses test tickets only when nothing else exists', () => {
    assert.equal(tdm101PickTicket([tk(4, 'train', 50, 'test.x')], null).ticket?.id, 4n);
    assert.deepEqual(tdm101PickTicket([], null), { ticket: null, how: 'none' });
  });
});

describe('tdm101ParseTicketParam', () => {
  it('accepts positive integers only', () => {
    assert.equal(tdm101ParseTicketParam('12'), 12n);
    assert.equal(tdm101ParseTicketParam(['7', '8']), 7n);
    for (const bad of [undefined, null, '', '0', '-3', 'abc', '1.5']) assert.equal(tdm101ParseTicketParam(bad), null);
  });
});

describe('tdm101OneLine', () => {
  it('keeps the first sentence verbatim', () => {
    assert.equal(tdm101OneLine('Most failures are mid-grasp slips.  Recovery data fixes those. Clean demos do not.'), 'Most failures are mid-grasp slips.');
  });
  it('cuts long text at a word with an ellipsis', () => {
    const out = tdm101OneLine('word '.repeat(80), 40);
    assert.ok(out.endsWith('…'));
    assert.ok(Array.from(out).length <= 40);
    assert.ok(!out.includes('wor…'));
  });
  it('handles empty reasoning', () => assert.equal(tdm101OneLine('   '), ''));
});

describe('tdm101EvalPair', () => {
  const run = (id: number, kind: string, at: number, o: Record<string, unknown> = {}) => ({
    id: BigInt(id), kind, status: 'done', trials: 10, completed: 10, successes: 3, ciLow: 0.1, ciHigh: 0.6, createdAt: ts(at), ...o,
  });
  it('pairs the newest baseline with the newest fine-tuned run', () => {
    const p = tdm101EvalPair([run(1, 'baseline', 1), run(2, 'candidate', 2, { successes: 8 }), run(3, 'candidate', 3, { status: 'running', completed: 4, successes: 2 })]);
    assert.equal(p.baseline?.rate, 0.3);
    assert.deepEqual(p.baseline?.ci, { lo: 0.1, hi: 0.6 });
    assert.equal(p.current?.run.id, 3n);
    assert.equal(p.current?.rate, 0.5);
    assert.equal(p.current?.ci, null, 'no CI until the run is finalized');
  });
  it('has no rate before the first trial', () => {
    const p = tdm101EvalPair([run(1, 'baseline', 1, { status: 'running', completed: 0, successes: 0 })]);
    assert.equal(p.baseline?.rate, null);
    assert.equal(p.current, null);
  });
});

describe('tdm101Ticker', () => {
  const ev = (id: string, t: number, type: string, data: Record<string, unknown> = {}) => ({ id, ts_us: t, kind: 'info', type, actor: 'a', text: type, is_error: false, data });
  it('flattens agents newest first and flags SpacetimeDB wake-ups', () => {
    const agents = [
      { key: 's:1', label: 'collector (data droid #1)', role: 'data_droid', kind: 'droid', events: [ev('ev:1', 10, 'droid.tool_call'), ev('ev:3', 30, 'agent.resumed', { reason: 'predicate true: episode.count>=5; 5 rows; waited 40s' })] },
      { key: 'service:planner', label: 'Planner', role: 'planner', kind: 'service', events: [ev('ev:2', 20, 'planner.data_plan'), ev('ev:1', 10, 'droid.tool_call')] },
    ];
    const out = tdm101Ticker(agents, 10);
    assert.deepEqual(out.map((i) => i.id), ['ev:3', 'ev:2', 'ev:1']);
    assert.equal(out[0].agent, 'collector');
    assert.deepEqual(out[0].wake, { predicate: 'episode.count>=5' });
    assert.equal(out[1].wake, null);
  });
  it('does not claim a database wake for manual resumes', () => {
    assert.equal(tdm101Wake({ type: 'agent.resumed', data: { reason: 'manual: operator' } }), null);
    assert.equal(tdm101Wake({ type: 'droid.tool_call', data: { reason: 'predicate true: x' } }), null);
  });
});

describe('tdm101KeyAction', () => {
  it('maps S/L/F/1-3 and ignores modified keys', () => {
    assert.deepEqual(tdm101KeyAction({ key: 'S' }), { kind: 'mode', mode: 'sim' });
    assert.deepEqual(tdm101KeyAction({ key: 'l' }), { kind: 'mode', mode: 'live' });
    assert.deepEqual(tdm101KeyAction({ key: 'f' }), { kind: 'fullscreen' });
    assert.deepEqual(tdm101KeyAction({ key: '3' }), { kind: 'preset', index: 2 });
    assert.equal(tdm101KeyAction({ key: 'l', metaKey: true }), null);
    assert.equal(tdm101KeyAction({ key: 'x' }), null);
  });
});

describe('tdm101LineStates', async () => {
  const { tdm101LineStates, TDM101_STATIONS } = (await import(new URL('../src/lib/tdm101/live.ts', import.meta.url).href)) as typeof import('../src/lib/tdm101/live');
  it('lights exactly one running station', () => {
    const s = tdm101LineStates('dispatch');
    assert.equal(s.filter((x) => x === 'current').length, 1);
    assert.equal(TDM101_STATIONS[s.indexOf('current')].label, 'Collect');
    assert.deepEqual(s.slice(0, 4), ['done', 'done', 'done', 'done']);
    assert.ok(s.slice(5).every((x) => x === 'upcoming'));
  });
  it('maps created and onboarding onto Queued', () => {
    assert.equal(tdm101LineStates('onboarding')[0], 'current');
    assert.equal(tdm101LineStates('created')[0], 'current');
  });
  it('resets later stations on a loop back and marks where a ticket failed', () => {
    assert.deepEqual(tdm101LineStates('plan_data').slice(3), ['current', 'upcoming', 'upcoming', 'upcoming', 'upcoming', 'upcoming']);
    const f = tdm101LineStates('failed', 'train');
    assert.equal(f[6], 'failed');
    assert.ok(!f.includes('current'));
    assert.equal(tdm101LineStates('shipped')[8], 'shipped');
    assert.equal(tdm101LineStates('cancelled', 'baseline')[1], 'cancelled');
  });
});
