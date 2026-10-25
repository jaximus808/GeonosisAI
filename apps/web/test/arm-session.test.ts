// Reset arm + Exit on the teleop page (lib/teleop/armSession.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const imp = async <T>(p: string) => (await import(new URL(p, import.meta.url).href)) as T;
const arm = await imp<typeof import('../src/lib/teleop/armSession')>('../src/lib/teleop/armSession.ts');
const src = (p: string) => readFileSync(new URL(p, import.meta.url), 'utf8');

type Msg = Record<string, unknown> & { t: string };

/** A fake /control socket: records sends; `robot(sent)` answers with messages. */
function channel(robot: (sent: Msg, emit: (m: Msg) => void) => void, open = true) {
  const subs = new Set<(m: Msg) => void>();
  const sent: Msg[] = [];
  const emit = (m: Msg) => queueMicrotask(() => subs.forEach((f) => f(m)));
  return {
    sent,
    subs,
    ch: {
      send(m: Record<string, unknown>) {
        if (!open) return false;
        sent.push(m as Msg);
        robot(m as Msg, emit);
        return true;
      },
      subscribe(fn: (m: Msg) => void) {
        subs.add(fn);
        return () => void subs.delete(fn);
      },
    },
  };
}

const fastTimers = { setTimeout: (fn: () => void) => setTimeout(fn, 5), clearTimeout: (h: unknown) => clearTimeout(h as never) };

describe('reset progress', () => {
  it('reads resetting + percent from the state message', () => {
    assert.deepEqual(arm.resetProgress({ resetting: true, reset: { active: true, percent: 40.4 } }), { resetting: true, percent: 40 });
    assert.equal(arm.resetLabel(arm.resetProgress({ resetting: true, reset: { active: true, percent: 40 } })), 'Resetting… 40%');
    assert.equal(arm.resetLabel(arm.resetProgress({ resetting: false, reset: { active: false } })), 'Reset arm');
    assert.equal(arm.resetLabel(arm.resetProgress(null)), 'Reset arm');
    assert.equal(arm.recordingActive({ active: true }), true);
    assert.equal(arm.recordingActive({ active: false }), false);
  });
});

describe('requestReset', () => {
  it('sends reset_arm and resolves on its ack, reporting progress', async () => {
    const seen: number[] = [];
    const { ch, sent, subs } = channel((m, emit) => {
      emit({ t: 'state', resetting: true, reset: { active: true, percent: 30 } });
      emit({ t: 'ack', op: 'reset_arm', req: m.req, ok: true, percent: 100 });
    });
    const r = await arm.requestReset(ch, { onProgress: (p) => seen.push(p.percent ?? -1) });
    assert.equal(sent[0].t, 'reset_arm');
    assert.ok(r.ok && r.percent === 100);
    assert.deepEqual(seen, [30]);
    assert.equal(subs.size, 0, 'unsubscribed');
  });
  it('a refusal comes back with its code; a silent robot times out; a closed socket fails at once', async () => {
    const refused = channel((m, emit) => emit({ t: 'ack', op: 'reset_arm', req: m.req, ok: false, code: 'recording' }));
    const r = await arm.requestReset(refused.ch);
    assert.equal(r.ok, false);
    assert.equal(r.code, 'recording');
    assert.match(arm.resetErrorText(r), /recording/);
    const silent = channel(() => {});
    assert.equal((await arm.requestReset(silent.ch, { timers: fastTimers })).code, 'timeout');
    const closed = channel(() => {}, false);
    assert.equal((await arm.requestReset(closed.ch)).code, 'not_allowed');
  });
});

describe('runExit', () => {
  it('in order: discard the recording, reset and wait, give back control, task list', async () => {
    const order: string[] = [];
    const { ch } = channel((m, emit) => {
      order.push(m.t);
      if (m.t === 'record_discard') emit({ t: 'record_ack', op: 'record_discard', req: m.req, ok: true });
      if (m.t === 'reset_arm') emit({ t: 'ack', op: 'reset_arm', req: m.req, ok: true });
    });
    const r = await arm.runExit({
      channel: ch,
      recording: 'discard',
      release: async () => void order.push('release'),
      navigate: (href) => order.push(`go ${href}`),
    });
    assert.ok(r.ok && r.left);
    assert.deepEqual(order, ['record_discard', 'reset_arm', 'release', `go ${arm.EXIT_HREF}`]);
  });
  it('a failed reset stops before releasing (unless the operator chose Leave anyway)', async () => {
    const order: string[] = [];
    const { ch } = channel((m, emit) => emit({ t: 'ack', op: 'reset_arm', req: m.req, ok: false, code: 'stopped' }));
    const deps = { channel: ch, release: async () => void order.push('release'), navigate: () => order.push('go') };
    const r = await arm.runExit(deps);
    assert.equal(r.left, false);
    assert.deepEqual(order, []);
    const r2 = await arm.runExit({ ...deps, leaveOnResetFailure: true });
    assert.equal(r2.left, true);
    assert.deepEqual(order, ['release', 'go']);
  });
});

describe('the teleop page mounts it', () => {
  it('TeleopSession puts ArmSessionButtons next to the recording controls with the lease release', () => {
    const s = src('../src/components/teleop/TeleopSession.tsx');
    assert.match(s, /<ArmSessionButtons release=\{release\}/);
    assert.match(s, /release=\{dev \? undefined : lease\.release\}/);
    const b = src('../src/components/teleop/ArmSessionButtons.tsx');
    assert.match(b, /disabled=\{!open \|\| stopped \|\| recording/);
    assert.match(b, /Discard this recording\?/);
  });
});
