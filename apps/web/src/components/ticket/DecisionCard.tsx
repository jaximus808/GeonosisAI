'use client';

import { fmtCents, fmtTs } from '@/lib/format';
import { FakeBadge, StatusPill } from './Badges';
import { CamVideo } from './media';
import { LlmBadge } from '@/components/llm/LlmBadge';
import {
  DATA_TYPE_LABEL,
  byNewest,
  cameraUris,
  isFakeEpisode,
  type DataPlan,
  type Episode,
  type FailureCluster,
  type Json,
  type TicketEvent,
} from './util';

export type ClusterNote = { description?: string; phase?: string; recoverable?: boolean; recoverabilityReason?: string };

/** diagnose.clusters events (orchestrator diagnose.py) carry the per-cluster text the reducer does not store. */
export function clusterNotes(events: TicketEvent[]): { notes: Map<number, ClusterNote>; reasoning?: string } {
  const notes = new Map<number, ClusterNote>();
  let reasoning: string | undefined;
  for (const { row, payload } of events) {
    if (row.type !== 'diagnose.clusters' || !Array.isArray(payload.clusters)) continue;
    if (typeof payload.reasoning === 'string' && payload.reasoning) reasoning = payload.reasoning;
    for (const c of payload.clusters as Json[]) {
      const id = Number(c.failure_cluster_id);
      if (!id) continue;
      notes.set(id, {
        description: typeof c.description === 'string' ? c.description : undefined,
        phase: typeof c.phase === 'string' ? c.phase : undefined,
        recoverable: typeof c.recoverable_mid_episode === 'boolean' ? c.recoverable_mid_episode : undefined,
        recoverabilityReason: typeof c.recoverability_reason === 'string' ? c.recoverability_reason.replace(/\.\s*$/, '') : undefined,
      });
    }
  }
  return { notes, reasoning };
}

function ClusterItem({ c, note, episodes, total }: { c: FailureCluster; note?: ClusterNote; episodes: Map<bigint, Episode>; total: number }) {
  const examples = c.exampleEpisodeIds.map((id) => ({ id, ep: episodes.get(id) }));
  const thumb = examples.find((e) => e.ep && cameraUris(e.ep).length > 0)?.ep;
  const cam = thumb ? cameraUris(thumb)[0] : undefined;
  return (
    <li className="grid grid-cols-[7rem_minmax(0,1fr)] gap-4 py-3 sm:grid-cols-[9rem_minmax(0,1fr)]">
      <div>
        {cam && thumb ? (
          <a href={`#episode-${thumb.id}`} title={`Episode #${thumb.id}, ${cam.name} camera (hover to play)`}>
            <CamVideo uri={cam.uri} name={cam.name} fake={isFakeEpisode(thumb)} compact />
          </a>
        ) : (
          <div className="flex aspect-video items-center justify-center rounded bg-zinc-100 text-[11px] text-zinc-400 dark:bg-zinc-900">no clip</div>
        )}
      </div>
      <div className="min-w-0">
        <p className="font-medium">
          {c.label}{' '}
          <span className="font-normal tabular-nums text-zinc-500">
            {c.count} of {total} failure{total === 1 ? '' : 's'}
          </span>
        </p>
        {(note?.description || c.description) && <p className="mt-0.5 text-sm text-zinc-700 dark:text-zinc-300">{note?.description || c.description}</p>}
        <p className="mt-1 text-xs text-zinc-500">
          {note?.phase && <>Phase: {note.phase}. </>}
          {note?.recoverable !== undefined && (
            <span title={note.recoverabilityReason}>{note.recoverable ? 'Recoverable mid-episode' : 'Not recoverable mid-episode'}{note.recoverabilityReason ? `: ${note.recoverabilityReason}` : ''}. </span>
          )}
          {examples.length > 0 && (
            <>
              Examples:{' '}
              {examples.map((e, i) => (
                <span key={e.id.toString()}>
                  {i > 0 && ', '}
                  <a href={`#episode-${e.id}`} className="text-blue-600 hover:underline">#{e.id.toString()}</a>
                  {e.ep && isFakeEpisode(e.ep) && <span className="ml-1"><FakeBadge /></span>}
                </span>
              ))}
            </>
          )}
        </p>
      </div>
    </li>
  );
}

/**
 * The planner's data-type decision (the demo's key moment): which data it is buying, how much,
 * the failure clusters it targets, and its logged reasoning verbatim.
 */
export function DecisionCard({ plans, clusters, episodes, events }: {
  plans: readonly DataPlan[]; clusters: readonly FailureCluster[]; episodes: Map<bigint, Episode>; events: TicketEvent[];
}) {
  const sorted = byNewest(plans);
  const plan = sorted[0];
  const { notes, reasoning: diagnoseReasoning } = clusterNotes(events);
  const planEvent = plan
    ? [...events].reverse().find((e) => e.row.type === 'planner.data_plan' && Number(e.payload.data_plan_id) === Number(plan.id))
    : undefined;
  const evidence = planEvent?.payload.evidence && typeof planEvent.payload.evidence === 'object' ? (planEvent.payload.evidence as Json) : undefined;
  const alt = evidence?.alternative && typeof evidence.alternative === 'object' ? (evidence.alternative as Json) : undefined;

  const iteration = plan?.iteration ?? Math.max(-1, ...clusters.map((c) => c.iteration));
  const shown = plan && plan.failureClusterIds.length > 0
    ? clusters.filter((c) => plan.failureClusterIds.includes(c.id))
    : clusters.filter((c) => c.iteration === iteration);
  const totalFailures = shown.reduce((n, c) => n + c.count, 0);

  if (!plan && clusters.length === 0) {
    return <p className="text-sm text-zinc-500">No diagnosis yet. After the baseline, failures are grouped into types and the planner picks which data to buy.</p>;
  }

  return (
    <div className="relative overflow-hidden rounded-lg border border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900">
      <div className="absolute inset-y-0 left-0 w-1.5 bg-yellow-400" aria-hidden />
      <div className="px-5 py-5 pl-7 sm:px-8 sm:pl-10">
        {plan ? (
          <>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
              <p className="text-sm text-zinc-500">The planner chose to buy</p>
              <StatusPill status={plan.status} />
              <span className="text-xs text-zinc-500">iteration {plan.iteration}</span>
            </div>
            <p className="mt-1 text-3xl font-semibold tracking-tight sm:text-4xl">{DATA_TYPE_LABEL[plan.dataType] ?? plan.dataType}</p>
            <p className="mt-2 text-base tabular-nums text-zinc-700 dark:text-zinc-300">
              {plan.amount} episode{plan.amount === 1 ? '' : 's'} at {fmtCents(plan.unitPriceCents)} each, {fmtCents(plan.estCostCents)} estimated
              {plan.approvedCents > 0n && (
                <span className="text-zinc-500">
                  ; {fmtCents(plan.approvedCents)} approved{plan.approvedBy ? ` by ${plan.approvedBy}` : ''}
                  {plan.approvedAt ? ` at ${fmtTs(plan.approvedAt)}` : ''}
                </span>
              )}
            </p>

            <blockquote className="mt-5 max-w-3xl border-l-2 border-zinc-300 pl-4 font-serif text-[1.075rem] leading-relaxed text-zinc-800 dark:border-zinc-600 dark:text-zinc-200">
              <p className="whitespace-pre-wrap">{plan.reasoning}</p>
            </blockquote>
            <p className="mt-2 text-xs text-zinc-500">
              Planner reasoning, verbatim from data_plan #{plan.id.toString()}, logged {fmtTs(plan.createdAt)}
              {planEvent && <> (event #{planEvent.row.id.toString()}, actor {planEvent.row.actor})</>}.{' '}
              <LlmBadge prefix="decided by" rec={evidence?.model && typeof evidence.model === 'object' ? (evidence.model as Json) : null} />
            </p>
            {alt && (
              <p className="mt-3 max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">
                Considered instead: {DATA_TYPE_LABEL[String(alt.data_type)] ?? String(alt.data_type ?? '')}
                {typeof alt.amount === 'number' ? `, ${alt.amount} episodes` : ''}
                {typeof alt.est_cost_cents === 'number' ? ` for ${fmtCents(alt.est_cost_cents)}` : ''}
                {typeof alt.why_not === 'string' ? `. Not chosen: ${alt.why_not}.` : '.'}
              </p>
            )}
          </>
        ) : (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">Failures are diagnosed. Waiting for the planner to choose a data type.</p>
        )}

        {shown.length > 0 && (
          <div className="mt-6">
            <h3 className="text-sm font-semibold">{plan ? 'Failures this data targets' : 'Diagnosed failures'}</h3>
            <ul className="mt-1 divide-y divide-zinc-200 dark:divide-zinc-800">
              {shown.map((c) => (
                <ClusterItem key={c.id.toString()} c={c} note={notes.get(Number(c.id))} episodes={episodes} total={totalFailures} />
              ))}
            </ul>
          </div>
        )}

        {(evidence || diagnoseReasoning) && (
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">Logged evidence</summary>
            {diagnoseReasoning && <p className="mt-2 max-w-3xl whitespace-pre-wrap text-zinc-700 dark:text-zinc-300">Diagnosis: {diagnoseReasoning}</p>}
            {evidence && (
              <pre className="mt-2 max-h-72 overflow-auto rounded bg-zinc-50 p-3 text-xs dark:bg-zinc-950">{JSON.stringify(evidence, null, 2)}</pre>
            )}
          </details>
        )}

        {sorted.length > 1 && (
          <div className="mt-5 border-t border-zinc-200 pt-3 text-sm dark:border-zinc-800">
            <h3 className="font-semibold">Earlier plans</h3>
            <ul className="mt-1 space-y-1 text-zinc-600 dark:text-zinc-400">
              {sorted.slice(1).map((p) => (
                <li key={p.id.toString()}>
                  Iteration {p.iteration}: {DATA_TYPE_LABEL[p.dataType] ?? p.dataType}, {p.amount} for {fmtCents(p.estCostCents)} ({p.status}).{' '}
                  <span className="text-zinc-500" title={p.reasoning}>{p.reasoning.length > 140 ? `${p.reasoning.slice(0, 140)}…` : p.reasoning}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
