'use client';

import { useEffect, useState } from 'react';
import { useRows } from '@/lib/spacetime/hooks';
import { Readout } from '@/components/brand/Readout';
import { FakeBadge, SimBadge, StatusPill } from './Badges';
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
  const band = fake ? 'bg-slag/30' : rate !== undefined && target !== undefined && rate >= target ? 'bg-arc/45' : 'bg-steel/55';
  const point = fake ? 'bg-slag' : 'bg-ink';
  return (
    <div className="relative h-8" aria-hidden>
      <div className="absolute inset-x-0 top-1/2 h-px bg-line" />
      {[0, 25, 50, 75, 100].map((t) => (
        <div key={t} className="absolute top-1/2 h-2 w-px -translate-y-1/2 bg-line" style={{ left: `${t}%` }} />
      ))}
      {target !== undefined && (
        <div className="absolute inset-y-0 w-0 border-l-2 border-dashed border-melt" style={{ left: `${target * 100}%` }} />
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
          ? 'border border-dashed border-line-strong text-ink-muted'
          : t.success
            ? 'bg-arc-soft text-arc'
            : 'bg-slag-soft text-slag';
        const title = t
          ? `Trial ${i + 1}: ${t.success ? 'success' : 'failure'}${t.failureNote ? ` (${t.failureNote})` : ''}${t.episodeId ? `, episode #${t.episodeId}` : ''}`
          : `Trial ${i + 1}: not run yet`;
        const body = <span className={`flex h-6 w-6 items-center justify-center rounded-sm font-mono text-[11px] font-medium tabular-nums ${cls}`}>{i + 1}</span>;
        return (
          <li key={i} title={title}>
            {t && t.episodeId !== 0n ? (
              <a href={`#episode-${t.episodeId}`} className="block rounded-sm">
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

/** TDM-109: one eval run as a brand Readout, with a P/F chip per finished trial linking to its episode. */
function RunReadout({ run, label, settings }: { run: EvalRun; label: string; settings: Settings }) {
  const [trials] = useRows('evalTrial', (r) => r.evalRunId.eq(run.id));
  const done = run.status === 'done' && run.trials > 0;
  const n = done ? run.trials : run.completed;
  const v = verdict(run, settings);
  const chips = [...trials]
    .sort((a, b) => a.trialIndex - b.trialIndex)
    .map((t) => ({
      ok: t.success,
      title: `Trial ${t.trialIndex + 1}: ${t.success ? 'Pass' : 'Fail'}${t.failureNote ? ` (${t.failureNote})` : ''}`,
      href: t.episodeId !== 0n ? `#episode-${t.episodeId}` : undefined,
    }));
  return (
    <Readout
      label={label}
      successes={run.successes}
      trials={n}
      lo={done ? run.ciLow : undefined}
      hi={done ? run.ciHigh : undefined}
      target={settings?.successThreshold}
      pass={v ? v.met : false}
      pending={n === 0}
      trialChips={chips}
      footer={!done ? <span className="gx-small">{run.trials - run.completed} of {run.trials} trials left</span> : undefined}
    />
  );
}

function EvalRow({ run, label, summary, settings, robotFake, emphasis, sim }: {
  run: EvalRun; label: string; summary?: string; settings: Settings; robotFake: boolean; emphasis: boolean; sim: boolean;
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
          <h3 className="font-display text-lg font-bold">{label}</h3>
          {fake && <FakeBadge />}
          {sim && <SimBadge />}
          {run.status !== 'done' && <StatusPill status={run.status} />}
        </div>
        <p className="gx-id mt-0.5 truncate" title={run.checkpointUri}>
          <code>{shortUri(run.checkpointUri, 2)}</code>
        </p>
      </div>
      <div className="min-w-0 space-y-2">
        <Interval run={run} target={settings?.successThreshold} fake={fake} />
        <TrialChips run={run} onFake={setTrialFake} />
      </div>
      <div>
        <p className="font-mono text-base font-medium tabular-nums leading-snug">{text}</p>
        {v && (
          <p className={`mt-1 text-xs ${v.met ? 'text-arc' : 'text-ink-muted'}`}>{v.text}</p>
        )}
      </div>
    </div>
  );
}

/**
 * Baseline vs fine-tuned evals on one 0–100% axis: Wilson 95% CI band, point estimate, target line,
 * and one chip per trial (green success, red failure) linking to its episode.
 */
export function EvalCards({ runs, events, settings, robotFake, iterationOf, simRobotIds, mode }: {
  runs: readonly EvalRun[]; events: TicketEvent[]; settings: Settings; robotFake: boolean; iterationOf: Map<bigint, number>;
  /** TDM-107: robots that are the sim robot (each run is labelled by the robot it ran on). */
  simRobotIds?: Set<bigint>;
  /** TDM-107: the ticket's current robot mode; the before/after headline uses that mode's runs only. */
  mode?: 'sim' | 'real' | null;
}) {
  const ordered = byOldest(runs);
  const summaries = new Map<number, string>();
  for (const { row, payload } of events) {
    if (row.type === 'eval.finalized' && typeof payload.summary === 'string') summaries.set(Number(payload.eval_run_id), payload.summary);
  }
  const isSim = (r: EvalRun) => !!simRobotIds?.has(r.robotId);
  // Never mix sim and real: the headline compares runs of the ticket's current mode only.
  const sameMode = (r: EvalRun) => !mode || isSim(r) === (mode === 'sim');
  const baseline = [...ordered].reverse().find((r) => r.kind === 'baseline' && sameMode(r));
  const candidate = [...ordered].reverse().find((r) => r.kind === 'candidate' && sameMode(r));

  if (ordered.length === 0) {
    return (
      <div>
        <div className="mb-3 grid gap-6 sm:grid-cols-2" data-testid="before-after-readouts">
          <Readout label="Before · Baseline" successes={0} trials={0} pending target={settings?.successThreshold} />
          <Readout label="After · not trained yet" successes={0} trials={0} pending target={settings?.successThreshold} />
        </div>
        <p className="gx-small m-0">No eval runs yet. The baseline runs the base π0.5 policy for {settings ? 'the configured number of' : 'N'} trials first.</p>
      </div>
    );
  }
  return (
    <div>
      {/* TDM-109: Before and after as two Readouts on one track scale (Wilson 95%, target tick). */}
      {(baseline || candidate) && (
        <div className="mb-6 grid gap-6 sm:grid-cols-2" data-testid="before-after-readouts">
          {baseline ? (
            <RunReadout run={baseline} label={`Before · ${runLabel(baseline, iterationOf)}`} settings={settings} />
          ) : (
            <Readout label="Before · Baseline" successes={0} trials={0} pending target={settings?.successThreshold} />
          )}
          {candidate ? (
            <RunReadout run={candidate} label={`After · ${runLabel(candidate, iterationOf)}`} settings={settings} />
          ) : (
            <Readout label="After · not trained yet" successes={0} trials={0} pending target={settings?.successThreshold} />
          )}
          {(mode === 'sim' || robotFake) && (
            <p className="gx-small m-0 flex items-center gap-2 sm:col-span-2">
              {mode === 'sim' && <SimBadge />}
              {robotFake && <FakeBadge />}
              {mode === 'sim' ? 'Sim robot results, never real-arm success rates.' : 'Fake-robot numbers, not results.'}
            </p>
          )}
        </div>
      )}
      <div className="gx-id mb-1 hidden grid-cols-[12rem_minmax(0,1fr)_14rem] gap-x-6 md:grid">
        <span />
        <span className="relative h-4">
          {[0, 25, 50, 75, 100].map((t) => (
            <span key={t} className="absolute -translate-x-1/2 tabular-nums" style={{ left: `${t}%` }}>{t}%</span>
          ))}
        </span>
        <span>Successes, Wilson 95% CI</span>
      </div>
      <div className="divide-y divide-line border-y border-line">
        {ordered.map((r) => (
          <EvalRow
            key={r.id.toString()}
            run={r}
            label={runLabel(r, iterationOf)}
            summary={summaries.get(Number(r.id))}
            settings={settings}
            robotFake={robotFake}
            sim={isSim(r)}
            emphasis={r === baseline || r === candidate || r.kind === 'ab_live'}
          />
        ))}
      </div>
      {settings && (
        <p className="gx-small mt-2 mb-0">
          <span className="mr-1 inline-block h-3 w-0 border-l-2 border-dashed border-melt align-middle" /> target {pct(settings.successThreshold)}%.
          Bar is the 95% confidence interval, tick is the observed rate. Click a trial to see its episode.
        </p>
      )}
    </div>
  );
}
