'use client';

// TDM-101: the /live projector kiosk, in the GeonosisAI brand (docs/brand/project, Foundry theme).
// Composes, never forks:
//   * TDM-100 <Tdm100RobotPanel/>: RobotView (TDM-98) with the Sim | Live switch, the TDM-99 HUD and
//     TDM-105's sim cameras, all on one state feed
//   * the ticket's rows, live from SpacetimeDB: the line (ticket.status + event_log
//     ticket.status_changed), eval runs and trials (Wilson CI as finalize_eval_run stored it), the
//     planner's data_plan and failure clusters, episodes, payments, GPU cost
//   * TDM-35 useAgentFeed for the agents ticker, with the resume bridge's wake-ups highlighted
//   * TDM-60 <JoinQr/> for judges' phones
// Nothing here is invented: an empty step says what will appear there. Sim stays labelled (RobotView
// does the view; the numbers carry a "Simulated robot" tag when the ticket's robot is the sim).
//
// Brand rules kept here: plates (surface, 1px line, 2px radius, no shadow); square rivets; melt only
// on the one running station of the line; ochre for money; arc for pass / live / shipped; slag for
// failure, always with a word. Sizes are the brand scale enlarged for a projector across a room.
// Colours are the Foundry values of tokens.json, set as --color-<token> on the kiosk root so the
// page reads the same names TDM-109 declares app-wide (and stays Foundry on a light-mode laptop).
// TODO(TDM-109): swap the local Line / plate / readout markup for src/components/brand once it lands.
//
// Keys: S / L click RobotView's own Sim / Live switch, 1-3 its 3D camera presets (so the view keeps
// owning that state and remembering it), F toggles full screen.

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { JoinQr } from '@/components/JoinQr';
import { TestModeTag } from '@/components/payments/TestModeBand';
import { Tdm100RobotPanel } from '@/components/robotview/Tdm100RobotPanel';
import { byNewest, isFakeRobot, ticketEvents, type TicketEvent } from '@/components/ticket/util';
import { useAgentFeed } from '@/lib/agentfeed/useAgentFeed';
import { fmtCents } from '@/lib/format';
import { classifyRobot } from '@/lib/robotview/sources';
import { useConnectionStatus, useRows } from '@/lib/spacetime/hooks';
import {
  TDM101_STATIONS,
  tdm101EvalPair,
  tdm101KeyAction,
  tdm101LineStates,
  tdm101OneLine,
  tdm101ParseTicketParam,
  tdm101PickTicket,
  tdm101Ticker,
  type Tdm101Rate,
  type Tdm101StationKey,
  type Tdm101StationState,
  type Tdm101TickerItem,
} from '@/lib/tdm101/live';

// ------------------------------------------------------------------------------ tokens (Foundry)

const FOUNDRY = {
  '--color-ground': '#13110f',
  '--color-surface': '#1c1916',
  '--color-surface-sunken': '#0d0b0a',
  '--color-line': '#3b332d',
  '--color-line-strong': '#7a6b5f',
  '--color-ink': '#f1e9e1',
  '--color-ink-muted': '#b3a59a',
  '--color-melt': '#ff7a1f',
  '--color-melt-soft': '#3b1d0a',
  '--color-on-melt': '#1a0d04',
  '--color-ochre': '#d39457',
  '--color-ochre-soft': '#33220f',
  '--color-arc': '#79c7ff',
  '--color-arc-soft': '#0e2738',
  '--color-on-arc': '#04131d',
  '--color-slag': '#ff6d66',
  '--color-slag-soft': '#3a1513',
  '--color-steel': '#8f9ca5',
  '--color-steel-soft': '#222a2f',
  colorScheme: 'dark',
} as CSSProperties;

const F_STENCIL = 'font-[family-name:var(--font-stencil)]';
const F_DISPLAY = 'font-[family-name:var(--font-display)]';
const F_SANS = 'font-[family-name:var(--font-sans)]';
const F_MONO = 'font-[family-name:var(--font-mono)] tabular-nums';
const STATION = `${F_DISPLAY} font-bold uppercase tracking-[0.12em]`;
const PLATE = 'rounded-[2px] border border-[var(--color-line)] bg-[var(--color-surface)]';
const SIM_TAPE = 'repeating-linear-gradient(135deg, rgb(139 92 246 / 0.95) 0 6px, rgb(109 40 217 / 0.95) 6px 12px)';
// The running rivet pulses once every 2 s (brand gx-pulse), off under prefers-reduced-motion.
const KEYFRAMES =
  '@keyframes tdm101-pulse{0%,70%,100%{box-shadow:0 0 0 6px var(--color-melt-soft)}85%{box-shadow:0 0 0 11px var(--color-melt-soft)}}' +
  '@keyframes tdm101-wake{0%{box-shadow:0 0 0 0 rgba(121,199,255,.55)}100%{box-shadow:0 0 0 16px rgba(121,199,255,0)}}';

const pct = (x: number) => Math.floor(x * 100 + 0.5);
const DATA_KINDS = new Set(['clean_teleop', 'recovery_takeover', 'purchased']);
const EARNED = new Set(['paid', 'pending', 'awaiting_onboarding']);
const BUY_NAME: Record<string, [one: string, many: string]> = {
  recovery_takeover: ['recovery takeover', 'recovery takeovers'],
  clean_teleop: ['clean demo', 'clean demos'],
  purchased: ['purchased episode', 'purchased episodes'],
};
const buyName = (type: string, n: number) => {
  const b = BUY_NAME[type];
  return b ? b[n === 1 ? 0 : 1] : type.replace(/_/g, ' ');
};
const SHORT_BUY: Record<string, string> = { recovery_takeover: 'takeovers', clean_teleop: 'clean demos', purchased: 'purchased' };

/** Wall clock, 0 until mounted so the server render and the first client render agree. */
function useNow(ms: number) {
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

function clock(us: number) {
  return new Date(us / 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
}
function hm(us: number) {
  return new Date(us / 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
}
function since(us: number, now: number) {
  const s = Math.max(0, Math.round(now / 1000 - us / 1e6));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}

// ------------------------------------------------------------------------------ pieces

function Rivet({ className = '' }: { className?: string }) {
  return <span aria-hidden className={`inline-block h-[7px] w-[7px] shrink-0 bg-current ${className}`} />;
}

function Plate({ title, aside, children, className = '' }: { title: string; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`${PLATE} px-5 py-4 ${className}`} aria-label={title}>
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className={`${F_DISPLAY} text-[26px] font-bold leading-none tracking-[0.01em]`}>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function SimTag() {
  return (
    <span
      className={`${STATION} inline-flex h-7 items-center rounded-[2px] px-2.5 text-[15px] text-white`}
      style={{ backgroundImage: SIM_TAPE }}
      title="These results come from the simulated robot, not real hardware."
    >
      Simulated robot
    </span>
  );
}

function FakeTag() {
  return (
    <span className={`${STATION} inline-flex h-7 items-center rounded-[2px] bg-fuchsia-600 px-2.5 text-[15px] text-white`} title="Produced by the fake robot harness.">
      Fake robot
    </span>
  );
}

type Station = { key: Tdm101StationKey; label: string; state: Tdm101StationState; note: string };

/** The brand's Line (docs/brand/project/components/Line): stations as square rivets on a track. */
function Line({ stations }: { stations: Station[] }) {
  return (
    <ol className="grid" style={{ gridTemplateColumns: `repeat(${stations.length}, minmax(0, 1fr))` }} aria-label="Ticket progress">
      {stations.map((s, i) => {
        const reached = s.state !== 'upcoming';
        const rivet =
          s.state === 'current'
            ? 'border-[var(--color-melt)] bg-[var(--color-melt)] motion-safe:animate-[tdm101-pulse_2s_ease-out_infinite] shadow-[0_0_0_6px_var(--color-melt-soft)]'
            : s.state === 'shipped'
              ? 'border-[var(--color-arc)] bg-[var(--color-arc)]'
              : s.state === 'failed'
                ? 'border-[var(--color-slag)] bg-[var(--color-slag)]'
                : s.state === 'done'
                  ? 'border-[var(--color-ink)] bg-[var(--color-ink)]'
                  : 'border-[var(--color-steel)] bg-[var(--color-ground)]';
        const name =
          s.state === 'current'
            ? 'text-[var(--color-melt)]'
            : s.state === 'shipped'
              ? 'text-[var(--color-arc)]'
              : s.state === 'failed'
                ? 'text-[var(--color-slag)]'
                : s.state === 'done'
                  ? 'text-[var(--color-ink)]'
                  : 'text-[var(--color-ink-muted)]';
        const track = reached ? 'bg-[var(--color-ink-muted)]' : 'bg-[var(--color-line)]';
        return (
          <li key={s.key} className="relative min-w-0 pt-[30px]" aria-current={s.state === 'current' ? 'step' : undefined}>
            <span
              aria-hidden
              className={`absolute top-[7px] h-[3px] ${track} ${i === 0 ? 'left-2' : 'left-0'} ${i === stations.length - 1 ? 'w-2' : 'right-0'}`}
            />
            <span aria-hidden className={`absolute left-0 top-0 h-[17px] w-[17px] border-[3px] ${rivet}`} />
            <span className={`${STATION} block truncate pr-3 text-[21px] leading-[24px] ${name}`}>
              {s.label}
              {s.state === 'failed' && <span className="sr-only"> (failed here)</span>}
            </span>
            <span className={`${F_MONO} mt-1 block truncate pr-3 text-[17px] leading-[22px] ${s.state === 'failed' ? 'text-[var(--color-slag)]' : 'text-[var(--color-ink-muted)]'}`} title={s.note}>
              {s.note || ' '}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** One eval run's trial chips: Pass in arc, Fail in slag (the words are in each chip's label). */
function TrialChips({ runId, trials }: { runId: bigint; trials: number }) {
  const [rows] = useRows('evalTrial', (r) => r.evalRunId.eq(runId));
  const byIdx = new Map(rows.map((t) => [t.trialIndex, t]));
  const seeded = rows.some((t) => (t.failureNote ?? '').startsWith('[SEEDED]')); // RDF_SEED_EVALS: drawn, not run
  return (
    <>
    {seeded && (
      <p className={`${F_MONO} mb-1 text-[12px] text-[var(--color-slag)]`} title="RDF_SEED_EVALS demo run-through: outcomes drawn at the sim policy's calibrated rate, no robot trials">
        SEEDED · not run
      </p>
    )}
    <ol className="flex flex-wrap gap-[4px]" aria-label="Trials">
      {Array.from({ length: trials }, (_, i) => {
        const t = byIdx.get(i);
        const cls = !t
          ? 'border border-[var(--color-line-strong)] text-[var(--color-ink-muted)]'
          : t.success
            ? 'bg-[var(--color-arc-soft)] text-[var(--color-arc)]'
            : 'bg-[var(--color-slag-soft)] text-[var(--color-slag)]';
        const word = !t ? 'not run yet' : t.success ? 'Pass' : 'Fail';
        return (
          <li key={i} className={`${F_MONO} grid h-[22px] w-[22px] place-items-center text-[12px] font-medium ${cls}`} aria-label={`Trial ${i + 1}: ${word}`} title={`Trial ${i + 1}: ${word}`}>
            {t ? (t.success ? 'P' : 'F') : i + 1}
          </li>
        );
      })}
    </ol>
    </>
  );
}

/** Brand Readout: rate with its interval and n, the 0-100 track, the target tick, the trials. */
function Readout({ label, r, target, empty }: { label: string; r: Tdm101Rate | null; target: number | null; empty: string }) {
  if (!r) {
    return (
      <div className="py-2.5">
        <p className={`${F_SANS} text-[15px] font-medium tracking-[0.04em] text-[var(--color-ink-muted)]`}>{label}</p>
        <p className="mt-1 text-[17px] text-[var(--color-ink-muted)]">{empty}</p>
      </div>
    );
  }
  const pass = r.done && r.rate !== null && target !== null && r.rate + 1e-9 >= target;
  return (
    <div className="grid gap-2 py-2">
      <div className="flex items-baseline justify-between gap-3">
        <p className={`${F_SANS} text-[15px] font-medium tracking-[0.04em] text-[var(--color-ink-muted)]`}>{label}</p>
        <p className={`${F_MONO} text-[16px] text-[var(--color-ink-muted)]`}>
          {r.done ? (pass ? <span className="text-[var(--color-arc)]">Pass, meets target</span> : target !== null ? 'Below target' : '') : `trial ${Math.min(r.run.completed + 1, r.run.trials)} of ${r.run.trials} running`}
        </p>
      </div>
      <p className={`${F_MONO} text-[40px] font-medium leading-none`}>
        {r.rate === null ? '–' : `${pct(r.rate)}%`}
        <small className="ml-3 text-[19px] font-normal text-[var(--color-ink-muted)]">
          {r.done && r.ci ? `(${pct(r.ci.lo)}–${pct(r.ci.hi)}%, n=${r.n})` : `${r.run.successes}/${r.run.completed} so far`}
        </small>
      </p>
      <div className="relative h-[16px] bg-[var(--color-surface-sunken)]" aria-hidden>
        {r.ci && (
          <div
            className={`absolute bottom-[4px] top-[4px] ${pass ? 'bg-[var(--color-arc)] opacity-45' : 'bg-[var(--color-steel)] opacity-55'}`}
            style={{ left: `${r.ci.lo * 100}%`, width: `${Math.max(0.6, (r.ci.hi - r.ci.lo) * 100)}%` }}
          />
        )}
        {target !== null && <div className="absolute -bottom-1 -top-1 w-0 border-l-2 border-dashed border-[var(--color-ink-muted)]" style={{ left: `${target * 100}%` }} />}
        {r.rate !== null && <div className={`absolute bottom-0 top-0 -ml-[2px] w-[4px] bg-[var(--color-ink)] ${r.done ? '' : 'opacity-50'}`} style={{ left: `${r.rate * 100}%` }} />}
      </div>
      <TrialChips runId={r.run.id} trials={r.run.trials} />
    </div>
  );
}

function TickerRow({ it, now }: { it: Tdm101TickerItem; now: number }) {
  const fresh = now > 0 && now * 1000 - it.tsUs < 60_000_000;
  if (it.wake) {
    return (
      <li
        className={`rounded-[2px] border border-[var(--color-arc)]/60 bg-[var(--color-arc-soft)] px-3 py-2 ${fresh ? 'motion-safe:animate-[tdm101-wake_1.8s_ease-out_2]' : ''}`}
      >
        <div className="flex items-center gap-2 text-[var(--color-arc)]">
          <Rivet />
          <span className={`${STATION} text-[17px]`}>Woken by SpacetimeDB</span>
          {fresh && <span className={`${STATION} rounded-[2px] bg-[var(--color-arc)] px-1.5 text-[13px] text-[var(--color-on-arc)]`}>just now</span>}
          <span className={`${F_MONO} ml-auto text-[15px] text-[var(--color-ink-muted)]`}>{clock(it.tsUs)}</span>
        </div>
        <p className="mt-1 line-clamp-2 text-[17px] leading-snug text-[var(--color-ink)]">
          <b className="font-semibold">{it.agent}</b> resumed when{' '}
          <code className={`${F_MONO} bg-[var(--color-surface-sunken)] px-1 text-[15px]`}>{it.wake.predicate || 'its condition'}</code> came true
        </p>
      </li>
    );
  }
  return (
    <li className="grid grid-cols-[5.2rem_minmax(0,1fr)] gap-x-3 border-b border-[var(--color-line)] py-1.5 last:border-b-0">
      <span className={`${F_MONO} pt-0.5 text-[15px] text-[var(--color-ink-muted)]`}>{clock(it.tsUs)}</span>
      <p className={`line-clamp-2 text-[17px] leading-snug ${it.isError ? 'text-[var(--color-slag)]' : 'text-[var(--color-ink)]'}`}>
        <b className="font-semibold">{it.agent}</b>{' '}
        {it.isError && <span className={`${STATION} mr-1 text-[14px]`}>Error</span>}
        <span className={it.isError ? '' : 'text-[var(--color-ink-muted)]'}>{it.text}</span>
      </p>
    </li>
  );
}

function Kbd({ k, label }: { k: string; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <kbd className={`${F_MONO} min-w-[1.7rem] rounded-[2px] border border-[var(--color-line-strong)] px-1.5 py-0.5 text-center text-[14px] font-medium text-[var(--color-ink)]`}>{k}</kbd>
      {label && <span>{label}</span>}
    </span>
  );
}

// ------------------------------------------------------------------------------ the page

export function LiveKiosk({ ticketParam }: { ticketParam: string | null }) {
  // Everything on this page is live client data (subscriptions, the wall clock, the viewer's locale),
  // so it renders after mount only; the server sends the bare Foundry ground.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return <div style={FOUNDRY} className="fixed inset-0 z-[100] bg-[var(--color-ground)]" aria-busy="true" />;
  return <LiveKioskBody ticketParam={ticketParam} />;
}

function LiveKioskBody({ ticketParam }: { ticketParam: string | null }) {
  const pinned = useMemo(() => tdm101ParseTicketParam(ticketParam), [ticketParam]);
  const [tickets, ticketsReady] = useRows('ticket');
  const { ticket, how } = useMemo(() => tdm101PickTicket(tickets, pinned), [tickets, pinned]);
  const tid = ticket?.id ?? 0n;
  const robotId = ticket?.robotId ?? 0n;

  const [allRobots] = useRows('robot');
  const [evalRuns] = useRows('evalRun', (r) => r.ticketId.eq(tid));
  const [plans] = useRows('dataPlan', (r) => r.ticketId.eq(tid));
  const [clusters] = useRows('failureCluster', (r) => r.ticketId.eq(tid));
  const [episodes] = useRows('episode', (r) => r.ticketId.eq(tid));
  const [payments] = useRows('payment', (r) => r.ticketId.eq(tid));
  const [trainingRuns] = useRows('trainingRun', (r) => r.ticketId.eq(tid));
  const [settingsRows] = useRows('runSettings', (r) => r.ticketId.eq(tid));
  const [eventRows] = useRows('eventLog');
  const { status: conn } = useConnectionStatus();
  const now = useNow(1000);

  const events = useMemo(() => ticketEvents(eventRows, tid), [eventRows, tid]);
  const robot = allRobots.find((r) => r.id === robotId);
  const robotSim = robot ? classifyRobot(robot).kind === 'sim' : false;
  const robotFake = isFakeRobot(robot);
  const settings = settingsRows[0];
  const target = settings?.successThreshold ?? (ticket ? ticket.targetSuccess : null);
  // Never pool sim with real (TDM-107): before/after uses only evals run on a robot of the ticket's
  // current kind, each run judged by the robot it ran on.
  const modeRuns = useMemo(() => {
    if (!robot) return evalRuns;
    const kind = classifyRobot(robot).kind;
    const byId = new Map(allRobots.map((r) => [r.id, r]));
    return evalRuns.filter((run) => {
      const r = byId.get(run.robotId);
      return r ? classifyRobot(r).kind === kind : false;
    });
  }, [evalRuns, allRobots, robot]);
  const pair = useMemo(() => tdm101EvalPair(modeRuns), [modeRuns]);
  const plan = useMemo(() => byNewest(plans)[0], [plans]);
  const iteration = ticket?.iteration ?? 0;
  const iterClusters = useMemo(() => {
    const it = plan ? plan.iteration : iteration;
    return [...clusters].filter((c) => c.iteration === it).sort((a, b) => b.count - a.count);
  }, [clusters, plan, iteration]);

  const bought = useMemo(() => {
    const data = episodes.filter((e) => DATA_KINDS.has(e.kind));
    const accepted = data.filter((e) => e.reviewStatus === 'accepted').length;
    const reviewed = data.filter((e) => e.reviewStatus === 'accepted' || e.reviewStatus === 'rejected').length;
    const paidOut = payments.filter((p) => EARNED.has(p.status)).reduce((n, p) => n + Number(p.amountCents), 0);
    const gpu = trainingRuns.reduce((n, r) => n + Number(r.gpuCostCents), 0);
    return { total: data.length, accepted, reviewed, paidOut, gpu };
  }, [episodes, payments, trainingRuns]);

  // ---- the line: each station's state and a one-line note of real numbers
  const stations = useMemo<Station[]>(() => {
    if (!ticket) return [];
    const entered = new Map<string, number>();
    let stoppedAt: string | null = null;
    let endNote = '';
    for (const { row, payload } of events) {
      if (row.type !== 'ticket.status_changed') continue;
      const to = String(payload.to ?? '');
      entered.set(to, Number(row.ts.microsSinceUnixEpoch));
      if (to === 'failed' || to === 'cancelled') {
        stoppedAt = String(payload.from ?? '');
        endNote = typeof payload.note === 'string' ? payload.note : '';
      }
    }
    const states = tdm101LineStates(ticket.status, stoppedAt);
    const latestTrain = byNewest(trainingRuns)[0];
    let steps = 0;
    try {
      steps = Number(JSON.parse(latestTrain?.hyperparamsJson || settings?.hyperparamsJson || '{}').steps) || 0;
    } catch {
      steps = 0;
    }
    const rateNote = (r: Tdm101Rate | null) => (!r ? '' : r.done ? `${r.run.successes}/${r.run.trials}` : `${r.run.successes}/${r.run.completed} so far`);
    const notes: Record<Tdm101StationKey, string> = {
      queued: hm(Number(ticket.createdAt.microsSinceUnixEpoch)),
      baseline: rateNote(pair.baseline),
      diagnose: iterClusters.length ? `${iterClusters.length} cluster${iterClusters.length === 1 ? '' : 's'}` : '',
      plan_data: plan ? `${plan.amount} ${SHORT_BUY[plan.dataType] ?? plan.dataType.replace(/_/g, ' ')}` : '',
      collect: plan ? `${bought.total}/${plan.amount}` : bought.total ? String(bought.total) : '',
      review: bought.reviewed ? `${bought.accepted} accepted` : '',
      train: latestTrain
        ? (latestTrain.modalCallId?.startsWith('stub-') || (latestTrain.checkpointUri ?? '').includes('stub-no-gpu')
          ? `[STUB] no GPU, ${latestTrain.status}` // RDF_MODAL_STUB: no Modal credit spent, fake checkpoint
          : latestTrain.status === 'succeeded' || latestTrain.status === 'done' ? `${steps ? `${steps.toLocaleString('en-US')} steps` : 'done'}` : latestTrain.status)
        : '',
      eval: rateNote(pair.current),
      shipped: target !== null ? `target ${pct(target)}%` : '',
    };
    return TDM101_STATIONS.map((s, i) => {
      const state = states[i];
      let note = notes[s.key];
      if (state === 'current' && !note) {
        const at = s.key === 'queued' ? Number(ticket.createdAt.microsSinceUnixEpoch) : entered.get(s.statuses[0]);
        if (at && now) note = `for ${since(at, now)}`;
      }
      if (state === 'failed') note = endNote || 'stopped here';
      if (state === 'cancelled') note = endNote || 'cancelled here';
      if (state === 'upcoming' && s.key !== 'shipped') note = '';
      return { key: s.key, label: s.label, state, note };
    });
  }, [ticket, events, trainingRuns, settings, pair, iterClusters, plan, bought, target, now]);

  const { feed, ready: feedReady } = useAgentFeed({ ticketId: ticket ? Number(tid) : null, limit: 24 });
  const ticker = useMemo(() => tdm101Ticker(feed.agents, 14), [feed.agents]);
  const wakes = useMemo(
    () => feed.agents.reduce((n, a) => n + a.events.filter((e) => e.type === 'agent.resumed' && String(e.data?.reason ?? '').startsWith('predicate true:')).length, 0),
    [feed.agents],
  );

  // ---- keyboard: drive RobotView's own controls so it keeps owning (and remembering) its state
  const robotArea = useRef<HTMLDivElement>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [isFull, setIsFull] = useState(false);
  useEffect(() => {
    const t = flash ? setTimeout(() => setFlash(null), 1400) : undefined;
    return () => clearTimeout(t);
  }, [flash]);
  useEffect(() => {
    const onFs = () => setIsFull(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', onFs);
    return () => document.removeEventListener('fullscreenchange', onFs);
  }, []);
  const toggleFullscreen = useCallback(() => {
    try {
      if (document.fullscreenElement) void document.exitFullscreen().catch(() => {});
      else void document.documentElement.requestFullscreen?.().catch(() => setFlash('The browser refused full screen'));
    } catch {
      setFlash('Full screen is not available here');
    }
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
      const act = tdm101KeyAction(e);
      if (!act) return;
      const root = robotArea.current;
      if (act.kind === 'fullscreen') {
        e.preventDefault();
        toggleFullscreen();
      } else if (act.kind === 'mode') {
        const radios = root?.querySelectorAll<HTMLButtonElement>('[role="radiogroup"][aria-label="Robot source"] [role="radio"]');
        const btn = radios?.[act.mode === 'sim' ? 0 : 1];
        if (btn) {
          e.preventDefault();
          btn.click();
          setFlash(act.mode === 'sim' ? 'Sim robot' : 'Live robot');
        }
      } else {
        const btns = root?.querySelector('[role="group"][aria-label="3D camera"]')?.querySelectorAll<HTMLButtonElement>('button');
        const btn = btns?.[act.index];
        if (btn) {
          e.preventDefault();
          btn.click();
          setFlash(`${btn.textContent?.trim() || `View ${act.index + 1}`} view`);
        } else setFlash('No 3D view on screen');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggleFullscreen]);

  // The kiosk covers the app chrome; stop the page under it from scrolling.
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  const follow =
    how === 'pinned' ? `Pinned to ticket ${String(pinned)}` : how === 'active' ? 'Following the newest active ticket' : how === 'latest' ? 'Newest ticket, none active' : 'Waiting for a ticket';
  const maxIter = settings?.maxIterations ?? 3;
  const spent = bought.paidOut + bought.gpu;
  const budget = ticket ? Number(ticket.budgetCents) : 0;
  const baselineFailed = pair.baseline?.done ? pair.baseline.run.trials - pair.baseline.run.successes : null;

  return (
    <div
      style={FOUNDRY}
      className={`fixed inset-0 z-[100] flex flex-col gap-4 overflow-hidden bg-[var(--color-ground)] px-8 pb-3 pt-5 ${F_SANS} text-[var(--color-ink)] antialiased`}
      data-ticket={ticket ? String(ticket.id) : ''}
    >
      <style>{KEYFRAMES}</style>

      {/* ---- title block + the line, and the join QR for judges' phones */}
      <div className="flex items-stretch gap-6">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <header className="flex items-end gap-6">
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-4 text-[17px] text-[var(--color-ink-muted)]">
                <span className={`${F_STENCIL} text-[26px] font-extrabold uppercase leading-none tracking-[0.02em] text-[var(--color-ink)]`}>GeonosisAI</span>
                {ticket && <span className={`${F_MONO} text-[17px] font-medium tracking-[0.02em]`}>TKT-{String(ticket.id).padStart(4, '0')}</span>}
                {ticket && iteration > 0 && (
                  <span className={`${F_MONO} text-[17px]`}>
                    Iteration {iteration + 1} of {maxIter}
                  </span>
                )}
                {robot && <span className={`${F_MONO} text-[17px]`}>{robot.name}</span>}
              </p>
              <h1 className={`${F_DISPLAY} mt-1 truncate text-[50px] font-extrabold leading-[1.05]`} title={ticket?.taskDescription}>
                {ticket
                  ? ticket.taskDescription || 'Untitled ticket'
                  : ticketsReady
                    ? pinned
                      ? `No ticket ${String(pinned)} yet`
                      : 'No tickets yet'
                    : 'Connecting to the foundry'}
              </h1>
            </div>
            <div className="shrink-0 text-right text-[16px] leading-relaxed text-[var(--color-ink-muted)]">
              <p className={`flex items-center justify-end gap-2 ${F_MONO} text-[15px] ${conn === 'connected' ? 'text-[var(--color-arc)]' : conn === 'connecting' ? 'text-[var(--color-steel)]' : 'text-[var(--color-slag)]'}`}>
                <Rivet />
                {conn === 'connected' ? 'Live from SpacetimeDB' : conn === 'connecting' ? 'Connecting to SpacetimeDB' : 'SpacetimeDB disconnected, retrying'}
              </p>
              <p>{follow}</p>
            </div>
          </header>

          <div className={`${PLATE} px-6 pb-3 pt-4`}>
            {ticket ? (
              <Line stations={stations} />
            ) : (
              <p className="py-2 text-[20px] text-[var(--color-ink-muted)]">
                {ticketsReady ? 'File a ticket at /tickets/new or from the Claude connector to put a robot on the line. This page picks it up as soon as it exists.' : 'Waiting for the ticket list.'}
              </p>
            )}
          </div>
        </div>
        {/* JoinQr's link text is too small to read from the room, so it is hidden; the caption and the
            access code stay, and its localhost warning (gone under make demo-public) shrinks to one line. */}
        <div className="w-[14rem] shrink-0 [&>div]:h-full [&>div]:w-full [&>div]:justify-center [&>div]:gap-1.5 [&>div]:rounded-[2px] [&>div]:py-2 [&_.text-amber-700]:line-clamp-1 [&_a]:hidden">
          <JoinQr ticketId={ticket ? String(ticket.id) : undefined} size={118} />
        </div>
      </div>

      {/* ---- the robot, then the story on the right */}
      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_34rem] gap-6">
        <div ref={robotArea} className="min-h-0 min-w-0 overflow-y-auto overflow-x-hidden pr-1 [scrollbar-width:thin]">
          <Tdm100RobotPanel robotId={robotId} robot={robot} ticketId={ticket ? tid : null} />
        </div>

        <div className="flex min-h-0 flex-col gap-4">
          <Plate
            title="Before and after"
            aside={
              <span className="flex items-center gap-2">
                {robotFake && <FakeTag />}
                {robotSim && !robotFake && <SimTag />}
              </span>
            }
          >
            <div className="divide-y divide-[var(--color-line)]">
              <Readout label="Baseline, base π0.5" r={pair.baseline} target={target} empty="Runs first, with the base π0.5 policy." />
              <Readout
                label={pair.current?.run.kind === 'ab_live' ? 'Live A/B' : 'Fine-tuned'}
                r={pair.current}
                target={target}
                empty="Runs after training on the bought data."
              />
            </div>
            {target !== null && (
              <p className="mt-1 flex items-center gap-2 text-[15px] text-[var(--color-ink-muted)]">
                <span aria-hidden className="inline-block h-4 w-0 border-l-2 border-dashed border-[var(--color-ink-muted)]" />
                target {pct(target)}%; band is the 95% Wilson interval
              </p>
            )}
          </Plate>

          <Plate title="Planner decision" aside={<TestModeTag />}>
            {plan ? (
              <div className="grid gap-2.5">
                {/* 1. what failed */}
                <div className="text-[17px] leading-snug text-[var(--color-ink-muted)]">
                  {baselineFailed !== null && pair.baseline && (
                    <p>
                      <span className={F_MONO}>
                        {baselineFailed}/{pair.baseline.run.trials}
                      </span>{' '}
                      baseline trials failed{iterClusters.length ? ':' : '.'}
                    </p>
                  )}
                  {iterClusters.length > 0 && (
                    <ul className="mt-1 grid gap-0.5">
                      {iterClusters.slice(0, 2).map((c) => (
                        <li key={String(c.id)} className="flex items-center gap-2">
                          <Rivet className="text-[var(--color-slag)]" />
                          <span className="truncate text-[var(--color-ink)]">{c.label}</span>
                          <span className={`${F_MONO} ml-auto shrink-0`}>×{c.count}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                {/* 2. what we are buying, and the planner's own first sentence */}
                <p className="text-[21px] font-semibold leading-snug">
                  Buy {plan.amount} {buyName(plan.dataType, plan.amount)}.
                </p>
                {plan.reasoning && (
                  <p className="line-clamp-1 text-[17px] leading-snug text-[var(--color-ink-muted)]" title={plan.reasoning}>
                    “{tdm101OneLine(plan.reasoning)}”
                  </p>
                )}
                {/* 3. what it cost */}
                <div className="grid gap-1.5 pt-1">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className={`${F_MONO} text-[26px] font-medium leading-none text-[var(--color-ochre)]`}>
                      {fmtCents(spent)}
                      <small className="ml-2 text-[17px] font-normal text-[var(--color-ink-muted)]">of {fmtCents(budget)}</small>
                    </p>
                    <p className="text-right text-[15px] text-[var(--color-ink-muted)]">
                      <span className={F_MONO}>{fmtCents(bought.paidOut)}</span> to contributors, <span className={F_MONO}>{fmtCents(bought.gpu)}</span> GPU
                    </p>
                  </div>
                  <div className="relative h-[10px] bg-[var(--color-surface-sunken)]" aria-label={`Spent ${fmtCents(spent)} of ${fmtCents(budget)} budget`}>
                    <span className="absolute inset-y-0 left-0 bg-[var(--color-ochre)]" style={{ width: `${budget ? Math.min(100, (spent / budget) * 100) : 0}%` }} />
                  </div>
                  <p className="text-[15px] text-[var(--color-ink-muted)]">
                    <span className={F_MONO}>
                      {bought.accepted}/{plan.amount}
                    </span>{' '}
                    episodes accepted.{' '}
                    {plan.status === 'proposed' ? `Waiting for spend approval (est. ${fmtCents(plan.estCostCents)}).` : `Approved ${fmtCents(plan.approvedCents || plan.estCostCents)}.`}
                  </p>
                </div>
              </div>
            ) : (
              <p className="text-[17px] text-[var(--color-ink-muted)]">After the baseline, the planner works out why the robot fails and picks the data that fixes it.</p>
            )}
          </Plate>

          <Plate
            title="Agents"
            className="flex min-h-0 flex-1 flex-col"
            aside={
              wakes > 0 ? (
                <span className={`flex items-center gap-2 ${F_MONO} text-[15px] text-[var(--color-arc)]`}>
                  <Rivet />
                  {wakes} {wakes === 1 ? 'wake-up' : 'wake-ups'} from SpacetimeDB
                </span>
              ) : undefined
            }
          >
            {ticker.length ? (
              <ol className="min-h-0 flex-1 space-y-1 overflow-hidden" aria-live="polite">
                {ticker.map((it) => (
                  <TickerRow key={it.id} it={it} now={now} />
                ))}
              </ol>
            ) : (
              <p className="text-[17px] text-[var(--color-ink-muted)]">{feedReady ? 'No agent has worked on this ticket yet.' : 'Connecting to the agent feed.'}</p>
            )}
          </Plate>
        </div>
      </div>

      {/* ---- keys */}
      <footer className="flex items-center gap-6 text-[15px] text-[var(--color-ink-muted)]">
        <span className="flex items-center gap-4">
          <Kbd k="S" label="Sim" />
          <Kbd k="L" label="Live" />
          <span className="inline-flex items-center gap-1.5">
            <Kbd k="1" />
            <Kbd k="2" />
            <Kbd k="3" label="3D views" />
          </span>
          <Kbd k="F" label={isFull ? 'Exit full screen' : 'Full screen'} />
        </span>
        {flash && (
          <span role="status" className={`${STATION} rounded-[2px] bg-[var(--color-steel-soft)] px-2 py-0.5 text-[14px] text-[var(--color-steel)]`}>
            {flash}
          </span>
        )}
        <a href="/dashboard" className="ml-auto underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-arc)]">
          Leave the kiosk
        </a>
      </footer>
    </div>
  );
}
