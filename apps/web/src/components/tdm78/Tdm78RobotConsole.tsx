'use client';

// TDM-78: the live half of /robots/[id]. Every panel renders from SpacetimeDB subscriptions
// (lib/tdm78/useRobotConsole.ts) and sits in its own error boundary.

import { useState, type ReactNode } from 'react';
import type { FeedHealth } from '@/components/teleop/CameraFeed';
import { useConnectionStatus } from '@/lib/spacetime/hooks';
import {
  TDM78_SAFETY_KINDS,
  TDM78_STATES,
  TDM78_STATE_STATUS,
  tdm78Json,
  tdm78Ms,
  useRobotConsole,
  useTdm78Now,
  type CommandRow,
} from '@/lib/tdm78/useRobotConsole';
import { Tdm78Boundary } from './Tdm78Boundary';
import { Tdm78CameraHealth, Tdm78LiveFeed } from './Tdm78Cameras';
import { Tdm78CommandPanel } from './Tdm78CommandPanel';

const STATUS_TONE: Record<string, string> = {
  online: 'bg-emerald-600 text-white',
  busy: 'bg-sky-600 text-white',
  paused: 'bg-amber-400 text-zinc-950',
  estop: 'bg-[#c8102e] text-white',
  offline: 'bg-zinc-300 text-zinc-800 dark:bg-zinc-700 dark:text-zinc-100',
};

const STATE_LABEL: Record<string, string> = {
  idle: 'Idle',
  homing: 'Homing',
  rollout: 'Policy running',
  teleop: 'Teleop',
  paused_for_takeover: 'Paused for takeover',
  waiting_reset: 'Waiting for reset',
  locked: 'Locked',
};

const CMD_TONE: Record<string, string> = {
  issued: 'text-sky-700 dark:text-sky-400',
  acked: 'text-sky-700 dark:text-sky-400',
  done: 'text-emerald-700 dark:text-emerald-400',
  failed: 'text-red-700 dark:text-red-400',
  preempted: 'text-amber-700 dark:text-amber-400',
};

export function fmtAge(ms: number): string {
  if (ms < 0) ms = 0;
  if (ms < 60_000) return `${Math.floor(ms / 1000)} s`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)} min`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)} h`;
  return `${Math.floor(ms / 86_400_000)} d`;
}

function clock(ms: number | null): string {
  return ms ? new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
}

function Panel({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900/60">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-zinc-800 dark:text-zinc-200">{title}</h2>
        {aside}
      </div>
      <Tdm78Boundary label={title}>{children}</Tdm78Boundary>
    </section>
  );
}

export function Tdm78RobotConsole({ id }: { id: string }) {
  const v = useRobotConsole(BigInt(id));
  const now = useTdm78Now(1000);
  const conn = useConnectionStatus();
  const [feedHealth, setFeedHealth] = useState<FeedHealth | null>(null);

  if (!v.ready) {
    return <p className="text-sm text-zinc-500">{conn.status === 'disconnected' ? `Can't reach SpacetimeDB (${conn.error ?? 'disconnected'}).` : 'Loading robot…'}</p>;
  }
  if (!v.robot) return <p className="text-sm text-zinc-600 dark:text-zinc-400">There is no robot #{id}. The soft e-stop still sends, in case the row is just missing.</p>;

  const r = v.robot;
  const hbMs = tdm78Ms(r.lastHeartbeat);
  const hbAge = hbMs != null ? now - hbMs : null;
  const hbStale = hbAge == null || hbAge > 15_000;
  const locked = r.status === 'estop' || v.state?.value === 'locked';
  const fine = v.state?.value;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-2">
        <div>
          <p className="text-sm text-zinc-500">Robot #{id}</p>
          <h1 className="text-3xl font-semibold tracking-tight">{r.name}</h1>
          <p className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-400">
            {r.embodiment}, adapter {r.adapterVersion || 'unknown'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className={`rounded-md px-2.5 py-1 text-sm font-semibold ${STATUS_TONE[r.status] ?? STATUS_TONE.offline}`}>{r.status}</span>
          <span className={`text-sm tabular-nums ${hbStale ? 'font-medium text-red-700 dark:text-red-400' : 'text-zinc-600 dark:text-zinc-400'}`}>
            {hbAge == null ? 'No heartbeat yet' : `Heartbeat ${fmtAge(hbAge)} ago`}
          </span>
          {conn.status !== 'connected' && <span className="text-xs text-red-700 dark:text-red-400">Live data {conn.status}</span>}
        </div>
      </header>

      {locked && (
        <div
          role="status"
          className="rounded-lg bg-[repeating-linear-gradient(135deg,#f2b705_0_14px,#18181b_14px_28px)] p-1.5"
        >
          <div className="rounded-md bg-zinc-950 px-4 py-3 text-white">
            <p className="text-lg font-semibold">Locked by the soft e-stop</p>
            <p className="mt-1 max-w-prose text-sm text-zinc-300">
              The arm won&apos;t take any command until someone at the arm checks it and clears the lock: type{' '}
              <kbd className="rounded bg-zinc-800 px-1 font-mono">clear</kbd> in the robot-client terminal on the Jetson, or run{' '}
              <kbd className="rounded bg-zinc-800 px-1 font-mono">pkill -USR1 -f rdf-robot-client</kbd> there.
            </p>
          </div>
        </div>
      )}

      <Tdm78Boundary label="Robot state">
        <Tdm78StateRail fine={fine} status={r.status} since={v.state?.at ?? null} source={v.state?.source} now={now} />
      </Tdm78Boundary>

      <div className="grid gap-4 md:grid-cols-2">
        <Panel title="Arm control">
          <Tdm78ControlHolder tasks={v.tasks} name={v.contributorName} now={now} />
        </Panel>
        <Panel title="Active checkpoint">
          {v.checkpoint.uri ? (
            <>
              <p className="break-all font-mono text-xs">{v.checkpoint.uri}</p>
              <p className="mt-1 text-xs text-zinc-500">
                From {v.checkpoint.source}
                {v.checkpoint.at ? `, ${clock(v.checkpoint.at)}` : ''}.
              </p>
            </>
          ) : (
            <p className="text-sm text-zinc-600 dark:text-zinc-400">No checkpoint loaded since we started watching. Baseline runs use the base policy.</p>
          )}
          {v.deployed.length > 0 && (
            <p className="mt-2 text-xs text-zinc-500">
              Shipped on ticket #{v.deployed[0].ticketId}: <span className="break-all font-mono">{v.deployed[0].uri}</span>
            </p>
          )}
        </Panel>
        <Panel title="Safety layer" aside={<span className="text-xs text-zinc-500">since client boot {v.bootAt ? clock(v.bootAt) : '(not seen)'}</span>}>
          <Tdm78Safety safety={v.safety} />
        </Panel>
        <Panel title="Cameras">
          <Tdm78CameraHealth configJson={r.cameraConfigJson} health={feedHealth} />
        </Panel>
      </div>

      <Panel title="Live camera feed">
        <Tdm78LiveFeed configJson={r.cameraConfigJson} tasks={v.tasks} now={now} onHealth={setFeedHealth} />
      </Panel>

      <div className="grid gap-4 lg:hidden">
        <Panel title="Commands">
          <Tdm78CommandPanel robotId={id} status={r.status} commands={v.commands} tickets={v.tickets} />
        </Panel>
      </div>

      <Panel title="Command queue and history" aside={<span className="text-xs text-zinc-500">{v.commands.length} total</span>}>
        <Tdm78CommandTable commands={v.commands} now={now} />
      </Panel>

      <Panel title="State changes">
        {v.transitions.length === 0 ? (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">No state changes reported yet. They appear when the robot-client starts or runs a command.</p>
        ) : (
          <ol className="space-y-1 text-sm">
            {[...v.transitions].reverse().slice(0, 12).map((t) => (
              <li key={String(t.id)} className="flex gap-3">
                <span className="w-24 shrink-0 tabular-nums text-zinc-500">{clock(t.at)}</span>
                <span className="shrink-0">
                  {STATE_LABEL[t.from] ?? t.from} to <span className="font-medium">{STATE_LABEL[t.to] ?? t.to}</span>
                </span>
                <span className="min-w-0 truncate text-zinc-500" title={t.reason}>
                  {t.reason}
                </span>
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}

/** Desktop rail copy of the command panel (the mobile copy sits in the main column). */
export function Tdm78RailCommands({ id }: { id: string }) {
  const v = useRobotConsole(BigInt(id));
  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900/60">
      <h2 className="mb-3 text-sm font-semibold text-zinc-800 dark:text-zinc-200">Commands</h2>
      <Tdm78CommandPanel robotId={id} status={v.robot?.status} commands={v.commands} tickets={v.tickets} />
    </section>
  );
}

function Tdm78StateRail({ fine, status, since, source, now }: { fine?: string; status: string; since: number | null; source?: string; now: number }) {
  const groups: { status: string; states: string[] }[] = [];
  for (const s of TDM78_STATES) {
    const st = TDM78_STATE_STATUS[s];
    const g = groups.find((x) => x.status === st);
    if (g) g.states.push(s);
    else groups.push({ status: st, states: [s] });
  }
  // A fine state that disagrees with the coarse status (e.g. a stale event) is shown, but the
  // coarse status wins the highlight of its group.
  const current = fine && TDM78_STATE_STATUS[fine as keyof typeof TDM78_STATE_STATUS] === status ? fine : undefined;
  return (
    <div>
      <div className="flex flex-wrap gap-x-4 gap-y-3" role="list" aria-label="Robot states">
        {groups.map((g) => (
          <div key={g.status} className="min-w-0">
            <p className={`mb-1 text-xs ${g.status === status ? 'font-semibold text-zinc-900 dark:text-zinc-100' : 'text-zinc-500'}`}>{g.status}</p>
            <div className="flex gap-1">
              {g.states.map((s) => {
                const on = s === current;
                return (
                  <span
                    key={s}
                    role="listitem"
                    aria-current={on ? 'true' : undefined}
                    className={`rounded px-2 py-1 text-sm ${
                      on ? `${STATUS_TONE[g.status]} font-semibold` : 'bg-zinc-100 text-zinc-500 dark:bg-zinc-800/70 dark:text-zinc-400'
                    }`}
                  >
                    {STATE_LABEL[s]}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs text-zinc-500">
        {current
          ? `${STATE_LABEL[current]} for ${fmtAge(now - (since ?? now))}, from ${source}.`
          : fine
            ? `Last reported state ${STATE_LABEL[fine] ?? fine} doesn't match status ${status}; waiting for the next report.`
            : status === 'offline'
              ? 'Robot is offline.'
              : 'No detailed state reported yet; showing the coarse status only.'}
      </p>
    </div>
  );
}

function Tdm78ControlHolder({
  tasks,
  name,
  now,
}: {
  tasks: readonly { id: bigint; status: string; assigneeId: bigint; leaseExpiresAt?: { microsSinceUnixEpoch: bigint } | null }[];
  name: (id: bigint) => string | undefined;
  now: number;
}) {
  const live = tasks.filter((t) => t.status === 'claimed' && t.leaseExpiresAt && Number(t.leaseExpiresAt.microsSinceUnixEpoch / 1000n) > now);
  if (live.length === 0) return <p className="text-sm text-zinc-600 dark:text-zinc-400">Nobody holds a control lease. The factory drives the arm.</p>;
  return (
    <ul className="space-y-1 text-sm">
      {live.map((t) => {
        const left = Number(t.leaseExpiresAt!.microsSinceUnixEpoch / 1000n) - now;
        return (
          <li key={String(t.id)}>
            <span className="font-medium">{name(t.assigneeId) ?? `Contributor #${t.assigneeId}`}</span> holds task #{String(t.id)}
            <span className="tabular-nums text-zinc-500">, lease {fmtAge(left)} left</span>
          </li>
        );
      })}
    </ul>
  );
}

function Tdm78Safety({ safety }: { safety: ReturnType<typeof useRobotConsole>['safety'] }) {
  const any = Object.values(safety.counts).some((c) => c != null);
  return (
    <>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {TDM78_SAFETY_KINDS.map(([k, label]) => (
          <div key={k} className="flex justify-between gap-2">
            <dt className="text-zinc-600 dark:text-zinc-400">{label}</dt>
            <dd className={`tabular-nums ${safety.counts[k] ? 'font-semibold' : 'text-zinc-400'}`}>{safety.counts[k] ?? 0}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-zinc-500">
        {any
          ? `Last: ${safety.lastReason ?? safety.lastKind}${safety.speedScale != null ? `; speed scale ${safety.speedScale}` : ''}. `
          : 'Nothing clamped or rejected. '}
        Counts come from robot.limit_hit events, which are rate-limited, so a busy second can lag.
      </p>
    </>
  );
}

function Tdm78CommandTable({ commands, now }: { commands: readonly CommandRow[]; now: number }) {
  if (commands.length === 0) return <p className="text-sm text-zinc-600 dark:text-zinc-400">No commands yet. Commands you send appear here with the robot&apos;s ack and result.</p>;
  const open = commands.filter((c) => c.status === 'issued' || c.status === 'acked');
  const rows = [...open, ...commands.filter((c) => !open.includes(c))].slice(0, 30);
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-[40rem] text-left text-sm">
        <thead className="text-xs text-zinc-500">
          <tr>
            <th className="py-1 pr-3 font-medium">Seq</th>
            <th className="py-1 pr-3 font-medium">Command</th>
            <th className="py-1 pr-3 font-medium">Status</th>
            <th className="py-1 pr-3 font-medium">Issued</th>
            <th className="py-1 pr-3 font-medium">Ack</th>
            <th className="py-1 pr-3 font-medium">Took</th>
            <th className="py-1 font-medium">Result</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
          {rows.map((c) => {
            const issued = tdm78Ms(c.issuedAt)!;
            const acked = tdm78Ms(c.ackedAt);
            const done = tdm78Ms(c.finishedAt);
            const res = tdm78Json(c.resultJson);
            const isOpen = c.status === 'issued' || c.status === 'acked';
            let result = '';
            if (c.status === 'preempted') result = `Preempted by e-stop${res?.estop_seq != null ? ` #${String(res.estop_seq)}` : ''}`;
            else if (c.status === 'failed') result = String(res?.error ?? c.resultJson ?? 'failed');
            else if (res) result = Object.entries(res).filter(([k]) => k !== 'state').map(([k, val]) => `${k}: ${typeof val === 'object' ? JSON.stringify(val) : String(val)}`).join(', ');
            return (
              <tr key={String(c.id)} className={isOpen ? 'bg-sky-50/60 dark:bg-sky-950/20' : c.type === 'soft_estop' ? 'bg-red-50/60 dark:bg-red-950/20' : ''}>
                <td className="py-1.5 pr-3 tabular-nums">{String(c.seq)}</td>
                <td className="py-1.5 pr-3">
                  <span className="font-medium">{c.type}</span>
                  <span className="block text-xs text-zinc-500">{c.issuedBy}</span>
                </td>
                <td className={`py-1.5 pr-3 font-medium ${CMD_TONE[c.status] ?? ''}`}>{isOpen && c.status === 'issued' ? 'issued, waiting' : c.status}</td>
                <td className="py-1.5 pr-3 tabular-nums text-zinc-600 dark:text-zinc-400">{clock(issued)}</td>
                <td className="py-1.5 pr-3 tabular-nums text-zinc-600 dark:text-zinc-400">{acked ? `${acked - issued} ms` : isOpen ? `${fmtAge(now - issued)}…` : ''}</td>
                <td className="py-1.5 pr-3 tabular-nums text-zinc-600 dark:text-zinc-400">{done ? `${((done - issued) / 1000).toFixed(1)} s` : ''}</td>
                <td className="max-w-[18rem] truncate py-1.5 text-xs text-zinc-600 dark:text-zinc-400" title={c.resultJson ?? ''}>
                  {result}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
