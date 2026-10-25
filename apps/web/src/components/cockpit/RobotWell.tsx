'use client';

// TDM-116: the one live robot view the cockpit mounts at a time (TDM-98 RobotView with the TDM-105 sim
// cameras and the TDM-99 HUD, the same slots as Tdm100RobotPanel). It is only rendered by the visible
// stage or tab, and unmounts while the browser tab is hidden, so at most one 3D canvas and one state
// socket run. It also hands the robot's own policy phase (TDM-114 `state.policy`) up to the cockpit, so
// the eval readout can show the phase without a second socket.

import { useEffect, useRef } from 'react';
import { RobotView } from '@/components/robotview/RobotView';
import { RobotHud } from '@/components/robotview/Hud';
import { simCamerasSlot } from '@/components/robotview/SimCameras';
import { tdm100DefaultMode } from '@/components/robotview/Tdm100RobotPanel';
import type { Tdm114LivePolicy } from '@/lib/tdm114/arbitration';
import { useTdm116PageVisible } from './hooks';
import type { Tdm116Data } from './useCockpitData';

function PolicyReporter({ policy, onPolicy }: { policy: Tdm114LivePolicy | null | undefined; onPolicy?: (p: Tdm114LivePolicy | null) => void }) {
  const last = useRef('');
  const key = policy ? JSON.stringify([policy.phase, policy.trial_index, policy.trials, policy.eval_run_id]) : '';
  useEffect(() => {
    if (key === last.current) return;
    last.current = key;
    onPolicy?.(policy ?? null);
  }, [key, policy, onPolicy]);
  useEffect(() => () => onPolicy?.(null), [onPolicy]);
  return null;
}

export function Tdm116RobotWell({ d, compact = false, hud = true, onPolicy, robotOverride }: {
  d: Tdm116Data;
  compact?: boolean;
  hud?: boolean;
  onPolicy?: (p: Tdm114LivePolicy | null) => void;
  /** TDM-125: show another robot than the ticket's (a split ticket's collect robot). */
  robotOverride?: { id: bigint; robot: Tdm116Data['robot']; mode: Tdm116Data['mode'] };
}) {
  const visible = useTdm116PageVisible();
  const pinned = Number(robotOverride?.id ?? d.robotId);
  const shownRobot = robotOverride ? robotOverride.robot : d.robot;
  const shownMode = robotOverride ? robotOverride.mode : d.mode;
  if (pinned <= 0) {
    return (
      <div className="tdm116-empty">
        <strong>No robot on this ticket</strong>
        The robot view appears once the ticket has a robot.
      </div>
    );
  }
  if (!visible) {
    return (
      <div className="tdm116-empty">
        <strong>Robot view paused</strong>
        It resumes when this tab is visible again.
      </div>
    );
  }
  const defaultMode = tdm100DefaultMode(shownRobot, shownMode === 'sim' ? 'sim' : shownMode === 'real' ? 'live' : undefined);
  const ticketId = Number(d.tid);
  return (
    <div className="tdm116-robot" data-testid="cockpit-robot">
      <RobotView
        robotId={pinned}
        defaultMode={defaultMode}
        compact={compact}
        ticketId={ticketId}
        simCameras={simCamerasSlot}
        hud={({ live, robotId: shown }) => (
          <>
            <PolicyReporter policy={(live.state as { policy?: Tdm114LivePolicy } | null)?.policy} onPolicy={onPolicy} />
            {hud && <RobotHud robotId={shown} ticketId={shown === pinned ? ticketId : null} state={live.state} stale={live.stale} />}
          </>
        )}
      />
    </div>
  );
}
