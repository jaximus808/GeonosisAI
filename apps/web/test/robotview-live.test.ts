// TDM-96: the browser side of /ws/state (lib/robotview/liveFeed.ts). `pnpm --filter @rdf/web test`.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const { ClockSync, RateMeter, RollingMedian, STALE_MS, fixtureFrameAt, parseStateMessage, stateUrlFrom, toLiveState } = (await import(
  new URL('../src/lib/robotview/liveFeed.ts', import.meta.url).href
)) as typeof import('../src/lib/robotview/liveFeed');

const FIXTURE = JSON.parse(readFileSync(new URL('../src/lib/robotview/fixtures/sim_arm_motion.json', import.meta.url), 'utf8'));

const msg = (o: Record<string, unknown> = {}) => ({
  t: 'state', v: 1, sim: false, t_ms: 1000, joints: [0, 0.1, 0.2, 0, 0, 0], gripper: 0.5,
  ee: { pos: [0.3, 0, 0.2], quat: [1, 0, 0, 0] }, control_source: 'policy', robot_state: 'rollout', estop: false,
  safety: { clamps_1s: { clamped_speed: 2 } }, ...o,
});

describe('RateMeter', () => {
  it('counts messages in the last second', () => {
    const r = new RateMeter(1000);
    for (let i = 0; i < 30; i++) r.tick(10_000 + i * 33);
    assert.equal(r.hz(10_000 + 29 * 33), 30);
    assert.equal(r.hz(10_000 + 29 * 33 + 2000), 0);
  });
});

describe('ClockSync', () => {
  it('uses the min-RTT pong and corrects latency for the robot clock offset', () => {
    const c = new ClockSync();
    assert.equal(c.offsetMs, null);
    c.onPong(1000, 1100, 5_000_050 + 40); // rtt 100, noisy
    c.onPong(2000, 2010, 5_002_005); // rtt 10: robot clock = ours + 5_000_000
    assert.equal(c.rttMs, 10);
    assert.equal(c.offsetMs, 5_000_000);
    // stamped 5_003_000 on the robot = 3000 on ours; received at 3012 -> 12 ms
    assert.equal(c.latency(5_003_000, 3012), 12);
  });
});

describe('RollingMedian', () => {
  it('median of the window', () => {
    const m = new RollingMedian(3);
    [1, 100, 3, 2].forEach((x) => m.push(x));
    assert.equal(m.value, 3);
  });
});

describe('parseStateMessage', () => {
  it('accepts the contract and normalises enums', () => {
    const s = parseStateMessage(msg({ control_source: 'weird', estop: 1 }))!;
    assert.equal(s.control_source, 'idle');
    assert.equal(s.estop, false);
    const live = toLiveState(parseStateMessage(msg())!);
    assert.equal('t' in live || 'v' in live || 'sim' in live, false);
    assert.deepEqual(live.safety.clamps_1s, { clamped_speed: 2 });
  });
  it('drops anything that is not a well-formed state (never draws garbage)', () => {
    for (const bad of [null, 'x', msg({ t: 'pong' }), msg({ joints: [0, NaN] }), msg({ ee: { pos: [0, 0], quat: [1, 0, 0, 0] } }), msg({ t_ms: '1' })])
      assert.equal(parseStateMessage(bad), null);
  });
});

describe('stateUrlFrom', () => {
  it('prefers the published state_url, else swaps the camera path', () => {
    assert.equal(stateUrlFrom({ state_url: 'wss://a/ws/state', stream_url: 'wss://b/ws/cameras' }), 'wss://a/ws/state');
    assert.equal(stateUrlFrom({ stream_url: 'wss://b.trycloudflare.com/ws/cameras' }), 'wss://b.trycloudflare.com/ws/state');
    assert.equal(stateUrlFrom({ stream_url: 'wss://b/other' }), null);
  });
});

describe('sim_arm_motion fixture', () => {
  it('is labelled a SIM fixture and every frame passes the contract check', () => {
    assert.equal(FIXTURE.fixture, true);
    assert.equal(FIXTURE.sim, true);
    assert.match(FIXTURE.label, /FIXTURE/);
    assert.ok(FIXTURE.frames.length >= 60 && FIXTURE.hz >= 20);
    for (const f of FIXTURE.frames) {
      const s = parseStateMessage(f);
      assert.ok(s && s.sim === true && s.joints.length === 6);
    }
    const j0 = FIXTURE.frames.map((f: { joints: number[] }) => f.joints[0]);
    assert.ok(Math.max(...j0) - Math.min(...j0) > 0.05, 'the arm moves in the fixture');
  });
  it('plays back looping, re-stamped to now', () => {
    const start = 50_000;
    const a = fixtureFrameAt(FIXTURE, start, start);
    assert.equal(a.t_ms, start);
    assert.deepEqual(a.joints, FIXTURE.frames[0].joints);
    const span = FIXTURE.frames.at(-1).t_ms - FIXTURE.frames[0].t_ms + 1000 / FIXTURE.hz;
    assert.deepEqual(fixtureFrameAt(FIXTURE, start, start + span + 1).joints, FIXTURE.frames[0].joints);
  });
  it('stale threshold leaves room for the 10 Hz check inside 1 s', () => {
    assert.ok(STALE_MS + 100 < 1000);
  });
});
