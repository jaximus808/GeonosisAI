'use client';

import { useRobot } from '@/lib/spacetime/hooks';
import { fmtTs } from '@/lib/format';

export function RobotSummary({ id }: { id: string }) {
  const [robot, ready] = useRobot(BigInt(id));
  if (!ready) return <p className="text-sm text-zinc-500">Loading robot…</p>;
  if (!robot) return <p className="text-sm text-zinc-500">No robot #{id}.</p>;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 rounded-lg border border-zinc-200 p-4 text-sm sm:grid-cols-4 dark:border-zinc-800">
      <div><dt className="text-xs text-zinc-500">Name</dt><dd>{robot.name}</dd></div>
      <div><dt className="text-xs text-zinc-500">Embodiment</dt><dd>{robot.embodiment}</dd></div>
      <div><dt className="text-xs text-zinc-500">Status</dt><dd className="font-mono">{robot.status}</dd></div>
      <div><dt className="text-xs text-zinc-500">Last heartbeat</dt><dd>{robot.lastHeartbeat ? fmtTs(robot.lastHeartbeat) : 'never'}</dd></div>
    </dl>
  );
}
