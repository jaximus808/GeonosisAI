'use client';

// TDM-58: recording controls for the teleop page (TDM-56 mounts this next to the camera feed).
//
//   <RecordingControls taskId={task.id} kind={task.type} controlSocket={control} onPhase={setPhase} />
//
// `controlSocket` is the page's /control WebSocket wrapped as a ControlChannel
// (lib/teleop/tdm58Recording.ts): send(json) + subscribe(fn). Everything shown comes from the robot
// (`recording` in its {t:"state"} / {t:"record_ack"} messages; timestamps are the recorder's clock),
// then from SpacetimeDB once the episode row exists (review verdict, payout).
//
// clean_teleop:       Start recording -> (timer, frames) -> Finish — success (confirm) | Discard
// recovery_takeover:  Start episode (policy drives) -> Take over -> Hand back -> Finish — success | Discard
// TDM-69 rollout-owned takeover (a paused collection rollout's own recording): Take over -> Hand back,
//   which first asks "Did you recover the task? Yes / No" and sends {t:"handback", success}. The robot
//   writes that answer as the recovery episode's success (docs/decisions.md); the rollout submits it.
// Discard records nothing and keeps the claim. After Finish: upload progress, then the reviewer's
// verdict (accepted / rejected with reason) and the payout state, live.
//
// `onPhase` tells the page who has authority, e.g. enable the clutch only while phase is
// 'recording' (clean demo) or 'human' (takeover).

import { useCallback, useEffect, useRef, useState } from 'react';
import { MockedBadge } from '@/components/MockedBadge';
import { StatusPill } from '@/components/ticket/Badges';
import { fmtCents } from '@/lib/format';
import { useRows } from '@/lib/spacetime/hooks';
import {
  fmtClock,
  fmtRobotTs,
  nextReq,
  REVIEW_MAX_S,
  REVIEW_MIN_S,
  type ControlChannel,
  type LastRecording,
  type RecordAck,
  type RecordingOp,
  type RecordingPhase,
  type RecordingState,
} from '@/lib/teleop/tdm58Recording';

export interface RecordingControlsProps {
  taskId: bigint | number | string;
  /** task.type: 'clean_teleop' | 'recovery_takeover' */
  kind: string;
  controlSocket: ControlChannel | null | undefined;
  onPhase?: (phase: RecordingPhase) => void;
  className?: string;
  /** TDM-86 phone cockpit: one row (status + the next button), no header, timeline or hints. */
  compact?: boolean;
}

const STATE_STALE_MS = 2500;
/** TDM-86: the lease guard's refusal codes (robot-client tdm57_lease.py / teleop.py), in words. */
const REFUSED_TEXT: Record<string, string> = {
  wrong_task: 'The robot is still finishing the last person\'s session. Try again in about 15 seconds.',
  not_active: 'Another page of yours holds the controls. Close the other tab and try again.',
  lease_lost: 'You lost control of the robot. Claim the task again from the marketplace.',
  token_expired: 'Your control pass expired. Reload the page.',
};
const ACK_TIMEOUT_MS: Partial<Record<RecordingOp, number>> = { record_stop: 15000, record_discard: 15000 };

const PHASE_TEXT: Record<RecordingPhase, string> = {
  idle: 'Not recording',
  recording: 'Recording your demo',
  policy: 'Policy driving',
  human: 'You are driving',
  handed_back: 'Policy driving again',
};

function Btn({
  children,
  onClick,
  disabled,
  tone = 'neutral',
  busy,
  testId,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  tone?: 'rec' | 'go' | 'take' | 'neutral' | 'quiet';
  busy?: boolean;
  testId?: string;
}) {
  const tones = {
    rec: 'bg-red-600 text-white hover:bg-red-500',
    go: 'bg-emerald-600 text-white hover:bg-emerald-500',
    take: 'bg-amber-500 text-black hover:bg-amber-400',
    neutral: 'bg-zinc-900 text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300',
    quiet: 'bg-transparent text-zinc-700 ring-1 ring-inset ring-zinc-300 hover:bg-zinc-100 dark:text-zinc-200 dark:ring-zinc-700 dark:hover:bg-zinc-800',
  } as const;
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={onClick}
      disabled={disabled || busy}
      className={`min-h-12 rounded-lg px-4 py-3 text-base font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${tones[tone]}`}
    >
      {busy ? '…' : children}
    </button>
  );
}

export function RecordingControls({ taskId, kind, controlSocket, onPhase, className, compact }: RecordingControlsProps) {
  const [rec, setRec] = useState<RecordingState | null>(null);
  const [recAt, setRecAt] = useState(0); // performance.now() of the last robot report
  const [lastStateAt, setLastStateAt] = useState(0);
  const [pending, setPending] = useState<{ op: RecordingOp; req: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [askRecovered, setAskRecovered] = useState(false); // TDM-69 handback question
  const [now, setNow] = useState(() => (typeof performance !== 'undefined' ? performance.now() : 0));
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const phaseRef = useRef(onPhase);
  phaseRef.current = onPhase;
  const isTakeover = kind === 'recovery_takeover';
  const tid = BigInt(taskId);
  const [taskEpisodes] = useRows('episode', (r) => r.taskId.eq(tid));

  useEffect(() => {
    if (!controlSocket) return;
    return controlSocket.subscribe((m) => {
      if (m.t === 'state' && m.recording) {
        const t = performance.now();
        setRec(m.recording);
        setRecAt(t);
        setLastStateAt(t);
      } else if (m.t === 'record_ack') {
        const ack = m as unknown as RecordAck;
        if (ack.recording) {
          setRec(ack.recording);
          setRecAt(performance.now());
        }
        const p = pendingRef.current;
        if (p && ack.req === p.req) {
          setPending(null);
          setError(ack.ok ? null : (ack.error ?? 'refused by the robot'));
        }
      } else if (m.t === 'ack' && (m as { op?: string }).op) {
        // TDM-86: the robot's lease guard refuses an op with a plain {t:"ack", op, req, ok:false, code}
        // (no record_ack). Without this the button spun for 5 s and said "No answer from the robot".
        const a = m as unknown as { op: string; req?: string; ok?: boolean; code?: string };
        const p = pendingRef.current;
        if (p && a.ok === false && (a.req === p.req || (!a.req && a.op === p.op))) {
          setPending(null);
          setError(REFUSED_TEXT[a.code ?? ''] ?? `The robot refused it (${a.code ?? 'no reason given'}).`);
        }
      }
    });
  }, [controlSocket]);

  // smooth timer + staleness while anything is live
  useEffect(() => {
    const id = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(id);
  }, []);

  // ack timeout
  useEffect(() => {
    if (!pending) return;
    const ms = ACK_TIMEOUT_MS[pending.op] ?? 5000;
    const id = window.setTimeout(() => {
      if (pendingRef.current?.req === pending.req) {
        setPending(null);
        setError('No answer from the robot. Check the connection and try again.');
      }
    }, ms);
    return () => window.clearTimeout(id);
  }, [pending]);

  const phase: RecordingPhase = rec?.phase ?? 'idle';
  useEffect(() => {
    phaseRef.current?.(phase);
  }, [phase]);

  const send = useCallback(
    (op: RecordingOp, extra: Record<string, unknown> = {}) => {
      setError(null);
      if (!controlSocket) {
        setError('Not connected to the robot yet.');
        return;
      }
      const req = nextReq();
      if (!controlSocket.send({ t: op, req, ...extra })) {
        setError('Control connection is not open.');
        return;
      }
      setPending({ op, req });
    },
    [controlSocket],
  );

  const live = lastStateAt > 0 && now - lastStateAt < STATE_STALE_MS;
  const active = !!rec?.active;
  const elapsed = active && rec ? rec.elapsed_ms + (live ? now - recAt : 0) : 0;
  const can = rec?.can;
  // the robot says why it can't record (e.g. no working video encoder) instead of a silently grey button
  const recError = (rec as { error?: string } | null)?.error ?? null;
  const busy = (op: RecordingOp) => pending?.op === op;
  const lockOut = !live || !!pending;
  const overLong = elapsed / 1000 > REVIEW_MAX_S - 10;
  const last = rec?.last ?? null;
  const rolloutOwned = active && rec?.owner === 'rollout';
  // create_episode allows one pending/accepted episode per task: don't record a second one
  const blocking = taskEpisodes.find((e) => e.reviewStatus === 'pending' || e.reviewStatus === 'accepted');

  return (
    <section
      className={`${compact ? 'rounded-sm bg-surface px-2 py-1.5 ring-1 ring-line' : 'rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-950'} ${className ?? ''}`}
      data-testid="tdm58-recording-controls"
      data-phase={phase}
      data-compact={compact ? 'true' : undefined}
    >
      {!compact && (
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          {isTakeover ? 'Recovery takeover' : 'Teleop demo'} recording
        </h2>
        <span className="ml-auto inline-flex items-center gap-1.5 text-xs text-zinc-500">
          <span className={`inline-block h-2 w-2 rounded-full ${live ? 'bg-emerald-500' : 'bg-zinc-400'}`} />
          {live ? 'Robot connected' : controlSocket ? 'Waiting for robot state' : 'Not connected'}
        </span>
      </header>
      )}

      <div className={compact ? 'flex flex-wrap items-center gap-2' : undefined}>
      {/* status line */}
      <div className={compact ? 'flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-1' : 'mb-4 flex flex-wrap items-center gap-x-4 gap-y-2'} aria-live="polite">
        {active ? (
          <span className="inline-flex items-center gap-2 rounded-md bg-red-600/10 px-2 py-1 font-mono text-lg font-semibold tabular-nums text-red-700 dark:text-red-400">
            <span className="inline-block h-2.5 w-2.5 animate-pulse rounded-full bg-red-600" />
            REC {fmtClock(elapsed)}
          </span>
        ) : (
          <span className={`text-sm ${recError ? 'text-red-500' : 'text-zinc-500'}`}>
            {recError ?? (compact && !live ? 'Waiting for the robot' : PHASE_TEXT.idle)}
          </span>
        )}
        {active && !compact && (
          <span className="font-mono text-sm tabular-nums text-zinc-600 dark:text-zinc-300" data-testid="tdm58-frames">
            {rec?.frames ?? 0} frames
          </span>
        )}
        {active && (
          <span
            className={`rounded px-2 py-0.5 text-sm font-medium ${
              phase === 'human' || phase === 'recording'
                ? 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200'
                : 'bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200'
            }`}
          >
            {PHASE_TEXT[phase]}
          </span>
        )}
        {active && overLong && !compact && (
          <span className="text-sm font-medium text-red-600">Finish soon: episodes over {REVIEW_MAX_S} s are rejected.</span>
        )}
      </div>

      {/* takeover timeline */}
      {isTakeover && active && rec && !compact && (
        <ol className="mb-4 grid grid-cols-3 gap-2 text-sm" data-testid="tdm58-timeline">
          <TimelineCell label="Start" atMs={0} ts={rec.started_at_us} on />
          <TimelineCell label="Takeover" atMs={rec.takeover_at_ms} ts={rec.takeover_ts} on={rec.takeover_ts != null} />
          <TimelineCell label="Handback" atMs={rec.handback_at_ms} ts={rec.handback_ts} on={rec.handback_ts != null} />
        </ol>
      )}

      {/* buttons */}
      <div className="flex flex-wrap gap-2">
        {!active && (
          <Btn
            tone="rec"
            testId="tdm58-start"
            onClick={() => {
              setConfirming(false);
              send('record_start');
            }}
            disabled={lockOut || !can?.start || !!blocking}
            busy={busy('record_start')}
          >
            {isTakeover ? 'Start episode (policy drives)' : last ? 'Record again' : 'Start recording'}
          </Btn>
        )}
        {active && isTakeover && phase === 'policy' && (
          <Btn tone="take" testId="tdm58-takeover" onClick={() => send('takeover')} disabled={lockOut || !can?.takeover} busy={busy('takeover')}>
            Take over
          </Btn>
        )}
        {active && isTakeover && phase === 'human' && !askRecovered && (
          <Btn
            tone="go"
            testId="tdm58-handback"
            onClick={() => (rolloutOwned ? setAskRecovered(true) : send('handback'))}
            disabled={lockOut || !can?.handback}
            busy={busy('handback')}
          >
            Hand back to policy
          </Btn>
        )}
        {active && !rolloutOwned && (phase === 'recording' || phase === 'handed_back') && !confirming && (
          <Btn tone="go" testId="tdm58-finish" onClick={() => setConfirming(true)} disabled={lockOut || !can?.stop || elapsed < REVIEW_MIN_S * 1000}>
            Finish — success
          </Btn>
        )}
        {active && !rolloutOwned && (
          <Btn tone="quiet" testId="tdm58-discard" onClick={() => send('record_discard', { reason: 'discarded by operator' })} disabled={lockOut || !can?.discard} busy={busy('record_discard')}>
            Discard
          </Btn>
        )}
      </div>
      </div>

      {confirming && active && (
        <div className="mt-3 rounded-lg border border-emerald-600/40 bg-emerald-50 p-3 text-sm dark:bg-emerald-950/30" data-testid="tdm58-confirm">
          <p className="mb-2 font-medium">Did the task succeed? The reviewer checks the video, and failed demos are not paid.</p>
          <div className="flex flex-wrap gap-2">
            <Btn
              tone="go"
              testId="tdm58-confirm-yes"
              onClick={() => {
                setConfirming(false);
                send('record_stop', { success: true });
              }}
              disabled={lockOut}
              busy={busy('record_stop')}
            >
              Yes, submit episode
            </Btn>
            <Btn tone="quiet" onClick={() => setConfirming(false)}>
              Not yet
            </Btn>
          </div>
        </div>
      )}

      {askRecovered && active && isTakeover && phase === 'human' && (
        <div className="mt-3 rounded-lg border border-emerald-600/40 bg-emerald-50 p-3 text-sm dark:bg-emerald-950/30" data-testid="tdm58-recovered">
          <p className="mb-1 font-medium">Did you recover the task?</p>
          <p className="mb-2 text-xs text-zinc-600 dark:text-zinc-400">
            Your answer goes on the episode. The reviewer checks it against the video; a takeover that did not recover the task is not paid.
          </p>
          <div className="flex flex-wrap gap-2">
            <Btn
              tone="go"
              testId="tdm58-recovered-yes"
              onClick={() => {
                setAskRecovered(false);
                send('handback', { success: true });
              }}
              disabled={lockOut || !can?.handback}
              busy={busy('handback')}
            >
              Yes, hand back
            </Btn>
            <Btn
              tone="quiet"
              testId="tdm58-recovered-no"
              onClick={() => {
                setAskRecovered(false);
                send('handback', { success: false });
              }}
              disabled={lockOut || !can?.handback}
              busy={busy('handback')}
            >
              No, hand back
            </Btn>
            <Btn tone="quiet" onClick={() => setAskRecovered(false)}>
              Keep driving
            </Btn>
          </div>
        </div>
      )}

      {!active && blocking && (
        <p className="mt-2 text-xs text-zinc-500">
          This task already has episode #{blocking.id.toString()} ({blocking.reviewStatus.replace(/_/g, ' ')}). One paid episode per task.
        </p>
      )}

      {/* hints */}
      {!compact && rolloutOwned && (
        <p className="mt-2 text-xs text-zinc-500">
          This episode is the paused rollout&apos;s own recording. Take over, recover, hand back: the robot finishes the trial and submits it.
        </p>
      )}
      {!compact && active && isTakeover && phase === 'policy' && !can?.takeover && (
        <p className="mt-2 text-xs text-zinc-500">Let the policy run for a moment first: the reviewer needs the failure before your takeover.</p>
      )}
      {!compact && active && isTakeover && phase === 'human' && (
        <p className="mt-2 text-xs text-zinc-500">Recover the task, then hand back. A takeover must last at least 1 s.</p>
      )}
      {active && !isTakeover && elapsed < REVIEW_MIN_S * 1000 && !compact && (
        <p className="mt-2 text-xs text-zinc-500">Demos shorter than {REVIEW_MIN_S} s are rejected.</p>
      )}

      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-300" role="alert" data-testid="tdm58-error">
          {error}
        </p>
      )}

      {last && !active && <AfterSubmit last={last} taskId={taskId} />}
    </section>
  );
}

function TimelineCell({ label, atMs, ts, on }: { label: string; atMs: number | null; ts: number | null; on: boolean }) {
  return (
    <li className={`rounded-md px-2 py-1.5 ring-1 ring-inset ${on ? 'ring-zinc-300 dark:ring-zinc-700' : 'ring-zinc-200 opacity-50 dark:ring-zinc-800'}`}>
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className="font-mono tabular-nums">{on && atMs != null ? `+${(atMs / 1000).toFixed(2)} s` : '—'}</div>
      <div className="font-mono text-[11px] tabular-nums text-zinc-500" title="robot clock (same clock as the episode timestamps)">
        {on ? fmtRobotTs(ts) : ''}
      </div>
    </li>
  );
}

function AfterSubmit({ last, taskId }: { last: LastRecording; taskId: bigint | number | string }) {
  if (last.status === 'discarded') {
    return (
      <p className="mt-4 rounded-md bg-zinc-100 px-3 py-2 text-sm text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300" data-testid="tdm58-discarded">
        Discarded. Nothing was recorded or uploaded. You still hold this task, so you can record again.
      </p>
    );
  }
  if (last.status !== 'recorded') {
    return (
      <div className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-300" data-testid="tdm58-record-failed">
        The robot could not save this episode ({last.status}): {last.problems.join('; ') || 'unknown error'}. Record again.
      </div>
    );
  }
  const up = last.upload;
  const pct = up.files_total ? Math.round((100 * up.files_done) / up.files_total) : 0;
  return (
    <div className="mt-4 space-y-3 border-t border-zinc-200 pt-3 dark:border-zinc-800" data-testid="tdm58-after-submit">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">Submitted</span>
        <span className="text-zinc-500">
          {last.frame_count} frames · {(last.duration_ms / 1000).toFixed(1)} s
          {last.takeover_ts != null && last.handback_ts != null && (
            <>
              {' '}· takeover {fmtRobotTs(last.takeover_ts)} → handback {fmtRobotTs(last.handback_ts)} (
              {((last.handback_ts - last.takeover_ts) / 1e6).toFixed(2)} s)
            </>
          )}
        </span>
        {last.fixture && <MockedBadge label="SIM data" title="Recorded on the simulated arm / synthetic cameras, not the real robot." />}
      </div>

      {up.state !== 'done' && (
        <div data-testid="tdm58-upload" data-state={up.state}>
          <div className="mb-1 flex justify-between text-xs text-zinc-500">
            <span>
              {up.state === 'failed'
                ? 'Upload failed'
                : up.state === 'retrying'
                  ? `Upload retrying (attempt ${up.attempts ?? 1}${up.next_retry_in_s ? `, next in ${Math.round(up.next_retry_in_s)} s` : ''})`
                  : up.state === 'none'
                    ? 'Saved on the robot (no uploader configured)'
                    : 'Uploading to the factory'}
            </span>
            <span className="font-mono tabular-nums">
              {up.files_done}/{up.files_total} files
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded bg-zinc-200 dark:bg-zinc-800">
            <div className={`h-full transition-all ${up.state === 'failed' ? 'bg-red-500' : 'bg-sky-500'}`} style={{ width: `${pct}%` }} />
          </div>
          {up.error && <p className="mt-1 text-xs text-red-600">{up.error}</p>}
        </div>
      )}

      {up.episode_id != null ? (
        <EpisodeVerdict key={String(up.episode_id)} episodeId={BigInt(up.episode_id)} />
      ) : (
        up.state === 'done' && last.key && <EpisodeByKey taskId={taskId} recKey={last.key} />
      )}
    </div>
  );
}

/** Fallback when the robot's upload state has no episode id: find the row by its storage key. */
function EpisodeByKey({ taskId, recKey }: { taskId: bigint | number | string; recKey: string }) {
  const tid = BigInt(taskId);
  const [eps] = useRows('episode', (r) => r.taskId.eq(tid));
  const ep = eps.find((e) => e.storageUri.endsWith(`/${recKey}`));
  if (!ep) return <p className="text-sm text-zinc-500">Waiting for the episode row…</p>;
  return <EpisodeVerdict key={ep.id.toString()} episodeId={ep.id} />;
}

function EpisodeVerdict({ episodeId }: { episodeId: bigint }) {
  const [eps] = useRows('episode', (r) => r.id.eq(episodeId));
  const [reviews] = useRows('review', (r) => r.episodeId.eq(episodeId));
  const [payments] = useRows('payment', (r) => r.episodeId.eq(episodeId));
  const [qa] = useRows('qaResult', (r) => r.episodeId.eq(episodeId));
  const ep = eps[0];
  const review = [...reviews].sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch))[0];
  const pay = payments[0];
  const failed = qa.filter((q) => !q.passed).map((q) => q.checkName);

  if (!ep) return <p className="text-sm text-zinc-500">Episode #{episodeId.toString()} registered; syncing…</p>;
  const status = review ? (review.accepted ? 'accepted' : 'rejected') : ep.reviewStatus;
  return (
    <div className="rounded-lg bg-zinc-50 p-3 text-sm dark:bg-zinc-900" data-testid="tdm58-verdict" data-status={status}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">Episode #{ep.id.toString()}</span>
        <span className="text-zinc-500">{ep.kind.replace(/_/g, ' ')}</span>
        <StatusPill status={status} />
      </div>
      {status === 'pending' && (
        <p className="mt-2 inline-flex items-center gap-2 text-zinc-600 dark:text-zinc-300">
          <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-zinc-400 border-t-transparent" />
          The reviewer is checking it: length, frame drops, smoothness{ep.kind === 'recovery_takeover' ? ', takeover timestamps' : ''} and success.
        </p>
      )}
      {review && review.accepted && (
        <p className="mt-2 text-emerald-700 dark:text-emerald-400" data-testid="tdm58-accepted">
          {/^accepted/i.test(review.reason) ? review.reason : `Accepted.${review.reason ? ` ${review.reason}` : ''}`}
        </p>
      )}
      {review && !review.accepted && (
        <div className="mt-2 text-red-700 dark:text-red-400" data-testid="tdm58-rejected">
          <p>Rejected: {review.reason || 'no reason given'}</p>
          {failed.length > 0 && <p className="mt-1 text-xs">Failed checks: {failed.join(', ')}</p>}
          <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">Rejected episodes are not paid. You can record again while you hold the task.</p>
        </div>
      )}
      {review && review.accepted && (
        <p className="mt-2 flex flex-wrap items-center gap-2" data-testid="tdm58-payout">
          Payout{' '}
          {pay ? (
            <>
              {fmtCents(pay.amountCents)} <StatusPill status={pay.status} />
              {pay.error && <span className="text-xs text-zinc-500">{pay.error}</span>}
            </>
          ) : (
            <span className="text-zinc-500">being reserved…</span>
          )}
        </p>
      )}
    </div>
  );
}
