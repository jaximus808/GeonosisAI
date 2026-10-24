'use client';

import { useEffect, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { FakeBadge, StatusPill } from './Badges';
import { byOldest, ciText, isFakeTrial, pct, shortUri, type EvalRun, type TicketEvent } from './util';

type Settings = { successThreshold: number; requireCiLow: boolean } | undefined;

function runLabel(run: EvalRun, iterationOf: Map<bigint, number>): string {
  if (run.kind === 'baseline') return 'Baseline';
  if (run.kind === 'ab_live') return 'Live A/B';
  const it = iterationOf.get(run.trainingRunId);
  return it !== undefined ? `Fine-tune, iteration ${it}` : 'Fine-tune';
}

/** stats.py meets_threshold: point estimate, or the CI lower bound when require_ci_low. */
function verdict(run: EvalRun, s: Settings): { met: boolean; text: string } | null {
  if (run.status !== 'done' || !s || run.trials === 0) return null;
  const rate = run.successes / run.trials;
  const basis = s.requireCiLow ? run.ciLow : rate;
  const met = basis + 1e-9 >= s.successThreshold;
  return {
    met,
    text: `target ${pct(s.successThreshold)}% on ${s.requireCiLow ? 'CI lower bound' : 'point estimate'}: ${met ? 'met' : 'not met'}`,
  };
}

function Interval({ run, target, fake }: { run: EvalRun; target?: number; fake: boolean }) {
  const done = run.status === 'done' && run.trials > 0;
  const n = done ? run.trials : run.completed;
  const rate = n > 0 ? run.successes / n : undefined;
  const band = fake ? 'bg-fuchsia-500/35' : rate !== undefined && target !== undefined && rate >= target ? 'bg-emerald-600/30' : 'bg-zinc-500/30';
  const point = fake ? 'bg-fuchsia-600' : rate !== undefined && target !== undefined && rate >= target ? 'bg-emerald-700' : 'bg-zinc-900 dark:bg-zinc-100';
  return (
    <div className="relative h-8" aria-hidden>
      <div className="absolute inset-x-0 top-1/2 h-px bg-zinc-300 dark:bg-zinc-700" />
      {[0, 25, 50, 75, 100].map((t) => (
        <div key={t} className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-zinc-300 dark:bg-zinc-700" style={{ left: `${t}%` }} />
      ))}
      {target !== undefined && (
        <div className="absolute inset-y-0 w-0 border-l-2 border-dashed border-sky-600" style={{ left: `${target * 100}%` }} />
      )}
      {done && (
        <div
          className={`absolute top-1/2 h-3 -translate-y-1/2 rounded-sm ${band}`}
          style={{ left: `${run.ciLow * 100}%`, width: `${Math.max(0.5, (run.ciHigh - run.ciLow) * 100)}%` }}
        />
      )}
      {rate !== undefined && (
        <div
          className={`absolute top-1/2 h-4 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-sm ${point} ${done ? '' : 'opacity-40'}`}
          style={{ left: `${rate * 100}%` }}
        />
      )}
    </div>
  );
}

function TrialChips({ run, onFake }: { run: EvalRun; onFake: (fake: boolean) => void }) {
  const [trials] = useRows('evalTrial', (r) => r.evalRunId.eq(run.id));
  const byIdx = new Map(trials.map((t) => [t.trialIndex, t]));
  const fake = trials.some(isFakeTrial);
  useEffect(() => onFake(fake), [fake, onFake]);
  return (
    <ol className="flex flex-wrap gap-1" aria-label="Trials">
      {Array.from({ length: run.trials }, (_, i) => {
        const t = byIdx.get(i);
        const cls = !t
          ? 'border border-dashed border-zinc-300 text-zinc-400 dark:border-zinc-700'
          : t.success
            ? 'bg-emerald-600 text-white'
            : 'bg-red-600 text-white';
        const title = t
          ? `Trial ${i + 1}: ${t.success ? 'success' : 'failure'}${t.failureNote ? ` (${t.failureNote})` : ''}${t.episodeId ? `, episode #${t.episodeId}` : ''}`
          : `Trial ${i + 1}: not run yet`;
        const body = <span className={`flex h-6 w-6 items-center justify-center rounded text-[11px] font-medium tabular-nums ${cls}`}>{i + 1}</span>;
        return (
          <li key={i} title={title}>
            {t && t.episodeId !== 0n ? (
              <a href={`#episode-${t.episodeId}`} className="block rounded focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-sky-600">
                {body}
              </a>
            ) : (
              body
            )}
          </li>
        );
      })}
    </ol>
  );
}


function EvalRow({ run, label, summary, settings, robotFake, emphasis }: {
  run: EvalRun; label: string; summary?: string; settings: Settings; robotFake: boolean; emphasis: boolean;
}) {
  const [trialFake, setTrialFake] = useState(false);
  const fake = robotFake || trialFake;
  const v = verdict(run, settings);
  const text =
    run.status === 'done'
      ? (summary ?? ciText(run.successes, run.trials, run.ciLow, run.ciHigh))
      : `${run.successes}/${run.completed} so far, ${run.trials - run.completed} of ${run.trials} trials left`;
  return (
    <div className={`grid gap-x-6 gap-y-3 py-4 md:grid-cols-[12rem_minmax(0,1fr)_14rem] ${emphasis ? '' : 'opacity-80'}`}>
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-semibold">{label}</h3>
          {fake && <FakeBadge />}
          {run.status !== 'done' && <StatusPill status={run.status} />}
        </div>
        <p className="mt-0.5 truncate text-xs text-zinc-500" title={run.checkpointUri}>
          <code>{shortUri(run.checkpointUri, 2)}</code>
        </p>
      </div>
      <div className="min-w-0 space-y-2">
        <Interval run={run} target={settings?.successThreshold} fake={fake} />
        <TrialChips run={run} onFake={setTrialFake} />
      </div>
      <div>
        <p className="text-lg font-semibold tabular-nums leading-snug">{text}</p>
        {v && (
          <p className={`mt-1 text-xs ${v.met ? 'text-emerald-700 dark:text-emerald-400' : 'text-zinc-500'}`}>{v.text}</p>
        )}
      </div>
    </div>
  );
}

/**
 * Baseline vs fine-tuned evals on one 0–100% axis: Wilson 95% CI band, point estimate, target line,
 * and one chip per trial (green success, red failure) linking to its episode.
 */
export function EvalCards({ runs, events, settings, robotFake, iterationOf }: {
  runs: readonly EvalRun[]; events: TicketEvent[]; settings: Settings; robotFake: boolean; iterationOf: Map<bigint, number>;
}) {
  const ordered = byOldest(runs);
  const summaries = new Map<number, string>();
  for (const { row, payload } of events) {
    if (row.type === 'eval.finalized' && typeof payload.summary === 'string') summaries.set(Number(payload.eval_run_id), payload.summary);
  }
  const baseline = [...ordered].reverse().find((r) => r.kind === 'baseline');
  const candidate = [...ordered].reverse().find((r) => r.kind === 'candidate');
  const before = baseline?.status === 'done' && baseline.trials ? baseline.successes / baseline.trials : undefined;
  const after = candidate?.status === 'done' && candidate.trials ? candidate.successes / candidate.trials : undefined;

  if (ordered.length === 0) {
    return <p className="text-sm text-zinc-500">No eval runs yet. The baseline runs the base π0.5 policy for {settings ? 'the configured number of' : 'N'} trials first.</p>;
  }
  return (
    <div>
      {before !== undefined && after !== undefined && (
        <p className="mb-2 text-2xl font-semibold tracking-tight">
          {pct(before)}% before, {pct(after)}% after fine-tuning
          {robotFake && <span className="ml-2 align-middle"><FakeBadge /></span>}
        </p>
      )}
      <div className="mb-1 hidden grid-cols-[12rem_minmax(0,1fr)_14rem] gap-x-6 text-xs text-zinc-500 md:grid">
        <span />
        <span className="relative h-4">
          {[0, 25, 50, 75, 100].map((t) => (
            <span key={t} className="absolute -translate-x-1/2 tabular-nums" style={{ left: `${t}%` }}>{t}%</span>
          ))}
        </span>
        <span>Successes, Wilson 95% CI</span>
      </div>
      <div className="divide-y divide-zinc-200 border-y border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {ordered.map((r) => (
          <EvalRow
            key={r.id.toString()}
            run={r}
            label={runLabel(r, iterationOf)}
            summary={summaries.get(Number(r.id))}
            settings={settings}
            robotFake={robotFake}
            emphasis={r === baseline || r === candidate || r.kind === 'ab_live'}
          />
        ))}
      </div>
      {settings && (
        <p className="mt-2 text-xs text-zinc-500">
          <span className="mr-1 inline-block h-3 w-0 border-l-2 border-dashed border-sky-600 align-middle" /> target {pct(settings.successThreshold)}%.
          Bar is the 95% confidence interval, tick is the observed rate. Click a trial to see its episode.
        </p>
      )}
    </div>
  );
}
