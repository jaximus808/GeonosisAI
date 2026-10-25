'use client';

// TDM-62: the reset task page (/reset/[taskId]) for the person standing at the table, usually on
// a phone (a judge who just got paged). One screen, top to bottom:
//
//   1. Is the arm safe to touch? (fine robot state, TDM-49; red hazard banner whenever it may move)
//   2. What this is: scene reset #N, $2 TEST MODE, which eval trial it follows, how long it's waited
//      (and the escalation re-pages, TDM-62 reset_watch)
//   3. The steps: the robot's stored reset routine + start-state photo (reset_routine.py), else
//      default steps and a start-state diagram. Tap to tick.
//   4. Live robot camera (claimant only, TDM-55) to compare the scene with the start photo
//   5. One sticky action: join + claim, or "Done, robot may continue" -> optional scene photo
//      (robot camera snapshot / phone photo, stored as task evidence) -> complete_task
//   6. After Done: reset done -> $2 payment (pending / paid, test) -> resume_policy -> next trial
//
// complete_task is all the page does to release the robot: the TDM-26 dispatcher turns the
// completed reset into reserve_task_payment + resume_policy.

import Link from 'next/link';
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useReducer } from 'spacetimedb/react';
import { reducers } from '@/module_bindings';
import { useContributor, useContributorPresence } from '@/lib/contributor/hooks';
import {
  actionErrorMessage,
  claimErrorMessage,
  cleanInstructions,
  fmtCountdown,
  fmtPrice,
  isClaimable,
  leaseLeftMs,
  leaseLive,
  tsMs,
  useNow,
  useTdm61ClaimHeartbeat,
} from '@/lib/marketplace/tdm61';
import { attachPhoto, downscalePhoto, useResetInfo, useResetView, type ArmSafety } from '@/lib/tdm62/reset';
import { TaskCameraFeed } from '@/components/teleop/CameraFeed';

const RED_HAZARD = 'repeating-linear-gradient(-45deg, #b91c1c 0 10px, #7f1d1d 10px 20px)';
const ACCENT = '#0e7c6b';
const PHOTO_BUDGET_MS = 1500; // the camera photo never holds the robot up for longer than this

function clock(ms: number | undefined) {
  return ms ? new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' }) : '';
}

function ago(now: number, ms: number | undefined) {
  if (!ms) return '';
  const s = Math.max(0, Math.round((now - ms) / 1000));
  return s < 60 ? `${s} s` : fmtCountdown(s * 1000);
}

// ------------------------------------------------------------------ pieces

function ArmBanner({ safety, text, status }: { safety: ArmSafety; text: string; status: string }) {
  if (safety === 'safe') {
    return (
      <div role="status" className="flex items-center gap-3 rounded-xl bg-[#0e7c6b] px-4 py-3 text-white shadow-sm">
        <span aria-hidden className="relative flex h-3.5 w-3.5 shrink-0">
          <span className="tdm62-motion absolute inline-flex h-full w-full animate-ping rounded-full bg-white/60" />
          <span className="relative inline-flex h-3.5 w-3.5 rounded-full bg-white" />
        </span>
        <div className="min-w-0">
          <p className="text-[1.05rem] font-semibold leading-tight">Safe to reset</p>
          <p className="text-sm text-white/85">{text}</p>
        </div>
      </div>
    );
  }
  return (
    <div role="alert" className="overflow-hidden rounded-xl text-white shadow-sm" style={{ backgroundImage: safety === 'unknown' ? undefined : RED_HAZARD }}>
      <div className={`m-1.5 rounded-lg px-4 py-3 ${safety === 'unknown' ? 'bg-zinc-700' : 'bg-[#7f1d1d]/95'}`}>
        <p className="text-[1.05rem] font-bold uppercase tracking-wide">
          {safety === 'unknown' ? 'Check the arm before you touch it' : 'Do not touch the arm'}
        </p>
        <p className="text-sm text-white/90">
          {text}
          {status ? ` (${status})` : ''}. Only reach in while this says <span className="font-semibold">Safe to reset</span>.
        </p>
      </div>
    </div>
  );
}

function StartDiagram() {
  // Generic start-state sketch for robots with no stored start photo yet (labelled as such).
  return (
    <figure className="overflow-hidden rounded-lg border border-zinc-200 bg-[#f6f3ea] dark:border-zinc-800 dark:bg-zinc-900">
      <svg viewBox="0 0 320 170" role="img" aria-label="Start state sketch: arm at home, object on its marked spot, bin empty" className="block w-full">
        <rect x="10" y="10" width="300" height="150" rx="6" fill="none" stroke="currentColor" strokeOpacity=".25" strokeWidth="2" />
        <circle cx="160" cy="34" r="16" fill={ACCENT} fillOpacity=".15" stroke={ACCENT} strokeWidth="2" />
        <text x="160" y="38" textAnchor="middle" fontSize="10" fill={ACCENT} fontWeight="600">ARM</text>
        <text x="160" y="64" textAnchor="middle" fontSize="9" fill="currentColor" fillOpacity=".6">home pose, holding still</text>
        <g stroke="#b45309" strokeWidth="2.5" strokeLinecap="round">
          <line x1="70" y1="100" x2="94" y2="124" />
          <line x1="94" y1="100" x2="70" y2="124" />
        </g>
        <circle cx="82" cy="112" r="15" fill="none" stroke="#b45309" strokeDasharray="4 3" strokeWidth="1.5" />
        <text x="82" y="146" textAnchor="middle" fontSize="10" fill="currentColor" fillOpacity=".75">object on its mark</text>
        <rect x="206" y="92" width="64" height="42" rx="4" fill="none" stroke="currentColor" strokeOpacity=".55" strokeWidth="2" />
        <text x="238" y="117" textAnchor="middle" fontSize="10" fill="currentColor" fillOpacity=".75">bin, empty</text>
        <text x="238" y="146" textAnchor="middle" fontSize="10" fill="currentColor" fillOpacity=".75">in its outline</text>
      </svg>
      <figcaption className="border-t border-zinc-200 px-3 py-1.5 text-[11px] text-zinc-500 dark:border-zinc-800">
        Sketch, not a photo: no start photo has been saved for this robot yet.
      </figcaption>
    </figure>
  );
}

function Step({ n, text, done, onToggle }: { n: number; text: string; done: boolean; onToggle: () => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={done}
        className="flex w-full items-start gap-3 rounded-lg px-2 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-[#0e7c6b] active:bg-zinc-100 dark:active:bg-zinc-800"
      >
        <span
          aria-hidden
          className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 text-sm font-bold tabular-nums ${
            done ? 'border-[#0e7c6b] bg-[#0e7c6b] text-white' : 'border-zinc-300 text-zinc-500 dark:border-zinc-600'
          }`}
        >
          {done ? '✓' : n}
        </span>
        <span className={`text-[1.02rem] leading-snug ${done ? 'text-zinc-400 line-through decoration-zinc-300' : ''}`}>{text}</span>
      </button>
    </li>
  );
}

type Tone = 'done' | 'now' | 'todo' | 'bad';
function Milestone({ tone, title, detail, last }: { tone: Tone; title: ReactNode; detail?: ReactNode; last?: boolean }) {
  const dot =
    tone === 'done'
      ? 'bg-[#0e7c6b] text-white'
      : tone === 'now'
        ? 'bg-white text-[#0e7c6b] ring-2 ring-[#0e7c6b] dark:bg-zinc-900'
        : tone === 'bad'
          ? 'bg-red-600 text-white'
          : 'bg-zinc-200 text-zinc-400 dark:bg-zinc-800';
  return (
    <li className="relative flex gap-3 pb-4">
      {!last && <span aria-hidden className="absolute left-[13px] top-7 h-[calc(100%-1.25rem)] w-0.5 bg-zinc-200 dark:bg-zinc-800" />}
      <span aria-hidden className={`z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold ${dot}`}>
        {tone === 'done' ? '✓' : tone === 'bad' ? '!' : tone === 'now' ? <span className="tdm62-motion h-2 w-2 animate-pulse rounded-full bg-[#0e7c6b]" /> : ''}
      </span>
      <div className="min-w-0 pt-0.5">
        <p className="font-semibold leading-tight">{title}</p>
        {detail && <p className="mt-0.5 text-sm text-zinc-500 dark:text-zinc-400">{detail}</p>}
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ page

export function ResetTaskView({ taskId }: { taskId: bigint }) {
  useContributorPresence('reset');
  const { contributor, identity, ready: meReady, register } = useContributor();
  const v = useResetView(taskId);
  const { info, error: infoError, reload } = useResetInfo(taskId);
  const now = useNow(500);
  const claimTask = useReducer(reducers.claimTask);
  const releaseTask = useReducer(reducers.releaseTask);
  const completeTask = useReducer(reducers.completeTask);

  const t = v.task;
  const me = contributor?.id;
  const held = t != null && me != null && t.assigneeId === me && leaseLive(t, now);
  const { error: beatError } = useTdm61ClaimHeartbeat(held ? t : undefined);
  const claimable = t != null && isClaimable(t, now);
  const byOther = t != null && t.status === 'claimed' && leaseLive(t, now) && !held;

  const [ticks, setTicks] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<'' | 'claim' | 'join' | 'done' | 'release'>('');
  const [err, setErr] = useState<string | undefined>();
  const [cameraPhoto, setCameraPhoto] = useState(true);
  const [phonePhoto, setPhonePhoto] = useState<string | undefined>();
  const [doneInfo, setDoneInfo] = useState<{ tappedAt: number; photo?: string; photoNote?: string } | undefined>();
  const [name, setName] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const steps = info?.routine.steps ?? [];
  const createdMs = tsMs(t?.createdAt);
  const doneMs = tsMs(t?.completedAt);
  const left = t ? Math.max(0, leaseLeftMs(t, now) ?? 0) : 0;
  const lastEscalation = v.escalations[v.escalations.length - 1];
  const moving = v.armSafety === 'moving' || v.armSafety === 'stopped';

  useEffect(() => {
    document.title = t ? `Reset #${t.id} · ${fmtPrice(t.priceCents)} · GeonosisAI` : 'Scene reset · GeonosisAI';
  }, [t]);

  async function onJoinAndClaim(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy('join');
    setErr(undefined);
    try {
      await register({ name: name.trim(), email: '', devices: ['phone'], kind: 'contributor', sourceTicketId: t?.ticketId });
    } catch (e2) {
      setErr(actionErrorMessage(e2));
    } finally {
      setBusy('');
    }
  }

  // After an inline join, claim as soon as the contributor row arrives.
  const pendingClaim = useRef(false);
  useEffect(() => {
    if (busy === 'join') pendingClaim.current = true;
  }, [busy]);
  useEffect(() => {
    if (pendingClaim.current && me != null && t && isClaimable(t, Date.now())) {
      pendingClaim.current = false;
      void onClaim();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, t?.status]);

  async function onClaim() {
    if (me == null || !t) return;
    setBusy('claim');
    setErr(undefined);
    try {
      await claimTask({ taskId: t.id, contributorId: me });
    } catch (e) {
      setErr(claimErrorMessage(e));
    } finally {
      setBusy('');
    }
  }

  async function onRelease() {
    if (!t) return;
    setBusy('release');
    try {
      await releaseTask({ taskId: t.id });
    } catch (e) {
      setErr(actionErrorMessage(e));
    } finally {
      setBusy('');
    }
  }

  async function onPickPhoto(f: File | undefined) {
    if (!f) return;
    try {
      setPhonePhoto(await downscalePhoto(f));
    } catch {
      setErr('Could not read that photo. Try again or skip it.');
    }
  }

  async function onDone() {
    if (!t || !identity) return;
    setBusy('done');
    setErr(undefined);
    const tappedAt = Date.now();
    const photos: string[] = [];
    let photoNote: string | undefined;
    let photoUrl: string | undefined;
    try {
      if (cameraPhoto) {
        const shot = attachPhoto(t.id, identity, { camera: true });
        const timeout = new Promise<never>((_, rej) => window.setTimeout(() => rej(new Error('the robot camera was too slow')), PHOTO_BUDGET_MS));
        try {
          const r = await Promise.race([shot, timeout]);
          photos.push(r.storage_uri);
          photoUrl = r.url;
        } catch (e) {
          photoNote = `No robot photo (${(e as Error).message}).`;
        }
      }
      if (phonePhoto) {
        try {
          const r = await attachPhoto(t.id, identity, { dataUrl: phonePhoto });
          photos.push(r.storage_uri);
          photoUrl = photoUrl ?? r.url;
        } catch (e) {
          photoNote = `${photoNote ?? ''} Phone photo not saved (${(e as Error).message}).`.trim();
        }
      }
      await completeTask({
        taskId: t.id,
        resultJson: JSON.stringify({
          source: 'reset_page',
          done: 'reset',
          photo_uris: photos,
          arm_state: v.armState ?? null,
          steps_ticked: ticks.size,
          steps_total: steps.length,
          tapped_at_ms: tappedAt,
        }),
      });
      setDoneInfo({ tappedAt, photo: photoUrl, photoNote });
      reload();
      try {
        navigator.vibrate?.(40);
      } catch {
        /* unsupported */
      }
    } catch (e) {
      setErr(actionErrorMessage(e));
    } finally {
      setBusy('');
    }
  }

  // ---------------------------------------------------------------- render

  if (!v.ready) return <p className="py-10 text-center text-sm text-zinc-500">Connecting to the factory…</p>;
  if (!t) {
    return (
      <div className="mx-auto max-w-xl py-10 text-center">
        <p className="font-medium">No task #{String(taskId)}.</p>
        <Link href="/marketplace" className="mt-3 inline-block text-sm underline">See open tasks</Link>
      </div>
    );
  }
  if (t.type !== 'reset') {
    return (
      <div className="mx-auto max-w-xl py-10 text-center">
        <p className="font-medium">Task #{String(t.id)} is not a scene reset.</p>
        <Link href={`/teleop/${t.id}`} className="mt-3 inline-block text-sm underline">Open it on the teleop page</Link>
      </div>
    );
  }

  const completed = t.status === 'completed';
  const gone = t.status === 'cancelled' || t.status === 'expired';
  const trialText =
    v.context.trialIndex !== undefined
      ? `after trial ${v.context.trialIndex + 1}${v.evalRun ? ` of ${v.evalRun.trials}` : ''}${
          v.context.trialSuccess === undefined ? '' : v.context.trialSuccess ? ' (success)' : ' (fail)'
        }`
      : v.context.reason === 'robot_paused'
        ? 'the robot paused and needs the scene reset'
        : undefined;
  const pay = v.payment;
  const resumeDone = v.resume && ['acked', 'done'].includes(v.resume.status);
  const moved = v.armSafety === 'moving' || v.nextTrial != null;

  return (
    <div className="mx-auto max-w-xl pb-40">
      <style>{`@media (prefers-reduced-motion: reduce) { .tdm62-motion { animation: none !important; } }`}</style>

      {/* 1. arm safety */}
      {!completed && !gone && <ArmBanner safety={v.armSafety} text={v.armText} status={v.robot?.name ?? ''} />}

      {/* 2. what this is */}
      <header className="mt-4 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold" style={{ color: ACCENT }}>
            Scene reset <span className="font-normal tabular-nums text-zinc-400">#{String(t.id)}</span>
          </p>
          <h1 className="mt-0.5 text-[1.45rem] font-semibold leading-tight tracking-tight">
            {v.ticket?.taskDescription ?? `Ticket ${t.ticketId}`}
          </h1>
          <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
            {v.robot?.name ?? `Robot ${t.robotId}`}
            {trialText ? ` · ${trialText}` : ''}
          </p>
        </div>
        <div className="shrink-0 rounded-lg border-2 border-dashed border-zinc-300 px-3 py-2 text-center dark:border-zinc-700">
          <p className="text-[1.7rem] font-bold leading-none tabular-nums">{fmtPrice(t.priceCents)}</p>
        </div>
      </header>
      <p className="mt-2 text-[13px] text-zinc-500">
        Paid as soon as you tap Done. No review.
      </p>

      {/* waiting / escalation */}
      {t.status === 'open' && (
        <div className="mt-3 rounded-lg bg-zinc-100 px-3 py-2 text-sm dark:bg-zinc-900">
          <p>
            Posted <span className="font-semibold tabular-nums">{ago(now, createdMs)}</span> ago. The robot is waiting for someone at the table.
          </p>
          {lastEscalation && (
            <p className={`mt-1 font-medium ${lastEscalation.final ? 'text-red-700 dark:text-red-400' : 'text-amber-800 dark:text-amber-300'}`}>
              {lastEscalation.final
                ? 'Still nobody. The operator has been asked to reset it. You can still claim it.'
                : 'Nobody has claimed it yet, so the factory paged everyone again.'}
            </p>
          )}
        </div>
      )}
      {byOther && (
        <p className="mt-3 rounded-lg bg-zinc-100 px-3 py-2 text-sm dark:bg-zinc-900">
          {v.assignee?.name ?? 'Someone'} is doing this reset (claimed {ago(now, tsMs(t.claimedAt))} ago).
        </p>
      )}
      {gone && (
        <p className="mt-3 rounded-lg bg-zinc-100 px-3 py-2 text-sm dark:bg-zinc-900">
          This reset was {t.status === 'cancelled' ? 'withdrawn by the factory' : 'not finished in time'}. Nothing to do here.{' '}
          <Link href="/marketplace" className="underline">Other tasks</Link>
        </p>
      )}

      {/* 6. after Done */}
      {completed && (
        <section aria-label="What happened after the reset" className="mt-5 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
          <h2 className="mb-3 text-lg font-semibold">{held || doneInfo || t.assigneeId === me ? 'Thanks, reset done' : 'Reset done'}</h2>
          <ol>
            <Milestone
              tone="done"
              title="Scene reset"
              detail={`${clock(doneMs)}${v.assignee ? ` by ${v.assignee.name}` : ''}`}
            />
            <Milestone
              tone={pay ? (['failed', 'reversed', 'blocked_budget'].includes(pay.status) ? 'bad' : 'done') : 'now'}
              title={
                <>
                  {fmtPrice(pay?.amountCents ?? t.priceCents)} payout
                </>
              }
              detail={
                !pay
                  ? 'Reserving the payment…'
                  : pay.status === 'paid'
                    ? 'Paid.'
                    : pay.status === 'pending'
                      ? 'Reserved. The transfer goes out shortly.'
                      : pay.status === 'awaiting_onboarding'
                        ? 'Reserved. Set up payouts on the earnings page to receive it.'
                        : `Payment ${pay.status.replace('_', ' ')}.`
              }
            />
            <Milestone
              tone={v.resume ? (v.resume.status === 'failed' ? 'bad' : 'done') : 'now'}
              title="Robot released"
              detail={
                v.resume
                  ? `resume_policy ${v.resume.status}${v.resumeIssuedAtMs && doneMs ? `, ${((v.resumeIssuedAtMs - doneMs) / 1000).toFixed(1)} s after Done` : ''}`
                  : 'Sending the robot its go-ahead…'
              }
            />
            <Milestone
              last
              tone={v.nextTrial ? 'done' : resumeDone || moved ? 'now' : 'todo'}
              title={v.nextTrial ? `Trial ${v.nextTrial.index + 1} started` : v.context.evalRunId !== undefined ? 'Next trial' : 'Robot continues'}
              detail={
                v.nextTrial && doneMs
                  ? `${((v.nextTrial.atMs - doneMs) / 1000).toFixed(1)} s after Done${v.evalRun ? ` · ${v.evalRun.completed} of ${v.evalRun.trials} trials recorded` : ''}`
                  : v.evalRun && v.evalRun.status !== 'running'
                    ? `Eval ${v.evalRun.status}: no more trials.`
                    : 'Starting…'
              }
            />
          </ol>
          {(doneInfo?.photo || info?.evidence.length) && (
            <figure className="mt-1">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={doneInfo?.photo ?? info!.evidence[info!.evidence.length - 1].url}
                alt="The reset scene, as it was when you tapped Done"
                className="w-full rounded-lg border border-zinc-200 dark:border-zinc-800"
              />
              <figcaption className="mt-1 text-xs text-zinc-500">Scene photo saved with the task for the reviewer.</figcaption>
            </figure>
          )}
          {doneInfo?.photoNote && <p className="mt-2 text-xs text-zinc-500">{doneInfo.photoNote}</p>}
          <div className="mt-4 flex gap-3 text-sm">
            <Link href="/marketplace" className="rounded-md bg-zinc-900 px-4 py-2 font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">
              More tasks
            </Link>
            <Link href="/earnings" className="rounded-md border border-zinc-300 px-4 py-2 dark:border-zinc-700">
              Earnings
            </Link>
          </div>
        </section>
      )}

      {/* 3. steps */}
      {!completed && !gone && (
        <section aria-label="Reset steps" className="mt-5">
          <h2 className="mb-1 text-lg font-semibold">Put the scene back like this</h2>
          {(info?.routine.prompt || info?.routine.est_seconds) && (
            <p className="mb-2 text-[0.98rem] leading-snug text-zinc-700 dark:text-zinc-300">
              {info.routine.prompt}
              {info.routine.est_seconds ? (
                <span className="text-zinc-500">{info.routine.prompt ? ' · ' : ''}about {Math.round(info.routine.est_seconds)} s</span>
              ) : null}
            </p>
          )}
          {info?.routine.reference_url ? (
            <figure>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={info.routine.reference_url} alt="Start state of the scene" className="w-full rounded-lg border border-zinc-200 dark:border-zinc-800" />
              <figcaption className="mt-1 text-xs text-zinc-500">Start state, saved for this robot.</figcaption>
            </figure>
          ) : (
            <StartDiagram />
          )}
          {steps.length ? (
            <ol className="mt-2 -mx-2">
              {steps.map((s, i) => (
                <Step
                  key={i}
                  n={i + 1}
                  text={s}
                  done={ticks.has(i)}
                  onToggle={() =>
                    setTicks((cur) => {
                      const nx = new Set(cur);
                      if (nx.has(i)) nx.delete(i);
                      else nx.add(i);
                      return nx;
                    })
                  }
                />
              ))}
            </ol>
          ) : (
            <p className="mt-2 text-[1.02rem] leading-snug">{cleanInstructions(t.instructions)}</p>
          )}
          {infoError && <p className="mt-1 text-xs text-zinc-500">Showing the task text (routine not available: {infoError}).</p>}
        </section>
      )}

      {/* 4. live camera, claimant only */}
      {held && (
        <section aria-label="Robot camera" className="mt-5">
          <h2 className="mb-2 text-sm font-semibold text-zinc-600 dark:text-zinc-400">What the robot sees right now</h2>
          <TaskCameraFeed taskId={t.id} layout="stack" fps={8} staleMs={2000} />
        </section>
      )}

      {/* 5. the one action */}
      {!completed && !gone && (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-zinc-200 bg-white/95 px-4 pb-[max(0.85rem,env(safe-area-inset-bottom))] pt-3 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/95">
          <div className="mx-auto max-w-xl">
            {err && (
              <p role="alert" className="mb-2 rounded-md bg-red-50 px-3 py-2 text-sm font-medium text-red-800 dark:bg-red-950/50 dark:text-red-200">
                {err}
              </p>
            )}
            {held ? (
              <>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[13px] text-zinc-600 dark:text-zinc-400">
                  <label className="flex items-center gap-2">
                    <input type="checkbox" checked={cameraPhoto} onChange={(e) => setCameraPhoto(e.target.checked)} className="h-4 w-4 accent-[#0e7c6b]" />
                    Save a robot-camera photo
                  </label>
                  <button type="button" onClick={() => fileRef.current?.click()} className="underline underline-offset-2">
                    {phonePhoto ? 'Phone photo added ✓' : 'Add a phone photo'}
                  </button>
                  <input ref={fileRef} type="file" accept="image/*" capture="environment" className="hidden" onChange={(e) => void onPickPhoto(e.target.files?.[0])} />
                </div>
                <button
                  type="button"
                  disabled={busy !== '' || moving}
                  onClick={() => void onDone()}
                  className="w-full rounded-lg px-4 py-4 text-[1.1rem] font-bold text-white shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-[#0e7c6b] active:translate-y-px disabled:opacity-60"
                  style={{ background: moving ? '#7f1d1d' : ACCENT }}
                >
                  {busy === 'done' ? 'Telling the robot…' : moving ? 'Wait: the arm may move' : 'Done, robot may continue'}
                </button>
                <p className="mt-1.5 flex items-center justify-between text-xs text-zinc-500">
                  <span>{beatError ? beatError : `Yours for ${fmtCountdown(left)}`}</span>
                  <button type="button" disabled={busy !== ''} onClick={() => void onRelease()} className="underline underline-offset-2">
                    I can&apos;t do it, release
                  </button>
                </p>
              </>
            ) : byOther ? (
              <p className="py-2 text-center text-sm text-zinc-600 dark:text-zinc-400">Claimed by {v.assignee?.name ?? 'someone else'}.</p>
            ) : !meReady ? (
              <p className="py-2 text-center text-sm text-zinc-500">Connecting…</p>
            ) : !contributor ? (
              <form onSubmit={(e) => void onJoinAndClaim(e)} className="flex gap-2">
                <label className="sr-only" htmlFor="tdm62-name">Your first name</label>
                <input
                  id="tdm62-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Your first name"
                  autoComplete="given-name"
                  maxLength={40}
                  className="min-w-0 flex-1 rounded-lg border border-zinc-300 bg-white px-3 py-3 text-base dark:border-zinc-700 dark:bg-zinc-900"
                />
                <button
                  type="submit"
                  disabled={busy !== '' || !name.trim() || !claimable}
                  className="shrink-0 rounded-lg px-4 py-3 text-base font-bold text-white disabled:opacity-50"
                  style={{ background: ACCENT }}
                >
                  {busy ? 'Joining…' : `Claim ${fmtPrice(t.priceCents)}`}
                </button>
              </form>
            ) : contributor.kind !== 'contributor' ? (
              <p className="py-2 text-center text-sm text-zinc-600">Dataset seller accounts can&apos;t claim robot tasks.</p>
            ) : (
              <button
                type="button"
                disabled={busy !== '' || !claimable}
                onClick={() => void onClaim()}
                className="w-full rounded-lg px-4 py-4 text-[1.1rem] font-bold text-white shadow-sm disabled:opacity-60"
                style={{ background: ACCENT }}
              >
                {busy === 'claim' ? 'Claiming…' : `Claim this reset · ${fmtPrice(t.priceCents)}`}
              </button>
            )}
            {!held && !byOther && (
              <p className="mt-1.5 text-center text-xs text-zinc-500">You need to be at the robot&apos;s table. Takes about a minute.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

