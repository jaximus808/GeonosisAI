'use client';

// TDM-116: the ticket cockpit. An app shell for one ticket at desktop and laptop size, with no page scroll:
//   top bar  ticket, task, sim/real, robot · before -> after, spend, target · the one primary action · the line
//   rail     Overview (the stage) · Robot · Data · Money · Agents · Events · Settings · Report
//   stage    follows the ticket's current station on its own (hands-off); clicking a station on the line
//            pins its view ("Pinned: Diagnose · Back to live"). Above it, the AgentWorkbench: the station's
//            agent as a droid at work, its heartbeat and a live transcript (fixed height, no layout shift)
// Below 900 px: the top line, the stage, and a bottom tab bar. `?tab=` and `?stage=` keep the view in the URL.
//
//   <TicketCockpit id="12" />             the ticket page
//   <TicketCockpit id="12" embedded />    inside the /dashboard shell

import './cockpit.css';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/brand/Button';
import { gxCx } from '@/components/brand/cx';
import { FactoryIso } from '@/components/factory-iso/FactoryIso';
import type { Tdm114LivePolicy } from '@/lib/tdm114/arbitration';
import { tdm112Elapsed } from '@/lib/tdm112/floor';
import {
  TDM116_PREF_KEYS,
  TDM116_STAGE_KEYS,
  TDM116_STAGE_LABEL,
  TDM116_TABS,
  tdm116LiveStage,
  tdm116ParseOverviewView,
  tdm116ParseStage,
  tdm116ParseTab,
  tdm116PrimaryAction,
  tdm116StoppedAt,
  type Tdm116OverviewView,
  type Tdm116StageKey,
  type Tdm116TabKey,
} from '@/lib/tdm116/cockpit';
import { awResetAsk } from '@/lib/agentwork/workbench';
import { AgentWorkbench } from './AgentWorkbench';
import { Tdm116Panel } from './Panels';
import { Tdm116Stage } from './Stages';
import { Tdm116TopBar } from './TopBar';
import { useTdm116Now, useTdm116Param, useTdm116Stored } from './hooks';
import { useTdm116Cockpit } from './useCockpitData';

export function TicketCockpit({ id, embedded = false }: { id: string; embedded?: boolean }) {
  const tid = useMemo(() => {
    try {
      const b = BigInt(id);
      return b > 0n ? b : -1n;
    } catch {
      return -1n;
    }
  }, [id]);
  const [livePolicy, setLivePolicy] = useState<Tdm114LivePolicy | null>(null);
  const onPolicy = useCallback((p: Tdm114LivePolicy | null) => setLivePolicy(p), []);
  const d = useTdm116Cockpit(tid, { livePolicy });
  const [tabParam, setTabParam] = useTdm116Param('tab');
  const [stageParam, setStageParam] = useTdm116Param('stage');
  const tab = tdm116ParseTab(tabParam);
  const pinned = tdm116ParseStage(stageParam);
  const stoppedAt = useMemo(() => tdm116StoppedAt(d.floorEvents, d.ticket?.status ?? ''), [d.floorEvents, d.ticket?.status]);
  const live = tdm116LiveStage(d.floor, stoppedAt);
  const shown: Tdm116StageKey = pinned ?? live;

  const setTab = useCallback((t: Tdm116TabKey) => setTabParam(t === 'overview' ? null : t), [setTabParam]);
  const pick = useCallback(
    (k: Tdm116StageKey) => {
      // Clicking the live station (or the pinned one again) goes back to following live.
      setStageParam(k === live || k === pinned ? null : k);
      setTab('overview');
    },
    [live, pinned, setStageParam, setTab],
  );
  const backToLive = useCallback(() => setStageParam(null), [setStageParam]);
  // Overview shows the stage or the TDM-115 factory cell (per-viewer pref). Only one is mounted, so the
  // robot's three.js view is gone while the SVG factory is up.
  const [viewRaw, setViewRaw] = useTdm116Stored(TDM116_PREF_KEYS.overviewView);
  const view = tdm116ParseOverviewView(viewRaw);
  const setView = useCallback((v: Tdm116OverviewView) => setViewRaw(v), [setViewRaw]);
  const pinFromFactory = useCallback(
    (i: number) => {
      const k = TDM116_STAGE_KEYS[i] ?? 'baseline';
      setStageParam(k === live ? null : k);
      setViewRaw('stage');
    },
    [live, setStageParam, setViewRaw],
  );
  // A real robot's eval waiting on a reset task: the workbench asks the operator ("Done, scene reset").
  const resetAsk = useMemo(
    () => awResetAsk({ tasks: d.tasks, trials: d.trials, runs: d.evalRuns, isSim: (rid) => d.simRobotIds.has(BigInt(rid)) }),
    [d.tasks, d.trials, d.evalRuns, d.simRobotIds],
  );

  if (tid < 0n) return <Frame embedded={embedded}><Notice>“{id}” is not a ticket number.</Notice></Frame>;
  if (!d.ready) return <Frame embedded={embedded}><Notice>Connecting to the ticket…</Notice></Frame>;
  if (!d.ticket) {
    return (
      <Frame embedded={embedded}>
        <Notice>There is no ticket #{id} in this database. It appears here as soon as it is created.</Notice>
      </Frame>
    );
  }

  const t = d.ticket;
  const action = tdm116PrimaryAction({ ticketId: t.id, status: t.status, iteration: t.iteration, plans: d.plans, tasks: d.tasks });
  const counts: Partial<Record<Tdm116TabKey, number>> = { data: d.episodes.length, events: d.events.length };

  return (
    <div className={gxCx('tdm116-shell', !embedded && 'tdm116-fill')} data-embedded={embedded || undefined} data-testid="ticket-cockpit" data-stage={shown} data-live-stage={live} data-tab={tab}>
      <Tdm116TopBar
        d={d}
        embedded={embedded}
        shown={shown}
        onPick={pick}
        action={action}
        onTab={(x) => setTab(x)}
        factory={tab === 'overview' && view === 'factory'}
        onFactory={() => {
          // From another tab it opens the Overview in Factory; on the Overview it flips Stage <-> Factory.
          if (tab !== 'overview') {
            setTab('overview');
            setView('factory');
          } else setView(view === 'factory' ? 'stage' : 'factory');
        }}
      />

      <nav className="tdm116-rail" role="tablist" aria-label="Ticket views" aria-orientation="vertical">
        {TDM116_TABS.map((x) => (
          <button
            key={x.key}
            type="button"
            role="tab"
            className="tdm116-tab"
            aria-selected={tab === x.key}
            aria-controls="tdm116-main"
            onClick={() => setTab(x.key)}
            data-tab={x.key}
          >
            <i className="gx-rivet" aria-hidden />
            <span className="hidden min-[900px]:inline">{x.label}</span>
            <span className="min-[900px]:hidden">{x.short}</span>
            {counts[x.key] ? <span className="tdm116-tab-count">{counts[x.key]}</span> : null}
          </button>
        ))}
      </nav>

      <section className="tdm116-main" id="tdm116-main" role="tabpanel" aria-label={tab === 'overview' ? `${TDM116_STAGE_LABEL[shown]} stage` : tab}>
        {tab === 'overview' ? (
          <>
            <StageHead d={d} shown={shown} live={live} pinned={!!pinned} onBack={backToLive} view={view} onView={setView} />
            <AgentWorkbench station={d.floor?.stations.find((s) => s.key === shown)} events={d.floorEvents} ended={d.floor?.ended} reset={resetAsk} />
            {view === 'factory' ? (
              <div key="factory" className="tdm116-stage tdm116-shutter is-factory" data-testid="cockpit-stage" data-stage={shown} data-view="factory">
                <FactoryIso ticketId={Number(d.tid)} onStationClick={pinFromFactory} />
              </div>
            ) : (
              <div key={shown} className="tdm116-stage tdm116-shutter" data-testid="cockpit-stage" data-stage={shown} data-view="stage">
                <Tdm116Stage d={d} stage={shown} onPolicy={onPolicy} />
              </div>
            )}
          </>
        ) : (
          <div key={tab} className="tdm116-shutter flex min-h-0 flex-1 flex-col">
            <Tdm116Panel d={d} tab={tab} onPin={pick} />
          </div>
        )}
      </section>
    </div>
  );
}

function Frame({ embedded, children }: { embedded: boolean; children: React.ReactNode }) {
  return <div className={gxCx('tdm116-shell', !embedded && 'tdm116-fill')} style={{ display: 'block', padding: 'var(--space-8)' }}>{children}</div>;
}

function Notice({ children }: { children: React.ReactNode }) {
  return <p className="m-0 max-w-xl rounded-sm border border-dashed border-line-strong px-4 py-3 text-sm text-ink-muted">{children}</p>;
}

/** The stage header: which station, who works it, what it is doing, the timer, and live vs pinned. */
function StageHead({
  d,
  shown,
  live,
  pinned,
  onBack,
  view,
  onView,
}: {
  d: ReturnType<typeof useTdm116Cockpit>;
  shown: Tdm116StageKey;
  live: Tdm116StageKey;
  pinned: boolean;
  onBack: () => void;
  view: Tdm116OverviewView;
  onView: (v: Tdm116OverviewView) => void;
}) {
  const nowMs = useTdm116Now(1000);
  const st = d.floor?.stations.find((s) => s.key === shown);
  const running = st && (st.state === 'working' || st.state === 'waiting');
  const time = st?.startedUs && nowMs ? tdm112Elapsed(st.startedUs, running ? nowMs * 1000 : st.endedUs ?? nowMs * 1000) : '';
  const ended = d.floor?.ended;
  // Announce automatic stage changes to screen readers (pinning is the viewer's own action).
  const prev = useRef(live);
  const [announce, setAnnounce] = useState('');
  useEffect(() => {
    if (prev.current !== live) setAnnounce(`The ticket moved to ${TDM116_STAGE_LABEL[live]}.`);
    prev.current = live;
  }, [live]);
  return (
    <div className="tdm116-head" data-testid="cockpit-stage-head">
      <div className="tdm116-head-title">
        <h2>
          {st ? st.label : TDM116_STAGE_LABEL[shown]}
          {st?.sub && <small>{st.sub}</small>}
        </h2>
        <span className={gxCx('tdm116-activity', st?.state === 'waiting' && 'is-waiting', st?.activityIsError && 'is-error')} title={st?.activity}>
          {st ? `${st.agent} · ${st.activity}` : ''}
        </span>
        {time && (
          <span className="gx-id whitespace-nowrap" title={running ? 'Time at this station so far' : 'Time this station took (last run)'}>
            {running ? time : `took ${time}`}
          </span>
        )}
      </div>
      <div className="tdm116-follow" aria-live="polite">
        <span className="sr-only">{announce}</span>
        {pinned ? (
          <>
            <span className="tdm116-follow-pin" data-testid="cockpit-pinned">
              <i className="gx-rivet" aria-hidden />
              Pinned: {TDM116_STAGE_LABEL[shown]}
              {live !== shown && <em>· live is at {TDM116_STAGE_LABEL[live]}</em>}
            </span>
            <Button small onClick={onBack} data-testid="cockpit-back-to-live">
              Back to live
            </Button>
          </>
        ) : (
          <span className={gxCx('tdm116-follow-live', ended && ended !== 'shipped' && 'is-ended')} data-testid="cockpit-following" title="The stage switches by itself when the ticket moves to the next station.">
            <i className="gx-rivet" aria-hidden />
            {ended ? `Ticket ${ended}` : 'Following live'}
          </span>
        )}
        <span className="tdm116-viewswitch" role="radiogroup" aria-label="Overview view" data-testid="cockpit-view-switch">
          {(['stage', 'factory'] as const).map((v) => (
            <button
              key={v}
              type="button"
              role="radio"
              aria-checked={view === v}
              onClick={() => onView(v)}
              data-view={v}
              title={v === 'stage' ? 'The current station’s working view' : 'The factory cell: every agent at its machine. Click a machine to pin its station.'}
            >
              {v === 'stage' ? 'Stage' : 'Factory'}
            </button>
          ))}
        </span>
      </div>
    </div>
  );
}
