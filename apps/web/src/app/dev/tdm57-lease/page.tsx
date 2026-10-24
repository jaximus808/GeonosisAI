'use client';

// TDM-57 dev harness for exclusive control (not linked from the nav).
//   /dev/tdm57-lease?task=<id>
// Mounts useTeleopLease + <ControlLeaseBanner/> + <ControlHolderPanel/>, and a "hold to drive" button
// that sends 30 Hz stamped cmds to the robot's /control socket (the [MOCK] server in
// spikes/tdm-57-lease until TDM-52 lands). Use two browser identities (e.g. localhost vs 127.0.0.1)
// to see view-only, lapse, release, revoke and reclaim. The real page is TDM-56's /teleop/[taskId].

import { useEffect, useRef, useState } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { ControlHolderPanel, ControlLeaseBanner } from '@/components/teleop/ControlLease';
import { useTeleopLease } from '@/lib/teleop/tdm57Lease';
import { taskStreamGrant } from '@/lib/teleop/tdm55StreamGrant';

type Robot = { connected: boolean; acks: number; rejects: number; lastReject?: string; lost?: string; closeCode?: number };

export default function Tdm57LeaseDevPage() {
  const [taskId, setTaskId] = useState<string | null>(null);
  useEffect(() => setTaskId(new URLSearchParams(window.location.search).get('task') ?? ''), []);
  if (taskId === null) return null;
  if (!taskId) return <p className="text-sm">Pass ?task=&lt;id&gt;.</p>;
  return <Harness taskId={taskId} />;
}

function Harness({ taskId }: { taskId: string }) {
  const lease = useTeleopLease(taskId);
  const { identity } = useSpacetimeDB();
  const [driving, setDriving] = useState(false);
  const [sent, setSent] = useState(0);
  const [gated, setGated] = useState(0);
  const [robot, setRobot] = useState<Robot>({ connected: false, acks: 0, rejects: 0 });
  const ws = useRef<WebSocket | null>(null);
  const seq = useRef(0);
  const gen = lease.leaseToken?.claimedAtUs;
  const held = lease.state === 'held';

  // One control socket per held claim generation; closed the moment we stop holding it.
  useEffect(() => {
    if (!held || !identity || gen === undefined) return;
    let closed = false;
    let refresh: number | undefined;
    const getGrant = taskStreamGrant(taskId, identity.toHexString());
    (async () => {
      try {
        const g = await getGrant();
        if (closed) return;
        const url = g.stream_url.replace(/\/ws\/cameras$/, '/control');
        const sock = new WebSocket(url);
        ws.current = sock;
        sock.onopen = () => sock.send(JSON.stringify({ type: 'auth', token: g.token }));
        sock.onmessage = (ev) => {
          const m = JSON.parse(String(ev.data)) as { t?: string; code?: string };
          setRobot((r) => {
            if (m.t === 'auth_ok') return { ...r, connected: true, lost: undefined, closeCode: undefined };
            if (m.t === 'ack') return { ...r, acks: r.acks + 1 };
            if (m.t === 'rejected') return { ...r, rejects: r.rejects + 1, lastReject: m.code };
            if (m.t === 'lease_lost' || m.t === 'error') return { ...r, lost: m.code };
            return r;
          });
        };
        sock.onclose = (ev) => setRobot((r) => ({ ...r, connected: false, closeCode: ev.code }));
        refresh = window.setInterval(async () => {
          try {
            const n = await getGrant();
            if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ type: 'auth', token: n.token }));
          } catch {
            /* lease gone: the robot closes us */
          }
        }, 25_000);
      } catch (e) {
        setRobot((r) => ({ ...r, lost: `grant: ${(e as Error).message}` }));
      }
    })();
    return () => {
      closed = true;
      window.clearInterval(refresh);
      ws.current?.close();
      ws.current = null;
    };
  }, [held, gen, identity, taskId]);

  // 30 Hz while the button is held. The gate (lease.stamp) is the only way a command goes out.
  useEffect(() => {
    if (!driving) return;
    const id = window.setInterval(() => {
      const msg = lease.stamp({
        t: 'cmd',
        seq: ++seq.current,
        ts_ms: Date.now(),
        clutch: true,
        d_pos: [0, 0, 0.001],
        d_rot: [0, 0, 0],
        gripper: 0.5,
        speed_scale: 0.3,
      });
      if (!msg) {
        setGated((n) => n + 1);
        setDriving(false); // clutch released: control is gone
        return;
      }
      const sock = ws.current;
      if (sock?.readyState === WebSocket.OPEN) {
        sock.send(JSON.stringify(msg));
        setSent((n) => n + 1);
      }
    }, 33);
    return () => window.clearInterval(id);
  }, [driving, lease]);

  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Exclusive control (TDM-57 dev)</h1>
      <p className="mb-4 text-sm text-zinc-500">
        Task #{taskId}. The robot side here is a <strong>[MOCK]</strong> /control server unless TDM-52 is running.
      </p>
      <ControlLeaseBanner lease={lease} className="mb-4" />
      <button
        type="button"
        id="tdm57-drive"
        disabled={!lease.canControl}
        onPointerDown={() => lease.canControl && setDriving(true)}
        onPointerUp={() => setDriving(false)}
        onPointerLeave={() => setDriving(false)}
        onClick={() => lease.canControl && setDriving((d) => !d)}
        className="mb-4 min-h-14 w-full rounded-lg bg-[#2a52be] text-lg font-semibold text-white disabled:bg-zinc-400"
      >
        {driving ? 'Driving… (click to release)' : lease.canControl ? 'Hold / click to drive' : 'Controls disabled'}
      </button>
      {lease.task && (
        <div className="mb-4">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-500">Robot page panel</h2>
          <ControlHolderPanel robotId={lease.task.robotId} />
        </div>
      )}
      <pre id="tdm57-debug" className="rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-900">
        {JSON.stringify(
          {
            identity: identity?.toHexString().slice(0, 12),
            state: lease.state,
            canControl: lease.canControl,
            blockedBy: lease.blockedBy,
            secondsLeft: lease.secondsLeft,
            holderName: lease.holderName,
            lostReason: lease.lostReason,
            lostWhileHidden: lease.lostWhileHidden,
            error: lease.error,
            claimedAtUs: gen?.toString(),
            driving,
            sent,
            gated,
            robot,
          },
          null,
          1,
        )}
      </pre>
    </>
  );
}
