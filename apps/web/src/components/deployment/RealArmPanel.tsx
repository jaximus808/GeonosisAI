'use client';

// TDM-126: "Real arm" on the deployment page. ONE supervised run of pi0.5 on the real YAM, before (the
// YAM-adapted pre-trained smoke checkpoint) or after (the ticket's fine-tune). Shown only when the ticket's robot,
// its collect robot, or the only online real arm advertises policy (TDM119_ALLOW_POLICY=1 on the Jetson). Each
// button opens a confirm dialog; on confirm it calls POST /api/deployment/tickets/{id}/run {real_arm, confirm,
// checkpoint, robot_id} (the orchestrator resolves the URI and applies every gate), then shows the arm's live
// view, the run's status and the big STOP (the TDM-78 soft e-stop, same path as the robot page).

import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/brand/Button';
import { Tdm116RobotWell } from '@/components/cockpit/RobotWell';
import type { Tdm116Data } from '@/components/cockpit/useCockpitData';
import { Tdm78SoftEstop } from '@/components/tdm78/Tdm78SoftEstop';
import { parseObj } from '@/components/ticket/util';
import { useRows } from '@/lib/spacetime/hooks';
import { TDM126_PURPOSE, tdm126Body, tdm126RealArm, tdm126RealCheckpointOk, type Tdm126Choice } from '@/lib/tdm126/realRun';

const JOBS = ['run_baseline', 'run_eval', 'start_teleop', 'reset_request', 'home', 'load_checkpoint'];
const LABEL: Record<Tdm126Choice, string> = { before: 'Real arm: pre-trained π0.5', after: 'Real arm: fine-tuned' };

function detailText(body: unknown, status: number): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') return (d as { message: string }).message;
  return `HTTP ${status}`;
}

function resultLine(r: Record<string, unknown> | null): string {
  if (!r) return '';
  const bits: string[] = [];
  if (typeof r.refused === 'string') bits.push(`refused before moving: ${r.refused}`);
  if (typeof r.ended === 'string') bits.push(`ended ${r.ended}`);
  if (typeof r.seconds === 'number') bits.push(`${r.seconds}s`);
  if (typeof r.first_chunk_verdict === 'string') bits.push(`first chunk ${r.first_chunk_verdict}`);
  if (typeof r.error === 'string') bits.push(r.error.slice(0, 160));
  return bits.join(' · ');
}

export function Tdm126RealArmPanel({ d, id, shipped, uri }: { d: Tdm116Data; id: string; shipped: boolean; uri: string }) {
  const tid = BigInt(id);
  const arm = tdm126RealArm(d.ticket?.robotId ?? 0n, d.split.collectRobotId, d.allRobots);
  const armId = arm?.id ?? 0n;
  const [cmds] = useRows('robotCommand', (r) => r.robotId.eq(armId));
  const [choice, setChoice] = useState<Tdm126Choice | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const dlg = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = dlg.current;
    if (!el) return;
    if (choice && !el.open) el.showModal();
    else if (!choice && el.open) el.close();
  }, [choice]);

  const lastRun = useMemo(() => {
    const mine = cmds.filter((c) => c.type === 'run_baseline' && c.ticketId === tid && parseObj(c.payloadJson)?.purpose === TDM126_PURPOSE);
    return mine.sort((a, b) => Number(b.id - a.id))[0];
  }, [cmds, tid]);
  const openJob = useMemo(() => cmds.find((c) => (c.status === 'issued' || c.status === 'acked') && JOBS.includes(c.type)), [cmds]);

  if (!arm) return null;
  const robot = d.allRobots.find((r) => r.id === arm.id);
  const running = !!lastRun && (lastRun.status === 'issued' || lastRun.status === 'acked');
  const online = ['online', 'idle'].includes(arm.status);
  const blockedFor = (c: Tdm126Choice): string | null =>
    !shipped
      ? 'Ships first: this ticket has no deployed checkpoint yet.'
      : c === 'after' && !tdm126RealCheckpointOk(uri)
        ? 'The deployed checkpoint is not a real fine-tune (fake or stub): it never goes to the real arm.'
        : !online
          ? `${arm.name} is ${arm.status}.`
          : openJob && !running
            ? `Robot busy: ${openJob.type} #${openJob.id.toString()} is running.`
            : null;
  const lastPayload = lastRun ? (parseObj(lastRun.payloadJson) as Record<string, unknown> | null) : null;
  const lastResult = lastRun?.resultJson ? (parseObj(lastRun.resultJson) as Record<string, unknown> | null) : null;
  const showLive = started || running;

  async function start(c: Tdm126Choice) {
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`/api/deployment/tickets/${id}/run`, {
        method: 'POST',
        cache: 'no-store',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(tdm126Body(c, arm!.id)),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) setErr(res.status === 401 ? 'Operator login required: log in at /ops/login.' : detailText(body, res.status));
      else setStarted(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      setChoice(null);
    }
  }

  return (
    <section className="grid min-w-0 gap-3 border border-line bg-surface p-4" aria-label="Real arm" data-testid="deployment-real-arm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-ink">Real arm · {arm.name}</div>
          <div className="gx-small">
            One supervised rollout on the real YAM: at most 30 s, slow, the first chunk checked before it moves, and the
            arm holds at the end. Not recorded, not scored.
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {(['before', 'after'] as const).map((c) => (
            <Button
              key={c}
              variant={c === 'after' ? 'primary' : 'default'}
              disabled={busy || running || !!blockedFor(c)}
              disabledReason={running ? 'A real-arm run is in progress' : blockedFor(c) ?? undefined}
              onClick={() => setChoice(c)}
              data-testid={`deployment-real-${c}`}
            >
              {LABEL[c]}
            </Button>
          ))}
        </div>
      </div>

      {(err || lastRun) && (
        <p className={err ? 'm-0 gx-data text-slag' : 'm-0 gx-data text-ink-muted'} role="status" data-testid="deployment-real-status">
          {err ??
            (lastRun
              ? `${running ? 'Run' : 'Last run'} #${lastRun.id.toString()} (${lastPayload?.checkpoint_choice === 'before' ? 'pre-trained' : 'fine-tuned'}): ${
                  running ? (lastRun.status === 'acked' ? 'driving the arm…' : 'queued on the robot…') : lastRun.status
                }${resultLine(lastResult) ? ` · ${resultLine(lastResult)}` : ''}`
              : '')}
        </p>
      )}

      {showLive && (
        <div className="grid gap-3 min-[960px]:grid-cols-[minmax(0,1fr)_14rem]">
          <div className="min-h-[320px]">
            <Tdm116RobotWell d={d} compact robotOverride={{ id: arm.id, robot, mode: 'real' }} />
          </div>
          <Tdm78SoftEstop robotId={arm.id.toString()} />
        </div>
      )}

      <dialog ref={dlg} className="tdm116-dialog" onClose={() => setChoice(null)} aria-labelledby="tdm126-confirm-title">
        <h2 id="tdm126-confirm-title">{choice ? LABEL[choice] : 'Real arm'}</h2>
        <p className="m-0 text-base font-semibold text-ink">Hands on the e-stop. The arm will move slowly for up to 30 s.</p>
        <p className="gx-small mt-2 mb-0">
          {arm.name} runs the {choice === 'before' ? 'pre-trained YAM π0.5 (200 steps on public YAM data)' : "ticket's fine-tuned checkpoint"} once.
          The STOP button and the e-stop on the arm stop it at once.
        </p>
        <div className="tdm116-dialog-actions">
          <Button onClick={() => setChoice(null)} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => choice && start(choice)} disabled={busy || !choice} data-testid="deployment-real-confirm">
            {busy ? 'Starting…' : 'Start on the real arm'}
          </Button>
        </div>
      </dialog>
    </section>
  );
}
