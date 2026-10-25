'use client';

// TDM-116 stages: Diagnose, Plan data, Collect, Review, Train, Ship. Each is purpose-built for what a
// person needs to see at that station, and every value is a live row or event_log row. Model text is
// quoted as written; a missing value says what it waits for.

import { useMemo, useState } from 'react';
import { ButtonLink } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { JoinQr } from '@/components/JoinQr';
import { clusterNotes } from '@/components/ticket/DecisionCard';
import { EpisodeCard } from '@/components/ticket/EpisodesGrid';
import { EvalCards } from '@/components/ticket/EvalCards';
import { CamVideo } from '@/components/ticket/media';
import { StatusPill, StubBadge } from '@/components/ticket/Badges';
import {
  DATA_TYPE_LABEL,
  KIND_LABEL,
  TASK_TYPE_LABEL,
  byNewest,
  cameraUris,
  fmtTime,
  isFakeEpisode,
  isStubCheckpoint,
  parseObj,
  shortUri,
  type Episode,
  type Json,
  type Review,
  type Task,
} from '@/components/ticket/util';
import { fmtCents, fmtTs } from '@/lib/format';
import { llmBadge } from '@/lib/llmbackend/types';
import type { Tdm114LivePolicy } from '@/lib/tdm114/arbitration';
import { tdm112Words } from '@/lib/tdm112/floor';
import { tdm116Drops, tdm116Duration, tdm116PrimaryAction, type Tdm116StageKey } from '@/lib/tdm116/cockpit';
import { WaitingForRobot } from '@/components/tdm114/WaitingForRobot';
import { Tdm116PrimaryButton } from './Actions';
import { Tdm116EvalStage } from './EvalStage';
import { Tdm116RobotWell } from './RobotWell';
import { Tdm125SplitLine } from '@/components/tdm125/CollectRobot';
import { useTdm116Now } from './hooks';
import type { Tdm116Data } from './useCockpitData';

const pct = (x: number) => `${Math.round(x * 100)}%`;
const plural = (k: number, one: string, many = `${one}s`) => `${k} ${k === 1 ? one : many}`;

function Empty({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="tdm116-empty">
      <strong>{title}</strong>
      {children}
    </div>
  );
}

function LlmTag({ rec }: { rec: { backend?: unknown; model?: unknown; backend_model?: unknown; served?: unknown; requested?: unknown } | null | undefined }) {
  const b = llmBadge(rec);
  return (
    <span className="gx-tag" data-llm-backend={b.backend ?? 'unknown'} title={b.backend ? `Produced by the ${b.label} backend${b.model ? `, model ${b.model}` : ''}` : 'This record does not say which LLM backend produced it'} style={{ letterSpacing: '0.04em', textTransform: 'none', fontFamily: 'var(--font-mono)', fontWeight: 500, fontSize: 12 }}>
      <i className="gx-rivet" aria-hidden />
      {b.text}
    </span>
  );
}

/** The stage view for a station. */
export function Tdm116Stage({ d, stage, onPolicy }: { d: Tdm116Data; stage: Tdm116StageKey; onPolicy: (p: Tdm114LivePolicy | null) => void }) {
  switch (stage) {
    case 'baseline':
    case 'eval':
      return <Tdm116EvalStage d={d} which={stage} onPolicy={onPolicy} />;
    case 'diagnose':
      return <DiagnoseStage d={d} />;
    case 'plan_data':
      return <PlanStage d={d} />;
    case 'collect':
      return <CollectStage d={d} />;
    case 'review':
      return <ReviewStage d={d} />;
    case 'train':
      return <TrainStage d={d} />;
    case 'ship':
      return <ShipStage d={d} />;
  }
}

// ------------------------------------------------------------------------------ Diagnose

function currentIteration<T extends { iteration: number }>(rows: readonly T[], iteration: number): T[] {
  const mine = rows.filter((r) => r.iteration === iteration);
  if (mine.length) return mine;
  const top = Math.max(-1, ...rows.map((r) => r.iteration));
  return rows.filter((r) => r.iteration === top);
}

function DiagnoseStage({ d }: { d: Tdm116Data }) {
  const station = d.floor?.stations.find((s) => s.key === 'diagnose');
  const clusters = currentIteration(d.clusters, d.ticket?.iteration ?? 0).sort((a, b) => b.count - a.count);
  const { notes, reasoning } = useMemo(() => clusterNotes(d.events), [d.events]);
  const episodes = useMemo(() => new Map(d.episodes.map((e) => [e.id, e])), [d.episodes]);
  const total = clusters.reduce((n, c) => n + c.count, 0);
  const source = (d.ticket?.iteration ?? 0) > 0 ? d.candidate ?? d.baseline : d.baseline;
  const diagEv = [...d.events].reverse().find((e) => e.row.type === 'diagnose.clusters');
  const failedTrials = source ? d.trials.filter((t) => t.evalRunId === source.id && !t.success).sort((a, b) => a.trialIndex - b.trialIndex) : [];
  const model = (diagEv?.payload.model && typeof diagEv.payload.model === 'object' ? (diagEv.payload.model as Json) : null) ?? (station?.llm ? { backend: station.llm.backend, model: station.llm.model } : null);

  return (
    <div className="tdm116-col" data-testid="stage-diagnose">
      <section className="tdm116-plate is-tight flex flex-wrap items-center gap-x-6 gap-y-2" aria-label="Diagnosis">
        <span className="text-sm">
          {source ? (
            <>
              From the {source.kind === 'baseline' ? 'baseline' : 'fine-tuned eval'}:{' '}
              <span className="gx-data">
                {source.successes}/{source.status === 'done' ? source.trials : source.completed}
              </span>{' '}
              passed, <span className="gx-data text-slag">{(source.status === 'done' ? source.trials : source.completed) - source.successes}</span> failed
            </>
          ) : (
            'No eval to diagnose yet.'
          )}
        </span>
        {clusters.length > 0 && (
          <span className="text-sm">
            <span className="gx-data">{plural(clusters.length, 'failure cluster')}</span> covering <span className="gx-data">{plural(total, 'trial')}</span>
          </span>
        )}
        {clusters.length > 0 && (
          <span className="ml-auto flex items-center gap-2">
            <span className="gx-label">Clustered by</span>
            <LlmTag rec={model} />
          </span>
        )}
      </section>

      {clusters.length === 0 ? (
        failedTrials.length > 0 ? (
          <section className="tdm116-plate tdm116-scroll tdm116-grow" aria-label="Failed trials">
            <div className="tdm116-plate-head">
              <h3>{station?.state === 'working' ? 'Clustering these failed trials' : 'Failed trials'}</h3>
              <span className="gx-id">{station?.activity}</span>
            </div>
            <ul className="tdm116-rows">
              {failedTrials.map((t) => (
                <li key={t.trialIndex}>
                  <div className="tdm116-row-top">
                    <span className="gx-id text-slag">trial {t.trialIndex + 1}</span>
                    <span className="tdm116-row-main text-sm" title={t.failureNote ?? undefined}>
                      {t.failureNote || 'No note recorded'}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
            <p className="gx-small mt-3 mb-0">Cards appear here, one per failure cluster, as soon as the clusterer writes them.</p>
          </section>
        ) : (
          <Empty title="No failure clusters yet">{station?.activity === 'Clustering the failed trials' ? 'Cards appear here as clusters are written.' : station?.activity}</Empty>
        )
      ) : (
        <div className="tdm116-scroll tdm116-grow">
          <div className="tdm116-clusters">
            {clusters.map((c) => {
              const note = notes.get(Number(c.id));
              const ex = c.exampleEpisodeIds.map((id) => episodes.get(id)).filter((e): e is Episode => !!e).slice(0, 3);
              return (
                <article key={c.id.toString()} className="tdm116-plate tdm116-cluster" data-testid="stage-cluster">
                  <div className="tdm116-cluster-count">
                    <b>{c.count}</b>
                    <span className="gx-small">of {plural(total, 'failure')}</span>
                    {note?.phase && <Tag className="ml-auto">{note.phase}</Tag>}
                  </div>
                  <h3>{c.label}</h3>
                  <p className="m-0 text-sm text-ink-muted">{note?.description || c.description || 'No cause written for this cluster.'}</p>
                  {note?.recoverable !== undefined && (
                    <p className="gx-small m-0" title={note.recoverabilityReason}>
                      {note.recoverable ? 'Recoverable mid-episode' : 'Not recoverable mid-episode'}
                      {note.recoverabilityReason ? `: ${note.recoverabilityReason}` : ''}
                    </p>
                  )}
                  {c.exampleEpisodeIds.length > 0 && (
                  <div className="tdm116-thumbs">
                    {[0, 1, 2].map((i) => {
                      const e = ex[i];
                      const cam = e ? cameraUris(e)[0] : undefined;
                      if (e && cam)
                        return (
                          <a key={i} href={`?tab=data#episode-${e.id}`} title={`Episode #${e.id}, ${cam.name} (hover to play)`}>
                            <CamVideo uri={cam.uri} name={cam.name} fake={isFakeEpisode(e)} compact />
                          </a>
                        );
                      return (
                        <div key={i} className="tdm116-thumb">
                          {e ? `#${e.id} no clip` : i < c.exampleEpisodeIds.length ? `#${c.exampleEpisodeIds[i]}` : ''}
                        </div>
                      );
                    })}
                  </div>
                  )}
                </article>
              );
            })}
          </div>
          {reasoning && (
            <section className="tdm116-plate mt-4" aria-label="Diagnosis reasoning">
              <p className="gx-label m-0 mb-2">Diagnosis, verbatim</p>
              <p className="tdm116-quote">{reasoning}</p>
              {diagEv && <p className="tdm116-src">event #{diagEv.row.id.toString()} · {diagEv.row.actor} · {fmtTs(diagEv.row.ts)}</p>}
            </section>
          )}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------ Plan data

function PlanStage({ d }: { d: Tdm116Data }) {
  const t = d.ticket!;
  const station = d.floor?.stations.find((s) => s.key === 'plan_data');
  const plans = byNewest(currentIteration(d.plans, t.iteration));
  const plan = plans[0];
  if (!plan) return <Empty title={station?.state === 'working' ? 'The planner is choosing what to buy' : 'No data plan yet'}>{station?.activity}</Empty>;
  const ev = [...d.events].reverse().find((e) => e.row.type === 'planner.data_plan' && Number(e.payload.data_plan_id) === Number(plan.id));
  const evidence = ev?.payload.evidence && typeof ev.payload.evidence === 'object' ? (ev.payload.evidence as Json) : undefined;
  const alt = evidence?.alternative && typeof evidence.alternative === 'object' ? (evidence.alternative as Json) : undefined;
  const model = evidence?.model && typeof evidence.model === 'object' ? (evidence.model as Json) : station?.llm ? { backend: station.llm.backend, model: station.llm.model } : null;
  const targeted = plan.failureClusterIds.length ? d.clusters.filter((c) => plan.failureClusterIds.includes(c.id)) : currentIteration(d.clusters, plan.iteration);
  const failed = targeted.reduce((n, c) => n + c.count, 0);
  const budget = Number(t.budgetCents);
  const spent = Number(t.spentCents);
  const approved = Number(t.approvedCents);
  const est = Number(plan.estCostCents);
  const proposed = plan.status === 'proposed';
  const left = budget - Math.max(spent, approved) - (proposed ? est : 0);
  const action = tdm116PrimaryAction({ ticketId: t.id, status: t.status, iteration: t.iteration, plans: d.plans, tasks: d.tasks });
  const unit = DATA_TYPE_LABEL[plan.dataType] ?? plan.dataType.replace(/_/g, ' ');

  return (
    <div className="tdm116-decision" data-testid="stage-plan">
      <section className="tdm116-plate tdm116-scroll" aria-label="What failed">
        <p className="gx-label m-0 mb-2">What failed</p>
        {targeted.length === 0 ? (
          <p className="gx-small m-0">No failure clusters linked to this plan.</p>
        ) : (
          <ul className="tdm116-rows">
            {targeted.map((c) => (
              <li key={c.id.toString()}>
                <div className="tdm116-row-top">
                  <span className="gx-data text-slag">{c.count}</span>
                  <span className="tdm116-row-main text-sm">{c.label}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
        {failed > 0 && <p className="gx-small mt-2 mb-0">{plural(failed, 'failed trial')} this plan targets.</p>}
      </section>

      <section className="tdm116-plate tdm116-scroll" aria-label="The plan">
        <div className="flex flex-wrap items-center gap-2">
          <span className="gx-label">The planner chose to buy</span>
          <Tag tone={proposed ? 'money' : plan.status === 'rejected' ? 'failed' : 'shipped'}>{plan.status}</Tag>
          <span className="gx-id">plan #{plan.id.toString()} · iteration {plan.iteration}</span>
        </div>
        <p className="tdm116-buy">
          {plan.amount} {unit.toLowerCase()}
        </p>
        <p className="m-0 text-sm">
          <span className="tdm116-money">{fmtCents(plan.unitPriceCents)}</span> each, <span className="tdm116-money">{fmtCents(plan.estCostCents)}</span> estimated. Test mode.
        </p>
        <div className="tdm116-approve">
          {proposed ? (
            <>
              <Tdm116PrimaryButton action={action} ticketId={Number(t.id)} size="lg" />
              <span className="gx-small">Approving reserves {fmtCents(est)} of the budget for these tasks. Nothing is paid until a reviewer accepts an episode.</span>
            </>
          ) : (
            <span className="text-sm">
              {plan.approvedCents > 0n ? (
                <>
                  Approved <span className="tdm116-money">{fmtCents(plan.approvedCents)}</span>
                  {plan.approvedBy ? ` by ${plan.approvedBy}` : ''}
                  {plan.approvedAt ? ` at ${fmtTs(plan.approvedAt)}` : ''}.
                </>
              ) : (
                `Plan ${plan.status}.`
              )}
            </span>
          )}
        </div>
        <p className="gx-label m-0 mb-2">Why, verbatim</p>
        <p className="tdm116-quote">{plan.reasoning}</p>
        <p className="tdm116-src flex flex-wrap items-center gap-2">
          <span>data_plan #{plan.id.toString()} · {fmtTs(plan.createdAt)}{ev ? ` · event #${ev.row.id}` : ''}</span>
          <LlmTag rec={model} />
        </p>
        {alt && (
          <p className="gx-small mt-3 mb-0">
            Considered instead: {DATA_TYPE_LABEL[String(alt.data_type)] ?? String(alt.data_type ?? '')}
            {typeof alt.amount === 'number' ? `, ${alt.amount} episodes` : ''}
            {typeof alt.est_cost_cents === 'number' ? ` for ${fmtCents(alt.est_cost_cents)}` : ''}
            {typeof alt.why_not === 'string' ? `. Not chosen: ${alt.why_not}` : ''}
          </p>
        )}
      </section>

      <section className="tdm116-plate" aria-label="Budget">
        <p className="gx-label m-0 mb-2">Budget</p>
        <dl className="tdm116-kv">
          <dt>Budget</dt>
          <dd className="tdm116-money">{fmtCents(budget)}</dd>
          <dt>Spent</dt>
          <dd>{fmtCents(spent)}</dd>
          <dt>Approved</dt>
          <dd>{fmtCents(approved)}</dd>
          <dt>{proposed ? 'This plan' : 'Plan cost'}</dt>
          <dd>{fmtCents(est)}</dd>
          <dt>{proposed ? 'Left after' : 'Left'}</dt>
          <dd className={left < 0 ? 'text-slag' : undefined}>{fmtCents(Math.max(0, left))}{left < 0 ? ' (over budget)' : ''}</dd>
        </dl>
        <div className="gx-meter mt-3" role="img" aria-label={`Spent ${fmtCents(spent)} of ${fmtCents(budget)}`}>
          <span style={{ width: budget > 0 ? `${Math.min(100, ((Math.max(spent, approved) + (proposed ? est : 0)) / budget) * 100)}%` : '0%', opacity: 0.35 }} />
          <span style={{ width: budget > 0 ? `${Math.min(100, (spent / budget) * 100)}%` : '0%' }} />
        </div>
        <p className="gx-small mt-2 mb-0">Stripe test mode. No real money moves.</p>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------ Collect

const DATA_TYPES = new Set(['clean_teleop', 'recovery_takeover', 'purchased']);

function JobTile({ t, d, nowMs }: { t: Task; d: Tdm116Data; nowMs: number }) {
  const who = t.assigneeId ? d.people.get(t.assigneeId) : undefined;
  const leaseLeft = t.status === 'claimed' && t.leaseExpiresAt && nowMs ? t.leaseExpiresAt.toDate().getTime() - nowMs : undefined;
  return (
    <li className="tdm116-job" title={t.instructions || undefined}>
      <div className="tdm116-job-top">
        <span className="gx-id">#{t.id.toString()}</span>
        <span className="tdm116-ellipsis flex-1">{TASK_TYPE_LABEL[t.type] ?? t.type}</span>
        <span className="tdm116-money">{fmtCents(t.priceCents)}</span>
      </div>
      <span className="gx-small tdm116-ellipsis">
        {who ? who.name : t.assigneeId ? `contributor #${t.assigneeId}` : 'unassigned'}
        {t.status === 'claimed' && t.claimedAt ? ` · since ${fmtTime(t.claimedAt)}` : ''}
        {leaseLeft !== undefined ? (leaseLeft < 0 ? ' · lease lapsed' : ` · lease ${Math.round(leaseLeft / 1000)}s`) : ''}
        {t.status === 'completed' && t.completedAt ? ` · done ${fmtTime(t.completedAt)}` : ''}
        {t.status !== 'open' && t.status !== 'claimed' && t.status !== 'submitted' && t.status !== 'completed' ? ` · ${t.status}` : ''}
      </span>
    </li>
  );
}

function CollectStage({ d }: { d: Tdm116Data }) {
  const nowMs = useTdm116Now(1000);
  const t = d.ticket!;
  const station = d.floor?.stations.find((s) => s.key === 'collect');
  const jobs = d.tasks.filter((x) => x.type !== 'reset');
  const open = byNewest(jobs.filter((x) => x.status === 'open'));
  const claimed = byNewest(jobs.filter((x) => x.status === 'claimed' || x.status === 'submitted'));
  const finished = byNewest(jobs.filter((x) => !['open', 'claimed', 'submitted'].includes(x.status)));
  const driving = claimed.find((x) => DATA_TYPES.has(x.type));
  const driver = driving?.assigneeId ? d.people.get(driving.assigneeId) : undefined;
  const holder = d.floor?.holder;
  const dataTaskIds = useMemo(() => new Set(jobs.map((x) => Number(x.id))), [jobs]);
  const drops = useMemo(() => tdm116Drops(d.dropEvents, dataTaskIds), [d.dropEvents, dataTaskIds]);
  const plan = byNewest(currentIteration(d.plans, t.iteration))[0];
  const accepted = d.dataset?.acceptedCount ?? 0;

  return (
    <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 1.45fr) minmax(340px, 1fr)' }} data-testid="stage-collect">
      <div className="tdm116-col">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
          {plan ? (
            <span>
              Plan wants <span className="gx-data">{plan.amount}</span> {(DATA_TYPE_LABEL[plan.dataType] ?? plan.dataType).toLowerCase()}
            </span>
          ) : (
            <span className="text-ink-muted">No data plan for this iteration.</span>
          )}
          <span>
            <span className="gx-data">{accepted}</span> accepted so far
            {d.settings?.targetEpisodes ? (
              <span className="text-ink-muted"> of {d.settings.targetEpisodes} to train</span>
            ) : null}
          </span>
        </div>
        <div className="tdm116-board tdm116-grow" aria-label="Paid tasks">
          {[
            { key: 'open', title: 'Open', rows: open, empty: 'No open tasks.' },
            { key: 'claimed', title: 'Claimed', rows: claimed, empty: 'Nobody has claimed one yet.' },
            { key: 'done', title: 'Done', rows: finished, empty: 'None finished yet.' },
          ].map((lane) => (
            <section key={lane.key} className="tdm116-lane" data-lane={lane.key}>
              <div className="tdm116-lane-head">
                <span className="gx-station-label">{lane.title}</span>
                <span className="gx-id">{lane.rows.length}</span>
              </div>
              {lane.rows.length === 0 ? (
                <p className="gx-small m-0 px-1">{lane.empty}</p>
              ) : (
                <ul>
                  {lane.rows.map((x) => (
                    <JobTile key={x.id.toString()} t={x} d={d} nowMs={nowMs} />
                  ))}
                </ul>
              )}
            </section>
          ))}
        </div>
        <div className="grid gap-4" style={{ gridTemplateColumns: 'minmax(0, 1fr) auto' }}>
          <section className="tdm116-plate is-tight" aria-label="Job drops">
            <p className="gx-label m-0 mb-1">Job drops and claims by channel</p>
            {drops.length === 0 ? (
              <p className="gx-small m-0">No drops for this ticket&apos;s tasks yet. They go out when tasks are posted.</p>
            ) : (
              <table className="tdm116-table">
                <thead>
                  <tr>
                    <th>Channel</th>
                    <th className="num">Posted</th>
                    <th className="num">Drafted</th>
                    <th className="num">Claims</th>
                  </tr>
                </thead>
                <tbody>
                  {drops.map((r) => (
                    <tr key={r.channel}>
                      <td>{r.channel}</td>
                      <td className="num">{r.posted}</td>
                      <td className="num">{r.drafted}</td>
                      <td className="num">{r.claims}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {drops.some((r) => r.drafted > 0 && r.posted === 0) && <p className="gx-small mt-1 mb-0">Drafted: dry run, written but not sent.</p>}
          </section>
          <div className="tdm116-qr" aria-label="Scan to do a job">
            <JoinQr ticketId={t.id} size={112} />
          </div>
        </div>
      </div>

      <div className="tdm116-col tdm116-scroll">
        <Tdm125SplitLine split={d.split} status={t.status} stationIndex={station?.index ?? 3} ticketId={Number(t.id)} />
        {!d.split.split && d.floor?.waitingFor?.stationKey === 'collect' && <WaitingForRobot waitingFor={d.floor.waitingFor} ticketId={Number(t.id)} />}
        <section className={gxCx('tdm116-plate is-tight', station?.state === 'waiting' && 'is-waiting')} aria-label="Driving now">
          <p className="gx-label m-0">Driving now</p>
          {driving ? (
            <p className="m-0 mt-1 text-sm">
              <span className="font-semibold">{driver?.name ?? `Contributor #${driving.assigneeId}`}</span> on {TASK_TYPE_LABEL[driving.type]?.toLowerCase() ?? driving.type}{' '}
              <span className="gx-id">#{driving.id.toString()}</span>
            </p>
          ) : holder && holder.ticketId === Number(t.id) ? (
            <p className="m-0 mt-1 text-sm">{holder.words}</p>
          ) : (
            <p className="m-0 mt-1 text-sm text-ink-muted">{station?.activity ?? 'Nobody is driving.'}</p>
          )}
        </section>
        <Tdm116RobotWell
          d={d}
          compact
          hud={false}
          robotOverride={d.split.split ? { id: d.split.collectRobotId, robot: d.allRobots.find((r) => r.id === d.split.collectRobotId), mode: d.split.collectMode } : undefined}
        />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------ Review

function ReviewStage({ d }: { d: Tdm116Data }) {
  const data = d.episodes.filter((e) => e.kind !== 'baseline' && e.kind !== 'eval');
  const latest = useMemo(() => {
    const m = new Map<bigint, Review>();
    for (const r of byNewest(d.reviews).reverse()) m.set(r.episodeId, r);
    return m;
  }, [d.reviews]);
  const order = (e: Episode) => (e.reviewStatus === 'pending' ? 0 : 1);
  const queue = byNewest(data).sort((a, b) => order(a) - order(b));
  const [pick, setPick] = useState<bigint | null>(null);
  const sel = queue.find((e) => e.id === pick) ?? queue[0];
  const payByEp = new Map(d.payments.filter((p) => p.episodeId !== 0n).map((p) => [p.episodeId, p]));
  const accepted = data.filter((e) => e.reviewStatus === 'accepted').length;
  const rejected = data.filter((e) => e.reviewStatus === 'rejected').length;
  const pending = data.filter((e) => e.reviewStatus === 'pending').length;
  const station = d.floor?.stations.find((s) => s.key === 'review');
  if (queue.length === 0) return <Empty title="No recorded episodes yet">{station?.activity}</Empty>;
  return (
    <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(300px, 380px) minmax(0, 1fr)' }} data-testid="stage-review">
      <section className="tdm116-plate tdm116-col" style={{ padding: 0 }} aria-label="Episode queue">
        <div className="flex items-baseline justify-between gap-2 px-3 pt-3">
          <span className="gx-label">Episode queue</span>
          <span className="gx-id">
            {accepted} accepted · {rejected} rejected · {pending} pending
          </span>
        </div>
        <ul className="tdm116-queue tdm116-scroll">
          {queue.map((e) => {
            const r = latest.get(e.id);
            const who = e.contributorId ? d.people.get(e.contributorId) : undefined;
            const tone = e.reviewStatus === 'accepted' ? 'shipped' : e.reviewStatus === 'rejected' ? 'failed' : 'steel';
            return (
              <li key={e.id.toString()}>
                <button type="button" aria-current={sel?.id === e.id} onClick={() => setPick(e.id)}>
                  <span className="tdm116-row-top">
                    <span className="gx-id text-ink">#{e.id.toString()}</span>
                    <span className="tdm116-row-main text-sm">
                      {KIND_LABEL[e.kind] ?? e.kind}
                      {who ? ` · ${who.name}` : ''}
                    </span>
                    <Tag tone={tone}>{e.reviewStatus.replace(/_/g, ' ')}</Tag>
                  </span>
                  <span className="gx-small tdm116-ellipsis">{r?.reason ? r.reason : e.reviewStatus === 'pending' ? 'Waiting for the reviewer' : ''}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </section>
      <div className="tdm116-scroll" aria-label="Selected episode">
        {sel && (
          <EpisodeCard
            e={sel}
            review={latest.get(sel.id)}
            payment={payByEp.get(sel.id)}
            who={sel.contributorId ? d.people.get(sel.contributorId) : undefined}
            sim={d.simRobotIds.has(sel.robotId)}
          />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------ Train

function lossOf(m: Record<string, unknown> | null): { first?: number; last?: number } {
  if (!m) return {};
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);
  const arr = [m.loss_curve, m.losses, m.loss].find((v) => Array.isArray(v) && v.every((x) => typeof x === 'number')) as number[] | undefined;
  return { first: num(m.loss_first) ?? arr?.[0], last: num(m.loss_last) ?? num(m.final_loss) ?? num(m.train_loss) ?? num(m.loss) ?? arr?.[arr.length - 1] };
}

function TrainStage({ d }: { d: Tdm116Data }) {
  const nowMs = useTdm116Now(1000);
  const station = d.floor?.stations.find((s) => s.key === 'train');
  const run = byNewest(d.trainingRuns)[0];
  if (!run) return <Empty title="No training run yet">{station?.activity}</Empty>;
  const hp = parseObj(run.hyperparamsJson) ?? {};
  const m = parseObj(run.metricsJson);
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : typeof v === 'string' && v.trim() && Number.isFinite(Number(v)) ? Number(v) : undefined);
  const total = num(hp.num_train_steps) ?? num(hp.steps) ?? num(hp.train_steps);
  const step = num(m?.step) ?? num(m?.steps_done) ?? num(m?.global_step);
  const done = run.status === 'done';
  const frac = done ? 1 : total && step !== undefined ? Math.min(1, step / total) : undefined;
  const stub = (run.modalCallId ?? '').startsWith('stub-') || isStubCheckpoint(run.checkpointUri) || m?.fake === true;
  const compute = stub ? 'STUB' : run.modalCallId ? 'REAL' : null;
  const loss = lossOf(m);
  const started = run.startedAt?.toDate().getTime();
  const finished = run.finishedAt?.toDate().getTime();
  const elapsed = started ? (finished ?? (nowMs || started)) - started : undefined;
  const runEvents = d.events.filter((e) => e.row.type.startsWith('training.') && (Number(e.payload.training_run_id) === Number(run.id) || e.payload.training_run_id === undefined));
  const seen = new Set(runEvents.map((e) => e.row.type));
  const steps = [
    { label: 'Queued', done: true },
    { label: 'Dataset', done: seen.has('training.convert_spawned') || seen.has('training.launch') || seen.has('training.finetune_spawned') || seen.has('training.stub_running') || run.status !== 'queued' },
    { label: 'Fine-tune', done: done, now: run.status === 'running' },
    { label: run.status === 'failed' ? 'Failed' : 'Checkpoint', done, failed: run.status === 'failed' },
  ];
  const eps = run.episodeIds.map((id) => d.episodes.find((e) => e.id === id) ?? null);
  return (
    <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 1.4fr) minmax(300px, 1fr)' }} data-testid="stage-train">
      <div className="tdm116-col tdm116-scroll">
        <section className="tdm116-plate" aria-label="Training run">
          <div className="tdm116-plate-head">
            <h3>
              Run #{run.id.toString()} <span className="gx-id ml-1">iteration {run.iteration}</span>
            </h3>
            <span className="flex items-center gap-1">
              {compute === 'STUB' ? <StubBadge /> : compute === 'REAL' ? <Tag tone="shipped" title="Real Modal training run">REAL</Tag> : null}
              <StatusPill status={run.status} />
            </span>
          </div>
          <div className="tdm116-big">
            {frac !== undefined ? pct(frac) : elapsed !== undefined ? tdm116Duration(elapsed) : '—'}
            <small>
              {total
                ? step !== undefined || done
                  ? `${(step ?? total).toLocaleString('en-US')} of ${total.toLocaleString('en-US')} steps`
                  : `${run.status === 'queued' ? 'queued' : 'elapsed'} · ${total.toLocaleString('en-US')} steps planned, the trainer reports no step count`
                : 'steps not reported'}
            </small>
          </div>
          {frac !== undefined && (
            <div className={gxCx('tdm116-progress mt-3', done && 'is-done')} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(frac * 100)}>
              <span style={{ transform: `scaleX(${frac})` }} />
            </div>
          )}
          <ol className="tdm116-steps mt-4">
            {steps.map((s) => (
              <li key={s.label} className={gxCx(s.failed ? 'is-failed' : s.done ? 'is-done' : s.now && 'is-now')}>
                {s.label}
              </li>
            ))}
          </ol>
          <dl className="tdm116-kv mt-4">
            <dt>Loss</dt>
            <dd>{loss.last !== undefined ? `${loss.first !== undefined && loss.first !== loss.last ? `${loss.first.toFixed(3)} → ` : ''}${loss.last.toFixed(3)}` : 'not reported'}</dd>
            <dt>Time</dt>
            <dd>{elapsed !== undefined ? tdm116Duration(elapsed) : run.status === 'queued' ? 'queued' : '—'}</dd>
            <dt>Hyperparameters</dt>
            <dd title={run.hyperparamsJson}>{['lr', 'steps', 'num_train_steps', 'batch_size', 'lora_rank'].filter((k) => hp[k] !== undefined).map((k) => `${k} ${String(hp[k])}`).join(' · ') || '—'}</dd>
            <dt>GPU cost</dt>
            <dd className="tdm116-money">{fmtCents(run.gpuCostCents)}</dd>
            <dt>Checkpoint</dt>
            <dd title={run.checkpointUri ?? undefined}>{run.checkpointUri ? shortUri(run.checkpointUri, 3) : 'not yet'}</dd>
            {run.modalCallId && (
              <>
                <dt>Modal call</dt>
                <dd>{run.modalCallId}</dd>
              </>
            )}
          </dl>
          {stub && <p className="gx-small mt-3 mb-0">No GPU ran and no Modal credit was spent; the checkpoint is a stand-in.</p>}
          {run.error && <p className="mt-3 mb-0 text-sm text-slag">{run.error}</p>}
        </section>
      </div>
      <section className="tdm116-plate tdm116-col" aria-label="Episodes in this run">
        <div className="tdm116-plate-head" style={{ marginBottom: 0 }}>
          <h3>Episodes in this run</h3>
          <span className="gx-id">{run.episodeIds.length}</span>
        </div>
        <ul className="tdm116-rows tdm116-scroll">
          {run.episodeIds.map((id, i) => {
            const e = eps[i];
            const who = e?.contributorId ? d.people.get(e.contributorId) : undefined;
            return (
              <li key={id.toString()}>
                <div className="tdm116-row-top">
                  <span className="gx-id text-ink">#{id.toString()}</span>
                  <span className="tdm116-row-main text-sm">
                    {e ? KIND_LABEL[e.kind] ?? e.kind : 'episode'}
                    {who ? ` · ${who.name}` : ''}
                  </span>
                  {e && d.simRobotIds.has(e.robotId) && <span className="gx-id">SIM</span>}
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------ Ship

function ShipStage({ d }: { d: Tdm116Data }) {
  const t = d.ticket!;
  const station = d.floor?.stations.find((s) => s.key === 'ship');
  const shipEv = [...d.events].reverse().find((e) => e.row.type === 'flywheel.shipped' || e.row.type === 'flywheel.ship_blocked');
  const c = d.candidate;
  const target = d.settings?.successThreshold ?? t.targetSuccess;
  const basis = c && c.status === 'done' ? (d.settings?.requireCiLow ? c.ciLow : c.trials ? c.successes / c.trials : 0) : undefined;
  const met = basis !== undefined ? basis + 1e-9 >= target : undefined;
  return (
    <div className="tdm116-col" data-testid="stage-ship">
      <section className="tdm116-plate is-tight flex flex-wrap items-center gap-x-8 gap-y-3" aria-label="Ship decision">
        <span className="flex items-center gap-2">
          {t.status === 'shipped' ? <Tag tone="shipped">Shipped</Tag> : <Tag>{station?.state === 'idle' || !station ? 'Not yet' : station.state}</Tag>}
        </span>
        <dl className="m-0 flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <div>
            <dt className="gx-label">Target</dt>
            <dd className="m-0 gx-data">
              {pct(target)} on {d.settings?.requireCiLow ? 'CI lower bound' : 'point estimate'}
            </dd>
          </div>
          <div>
            <dt className="gx-label">Fine-tuned</dt>
            <dd className="m-0 gx-data">{c ? (c.status === 'done' ? `${c.successes}/${c.trials}, ${pct(c.ciLow)}–${pct(c.ciHigh)}` : `${c.successes}/${c.completed} so far`) : 'not evaluated yet'}</dd>
          </div>
          <div>
            <dt className="gx-label">Verdict</dt>
            <dd className={gxCx('m-0 gx-data', met === undefined ? undefined : met ? 'text-arc' : 'text-slag')}>{met === undefined ? 'waits for a finished eval' : met ? 'Target met' : 'Target not met'}</dd>
          </div>
          <div className="min-w-0">
            <dt className="gx-label">Checkpoint</dt>
            <dd className="m-0 gx-data" title={t.deployedCheckpointUri ?? undefined}>{t.deployedCheckpointUri ? shortUri(t.deployedCheckpointUri, 3) : 'not deployed'}</dd>
          </div>
        </dl>
        <ButtonLink href={`/tickets/${t.id}/report`} small className="ml-auto">
          Open the report
        </ButtonLink>
        <p className="gx-small m-0 basis-full">
          {shipEv ? `${tdm112Words({ type: shipEv.row.type, payload: shipEv.payload })} · ${fmtTs(shipEv.row.ts)}. ` : ''}
          The flywheel ships on its own once a fine-tuned eval meets the target; otherwise it loops back to plan more data.
        </p>
      </section>
      <section className="tdm116-plate tdm116-scroll tdm116-grow" aria-label="Before and after">
        <div className="tdm116-plate-head">
          <h3>Before and after</h3>
          <span className="gx-id">Wilson 95% · same robot mode only</span>
        </div>
        <EvalCards runs={d.evalRuns} events={d.events} settings={d.settings} robotFake={d.robotFake} iterationOf={d.iterationOf} simRobotIds={d.simRobotIds} mode={d.mode} />
      </section>
    </div>
  );
}
