// Parity check for the event_log chain hash (TDM-77): runs the browser implementation
// (apps/web/src/lib/eventlog/chain.ts) under Node and compares it with node:crypto and the
// Python reference on the same rows.
//   node spikes/tdm-77-events/parity_check.mts [rows.json]
// rows.json: [[id, ts_micros, actor, type, payload_json], ...] (e.g. from chain_hash_ref.py)
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';

// apps/web imports without extensions (bundler resolution); add `.ts` for Node.
registerHooks({
  resolve(spec, ctx, next) {
    try {
      return next(spec, ctx);
    } catch (e) {
      if (spec.startsWith('.')) return next(`${spec}.ts`, ctx);
      throw e;
    }
  },
});
const { chainSummary } = await import('../../apps/web/src/lib/eventlog/chain.ts');
const { sha256, toHex } = await import('../../apps/web/src/lib/eventlog/sha256.ts');

// 1. sha256 against node:crypto on assorted lengths (padding edge cases at 55/56/63/64).
for (const len of [0, 1, 3, 55, 56, 57, 63, 64, 65, 119, 120, 1000, 100_000]) {
  const buf = new Uint8Array(len).map((_, i) => (i * 31 + len) & 0xff);
  const want = createHash('sha256').update(buf).digest('hex');
  if (toHex(sha256(buf)) !== want) throw new Error(`sha256 mismatch at len ${len}`);
}
console.log('sha256: ok');

// 2. Chain over the self-test vectors (must equal `chain_hash_ref.py --selftest`).
const vec = [
  [1, 1792824327305282, 'setup-agent', 'infra.smoke_test', '{"note":"héllo"}'],
  [2, 1792824327576637, 'planner', 'data_plan.recorded', '{"ticket_id":1,"data_type":"recovery_takeover"}'],
] as const;
const toRows = (rs: readonly (readonly [number | string, number | string, string, string, string])[]) =>
  rs.map(([id, ts, actor, type, payloadJson]) => ({ id: BigInt(id), tsMicros: BigInt(ts), actor, type, payloadJson }));
console.log('selftest:', chainSummary(toRows(vec)).hash);

// 3. Optional: a dump of real rows.
const file = process.argv[2];
if (file) {
  const rows = toRows(JSON.parse(readFileSync(file, 'utf8'))).sort((a, b) => (a.id < b.id ? -1 : 1));
  const s = chainSummary(rows);
  console.log(`rows: ${s.count} ids ${s.firstId}..${s.lastId} gaps ${s.gaps.length}`);
  console.log('chain hash', s.hash);
}
