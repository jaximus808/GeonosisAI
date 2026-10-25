'use client';

// TDM-125: split-robot tickets in the cockpit.
//   <Tdm125CollectPicker>  Settings: "Collect data on" robot picker (sim robots and real robots, real
//                          ones tagged REAL). PATCHes run_settings hyperparams.collect_robot_id through
//                          the TDM-79 proxy (operator login); 0 = the ticket's own robot.
//   <Tdm125SplitLine>      "Data: 3 real-arm episodes (yam-01) · Evals: sim robot", plus TDM-114's
//                          waiting card when the collect robot is offline / e-stopped.

import { useState } from 'react';
import { Tag } from '@/components/brand/StatusTag';
import { WaitingForRobot } from '@/components/tdm114/WaitingForRobot';
import { patchSettings } from '@/lib/tdm79/settings';
import { tdm125CollectWaiting, type Tdm125Split } from '@/lib/tdm125/collect';
import { tdm107IsSimRobot } from '@/lib/tdm107/robotMode';

type RobotLite = { id: bigint; name: string; status: string; cameraConfigJson: string };

export function Tdm125CollectPicker({
  ticketId,
  split,
  robots,
  frozen,
}: {
  ticketId: bigint;
  split: Tdm125Split;
  robots: readonly RobotLite[];
  frozen?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const current = split.split ? split.collectRobotId.toString() : '0';
  const list = robots.filter((r) => !r.name.startsWith('test.'));
  const onChange = async (v: string) => {
    setBusy(true);
    setErr(null);
    const res = await patchSettings(ticketId, { hyperparams: { collect_robot_id: Number(v) } }, 'operator');
    setBusy(false);
    if (!res.ok) setErr(Object.values(res.errors).join('; ') || 'Rejected');
  };
  return (
    <section className="tdm116-plate" aria-label="Collect data on" data-testid="tdm125-collect-picker">
      <div className="tdm116-plate-head">
        <h3>Collect data on</h3>
        {split.split && <Tag tone={split.collectMode === 'real' ? 'failed' : 'steel'}>{split.collectMode === 'real' ? 'REAL' : 'SIM'}</Tag>}
      </div>
      <select
        className="w-full rounded-sm border border-line bg-paper px-2 py-1 text-sm"
        value={current}
        disabled={busy || frozen}
        onChange={(e) => void onChange(e.target.value)}
        data-testid="tdm125-collect-select"
      >
        <option value="0">Same robot as the evals ({split.evalRobot?.name ?? 'ticket robot'})</option>
        {list
          .filter((r) => r.id !== split.evalRobot?.id)
          .map((r) => (
            <option key={r.id.toString()} value={r.id.toString()}>
              {r.name} {tdm107IsSimRobot(r) ? '(sim)' : '(REAL)'} · {r.status}
            </option>
          ))}
      </select>
      <p className="gx-small mt-1 mb-0">
        Paid teleop demos and their resets drive this robot. Baseline and evals stay on {split.evalRobot?.name ?? 'the ticket robot'}. A
        robot with no policy of this ticket can only record clean demos, so the planner buys clean teleop.
      </p>
      {err && (
        <p className="gx-small mt-1 mb-0 text-red-700" role="alert">
          {err}
        </p>
      )}
    </section>
  );
}

export function Tdm125SplitLine({ split, status, stationIndex, ticketId }: { split: Tdm125Split; status: string; stationIndex: number; ticketId: number }) {
  if (!split.split) return null;
  const wait = tdm125CollectWaiting(split, status, stationIndex);
  return (
    <>
      <section className="tdm116-plate is-tight" aria-label="Split robots" data-testid="tdm125-split">
        <p className="gx-label m-0">Jobs drive</p>
        <p className="m-0 mt-1 text-sm">
          <span className="font-semibold">{split.collectRobot?.name ?? `robot ${split.collectRobotId}`}</span>{' '}
          <Tag tone={split.collectMode === 'real' ? 'failed' : 'steel'}>{split.collectMode === 'real' ? 'REAL ARM' : 'SIM'}</Tag>
        </p>
        <p className="m-0 mt-1 text-sm" data-testid="tdm125-split-line">
          {split.line}
        </p>
      </section>
      {wait && <WaitingForRobot waitingFor={wait} ticketId={ticketId} />}
    </>
  );
}
