// TDM-125: split-robot tickets. A ticket runs its baseline and evals on its own robot (ticket.robot_id,
// usually the MuJoCo sim robot) but may collect its paid data (clean teleop demos) on another robot,
// e.g. the real yam-01: run_settings hyperparams.collect_robot_id (0 / unset = the ticket's robot).
// The orchestrator routes collection tasks, start_teleop and their resets there; each episode keeps the
// robot it was recorded on. Pure helpers, no React (tested in apps/web/test/tdm125-collect.test.ts).

import type { Tdm114WaitingFor } from '../tdm112/floor';

/** Same rule as lib/tdm107/robotMode.ts (kept inline so this file has no runtime imports). */
function tdm125IsSim(r: { cameraConfigJson?: string | null }): boolean {
  try {
    const cfg = JSON.parse(r.cameraConfigJson || '{}') as unknown;
    return !!cfg && typeof cfg === 'object' && (cfg as Record<string, unknown>).sim === true;
  } catch {
    return false;
  }
}

type RobotLite = { id: bigint; name: string; status: string; cameraConfigJson: string };
type SettingsLite = { hyperparamsJson?: string | null } | null | undefined;
type EpisodeLite = { robotId: bigint; kind: string; reviewStatus: string };

export function tdm125CollectRobotIdSetting(settings: SettingsLite): bigint {
  try {
    const hp = JSON.parse(settings?.hyperparamsJson || '{}') as Record<string, unknown>;
    const v = hp && typeof hp === 'object' ? hp.collect_robot_id : undefined;
    if (typeof v === 'number' && Number.isInteger(v) && v > 0) return BigInt(v);
  } catch {
    /* not JSON: no setting */
  }
  return 0n;
}

export interface Tdm125Split {
  /** The robot collection drives (= the ticket's robot when not split). */
  collectRobotId: bigint;
  collectRobot: RobotLite | undefined;
  collectMode: 'sim' | 'real' | null;
  evalRobot: RobotLite | undefined;
  evalMode: 'sim' | 'real' | null;
  /** True when collection runs on another robot than baseline / eval. */
  split: boolean;
  /** Accepted dataset episodes recorded on the collect robot. */
  collectEpisodesAccepted: number;
  /** "Data: 3 real-arm episodes (yam-01) · Evals: sim robot" (split tickets only). */
  line: string | null;
}

const MODE = (r: RobotLite | undefined) => (r ? (tdm125IsSim(r) ? 'sim' : 'real') : null);

export function tdm125Split(
  ticket: { robotId: bigint } | null | undefined,
  settings: SettingsLite,
  robots: readonly RobotLite[],
  episodes: readonly EpisodeLite[] = [],
): Tdm125Split {
  const evalId = ticket?.robotId ?? 0n;
  const set = tdm125CollectRobotIdSetting(settings);
  const collectRobotId = set > 0n ? set : evalId;
  const collectRobot = robots.find((r) => r.id === collectRobotId);
  const evalRobot = robots.find((r) => r.id === evalId);
  const split = set > 0n && set !== evalId;
  const collectMode = MODE(collectRobot);
  const evalMode = MODE(evalRobot);
  const collectEpisodesAccepted = episodes.filter(
    (e) => e.robotId === collectRobotId && e.reviewStatus === 'accepted' && (e.kind === 'clean_teleop' || e.kind === 'recovery_takeover'),
  ).length;
  const name = collectRobot?.name ?? `robot ${collectRobotId}`;
  const line = split
    ? `Data: ${collectEpisodesAccepted} ${collectMode === 'real' ? 'real-arm' : 'sim-robot'} episode${collectEpisodesAccepted === 1 ? '' : 's'} (${name}) · Evals: ${evalMode === 'sim' ? 'sim robot' : 'real robot'}`
    : null;
  return { collectRobotId, collectRobot, collectMode, evalRobot, evalMode, split, collectEpisodesAccepted, line };
}

/** TDM-114's waitingFor, for a split ticket's collect robot that is offline or e-stopped (collection waits). */
export function tdm125CollectWaiting(s: Tdm125Split, status: string, stationIndex: number): Tdm114WaitingFor | undefined {
  if (!s.split || status !== 'dispatch') return undefined;
  const r = s.collectRobot;
  const name = r?.name ?? `robot ${s.collectRobotId}`;
  const none = { claimNow: false, resume: false, clearEstop: false };
  const base = { stationKey: 'collect' as const, stationIndex, robotId: Number(s.collectRobotId), robotName: name, holder: null };
  if (!r || r.status === 'offline')
    return { ...base, reason: 'robot_offline', text: `Waiting for ${name} (offline). Its collection tasks are posted when it comes back.`, actions: none };
  if (r.status === 'estop')
    return {
      ...base,
      reason: 'robot_estop',
      text: `Waiting for ${name} (e-stopped${s.collectMode === 'real' ? '; a person at the arm clears it' : ''}). Collection waits.`,
      actions: none,
    };
  return undefined;
}
