// TDM-114 proof helper: run the web floor model (apps/web/src/lib/tdm112/floor.ts) on rows dumped from a
// SpacetimeDB SQL query (snake_case JSON), for one ticket. Prints the floor as JSON.
//   node spikes/tdm-114/floor_from_rows.ts rows.json <ticketId>
import { readFileSync } from 'node:fs';

const { tdm112Floor } = (await import(new URL('../../apps/web/src/lib/tdm112/floor.ts', import.meta.url).href)) as typeof import('../../apps/web/src/lib/tdm112/floor');

type Row = Record<string, unknown>;
const rows = JSON.parse(readFileSync(process.argv[2], 'utf8')) as Record<string, Row[]>;
const tid = Number(process.argv[3]);

// SQL timestamps come back as ISO strings (or {__timestamp_micros_since_unix_epoch__}); options as {some}/{none} or null
function tsOf(v: unknown) {
  if (v && typeof v === 'object' && '__timestamp_micros_since_unix_epoch__' in (v as Row)) return { microsSinceUnixEpoch: BigInt((v as Row).__timestamp_micros_since_unix_epoch__ as number) };
  if (typeof v === 'number') return { microsSinceUnixEpoch: BigInt(v) };
  if (typeof v === 'string' && v) return { microsSinceUnixEpoch: BigInt(Date.parse(v)) * 1000n };
  return null;
}
const opt = (v: unknown) => (v && typeof v === 'object' && 'some' in (v as Row) ? (v as Row).some : v && typeof v === 'object' && 'none' in (v as Row) ? null : v);
const big = (v: unknown) => BigInt(Number(v ?? 0));
const obj = (s: unknown) => {
  try {
    return JSON.parse(String(s ?? '{}'));
  } catch {
    return {};
  }
};

const ticketRow = rows.ticket.find((t) => Number(t.id) === tid)!;
const rid = Number(ticketRow.robot_id);
const robotRow = rows.robot.find((r) => Number(r.id) === rid)!;
const evRows = rows.event_log.map((e) => ({ id: Number(e.id), tsUs: Number(tsOf(e.ts)?.microsSinceUnixEpoch ?? 0n), actor: String(e.actor), type: String(e.type), payload: obj(e.payload_json) }));
evRows.sort((a, b) => a.tsUs - b.tsUs || a.id - b.id);
const runs = rows.eval_run.map((r) => ({ id: big(r.id), ticketId: big(r.ticket_id), robotId: big(r.robot_id), kind: String(r.kind), status: String(r.status), trials: Number(r.trials), completed: Number(r.completed), successes: Number(r.successes), trainingRunId: big(r.training_run_id), createdAt: tsOf(r.created_at)! }));
const floor = tdm112Floor({
  ticket: { id: big(ticketRow.id), status: String(ticketRow.status), iteration: Number(ticketRow.iteration), targetSuccess: Number(ticketRow.target_success), createdAt: tsOf(ticketRow.created_at)! },
  evalRuns: runs.filter((r) => Number(r.ticketId) === tid),
  evalTrials: rows.eval_trial.map((t) => ({ evalRunId: big(t.eval_run_id), trialIndex: Number(t.trial_index), success: !!t.success, failureNote: opt(t.failure_note) as string | null, createdAt: tsOf(t.created_at) })),
  tasks: rows.task.filter((t) => Number(t.ticket_id) === tid).map((t) => ({ id: big(t.id), type: String(t.type), status: String(t.status), dataPlanId: big(t.data_plan_id), priceCents: big(t.price_cents) })),
  events: evRows.filter((e) => Number(e.payload.ticket_id) === tid),
  robot: { id: big(robotRow.id), name: String(robotRow.name), status: String(robotRow.status), cameraConfigJson: String(robotRow.camera_config_json) },
  robotCommands: rows.robot_command.map((c) => ({ id: big(c.id), robotId: big(c.robot_id), ticketId: big(c.ticket_id), type: String(c.type), status: String(c.status), payloadJson: String(c.payload_json), issuedAt: tsOf(c.issued_at), ackedAt: tsOf(opt(c.acked_at)) })),
  robotTickets: rows.ticket.filter((t) => Number(t.robot_id) === rid).map((t) => ({ id: big(t.id), status: String(t.status), robotId: big(t.robot_id) })),
  robotEvalRuns: runs.filter((r) => Number(r.robotId) === rid),
  robotEvents: evRows.filter((e) => (e.type.startsWith('intervention.') || e.type.startsWith('robot.')) && Number(e.payload.robot_id) === rid),
  nowUs: Date.now() * 1000,
});
process.stdout.write(JSON.stringify(floor, (_k, v) => (typeof v === 'bigint' ? Number(v) : v)));
