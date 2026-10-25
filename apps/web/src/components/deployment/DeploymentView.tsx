'use client';

// A shipped ticket, as the finished unit coming off the line: a spec sheet in the landing hero's title-block
// language (mono labels, ruled cells), the proof (before -> after, Wilson 95%, data bought, cost), the policy
// (checkpoint, endpoint, where it runs, how to call it) and "Run my policy": one watched rollout of the
// deployed checkpoint on the ticket's sim robot (orchestrator POST /tickets/{id}/deployment/run), with the
// live sim view inline. Every value is a live SpacetimeDB row or event_log row (the cockpit's own hook).

import '@/components/cockpit/cockpit.css';
import Link from 'next/link';
import { useMemo, useState } from 'react';
import { Button, ButtonLink } from '@/components/brand/Button';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { Tdm116RobotWell } from '@/components/cockpit/RobotWell';
import { useTdm116Cockpit } from '@/components/cockpit/useCockpitData';
import { isStubCheckpoint, parseObj } from '@/components/ticket/util';
import { useRows } from '@/lib/spacetime/hooks';
import { fmtCents } from '@/lib/format';

const pct = (x: number) => `${Math.round(x * 100)}%`;
const SIM_POLICY_URL = 'http://127.0.0.1:8765';

type Run = { status: string; trials: number; successes: number; ciLow: number; ciHigh: number } | undefined;

function Cell({ label, children, className, mono = true }: { label: string; children: React.ReactNode; className?: string; mono?: boolean }) {
  return (
    <div className={gxCx('min-w-0 border-line px-3 py-2', className)}>
      <div className="font-mono text-[10px] uppercase leading-4 tracking-[0.1em] text-ink-muted">{label}</div>
      <div className={gxCx('mt-0.5 min-w-0 break-words text-ink', mono ? 'gx-data' : 'text-sm')}>{children}</div>
    </div>
  );
}

function RunFig({ run, label, tone }: { run: Run; label: string; tone?: 'after' }) {
  if (!run || run.status !== 'done' || !run.trials) {
    return (
      <div>
        <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-muted">{label}</div>
        <div className="font-mono text-3xl text-ink-muted">—</div>
      </div>
    );
  }
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-muted">{label}</div>
      <div className={gxCx('font-mono text-[40px] leading-[44px] tabular-nums', tone === 'after' ? 'text-arc' : 'text-ink')}>{pct(run.successes / run.trials)}</div>
      <div className="gx-id">
        {run.successes}/{run.trials} trials · 95% CI {pct(run.ciLow)}–{pct(run.ciHigh)}
      </div>
    </div>
  );
}

function CopyBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative min-w-0 overflow-hidden border border-line bg-surface-sunken">
      <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-ink-muted">{label}</span>
        <button
          type="button"
          className="gx-btn gx-btn-sm gx-btn-ghost"
          onClick={() => {
            navigator.clipboard?.writeText(text).then(
              () => {
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              },
              () => undefined,
            );
          }}
          data-testid="deployment-copy"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="m-0 overflow-x-auto p-3 font-mono text-[12px] leading-[18px] text-ink">{text}</pre>
    </div>
  );
}

function detailText(body: unknown, status: number): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') return (d as { message: string }).message;
  return `HTTP ${status}`;
}

export function DeploymentView({ id }: { id: string }) {
  const tid = BigInt(id);
  const d = useTdm116Cockpit(tid);
  const t = d.ticket;
  const robotId = d.robotId;
  const [cmds] = useRows('robotCommand', (r) => r.robotId.eq(robotId));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // the newest deployment run of this ticket on its robot (run_baseline purpose=deployment_run)
  const lastRun = useMemo(() => {
    const mine = cmds.filter((c) => c.type === 'run_baseline' && c.ticketId === tid && (parseObj(c.payloadJson)?.purpose === 'deployment_run'));
    return mine.sort((a, b) => Number(b.id - a.id))[0];
  }, [cmds, tid]);
  const openJob = useMemo(
    () => cmds.find((c) => (c.status === 'issued' || c.status === 'acked') && ['run_baseline', 'run_eval', 'start_teleop', 'reset_request', 'home', 'load_checkpoint'].includes(c.type)),
    [cmds],
  );

  if (!d.ready) return <p className="gx-small">Loading TKT-{id.padStart(4, '0')}…</p>;
  if (!t) return <p className="gx-small">There is no ticket {id}.</p>;

  const ref4 = `TKT-${t.id.toString().padStart(4, '0')}`;
  const shipped = t.status === 'shipped' && !!t.deployedCheckpointUri;
  const uri = t.deployedCheckpointUri ?? '';
  const shipEv = [...d.events].reverse().find((e) => e.row.type === 'flywheel.shipped');
  const sp = shipEv?.payload ?? {};
  const shippedAt = shipEv?.row.ts.toDate() ?? t.updatedAt.toDate();
  const iteration = Number(sp.iteration ?? t.iteration ?? 0);
  const activated = [...d.events].reverse().find((e) => e.row.type === 'flywheel.policy_activated' && e.payload.checkpoint_uri === uri);
  const endpoint = typeof activated?.payload.url === 'string' ? (activated.payload.url as string) : null;
  const run = d.trainingRuns.find((r) => r.checkpointUri === uri) ?? d.trainingRuns.find((r) => Number(r.id) === Number(sp.training_run_id));
  const hp = (parseObj(run?.hyperparamsJson) ?? {}) as Record<string, unknown>;
  const metrics = (parseObj(run?.metricsJson) ?? {}) as Record<string, unknown>;
  const steps = Number(hp.num_train_steps ?? hp.steps ?? hp.train_steps ?? metrics.step ?? metrics.steps_done ?? metrics.global_step ?? 0) || null;
  const stub = uri.startsWith('fake://') || isStubCheckpoint(uri) || (run?.modalCallId ?? '').startsWith('stub-');
  const sim = d.mode === 'sim';
  const accepted = Number(sp.accepted_episodes ?? d.reviews.filter((r) => r.accepted).length);
  const spent = Number(t.spentCents);
  const gpu = Number(sp.gpu_cents ?? 0);
  const target = d.settings?.successThreshold ?? t.targetSuccess;
  const robotName = d.robot?.name ?? `robot #${t.robotId}`;
  const unit = `YAM-${t.id.toString().padStart(2, '0')}`;
  const runsOn = sim
    ? stub || !endpoint
      ? 'MuJoCo sim (scripted stand-in policy)'
      : 'Modal policy server (pi0.5 fine-tune) · watched on the MuJoCo sim'
    : endpoint
      ? 'Modal policy server (pi0.5 fine-tune)'
      : 'Real YAM arm';
  const endpointText = endpoint ?? (stub || sim ? `Local sim policy server · ${SIM_POLICY_URL}` : 'POLICY_SERVER_URL not set');
  const c = d.candidate;

  // Next steps: from the ticket's own rows, 2-4 of them
  const settingsHref = `/tickets/${id}?tab=settings`;
  const next: { text: string; href?: string; link?: string }[] = [];
  if (accepted < 10) {
    next.push({ text: `Trained on ${accepted} demo${accepted === 1 ? '' : 's'}. Collect 10–20 more for robustness.`, href: settingsHref, link: 'Collect more' });
  }
  if (c && c.status === 'done' && c.trials > 0 && (c.ciHigh - c.ciLow > 0.3 || c.trials < 10)) {
    next.push({ text: `The 95% interval is wide (${pct(c.ciLow)}–${pct(c.ciHigh)} on ${c.trials} trials). Run more eval trials to tighten it.`, href: settingsHref, link: 'Eval settings' });
  }
  if (sim) next.push({ text: 'Evaluated in sim. Validate on the real arm before production.', href: settingsHref, link: 'Robot settings' });
  if (stub) next.push({ text: 'Training was a stub run. Fine-tune on Modal for a real checkpoint.', href: settingsHref, link: 'Training settings' });
  else if (steps) next.push({ text: `Fine-tuned ${steps.toLocaleString('en-US')} steps on Modal. Longer runs usually help once there is more data.` });
  const base = endpoint ?? SIM_POLICY_URL;
  const prompt = (typeof (parseObj(d.settings?.hyperparamsJson) ?? {}).prompt === 'string' ? ((parseObj(d.settings?.hyperparamsJson) ?? {}).prompt as string) : '') || t.taskDescription;
  const snippet = `# Serve the deployed checkpoint, then ask it for an action chunk.
import requests
URL = "${base}"
CKPT = "${uri}"
auth = {"Authorization": "Bearer $POLICY_SERVER_TOKEN"}${endpoint ? '' : '  # sim: no token'}

requests.post(f"{URL}/policy/activate", json={"checkpoint_uri": CKPT}, headers=auth)
r = requests.post(f"{URL}/infer", headers=auth, json={
    "images": [top_jpeg_b64, wrist_jpeg_b64],   # 224x224 base cam, wrist cam
    "state": joints + [gripper],                 # 6 joints, gripper last
    "prompt": ${JSON.stringify(prompt)},
    "checkpoint_uri": CKPT,
})
actions = r.json()["actions"]                    # 50 steps x 7 (joint_pos_abs)`;

  const running = lastRun && (lastRun.status === 'issued' || lastRun.status === 'acked');
  const lastResult = lastRun?.resultJson ? (parseObj(lastRun.resultJson) as Record<string, unknown> | null) : null;
  const robotOnline = d.robot && ['online', 'idle'].includes(d.robot.status);
  const blocked = !shipped
    ? 'Ships first: this ticket has no deployed checkpoint yet.'
    : !sim
      ? 'Deployment runs go to the sim robot only.'
      : !robotOnline
        ? `${robotName} is ${d.robot?.status ?? 'offline'}. Start the sim robot.`
        : openJob && !running
          ? `Robot busy: ${openJob.type} #${openJob.id.toString()} is running.`
          : null;

  async function runPolicy() {
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`/api/deployment/tickets/${id}/run`, { method: 'POST', cache: 'no-store' });
      const body = await res.json().catch(() => null);
      if (!res.ok) setErr(res.status === 401 ? 'Operator login required: log in at /ops/login.' : detailText(body, res.status));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6" data-testid="deployment">
      <p className="m-0 gx-small">
        <Link href={`/tickets/${id}`} className="hover:underline">
          {ref4}
        </Link>{' '}
        / Deployment
      </p>

      {/* the spec sheet */}
      <section className="relative isolate overflow-hidden border border-line-strong/70 bg-surface" aria-label="Deployment spec sheet">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 -z-10 opacity-40 [mask-image:radial-gradient(ellipse_80%_70%_at_70%_30%,#000_30%,transparent_85%)]"
          style={{
            backgroundImage:
              'linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px)',
            backgroundSize: '24px 24px',
          }}
        />
        <div className="flex flex-wrap items-start justify-between gap-6 p-6">
          <div className="min-w-0 max-w-[640px]">
            <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink-muted">
              <span className="text-melt">Foundry unit</span> · Geonosis line · {ref4}
            </div>
            <h1 className="m-0 mt-2 font-[family-name:var(--font-display)] text-[30px] font-extrabold leading-[34px] text-ink">
              {unit} · {t.taskDescription}
            </h1>
            <div className="mt-2 font-mono text-[12px] uppercase tracking-[0.08em] text-ink-muted">
              Policy v{iteration + 1} (iteration {iteration}) · {robotName}
            </div>
          </div>
          {shipped ? (
            <div
              className="shrink-0 rotate-[-4deg] border-2 border-arc px-4 py-2 text-center font-mono uppercase text-arc"
              data-testid="deployment-stamp"
              title={`Shipped ${shippedAt.toISOString()}`}
            >
              <div className="text-[22px] font-bold leading-6 tracking-[0.2em]">Shipped</div>
              <div className="text-[11px] tracking-[0.12em]">{shippedAt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' })} · {shippedAt.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</div>
            </div>
          ) : (
            <Tag>Not shipped · {t.status}</Tag>
          )}
        </div>

        {/* proof */}
        <div className="grid gap-6 border-t border-line px-6 py-5 min-[760px]:grid-cols-[auto_auto_1fr] min-[760px]:items-end">
          <RunFig run={d.baseline} label="Before · base pi0.5" />
          <div className="flex items-end gap-6">
            <span aria-hidden className="pb-6 font-mono text-2xl text-ink-muted">→</span>
            <RunFig run={d.candidate} label="After · fine-tuned" tone="after" />
          </div>
          <p className="m-0 gx-small min-[760px]:justify-self-end min-[760px]:text-right">
            Target {pct(target)} {d.settings?.requireCiLow ? 'on the CI lower bound' : 'success'} · Wilson 95% · {sim ? 'sim' : 'real'} trials only
          </p>
        </div>

        {/* title block */}
        <div className="grid grid-cols-2 border-t border-line-strong/70 min-[760px]:grid-cols-4">
          <Cell label="Data bought" className="border-b border-r min-[760px]:border-b-0">
            {accepted} accepted demo{accepted === 1 ? '' : 's'}
          </Cell>
          <Cell label="Cost" className="border-b min-[760px]:border-b-0 min-[760px]:border-r">
            {fmtCents(spent)} of {fmtCents(t.budgetCents)}
            {gpu > 0 ? ` · GPU ${fmtCents(gpu)}` : ''}
          </Cell>
          <Cell label="Training" className="border-r">
            {steps ? `${steps.toLocaleString('en-US')} steps` : 'steps not reported'}
            {stub ? ' · STUB' : run?.modalCallId ? ' · Modal' : ''}
          </Cell>
          <Cell label="Rev">
            v{iteration + 1} · run #{run?.id.toString() ?? String(sp.training_run_id ?? '—')} · eval #{String(sp.eval_run_id ?? d.candidate?.id ?? '—')}
          </Cell>
        </div>
      </section>

      {/* policy */}
      <section className="grid gap-4 min-[960px]:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]" aria-label="Policy">
        <div className="grid min-w-0 content-start gap-4">
          <div className="border border-line bg-surface">
            <div className="border-b border-line px-3 py-2 font-mono text-[11px] uppercase tracking-[0.12em] text-ink">Policy</div>
            <Cell label="Checkpoint" className="border-b">
              <span title={uri}>{uri || 'not deployed'}</span>
            </Cell>
            <Cell label="Endpoint" className="border-b">
              {endpointText}
            </Cell>
            <Cell label="Runs on" mono={false}>
              {runsOn}
            </Cell>
          </div>
          <CopyBlock label="Call the endpoint · python" text={snippet} />
        </div>

        {/* run my policy */}
        <div className="grid min-w-0 content-start gap-3 border border-line bg-surface p-4" data-testid="deployment-run">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink">Run my policy</div>
              <div className="gx-small">One rollout of the deployed checkpoint on {sim ? robotName : 'the sim robot'}. Not recorded, not scored.</div>
            </div>
            <Button
              variant="primary"
              disabled={busy || !!running || !!blocked}
              disabledReason={running ? 'Your policy is running' : blocked ?? undefined}
              onClick={runPolicy}
              data-testid="deployment-run-button"
            >
              {running ? 'Running…' : busy ? 'Starting…' : 'Run my policy'}
            </Button>
          </div>
          {(err || blocked || lastRun) && (
            <p className={gxCx('m-0 gx-data', err ? 'text-slag' : 'text-ink-muted')} role="status" data-testid="deployment-run-status">
              {err ??
                (running
                  ? `Run #${lastRun!.id.toString()} ${lastRun!.status === 'acked' ? 'is driving the arm' : 'is queued on the robot'}…`
                  : lastRun
                    ? `Last run #${lastRun.id.toString()}: ${lastRun.status}${lastResult && typeof lastResult.success === 'boolean' ? (lastResult.success ? ' · task done' : ' · task not done') : ''}${lastResult && typeof lastResult.seconds === 'number' ? ` in ${lastResult.seconds}s` : ''}${lastRun.status === 'failed' && typeof lastResult?.error === 'string' ? ` · ${lastResult.error.slice(0, 160)}` : ''}`
                    : blocked)}
            </p>
          )}
          <div className="min-h-[320px]">
            {sim ? (
              <Tdm116RobotWell d={d} compact />
            ) : (
              <div className="tdm116-empty">
                <strong>Sim only</strong>
                This ticket ran on {robotName}. Deployment runs never send a policy to a real arm.
              </div>
            )}
          </div>
        </div>
      </section>

      {shipped && next.length > 0 && (
        <section className="border border-line bg-surface" aria-label="Next steps" data-testid="deployment-next">
          <div className="border-b border-line px-3 py-2 font-mono text-[11px] uppercase tracking-[0.12em] text-ink">Next steps</div>
          <ol className="m-0 list-none p-0">
            {next.slice(0, 4).map((n, i) => (
              <li key={i} className={gxCx('flex flex-wrap items-baseline gap-x-4 gap-y-1 px-3 py-2.5', i > 0 && 'border-t border-line')}>
                <span className="font-mono text-[11px] text-melt">{String(i + 1).padStart(2, '0')}</span>
                <span className="min-w-0 flex-1 text-sm text-ink">{n.text}</span>
                {n.href && (
                  <Link href={n.href} className="gx-id hover:text-ink hover:underline">
                    {n.link} →
                  </Link>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      <div className="flex flex-wrap gap-3">
        <ButtonLink href={`/tickets/${id}/report`} small>
          Open the report
        </ButtonLink>
        <ButtonLink href={`/tickets/${id}`} small variant="ghost">
          Back to the ticket
        </ButtonLink>
      </div>
    </div>
  );
}
