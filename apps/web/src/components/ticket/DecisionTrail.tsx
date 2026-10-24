'use client';

// TDM-68 evidence trail for the recovery-vs-clean decision.
//
// Renders GET /tickets/{id}/decision from the orchestrator (services/orchestrator/.../decision.py):
// real failed trials (episode clips) -> failure clusters -> the planner's verbatim reasoning ->
// the data bought -> the before/after eval. Every step cites its row ids and event_log row (id, ts,
// actor). Nothing here writes prose about the decision: model text is shown verbatim, everything
// else is row values with labels, and a missing step is shown as missing.
//
// Mount:  <DecisionTrail ticketId={id} refreshKey={events.length} />
// It re-fetches when refreshKey changes and every 15 s. Self-contained on purpose (own media
// signing via GET /media/sign) so it does not depend on other tickets' helpers.
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { LlmBadge } from '@/components/llm/LlmBadge';
import { ApprovePlanButton } from './ApprovePlanButton';

const ORCH = (process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');
const POLL_MS = 15_000;

// ---------------------------------------------------------------- response shape (decision.py)

type Ref = { event_id: number; ts: number; actor: string; type: string };
type Camera = { name: string; uri: string };
type Trial = { eval_run_id: number; trial_index: number; success: boolean; failure_note: string | null; duration_ms: number };
type EpisodeView = {
  episode_id: number; kind?: string; review_status?: string; success?: boolean | null; duration_ms?: number;
  storage_uri?: string; cameras?: Camera[]; task_id?: number; contributor_id?: number; fake?: boolean;
  missing?: boolean; trial?: Trial | null;
};
type EvalView = {
  eval_run_id: number; kind: string; status: string; checkpoint_uri: string; training_run_id: number;
  trials: number; completed: number; successes: number; ci_low: number | null; ci_high: number | null;
  created_at: number; finished_at: number | null; summary: string | null; finalized_event: Ref | null;
  linked_by?: string;
  trials_detail?: { trial_index: number; success: boolean; duration_ms: number; episode_id: number; failure_note: string | null }[];
};
type Cluster = {
  failure_cluster_id: number; label: string; count: number; description: string | null; phase: string | null;
  recoverable_mid_episode: boolean | null; recoverability_reason: string | null; trial_indices: number[] | null;
  in_diagnosis: boolean; event: Ref | null; examples: EpisodeView[];
};
type Plan = {
  data_plan_id: number; data_type: string; amount: number; unit_price_cents: number; est_cost_cents: number;
  est_cost_incl_resets_cents: number | null; reasoning: string; status: string; approved_cents: number;
  approved_by: string | null; created_at: number; failure_cluster_ids: number[]; event: Ref | null;
  event_reasoning_verbatim: boolean | null; fixture: string | null;
  guardrail: { rule: string | null; required_type: string | null; why: string } | null;
  guardrail_forced: string[] | null; guardrail_agreed_first_try: boolean | null;
  budget_math: { remaining_cents: number; needed: number } | null;
  model: { backend?: string | null; backend_model?: string | null; requested: string | null; served: string | null; effort: string | null; attempts: unknown[] | null } | null;
  data_type_override: string | null;
};
type Alternative = {
  data_type: string; unit_price_cents: number | null; cost_per_episode_incl_reset_cents: number | null;
  max_amount: number | null; affordable: number | null; ruled_out_by_rule: string | null; rule_why: string | null;
};
type Link = { check: string; ok: boolean | null; detail: string };
type Decision = {
  iteration: number; source_eval: EvalView | null;
  diagnosis: { event: Ref; model: string | null; backend?: string | null; reasoning: string | null; recoverable_failures: number | null; unrecoverable_failures: number | null } | null;
  clusters: Cluster[]; plan: Plan | null; alternative: Alternative | null;
  approvals: (Ref & { approved_by: string | null; approved_cents: number | null })[];
  tasks: { task_id: number; type: string; status: string; price_cents: number; assignee_id: number; event: Ref | null; episode_ids: number[] }[];
  data_bought: EpisodeView[];
  training_runs: { training_run_id: number; status: string; checkpoint_uri: string | null; episodes_total: number; episodes_from_this_plan: number; gpu_cost_cents: number }[];
  after_evals: EvalView[];
  autonomy: {
    chain_events: Ref[]; last_chain_ts: number | null; first_human_action: Ref | null; before_any_human_action: boolean | null;
    human_actions_in_decision_window: Ref[]; owner_token_actions_in_decision_window: Ref[]; override_in_force: string | null;
  };
  links: Link[]; missing: string[];
};
type Trail = { ticket: { id: number; status: string }; evals: EvalView[]; decisions: Decision[]; missing: string[]; generated_at: number };

// ---------------------------------------------------------------- formatting (values only)

const TYPE: Record<string, string> = { recovery_takeover: 'Recovery takeovers', clean_teleop: 'Clean teleop demos' };
const typeName = (t: string | null | undefined) => (t ? TYPE[t] ?? t.replace(/_/g, ' ') : '');
const usd = (c: number | null | undefined) => (c === null || c === undefined ? '' : `$${(c / 100).toFixed(2)}`);
const pct = (x: number) => `${Math.floor(x * 100 + 0.5)}%`;
const time = (us: number | null | undefined) =>
  us ? new Date(us / 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
function gap(a: number, b: number): string {
  const s = Math.round(Math.abs(b - a) / 1e6);
  return s < 1 ? 'under 1 s' : s < 60 ? `${s} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}
const rate = (e: EvalView) =>
  `${e.successes}/${e.status === 'done' ? e.trials : e.completed}` +
  (e.status === 'done' && e.ci_low !== null && e.ci_high !== null ? `, 95% CI ${pct(e.ci_low)}–${pct(e.ci_high)}` : ` (${e.status})`);

function Cite({ ev, label }: { ev: Ref | null | undefined; label?: string }) {
  if (!ev) return <span className="rounded bg-red-50 px-1 text-[11px] text-red-700 dark:bg-red-950/40 dark:text-red-300">no event_log row</span>;
  return (
    <span className="whitespace-nowrap rounded bg-zinc-100 px-1 font-mono text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
      title={`event_log #${ev.event_id} ${ev.type} by ${ev.actor} at ${time(ev.ts)}`}>
      {label ? `${label} · ` : ''}event #{ev.event_id} · {ev.actor} · {time(ev.ts)}
    </span>
  );
}

function Step({ n, title, children, aside }: { n: number; title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <li className="relative pl-9">
      <span className="absolute left-0 top-0 flex h-6 w-6 items-center justify-center rounded-full bg-zinc-900 text-xs font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">{n}</span>
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="font-semibold">{title}</h3>
        {aside}
      </div>
      <div className="pb-7">{children}</div>
    </li>
  );
}

function Missing({ children }: { children: ReactNode }) {
  return <p className="rounded border border-dashed border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">{children}</p>;
}

function Quote({ text, source }: { text: string; source: ReactNode }) {
  return (
    <figure className="max-w-3xl">
      <blockquote className="border-l-2 border-zinc-300 pl-4 font-serif text-[1.05rem] leading-relaxed text-zinc-800 dark:border-zinc-600 dark:text-zinc-200">
        <p className="whitespace-pre-wrap">{text}</p>
      </blockquote>
      <figcaption className="mt-1.5 text-xs text-zinc-500">{source}</figcaption>
    </figure>
  );
}

// ---------------------------------------------------------------- episode thumbnails

const signed = new Map<string, { url: string; exp: number }>();
async function signUri(uri: string): Promise<string> {
  if (/^https?:\/\//.test(uri)) return uri;
  const hit = signed.get(uri);
  if (hit && hit.exp * 1000 > Date.now() + 60_000) return hit.url;
  const r = await fetch(`${ORCH}/media/sign?uri=${encodeURIComponent(uri)}`);
  if (!r.ok) throw new Error(`media sign ${r.status}`);
  const j = (await r.json()) as { url: string; expires_at: number };
  signed.set(uri, { url: j.url, exp: j.expires_at });
  return j.url;
}

/** A frame near the end of the failed clip (where the failure state is); hover plays it. */
function EpisodeThumb({ ep }: { ep: EpisodeView }) {
  const cam = ep.cameras?.[0];
  const [url, setUrl] = useState<string>();
  const [err, setErr] = useState<string>();
  useEffect(() => {
    if (!cam || cam.uri.startsWith('file://') || cam.uri.startsWith('fake://')) return;
    let live = true;
    signUri(cam.uri).then((u) => live && setUrl(u), (e: Error) => live && setErr(e.message));
    return () => { live = false; };
  }, [cam]);
  const at = ep.duration_ms ? Math.max(0, ep.duration_ms / 1000 - 1.5) : 0;
  const note = !cam ? 'no camera recorded' : cam.uri.startsWith('file://') ? 'clip not uploaded' : cam.uri.startsWith('fake://') ? 'fake camera' : err ? `cannot sign clip (${err})` : 'loading clip';
  return (
    <figure className="w-36 shrink-0">
      <div className={`relative aspect-video overflow-hidden rounded bg-zinc-900 ${ep.fake ? 'outline outline-2 -outline-offset-2 outline-fuchsia-600' : ''}`}>
        {url ? (
          <video src={`${url}#t=${at.toFixed(1)}`} muted playsInline preload="metadata" className="h-full w-full object-cover"
            onMouseEnter={(e) => void e.currentTarget.play().catch(() => {})}
            onMouseLeave={(e) => { e.currentTarget.pause(); e.currentTarget.currentTime = at; }} />
        ) : (
          <div className="flex h-full items-center justify-center p-1 text-center text-[10px] text-zinc-400">{note}</div>
        )}
        {ep.fake && <span className="absolute left-1 top-1 rounded-sm bg-fuchsia-600 px-1 text-[10px] font-bold text-white">FAKE</span>}
      </div>
      <figcaption className="mt-1 text-[11px] leading-snug text-zinc-500">
        <a href={`#episode-${ep.episode_id}`} className="text-blue-600 hover:underline">episode #{ep.episode_id}</a>
        {ep.trial ? <> · trial {ep.trial.trial_index} of eval #{ep.trial.eval_run_id}</> : ep.missing ? ' · row missing' : ' · not an eval trial'}
        {ep.trial?.failure_note && <span className="block text-zinc-600 dark:text-zinc-400" title="eval_trial.failure_note">“{ep.trial.failure_note}”</span>}
      </figcaption>
    </figure>
  );
}

// ---------------------------------------------------------------- one iteration's chain

function TrialStrip({ ev }: { ev: EvalView }) {
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {(ev.trials_detail ?? []).map((t) => (
        <a key={t.trial_index} href={t.episode_id ? `#episode-${t.episode_id}` : undefined}
          title={`trial ${t.trial_index}: ${t.success ? 'success' : 'failure'}${t.failure_note ? ` — ${t.failure_note}` : ''}${t.episode_id ? ` (episode #${t.episode_id})` : ' (no episode recorded)'}`}
          className={`flex h-6 w-6 items-center justify-center rounded text-[11px] font-medium tabular-nums ${t.success ? 'bg-emerald-600 text-white' : 'bg-red-100 text-red-800 ring-1 ring-inset ring-red-300 dark:bg-red-950/50 dark:text-red-300'}`}>
          {t.trial_index}
        </a>
      ))}
    </div>
  );
}

function Chain({ d, ticketId }: { d: Decision; ticketId: number | string }) {
  const p = d.plan;
  const failures = d.clusters.reduce((n, c) => n + c.count, 0);
  const bad = d.links.filter((l) => l.ok === false);
  const a = d.autonomy;
  let step = 0;
  return (
    <div>
      <ol className="relative border-l border-zinc-200 pl-0 dark:border-zinc-800 [&>li]:-ml-3">
        <Step n={++step} title="Failed trials" aside={d.source_eval && <Cite ev={d.source_eval.finalized_event} label={`eval_run #${d.source_eval.eval_run_id}`} />}>
          {d.source_eval ? (
            <>
              <p className="text-sm tabular-nums text-zinc-700 dark:text-zinc-300">
                {d.source_eval.kind} eval of <span className="font-mono text-xs">{d.source_eval.checkpoint_uri}</span>: {rate(d.source_eval)} succeeded.
              </p>
              <TrialStrip ev={d.source_eval} />
            </>
          ) : <Missing>The eval run these failures came from is not linked.</Missing>}
        </Step>

        <Step n={++step} title="Failure clusters" aside={d.diagnosis && <><LlmBadge rec={{ backend: d.diagnosis.backend, model: d.diagnosis.model }} className="mr-2" /><Cite ev={d.diagnosis.event} label={d.diagnosis.model ?? 'diagnosis'} /></>}>
          {d.clusters.length === 0 ? <Missing>No failure clusters recorded.</Missing> : (
            <ul className="space-y-4">
              {d.clusters.map((c) => (
                <li key={c.failure_cluster_id}>
                  <p className="text-sm">
                    <span className="font-medium">“{c.label}”</span>{' '}
                    <span className="tabular-nums text-zinc-500">{c.count} of {failures} failures</span>
                    {c.phase && <span className="text-zinc-500"> · phase {c.phase}</span>}
                    {c.recoverable_mid_episode !== null && (
                      <span className="text-zinc-500" title={c.recoverability_reason ?? undefined}> · {c.recoverable_mid_episode ? 'recoverable mid-episode' : 'not recoverable mid-episode'}</span>
                    )}{' '}
                    <Cite ev={c.event} label={`failure_cluster #${c.failure_cluster_id}`} />
                  </p>
                  {c.description && <p className="mt-0.5 max-w-3xl text-sm text-zinc-600 dark:text-zinc-400">{c.description}</p>}
                  <div className="mt-2 flex gap-3 overflow-x-auto pb-1">
                    {c.examples.length ? c.examples.map((e) => <EpisodeThumb key={e.episode_id} ep={e} />) : <Missing>No example episodes on this cluster.</Missing>}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {d.diagnosis?.reasoning && (
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200">How the clustering model grouped them (verbatim)</summary>
              <div className="mt-2"><Quote text={d.diagnosis.reasoning} source={<>diagnose.clusters, <Cite ev={d.diagnosis.event} /></>} /></div>
            </details>
          )}
        </Step>

        <Step n={++step} title="Decision" aside={p && <Cite ev={p.event} label={`data_plan #${p.data_plan_id}`} />}>
          {!p ? <Missing>No planner output for iteration {d.iteration}. {d.missing.find((m) => m.includes('data_plan'))}</Missing> : (
            <>
              {p.fixture && <p className="mb-2 rounded bg-fuchsia-600 px-2 py-1 text-xs font-bold text-white">FIXTURE: {p.fixture}</p>}
              <p className="text-2xl font-semibold tracking-tight">{typeName(p.data_type)}</p>
              <p className="mt-1 text-sm tabular-nums text-zinc-700 dark:text-zinc-300">
                {p.amount} episode{p.amount === 1 ? '' : 's'} × {usd(p.unit_price_cents)} = {usd(p.est_cost_cents)}
                {p.est_cost_incl_resets_cents !== null && <> ({usd(p.est_cost_incl_resets_cents)} with one reset per episode)</>}
                {p.budget_math && <span className="text-zinc-500"> · {usd(p.budget_math.remaining_cents)} budget left, {p.budget_math.needed} episodes still needed</span>}
              </p>
              <div className="mt-4">
                <Quote text={p.reasoning} source={<>
                  Planner reasoning, verbatim from data_plan #{p.data_plan_id}
                  {' · '}<LlmBadge prefix="decided by" rec={p.model} />
                  {p.model?.served && <> · model {p.model.served}{p.model.effort ? ` (${p.model.effort} effort)` : ''}, {p.model.attempts?.length ?? 0} attempt{p.model.attempts?.length === 1 ? '' : 's'}</>}
                  {p.event_reasoning_verbatim === true && <> · matches event #{p.event?.event_id} byte for byte</>}
                  {p.event_reasoning_verbatim === false && <span className="text-red-600"> · DIFFERS from event #{p.event?.event_id}</span>}
                </>} />
              </div>
              {d.alternative && (
                <div className="mt-4 max-w-3xl rounded border border-zinc-200 px-3 py-2 text-sm dark:border-zinc-800">
                  <p><span className="text-zinc-500">Rejected:</span> <span className="font-medium">{typeName(d.alternative.data_type)}</span>
                    {d.alternative.cost_per_episode_incl_reset_cents !== null && <span className="tabular-nums text-zinc-600 dark:text-zinc-400"> · {usd(d.alternative.cost_per_episode_incl_reset_cents)} per episode with reset, at most {d.alternative.max_amount} within budget</span>}
                  </p>
                  {d.alternative.ruled_out_by_rule && (
                    <p className="mt-1 text-xs text-zinc-500">Guardrail {d.alternative.ruled_out_by_rule} (code check, logged in the plan's evidence): {d.alternative.rule_why}</p>
                  )}
                  {!d.alternative.ruled_out_by_rule && p.guardrail && <p className="mt-1 text-xs text-zinc-500">No guardrail rule fired; the model weighed the clusters (its reasons are in the paragraph above).</p>}
                </div>
              )}
              {!!p.guardrail_forced?.length && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">Guardrail overrode the model: {p.guardrail_forced.join('; ')}</p>}
              {p.data_type_override && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">A user data_type_override ({p.data_type_override}) was in force for this decision.</p>}
            </>
          )}
        </Step>

        <Step n={++step} title="Data bought">
          {!p ? <Missing>Nothing bought: there is no plan.</Missing> : (
            <div className="space-y-1 text-sm">
              {d.approvals.length ? d.approvals.map((ap) => (
                <p key={ap.event_id}>Spend approved: {usd(ap.approved_cents)} by {ap.approved_by} <Cite ev={ap} /></p>
              )) : (
                <>
                  <p className="text-zinc-500">Spend not approved yet (plan status {p.status}).</p>
                  <ApprovePlanButton ticketId={ticketId} planId={p.data_plan_id} estCostCents={p.est_cost_cents} status={p.status} />
                </>
              )}
              {d.tasks.length ? (
                <p>{d.tasks.length} paid task{d.tasks.length === 1 ? '' : 's'} dispatched ({d.tasks.map((t) => `#${t.task_id} ${t.status}`).join(', ')}).</p>
              ) : <p className="text-zinc-500">No tasks dispatched for data_plan #{p.data_plan_id} yet.</p>}
              {d.data_bought.length > 0 && (
                <p className="tabular-nums">
                  {d.data_bought.length} episode{d.data_bought.length === 1 ? '' : 's'} recorded:{' '}
                  {(['accepted', 'rejected', 'pending'] as const).map((s) => `${d.data_bought.filter((e) => e.review_status === s).length} ${s}`).join(', ')}
                  {' '}({d.data_bought.map((e) => <a key={e.episode_id} href={`#episode-${e.episode_id}`} className="mr-1 text-blue-600 hover:underline">#{e.episode_id}</a>)})
                </p>
              )}
            </div>
          )}
        </Step>

        <Step n={++step} title="Before and after">
          {d.after_evals.length === 0 ? (
            <p className="text-sm text-zinc-500">
              {d.training_runs.length ? `Training run #${d.training_runs[0].training_run_id} is ${d.training_runs[0].status}; no eval of it yet.` : 'No training run has used this data yet.'}
            </p>
          ) : (
            <table className="text-sm tabular-nums">
              <tbody>
                {d.source_eval && <tr><td className="pr-4 text-zinc-500">Before</td><td className="pr-4">eval #{d.source_eval.eval_run_id}</td><td>{rate(d.source_eval)}</td></tr>}
                {d.after_evals.map((e) => {
                  const tr = d.training_runs.find((t) => t.training_run_id === e.training_run_id);
                  return (
                    <tr key={e.eval_run_id}>
                      <td className="pr-4 text-zinc-500">After</td><td className="pr-4">eval #{e.eval_run_id}</td><td>{rate(e)}</td>
                      {tr && <td className="pl-4 text-xs text-zinc-500">training #{tr.training_run_id}: {tr.episodes_from_this_plan} of {tr.episodes_total} episodes from this plan</td>}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Step>
      </ol>

      <div className="mt-1 space-y-2 border-t border-zinc-200 pt-3 text-sm dark:border-zinc-800">
        {p?.event && a.before_any_human_action !== null && (
          <p className={a.before_any_human_action ? 'text-emerald-700 dark:text-emerald-400' : 'text-amber-700 dark:text-amber-300'}>
            {a.before_any_human_action
              ? a.first_human_action
                ? `The eval, clusters and plan were logged ${gap(a.last_chain_ts ?? 0, a.first_human_action.ts)} before the first human action on the ticket (${a.first_human_action.type} by ${a.first_human_action.actor}, event #${a.first_human_action.event_id}).`
                : 'The eval, clusters and plan were logged with no human action on the ticket since it was created.'
              : `A human action precedes part of this chain: ${a.first_human_action?.type} by ${a.first_human_action?.actor} (event #${a.first_human_action?.event_id}, ${time(a.first_human_action?.ts)}).`}
            {a.human_actions_in_decision_window.length > 0 && ` ${a.human_actions_in_decision_window.length} human action(s) fell between the eval and the plan.`}
            {a.owner_token_actions_in_decision_window.length > 0 && (
              <span className="block text-xs text-zinc-500">
                {a.owner_token_actions_in_decision_window.length} row(s) between the eval and the plan carry actor “owner”: the orchestrator and operator scripts share that token, so the log cannot tell them apart.
              </span>
            )}
          </p>
        )}
        <details>
          <summary className={`cursor-pointer ${bad.length ? 'text-red-600' : 'text-zinc-500'}`}>
            Link checks: {d.links.length - bad.length} of {d.links.length} pass{bad.length ? `, ${bad.length} broken` : ''}
          </summary>
          <ul className="mt-2 space-y-0.5 font-mono text-xs">
            {d.links.map((l, i) => (
              <li key={i} className={l.ok === false ? 'text-red-600' : 'text-zinc-500'}>{l.ok === false ? '✗' : '✓'} {l.check}: {l.detail}</li>
            ))}
          </ul>
        </details>
        {d.missing.length > 0 && (
          <ul className="list-disc pl-5 text-amber-800 dark:text-amber-300">{d.missing.map((m) => <li key={m}>{m}</li>)}</ul>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- the component

export function DecisionTrail({ ticketId, refreshKey }: { ticketId: string | number | bigint; refreshKey?: unknown }) {
  const [trail, setTrail] = useState<Trail>();
  const [error, setError] = useState<string>();
  const [pick, setPick] = useState<number>();
  const busy = useRef(false);

  const load = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    try {
      const r = await fetch(`${ORCH}/tickets/${ticketId}/decision`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
      setTrail((await r.json()) as Trail);
      setError(undefined);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      busy.current = false;
    }
  }, [ticketId]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(t);
  }, [load, refreshKey]);

  if (!trail) {
    return error
      ? <Missing>Cannot load the evidence trail from the orchestrator at {ORCH} ({error}). Nothing is shown in its place.</Missing>
      : <p className="text-sm text-zinc-500">Loading the evidence trail…</p>;
  }
  const decisions = trail.decisions;
  const shown = decisions.find((d) => d.iteration === pick) ?? decisions[decisions.length - 1];

  return (
    <div className="rounded-lg border border-zinc-300 bg-white px-5 py-5 dark:border-zinc-700 dark:bg-zinc-900">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
        <span className="text-zinc-500">From failed trials to the data bought, each step linked by row id.</span>
        {decisions.length > 1 && decisions.map((d) => (
          <button key={d.iteration} onClick={() => setPick(d.iteration)}
            className={`rounded px-2 py-0.5 text-xs ${d === shown ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : 'bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300'}`}>
            iteration {d.iteration}
          </button>
        ))}
        {error && <span className="text-xs text-amber-700">refresh failed ({error}); showing the {time(trail.generated_at)} copy</span>}
      </div>
      {shown ? <Chain d={shown} ticketId={String(ticketId)} /> : (
        <Missing>No decision yet. {trail.missing.join('; ')}.</Missing>
      )}
    </div>
  );
}
