// TDM-116: the ticket cockpit's pure model (lib/tdm116/cockpit.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const m = (await import(new URL('../src/lib/tdm116/cockpit.ts', import.meta.url).href)) as typeof import('../src/lib/tdm116/cockpit');

const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(Math.round(s * 1_000_000)) });
const KEYS = m.TDM116_STAGE_KEYS;
const floor = (states: string[], activeIndex: number, extra: Partial<{ ended: 'shipped' | 'failed' | 'cancelled'; queued: boolean }> = {}) => ({
  activeIndex,
  queued: false,
  ...extra,
  stations: KEYS.map((key, i) => ({ key, state: states[i] ?? 'idle' })),
});

describe('tdm116LiveStage', () => {
  it('follows the running station', () => {
    assert.equal(m.tdm116LiveStage(floor(['done', 'done', 'waiting'], 2)), 'plan_data');
    assert.equal(m.tdm116LiveStage(floor(['done', 'done', 'done', 'working'], 3)), 'collect');
  });
  it('shows Ship once shipped, the failed station when failed, the baseline while queued', () => {
    assert.equal(m.tdm116LiveStage(floor(Array(8).fill('done'), -1, { ended: 'shipped' })), 'ship');
    assert.equal(m.tdm116LiveStage(floor(['done', 'done', 'done', 'done', 'done', 'failed'], -1, { ended: 'failed' })), 'train');
    assert.equal(m.tdm116LiveStage(floor([], -1, { queued: true })), 'baseline');
    assert.equal(m.tdm116LiveStage(floor(['done', 'done'], -1, { ended: 'cancelled' })), 'diagnose');
    assert.equal(m.tdm116LiveStage(null), 'baseline');
  });
});

describe('tdm116StoppedAt', () => {
  it('reads where a cancelled ticket stopped, so the stage shows that station', () => {
    const evs = [
      { type: 'ticket.status_changed', payload: { from: 'diagnose', to: 'plan_data' } },
      { type: 'ticket.status_changed', payload: { from: 'plan_data', to: 'cancelled' } },
    ];
    const at = m.tdm116StoppedAt(evs, 'cancelled');
    assert.equal(at, 'plan_data');
    assert.equal(m.tdm116LiveStage(floor(['done', 'done'], -1, { ended: 'cancelled' }), at), 'plan_data');
    assert.equal(m.tdm116StoppedAt([{ type: 'ticket.status_changed', payload: { from: 'dispatch', to: 'failed' } }], 'failed'), 'collect');
    assert.equal(m.tdm116StoppedAt(evs, 'train'), null);
  });
});

describe('tdm116 params', () => {
  it('parses a pinned stage and a tab, rejecting junk', () => {
    assert.equal(m.tdm116ParseStage('diagnose'), 'diagnose');
    assert.equal(m.tdm116ParseStage('dispatch'), null);
    assert.equal(m.tdm116ParseStage(null), null);
    assert.equal(m.tdm116ParseTab('money'), 'money');
    assert.equal(m.tdm116ParseTab('nope'), 'overview');
  });
});

describe('tdm116PrimaryAction', () => {
  const plan = (status: string, iteration = 0, cents = 1750n) => ({ id: 4n, iteration, status, estCostCents: cents, createdAt: ts(10) });
  it('asks to approve a proposed plan at its estimate', () => {
    const a = m.tdm116PrimaryAction({ ticketId: 3, status: 'plan_data', iteration: 0, plans: [plan('proposed')], tasks: [] });
    assert.deepEqual(a, { kind: 'approve', label: 'Approve plan $17.50', planId: 4, cents: 1750 });
  });
  it('has nothing to do once the plan is approved', () => {
    assert.equal(m.tdm116PrimaryAction({ ticketId: 3, status: 'plan_data', iteration: 0, plans: [plan('approved')], tasks: [] }), null);
  });
  it('offers the oldest open takeover during collection, never a reset', () => {
    const tasks = [
      { id: 12n, type: 'recovery_takeover', status: 'open', createdAt: ts(5) },
      { id: 14n, type: 'recovery_takeover', status: 'open', createdAt: ts(9) },
      { id: 9n, type: 'reset', status: 'open', createdAt: ts(1) },
    ];
    const a = m.tdm116PrimaryAction({ ticketId: 3, status: 'dispatch', iteration: 0, plans: [], tasks });
    assert.equal(a?.kind, 'takeover');
    assert.equal(a?.label, 'Take over #12');
  });
  it('links the report once shipped, and nothing while the line runs', () => {
    assert.equal(m.tdm116PrimaryAction({ ticketId: 3, status: 'shipped', iteration: 1, plans: [], tasks: [] })?.label, 'View report');
    assert.equal(m.tdm116PrimaryAction({ ticketId: 3, status: 'train', iteration: 0, plans: [], tasks: [] }), null);
  });
});

describe('tdm116LastFailure', () => {
  const trial = (i: number, s: number, success: boolean, note?: string) => ({ trialIndex: i, success, durationMs: 9000, failureNote: note ?? null, createdAt: ts(s) });
  it('formats durations', () => {
    assert.equal(m.tdm116Duration(60_000), '1m 00s');
    assert.equal(m.tdm116Duration(45_400), '45s');
    assert.equal(m.tdm116Duration(3_720_000), '1h 02m');
  });
  it('quotes the newest failure note without harness tags', () => {
    const f = m.tdm116LastFailure([trial(0, 10, false, '[SIM] Auto outcome from sim state: cube slipped'), trial(1, 20, true), trial(2, 30, false, '[SIM] missed the plate')]);
    assert.deepEqual(f, { index: 2, note: 'missed the plate' });
    assert.equal(m.tdm116LastFailure([trial(0, 1, true)]), null);
  });
});

describe('tdm116Drops', () => {
  it('counts posts, drafts and claims per channel for this ticket only', () => {
    const ids = new Set([21, 22]);
    const rows = m.tdm116Drops(
      [
        { type: 'job_drop.preview', payload: { task_id: 21, task_ids: [21, 22], channels: { discord: 'drafted' } } },
        { type: 'job_drop.posted', payload: { task_id: 21, channel: 'discord' } },
        { type: 'job_drop.claimed', payload: { task_id: 22, src: 'discord' } },
        { type: 'job_drop.claimed', payload: { task_id: 21, src: 'text' } },
        { type: 'text.offer_sent', payload: { task_id: 21, task_ids: [21], mode: 'dry_run', count: 2 } },
        { type: 'job_drop.claimed', payload: { task_id: 99, src: 'discord' } },
      ],
      ids,
    );
    assert.deepEqual(rows, [
      { channel: 'Discord', posted: 1, drafted: 1, claims: 1 },
      { channel: 'Text', posted: 0, drafted: 2, claims: 1 },
    ]);
  });
});

describe('tdm116CancelNote', () => {
  it('says who cancelled and why, one line', () => {
    assert.equal(m.tdm116CancelNote('  wrong   task \n'), 'Cancelled by web-operator: wrong task');
    assert.equal(m.tdm116CancelNote(''), 'Cancelled by web-operator');
    assert.ok(m.TDM116_TERMINAL.has('shipped'));
  });
});
