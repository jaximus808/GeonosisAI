// TDM-79: the deployment report, for the customer and the judges. Read-only and public: it renders
// the orchestrator's GET /tickets/{id}/report (TDM-71, rdf-report-v1) on the server, so the page
// prints and shares without a SpacetimeDB connection or any token. Every number and derived string
// is the report's own; anything missing is listed as missing, fake inputs get a loud banner, and
// money is labelled Stripe TEST MODE.

import type { Metadata } from 'next';
import { Newsreader, Public_Sans } from 'next/font/google';
import type { ReactNode } from 'react';
import {
  DATA_TYPE_LABEL,
  clock,
  pct,
  timelineText,
  usd,
  utc,
  type EvalView,
  type IterationBlock,
  type Report,
} from '@/lib/tdm79/report';
import { llmBadge } from '@/lib/llmbackend/types';
import { ReportToolbar } from './ReportToolbar';
import './report.css';

const serif = Newsreader({ subsets: ['latin'], variable: '--r-serif', display: 'swap' });
const sans = Public_Sans({ subsets: ['latin'], variable: '--r-sans', display: 'swap' });

const ORCH = (process.env.RDF_ORCHESTRATOR_URL || process.env.NEXT_PUBLIC_ORCHESTRATOR_URL || 'http://localhost:8000').replace(/\/$/, '');

export const dynamic = 'force-dynamic';

type Loaded = { ok: true; report: Report } | { ok: false; status: number; message: string };

// TDM-107: the report's robot_mode block (absent in reports saved before it). Sim and real results
// are listed apart and never pooled; the hero numbers are the ticket's current mode only.
type Tdm107ModeBlock = {
  current: 'sim' | 'real' | null;
  include_sim_data: boolean;
  by_mode: Record<'sim' | 'real', {
    robot_ids: number[];
    eval_runs: EvalView[];
    baseline: EvalView | null;
    latest_candidate: EvalView | null;
    trials: number;
    successes: number;
    episodes: number;
    episodes_accepted: number;
  }>;
  switches: { event_id: number; from_mode?: string; to_mode?: string; from_robot_id?: number; to_robot_id?: number; status?: string; by?: string; reason?: string }[];
};

function tdm107Mode(r: Report): Tdm107ModeBlock | null {
  return ((r as unknown as { robot_mode?: Tdm107ModeBlock }).robot_mode ?? null) || null;
}

async function loadReport(id: string): Promise<Loaded> {
  try {
    const res = await fetch(`${ORCH}/tickets/${id}/report`, { cache: 'no-store', signal: AbortSignal.timeout(30_000) });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
      return { ok: false, status: res.status, message: typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}` };
    }
    return { ok: true, report: (await res.json()) as Report };
  } catch (e) {
    return { ok: false, status: 502, message: `The orchestrator at ${ORCH} did not answer (${(e as Error).message}).` };
  }
}

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const { id } = await params;
  return { title: `Deployment report, ticket ${id}` };
}

export default async function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const shell = (body: ReactNode) => <div className={`tdm79-report ${serif.variable} ${sans.variable}`}>{body}</div>;

  if (!/^\d{1,18}$/.test(id)) {
    return shell(<p className="r-sheet r-state">“{id}” is not a ticket number.</p>);
  }
  const loaded = await loadReport(id);
  if (!loaded.ok) {
    return shell(
      <>
        <ReportToolbar ticketId={id} />
        <div className="r-sheet r-state">
          <h1 className="r-h1">{loaded.status === 404 ? `There is no ticket ${id}` : 'The report could not be built'}</h1>
          <p>{loaded.message}</p>
          {loaded.status !== 404 && (
            <p className="r-muted">
              Start the orchestrator (<code>uv run rdf-orchestrator</code>) and set RDF_ORCHESTRATOR_URL for the web app, then refresh.
            </p>
          )}
        </div>
      </>,
    );
  }

  const r = loaded.report;
  const o = r.outcome;
  const threshold = o.success_threshold ?? o.target_success;
  const shipped = o.status === 'shipped';

  return shell(
    <>
      <ReportToolbar ticketId={id} />

      {r.fake.any && (
        <div className="r-fake" role="alert">
          <p className="r-fake-title">Fake inputs. These are not robot results.</p>
          <p>{r.fake.banner}</p>
        </div>
      )}

      <article className="r-sheet">
        <header className="r-head">
          <p className="r-kicker">
            Deployment report for ticket {r.ticket_id}
            <span className="r-kicker-right">
              Generated {utc(r.generated_at)}, {r.report_version}
            </span>
          </p>
          <h1 className="r-h1">{r.task.description || <span className="r-missing-inline">No task description</span>}</h1>
          <dl className="r-facts">
            <Fact label="Robot">
              {r.robot ? (
                <>
                  {r.robot.name} <span className="r-muted">({r.robot.embodiment}, adapter {r.robot.adapter_version})</span>
                  {r.robot.fake && <FakeTag />}
                  {tdm107Mode(r)?.current === 'sim' && <span className="r-sim-tag">SIM ROBOT</span>}
                </>
              ) : (
                <span className="r-missing-inline">not recorded</span>
              )}
            </Fact>
            <Fact label="Status">
              <span className={shipped ? 'r-pass' : o.status === 'failed' ? 'r-fail' : ''}>{o.status.replace(/_/g, ' ')}</span>
              <span className="r-muted">
                {' '}after {r.iterations.length} data iteration{r.iterations.length === 1 ? '' : 's'}
              </span>
            </Fact>
            <Fact label="Target">
              {pct(threshold)} task success{' '}
              <span className="r-muted">on {o.require_ci_low ? 'the 95% CI lower bound' : 'the measured rate'}</span>
            </Fact>
            {r.task.policy_prompt ? <Fact label="Policy prompt">“{r.task.policy_prompt}”</Fact> : null}
          </dl>
        </header>

        {r.mocked.length > 0 && (
          <ul className="r-mocked">
            {r.mocked.map((m) => (
              <li key={m.component}>
                <strong>Mocked {m.component}:</strong> {m.detail}
              </li>
            ))}
          </ul>
        )}

        <section className="r-hero" aria-label="Success before and after">
          {tdm107Mode(r)?.current && (
            <p className="r-muted">
              {tdm107Mode(r)!.current === 'sim'
                ? 'SIMULATED: sim-robot results (MuJoCo), not real-arm success numbers.'
                : 'Real-robot results only. Sim-robot results are listed separately below.'}
            </p>
          )}
          <div className="r-hero-nums">
            <EvalNumber label="Before" sub="base policy" v={r.baseline} />
            <svg className="r-hero-arrow" viewBox="0 0 48 24" aria-hidden>
              <path d="M2 12h40M34 4l9 8-9 8" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <EvalNumber label="After" sub={r.final?.is_deployed_checkpoint ? 'deployed checkpoint' : 'latest trained checkpoint'} v={r.final} after />
          </div>
          <IntervalChart before={r.baseline} after={r.final} threshold={threshold} />
          <p className={`r-verdict ${o.met_target === true ? 'r-pass' : o.met_target === false ? 'r-fail' : ''}`}>{o.verdict}.</p>
        </section>

        {tdm107Mode(r) && <Tdm107SimVsReal m={tdm107Mode(r)!} />}

        <Section title="What data was bought, and why">
          {r.iterations.length === 0 ? (
            <Missing>No data iteration has run yet.</Missing>
          ) : (
            r.iterations.map((it) => <Iteration key={it.iteration} it={it} />)
          )}
        </Section>

        <div className="r-two">
          <Section title="Cost" aside={<TestModeTag />}>
            <table className="r-table">
              <tbody>
                <tr>
                  <th scope="row">Data payouts</th>
                  <td>{usd(r.total_cost.data.committed_cents)}</td>
                </tr>
                <tr className="r-sub">
                  <th scope="row">paid / in flight</th>
                  <td>
                    {usd(r.total_cost.data.paid_cents)} / {usd(r.total_cost.data.pending_cents)}
                  </td>
                </tr>
                <tr>
                  <th scope="row">GPU training</th>
                  <td>
                    {usd(r.total_cost.gpu.cents)}{' '}
                    <span className="r-muted">
                      ({r.total_cost.gpu.runs} run{r.total_cost.gpu.runs === 1 ? '' : 's'})
                    </span>
                  </td>
                </tr>
                <tr className="r-total">
                  <th scope="row">Total</th>
                  <td>{usd(r.total_cost.total_cents)}</td>
                </tr>
                <tr className="r-sub">
                  <th scope="row">Budget / approved</th>
                  <td>
                    {usd(r.total_cost.budget_cents)} / {usd(r.total_cost.approved_cents)}
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="r-note">{r.total_cost.note}. All payouts are Stripe test mode; no real money moved.</p>
          </Section>

          <Section title="Deployed checkpoint">
            {r.deployed_checkpoint ? (
              <>
                <p className="r-uri">
                  {r.deployed_checkpoint.uri ?? <span className="r-missing-inline">no URI</span>}
                  {r.deployed_checkpoint.fake && <FakeTag />}
                </p>
                <p className="r-small">
                  Training run {r.deployed_checkpoint.training_run_id ?? 'n/a'}, evaluated by eval run{' '}
                  {r.deployed_checkpoint.eval_run_id ?? 'n/a'}.
                </p>
                <p className="r-note">Source: {r.deployed_checkpoint.source}.{r.deployed_checkpoint.note ? ` ${r.deployed_checkpoint.note}.` : ''}</p>
              </>
            ) : (
              <Missing>Nothing deployed yet.</Missing>
            )}
          </Section>
        </div>

        <Section title="Timeline">
          {r.timeline.length === 0 ? (
            <Missing>No ticket events recorded.</Missing>
          ) : (
            <ol className="r-timeline">
              {r.timeline.map((t) => (
                <li key={t.event_id}>
                  <time dateTime={t.at ?? undefined}>{clock(t.ts)}</time>
                  <span className="r-tl-what">{timelineText(t)}</span>
                  <span className="r-tl-ref">
                    #{t.event_id} {t.actor}
                  </span>
                </li>
              ))}
            </ol>
          )}
          {r.timeline.length > 0 && <p className="r-note">Times are UTC on {utc(r.timeline[0].ts).slice(0, 10)}.</p>}
        </Section>

        <div className="r-two">
          <Section title="Event-log seal">
            <p className="r-small">
              {r.event_log.algorithm} over all {r.event_log.count} event_log rows (#{r.event_log.first_id ?? '?'} to #
              {r.event_log.last_id ?? '?'}
              {r.event_log.gaps.length ? `, ${r.event_log.gaps.length} gap(s)` : ', no gaps'}). This ticket&apos;s span:{' '}
              {r.event_log.ticket_span.count} rows, #{r.event_log.ticket_span.first_event_id ?? '?'} to #
              {r.event_log.ticket_span.last_event_id ?? '?'}.
            </p>
            <p className="r-hash">{r.event_log_hash}</p>
            <p className="r-note">The same hash is shown on the event-log page; any edited or deleted row changes it.</p>
          </Section>

          <Section title="Missing from the record">
            {r.missing.length === 0 ? (
              <p className="r-small">Nothing. Every step above links to the rows it came from.</p>
            ) : (
              <ul className="r-missing">
                {r.missing.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            )}
          </Section>
        </div>

        <footer className="r-foot">
          {r.report_version}, built only from SpacetimeDB rows by the orchestrator. Ticket created {utc(r.task.created_at)}.
        </footer>
      </article>
    </>,
  );
}

// ---------------------------------------------------------------- pieces

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="r-section">
      <h2 className="r-h2">
        {title}
        {aside}
      </h2>
      {children}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Tdm107SimVsReal({ m }: { m: Tdm107ModeBlock }) {
  const line = (v: EvalView | null) => (v ? (v.ci_text ?? `${v.status}, ${v.completed}/${v.trials} trials`) : 'not run');
  return (
    <Section title="Sim vs real robot">
      <table className="r-table">
        <thead>
          <tr>
            <th scope="col">Mode</th>
            <th scope="col">Finalized trials</th>
            <th scope="col">Baseline</th>
            <th scope="col">Latest fine-tune</th>
            <th scope="col">Episodes (accepted)</th>
          </tr>
        </thead>
        <tbody>
          {(['real', 'sim'] as const).map((k) => {
            const b = m.by_mode[k];
            return (
              <tr key={k}>
                <th scope="row">{k === 'sim' ? <span className="r-sim-tag">SIM</span> : 'Real'}{m.current === k ? ' (current)' : ''}</th>
                <td>{b.successes}/{b.trials}</td>
                <td>{line(b.baseline)}</td>
                <td>{line(b.latest_candidate)}</td>
                <td>{b.episodes} ({b.episodes_accepted})</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="r-muted">
        Sim and real results are never pooled. Training {m.include_sim_data ? 'includes' : 'excludes'} sim-robot episodes.
      </p>
      {m.switches.length > 0 && (
        <ul className="r-muted">
          {m.switches.map((s) => (
            <li key={s.event_id}>
              Switched {s.from_mode} robot {s.from_robot_id} to {s.to_mode} robot {s.to_robot_id} during {s.status} by {s.by}
              {s.reason ? `: ${s.reason}` : ''} (event {s.event_id})
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function FakeTag() {
  return <span className="r-fake-tag">FAKE</span>;
}

function TestModeTag() {
  return <span className="r-test-tag">Stripe test mode</span>;
}

function Missing({ children }: { children: ReactNode }) {
  return <p className="r-missing-block">{children}</p>;
}

function EvalNumber({ label, sub, v, after }: { label: string; sub: string; v: EvalView | null; after?: boolean }) {
  return (
    <div className={`r-num ${after ? 'r-num-after' : 'r-num-before'}`}>
      <p className="r-num-label">
        {label} <span className="r-muted">{sub}</span>
      </p>
      {v && v.ci_text ? (
        <>
          <p className="r-num-big">
            {v.successes}
            <span className="r-num-of">/{v.trials}</span>
          </p>
          <p className="r-num-ci">
            {pct(v.rate)} success, 95% CI {pct(v.ci?.[0])} to {pct(v.ci?.[1])}
          </p>
          <TrialStrip v={v} />
          <p className="r-note">
            Eval run {v.eval_run_id}
            {v.fake_trials ? `, ${v.fake_trials} fake trial${v.fake_trials === 1 ? '' : 's'}` : ''}
          </p>
        </>
      ) : (
        <p className="r-num-none">
          {v ? `Eval run ${v.eval_run_id} is ${v.status} (${v.completed}/${v.trials} trials)` : 'Not run yet'}
        </p>
      )}
    </div>
  );
}

function TrialStrip({ v }: { v: EvalView }) {
  if (!v.trial_outcomes.length) return null;
  return (
    <p className="r-trials" aria-label={`${v.successes} of ${v.trials} trials succeeded`}>
      {v.trial_outcomes.map((t) => (
        <span
          key={t.trial_index}
          className={t.success ? 'r-trial r-trial-ok' : 'r-trial'}
          title={`Trial ${t.trial_index + 1}: ${t.success ? 'success' : 'failure'}${t.failure_note ? `, ${t.failure_note}` : ''}`}
        />
      ))}
    </p>
  );
}

/** Before and after on one 0-100% axis: the Wilson 95% interval as a bar, the rate as a dot, the ship line dashed. */
function IntervalChart({ before, after, threshold }: { before: EvalView | null; after: EvalView | null; threshold: number | null }) {
  const W = 640;
  const L = 64;
  const R = 16;
  const x = (f: number) => L + Math.max(0, Math.min(1, f)) * (W - L - R);
  const rows = [
    { key: 'before', label: 'Before', v: before, y: 34 },
    { key: 'after', label: 'After', v: after, y: 74 },
  ];
  return (
    <svg className="r-chart" viewBox={`0 0 ${W} 120`} role="img" aria-label="95% confidence intervals of task success before and after training">
      {[0, 0.25, 0.5, 0.75, 1].map((t) => (
        <g key={t}>
          <line x1={x(t)} x2={x(t)} y1={16} y2={92} className="r-chart-grid" />
          <text x={x(t)} y={110} textAnchor="middle" className="r-chart-tick">
            {Math.round(t * 100)}%
          </text>
        </g>
      ))}
      {threshold !== null && (
        <g>
          <line x1={x(threshold)} x2={x(threshold)} y1={8} y2={96} className="r-chart-threshold" />
          <text x={x(threshold) - 6} y={12} textAnchor="end" className="r-chart-threshold-label">
            ship at {Math.round(threshold * 100)}%
          </text>
        </g>
      )}
      {rows.map(({ key, label, v, y }) => (
        <g key={key} className={`r-chart-row r-chart-${key}`}>
          <text x={0} y={y + 4} className="r-chart-label">
            {label}
          </text>
          {v && v.ci && v.ci[0] !== null && v.ci[1] !== null && v.rate !== null ? (
            <>
              <line x1={x(v.ci[0])} x2={x(v.ci[1])} y1={y} y2={y} className="r-chart-ci" />
              <circle cx={x(v.rate)} cy={y} r={7} className="r-chart-dot" />
            </>
          ) : (
            <text x={L} y={y + 4} className="r-chart-tick">
              no finalized eval
            </text>
          )}
        </g>
      ))}
    </svg>
  );
}

function Iteration({ it }: { it: IterationBlock }) {
  const p = it.data_plan;
  const t = it.training_run;
  const hp = t?.hyperparams ?? null;
  return (
    <div className="r-iter">
      <h3 className="r-h3">Iteration {it.iteration}</h3>

      <div className="r-iter-grid">
        <div>
          <p className="r-step">Failures seen</p>
          {it.diagnosis.clusters.length ? (
            <ul className="r-clusters">
              {it.diagnosis.clusters.map((c) => (
                <li key={c.failure_cluster_id}>
                  <span className="r-count">{c.count}</span> {c.label}
                </li>
              ))}
            </ul>
          ) : (
            <Missing>No failure clusters recorded.</Missing>
          )}
          {it.diagnosis.source_eval_run_id ? <p className="r-note">From eval run {it.diagnosis.source_eval_run_id}.</p> : null}
        </div>

        <div>
          <p className="r-step">Bought</p>
          {p ? (
            <>
              <p className="r-bought">
                {DATA_TYPE_LABEL[p.data_type] ?? p.data_type} × {p.amount}
                {p.fake && <FakeTag />}
              </p>
              <p className="r-small">
                {usd(p.unit_price_cents)} each, est. {usd(p.est_cost_cents)}. {it.episodes_bought} episode
                {it.episodes_bought === 1 ? '' : 's'} came in: {it.episodes_accepted} accepted, {it.episodes_rejected.length} rejected.
              </p>
              <p className="r-note">
                Plan {p.data_plan_id}, {p.status}
                {p.approved_by ? `, approved ${usd(p.approved_cents)} by ${p.approved_by}` : ', no spend approval on record'}.
                {p.guardrail?.rule ? ` Guardrail: ${p.guardrail.rule}${p.guardrail.why ? ` (${p.guardrail.why})` : ''}.` : ''}
              </p>
            </>
          ) : (
            <Missing>No data plan recorded.</Missing>
          )}
        </div>
      </div>

      {p && (
        <figure className="r-quote">
          <blockquote>{p.reasoning ? p.reasoning : <span className="r-missing-inline">The planner left no reasoning.</span>}</blockquote>
          <figcaption>
            The planner&apos;s reasoning, verbatim
            {p.planner_event ? `, event #${p.planner_event.event_id}` : ''}
            {p.fake ? '. A scripted stand-in, not a planner decision.' : '.'}{' '}
            <LlmLine rec={p.model} />
          </figcaption>
        </figure>
      )}

      {it.diagnosis.reasoning && (
        <details className="r-details">
          <summary>How the failures were clustered <LlmLine rec={{ backend: it.diagnosis.backend, model: it.diagnosis.model }} /></summary>
          <p className="r-small r-pre">{it.diagnosis.reasoning}</p>
        </details>
      )}

      {it.episodes_rejected.length > 0 && (
        <ul className="r-rejected">
          {it.episodes_rejected.map((e) => (
            <li key={e.episode_id}>
              Episode {e.episode_id} rejected{e.reviewer ? ` by ${e.reviewer}` : ''}: {e.reason ?? 'no reason given'}
            </li>
          ))}
        </ul>
      )}

      <p className="r-small r-train">
        <span className="r-step-inline">Trained</span>{' '}
        {t ? (
          <>
            run {t.training_run_id} ({t.status}) on {t.episodes} episode{t.episodes === 1 ? '' : 's'}
            {t.duration_s !== null ? ` in ${t.duration_s}s` : ''}, GPU {usd(t.gpu_cost_cents)}
            {hp ? `; lr ${String(hp.lr)}, ${String(hp.steps)} steps, batch ${String(hp.batch_size)}, LoRA rank ${String(hp.lora_rank)}` : ''}
            {t.fake && <FakeTag />}
          </>
        ) : (
          <span className="r-missing-inline">no training run</span>
        )}
        {it.eval?.ci_text ? (
          <>
            {'. '}
            <span className="r-step-inline">Evaluated</span> {it.eval.ci_text}.
          </>
        ) : null}
        {t ? ' ' : '. '}
        <span className="r-muted">Iteration cost {usd(it.cost_cents.total)}.</span>
      </p>
    </div>
  );
}

/** Which LLM backend and model produced a decision (never assumed to be the API when unrecorded). */
function LlmLine({ rec }: { rec: Parameters<typeof llmBadge>[0] }) {
  const b = llmBadge(rec);
  return (
    <span className={`r-llm r-llm-${b.tone}`} data-llm-backend={b.backend ?? 'unknown'}>
      Produced by {b.text}
    </span>
  );
}
