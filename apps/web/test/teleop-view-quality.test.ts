// Teleop performance mode: "Quality: Smooth / Sharp" (lib/teleop/viewQuality.ts). `pnpm --filter @rdf/web test`.
// The no-layout-shift check is a browser test: spikes/teleop-perf/layout_check.py.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const imp = async <T>(p: string) => (await import(new URL(p, import.meta.url).href)) as T;
const vq = await imp<typeof import('../src/lib/teleop/viewQuality')>('../src/lib/teleop/viewQuality.ts');

const mem = () => {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
};
const broken = { getItem: () => { throw new Error('SecurityError'); }, setItem: () => { throw new Error('QuotaExceeded'); } };

describe('view quality', () => {
  it('Smooth caps the big view at 1.25x and the small one at 1x, no shadows, 30 fps', () => {
    const main = vq.twinPerf('smooth', 'main')!;
    const inset = vq.twinPerf('smooth', 'inset')!;
    assert.equal(main.maxDpr, 1.25);
    assert.equal(main.shadows, true);
    assert.ok(main.shadowMapSize <= 1024);
    assert.equal(main.maxFps, null);
    assert.equal(inset.maxDpr, 1);
    assert.equal(inset.shadows, false);
    assert.equal(inset.maxFps, 30);
    // constant objects: safe as memo deps
    assert.equal(vq.twinPerf('smooth', 'main'), main);
  });
  it('Sharp (and no choice) leaves the twin at its own full-quality defaults', () => {
    assert.equal(vq.twinPerf('sharp', 'main'), null);
    assert.equal(vq.twinPerf('sharp', 'inset'), null);
    assert.equal(vq.twinPerf(undefined, 'main'), null);
  });
  it('pixel ratio is min(devicePixelRatio, cap)', () => {
    assert.equal(vq.effectiveDpr(2, 1.25), 1.25);
    assert.equal(vq.effectiveDpr(1, 1.25), 1);
    assert.equal(vq.effectiveDpr(3, 1), 1);
    assert.equal(vq.effectiveDpr(undefined, 1.25), 1);
    assert.equal(vq.effectiveDpr(0.8, 1), 0.8);
  });
  it('is remembered in storage and defaults to Smooth', () => {
    const s = mem();
    assert.equal(vq.readViewQuality(s), 'smooth');
    assert.equal(vq.writeViewQuality('sharp', s), true);
    assert.equal(s.m.get(vq.VIEW_QUALITY_KEY), 'sharp');
    assert.equal(vq.readViewQuality(s), 'sharp');
    s.setItem(vq.VIEW_QUALITY_KEY, 'ultra');
    assert.equal(vq.readViewQuality(s), 'smooth');
  });
  it('survives storage that throws (private windows, blocked site data)', () => {
    assert.equal(vq.readViewQuality(broken), 'smooth');
    assert.equal(vq.writeViewQuality('sharp', broken), false);
  });
});

describe('pane layout', () => {
  const ids = ['top', 'side', 'wrist'];
  it('Smooth while holding control: the big view + ONE live small view, the rest paused', () => {
    assert.deepEqual(vq.paneLayout(ids, { quality: 'smooth', driving: true }), { main: 'top', live: ['side'], paused: ['wrist'] });
    assert.deepEqual(vq.paneLayout(ids, { quality: 'smooth', driving: true, inset: 'wrist' }), { main: 'top', live: ['wrist'], paused: ['side'] });
    assert.deepEqual(vq.paneLayout(ids, { quality: 'smooth', driving: true, inset: 'nope' }).live, ['side']);
  });
  it('watching, or Sharp: every view live', () => {
    assert.deepEqual(vq.paneLayout(ids, { quality: 'smooth', driving: false }).paused, []);
    assert.deepEqual(vq.paneLayout(ids, { quality: 'sharp', driving: true }), { main: 'top', live: ['side', 'wrist'], paused: [] });
    assert.deepEqual(vq.paneLayout(['top', 'wrist'], { quality: 'smooth', driving: true }).paused, []);
  });
});
