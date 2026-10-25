'use client';

// TDM-116 stage: Baseline / Eval. The live robot view big, and beside it the run as it happens: trial
// strip filling in, trial x of N with the robot's phase and an ETA (TDM-114 floor.evalPhase), the last
// failure note, and when the ticket is waiting for its robot, who holds it and what an operator can do
// (TDM-114's WaitingForRobot / EvalProgressLine / RobotQueueList, mounted as they are).
// On a REAL robot the left pane gets the teleop view switcher: Twin (the 3D view) or the robot's own
// cameras, Top / Wrist / All (EvalCams.tsx), remembered per viewer.

import { useMemo } from 'react';
import { Tag } from '@/components/brand/StatusTag';
import { gxCx } from '@/components/brand/cx';
import { SeededBadge, StatusPill } from '@/components/ticket/Badges';
import { isSeededTrial } from '@/components/ticket/util';
import type { Tdm114LivePolicy } from '@/lib/tdm114/arbitration';
import { tdm112Words } from '@/lib/tdm112/floor';
import { TDM116_PREF_KEYS, tdm116EvalCamViews, tdm116LastFailure, tdm116PickEvalView } from '@/lib/tdm116/cockpit';
import { EvalProgressLine, RobotQueueList, WaitingForRobot } from '@/components/tdm114/WaitingForRobot';
import { isFakeRobot } from '@/components/ticket/util';
import { Tdm116EvalCams, Tdm116EvalViewTabs } from './EvalCams';
import { useTdm116Stored } from './hooks';
import { Tdm116RobotWell } from './RobotWell';
import type { Tdm116Data } from './useCockpitData';

const pct = (x: number) => `${Math.round(x * 100)}%`;
const clock = (us: number) => new Date(us / 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

export function Tdm116EvalStage({ d, which, onPolicy }: { d: Tdm116Data; which: 'baseline' | 'eval'; onPolicy: (p: Tdm114LivePolicy | null) => void }) {
  const floor = d.floor;
  const station = floor?.stations.find((s) => s.key === which);
  const runs = [...d.evalRuns]
    .filter((r) => (which === 'baseline' ? r.kind === 'baseline' : r.kind !== 'baseline'))
    .sort((a, b) => Number(b.createdAt.microsSinceUnixEpoch - a.createdAt.microsSinceUnixEpoch));
  const run = runs[0];
  const trials = run ? d.trials.filter((t) => t.evalRunId === run.id).sort((a, b) => a.trialIndex - b.trialIndex) : [];
  const phase = run && floor?.evalPhase && floor.evalPhase.evalRunId === Number(run.id) ? floor.evalPhase : undefined;
  const waiting = floor?.waitingFor && floor.waitingFor.stationKey === which ? floor.waitingFor : undefined;
  const seeded = trials.some(isSeededTrial);
  const lastFail = tdm116LastFailure(trials);
  const done = run?.status === 'done';
  const n = run ? (done ? run.trials : run.completed) : 0;
  const byIdx = new Map(trials.map((t) => [t.trialIndex, t]));
  const nowIdx = run && !done && run.status === 'running' ? (phase ? phase.trial - 1 : run.completed) : -1;
  const target = d.settings?.successThreshold ?? d.ticket?.targetSuccess;
  const recent = station ? station.eventIds.slice(-6).reverse() : [];
  const evById = new Map(d.floorEvents.map((e) => [e.id, e]));
  const label = which === 'baseline' ? 'Baseline eval' : 'Fine-tuned eval';
  // The robot this eval runs on (the run's, else the ticket's): real -> its cameras can be watched.
  const evalRobotId = run ? run.robotId : d.robotId;
  const evalRobot = d.allRobots.find((r) => r.id === evalRobotId);
  const real = !!evalRobot && !d.simRobotIds.has(evalRobot.id) && !isFakeRobot(evalRobot);
  const camViews = useMemo(() => (real ? tdm116EvalCamViews(evalRobot?.cameraConfigJson) : []), [real, evalRobot?.cameraConfigJson]);
  const [storedView, setStoredView] = useTdm116Stored(TDM116_PREF_KEYS.evalView);
  const camView = camViews.length > 1 ? tdm116PickEvalView(camViews, storedView) : null;

  return (
    <div className="tdm116-cols" style={{ gridTemplateColumns: 'minmax(0, 1.7fr) minmax(320px, 1fr)' }} data-testid="stage-eval">
      <div className="tdm116-col">
        {camView && <Tdm116EvalViewTabs views={camViews} value={camView.id} onChange={setStoredView} />}
        {camView && camView.id !== 'twin' ? (
          <Tdm116EvalCams robotId={Number(evalRobotId)} view={camView} />
        ) : (
          <Tdm116RobotWell d={d} onPolicy={onPolicy} />
        )}
      </div>

      <div className="tdm116-col tdm116-scroll">
        {/* TDM-114: who holds the robot and what an operator can do about it. */}
        {waiting && <WaitingForRobot waitingFor={waiting} ticketId={Number(d.tid)} />}

        <section className="tdm116-plate" aria-label={label}>
          <div className="tdm116-plate-head">
            <h3>
              {label}
              {run && <span className="gx-id ml-2">run #{run.id.toString()}</span>}
            </h3>
            <span className="flex items-center gap-1">
              {seeded && <SeededBadge />}
              {run && <StatusPill status={run.status} />}
            </span>
          </div>
          {!run ? (
            <p className="gx-small m-0">{station?.activity ?? 'No eval run yet.'}</p>
          ) : (
            <>
              <div className="tdm116-big" data-testid="stage-eval-rate">
                {n > 0 ? pct(run.successes / n) : '—'}
                <small>
                  {run.successes}/{n} passed{done ? '' : ' so far'}
                </small>
              </div>
              <p className="gx-small mt-1 mb-3" hidden={!done && !!phase}>
                {done
                  ? `${run.trials} trials, 95% interval ${pct(run.ciLow)}–${pct(run.ciHigh)}${target != null ? ` · target ${pct(target)}` : ''}`
                  : phase
                    ? ''
                    : `${run.completed} of ${run.trials} trials finished`}
              </p>
              {!done && <EvalProgressLine phase={phase} className="mb-3" />}
              <ol className="tdm116-trials m-0 p-0" style={{ listStyle: 'none' }} aria-label="Trials">
                {Array.from({ length: run.trials }, (_, i) => {
                  const t = byIdx.get(i);
                  const now = !t && i === nowIdx;
                  return (
                    <li
                      key={i}
                      className={gxCx('tdm116-trial', t && (t.success ? 'is-pass' : 'is-fail'), now && 'is-now')}
                      title={t ? `Trial ${i + 1}: ${t.success ? 'Pass' : 'Fail'}${t.failureNote ? ` (${t.failureNote})` : ''}` : now ? `Trial ${i + 1}: running` : `Trial ${i + 1}: not run yet`}
                    >
                      {t ? (t.success ? 'P' : 'F') : i + 1}
                    </li>
                  );
                })}
              </ol>
              <div className="tdm116-legend">
                <span>P pass</span>
                <span>F fail</span>
                {nowIdx >= 0 && <span className="text-melt">outlined: running now</span>}
              </div>
            </>
          )}
        </section>

        {lastFail && (
          <section className="tdm116-plate is-tight" aria-label="Last failure">
            <p className="gx-label m-0">Last failure · trial {lastFail.index + 1}</p>
            <p className="m-0 mt-1 text-sm text-slag">{lastFail.note}</p>
          </section>
        )}

        {floor && floor.robotQueue.length > 0 && (
          <section className="tdm116-plate is-tight" aria-label="Robot queue">
            <p className="gx-label m-0 mb-1">Robot queue</p>
            <RobotQueueList queue={floor.robotQueue} />
          </section>
        )}

        {recent.length > 0 && (
          <section className="tdm116-plate is-tight" aria-label="Latest at this station">
            <p className="gx-label m-0 mb-1">Latest</p>
            <ul className="tdm116-rows">
              {recent.map((id) => {
                const e = evById.get(id);
                if (!e) return null;
                return (
                  <li key={id}>
                    <div className="tdm116-row-top">
                      <span className="gx-id">{clock(e.tsUs)}</span>
                      <span className="tdm116-row-main text-sm" title={e.type}>
                        {tdm112Words(e)}
                      </span>
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
