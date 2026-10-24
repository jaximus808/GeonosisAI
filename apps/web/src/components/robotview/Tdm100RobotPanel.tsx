'use client';

// TDM-100: RobotView as mounted on the robot page (/robots/[id]) and the ticket page (/tickets/[id]).
// One place that wires the slots, so both pages show the same thing:
//   * the robot pinned for its kind, opening in that kind's mode (a sim robot opens in Sim, a real
//     arm in Live) unless the viewer picked a mode before; `defaultMode` overrides (the ticket's robot
//     mode once TDM-107 lands)
//   * the TDM-99 HUD under the panes, fed by RobotView's own state feed (no second socket), and
//     following whichever robot the view is showing
//   * `simCameras` passes straight through. TODO(TDM-105): once SimCameras.tsx is committed, default
//     it to `simCameras ?? simCamerasSlot` (import { simCamerasSlot } from './SimCameras') so the
//     rendered top / side / wrist sim cams show in Sim on both pages (verified working on a local stack).

import type { ReactNode } from 'react';
import type { FeedHealth } from '@/components/teleop/CameraFeed';
import { classifyRobot, type RobotRowLike, type RobotViewMode } from '@/lib/robotview/sources';
import { RobotHud } from './Hud';
import { RobotView, type SimCamerasContext } from './RobotView';

export interface Tdm100RobotPanelProps {
  robotId: number | bigint;
  /** The robot row, when the page has it: its sim/live kind picks the opening mode. */
  robot?: Pick<RobotRowLike, 'embodiment' | 'adapterVersion' | 'cameraConfigJson'> | null;
  /** HUD counts this ticket's eval runs (ticket page); absent = the robot's newest run. */
  ticketId?: number | bigint | null;
  defaultMode?: RobotViewMode;
  compact?: boolean;
  simCameras?: ReactNode | ((ctx: SimCamerasContext) => ReactNode);
  onFeedHealth?: (h: FeedHealth | null) => void;
}

export function tdm100DefaultMode(
  robot: Tdm100RobotPanelProps['robot'],
  override?: RobotViewMode,
): RobotViewMode | undefined {
  if (override) return override;
  return robot ? classifyRobot(robot).kind : undefined;
}

export function Tdm100RobotPanel({ robotId, robot, ticketId, defaultMode, compact, simCameras, onFeedHealth }: Tdm100RobotPanelProps) {
  const pinned = Number(robotId);
  return (
    <RobotView
      robotId={pinned > 0 ? pinned : undefined}
      defaultMode={tdm100DefaultMode(robot, defaultMode)}
      compact={compact}
      simCameras={simCameras}
      onFeedHealth={onFeedHealth}
      hud={({ live, robotId: shown }) => (
        // The ticket's eval runs belong to the ticket's robot; another robot picked in the view
        // shows its own newest run instead.
        <RobotHud robotId={shown} ticketId={shown === pinned ? ticketId : null} state={live.state} stale={live.stale} />
      )}
    />
  );
}

export default Tdm100RobotPanel;
