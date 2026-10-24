'use client';

import { fmtCents, fmtTs } from '@/lib/format';
import { Empty, StatusPill } from './Badges';
import { byNewest, fmtDuration, parseObj, shortUri, type TrainingRun } from './util';

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

/** Loss from metrics_json: a curve array if present, else first/last (spikes/tdm-36 modal_finetune). */
function lossOf(m: Record<string, unknown> | null): { curve?: number[]; first?: number; last?: number } {
  if (!m) return {};
  const arr = [m.loss_curve, m.losses, m.loss].find((v) => Array.isArray(v) && v.every((x) => typeof x === 'number')) as number[] | undefined;
  const first = num(m.loss_first) ?? arr?.[0];
  const last = num(m.loss_last) ?? num(m.final_loss) ?? num(m.train_loss) ?? num(m.loss) ?? arr?.[arr.length - 1];
  return { curve: arr && arr.length > 1 ? arr : undefined, first, last };
}

function Sparkline({ values }: { values: number[] }) {
  const w = 120;
  const h = 28;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - 2 - ((v - lo) / span) * (h - 4)}`).join(' ');
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={`Loss from ${values[0]} to ${values[values.length - 1]}`} className="text-zinc-800 dark:text-zinc-200">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

const HP_KEYS = ['lr', 'steps', 'batch_size', 'lora_rank'];

export function TrainingRuns({ runs }: { runs: readonly TrainingRun[] }) {
  if (runs.length === 0) return <Empty>No training runs yet. A fine-tune starts once enough episodes are accepted.</Empty>;
  return (
    <ul className="space-y-3">
      {byNewest(runs).map((r) => {
        const hp = parseObj(r.hyperparamsJson) ?? {};
        const metrics = parseObj(r.metricsJson);
        const loss = lossOf(metrics);
        const started = r.startedAt?.toDate().getTime();
        const finished = r.finishedAt?.toDate().getTime();
        const elapsed = started ? (finished ?? Date.now()) - started : undefined;
        const extraHp = Object.keys(hp).filter((k) => !HP_KEYS.includes(k));
        return (
          <li key={r.id.toString()} className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h3 className="font-semibold">Run #{r.id.toString()}, iteration {r.iteration}</h3>
              <StatusPill status={r.status} />
              <span className="text-sm tabular-nums text-zinc-500">{r.episodeIds.length} episodes</span>
              <span className="ml-auto text-sm tabular-nums" title="GPU cost (Modal)">
                GPU {fmtCents(r.gpuCostCents)}
              </span>
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
              <div>
                <dt className="text-xs text-zinc-500">Loss</dt>
                <dd className="mt-0.5 flex items-center gap-2 tabular-nums">
                  {loss.curve && <Sparkline values={loss.curve} />}
                  {loss.last !== undefined ? (
                    <span>
                      {loss.first !== undefined && loss.first !== loss.last ? `${loss.first.toFixed(3)} to ` : ''}
                      {loss.last.toFixed(3)}
                    </span>
                  ) : (
                    <span className="text-zinc-400">not reported</span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-zinc-500">Hyperparameters</dt>
                <dd className="mt-0.5 tabular-nums">
                  {HP_KEYS.filter((k) => hp[k] !== undefined).map((k) => `${k.replace('_', ' ')} ${String(hp[k])}`).join(', ') || '—'}
                  {extraHp.length > 0 && <span className="text-zinc-500" title={JSON.stringify(hp)}> +{extraHp.length} more</span>}
                </dd>
              </div>
              <div>
                <dt className="text-xs text-zinc-500">Time</dt>
                <dd className="mt-0.5 tabular-nums">
                  {elapsed !== undefined ? fmtDuration(elapsed) : r.status === 'queued' ? 'queued' : '—'}
                  {r.startedAt && <span className="block text-xs text-zinc-500">started {fmtTs(r.startedAt)}</span>}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-xs text-zinc-500">Checkpoint</dt>
                <dd className="mt-0.5 truncate" title={r.checkpointUri ?? r.baseCheckpointUri}>
                  {r.checkpointUri ? <code className="text-xs">{shortUri(r.checkpointUri, 3)}</code> : <span className="text-zinc-400">not yet</span>}
                  <span className="block truncate text-xs text-zinc-500" title={r.baseCheckpointUri}>from {shortUri(r.baseCheckpointUri, 2)}</span>
                </dd>
              </div>
            </dl>
            {r.modalCallId && <p className="mt-2 text-xs text-zinc-500">Modal call <code>{r.modalCallId}</code></p>}
            {r.error && <p className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/30 dark:text-red-300">{r.error}</p>}
          </li>
        );
      })}
    </ul>
  );
}
