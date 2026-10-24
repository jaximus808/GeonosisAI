'use client';

// TDM-78: camera health + live feed on the robot page, on TDM-55's CameraFeed.
//
// Who sees the feed follows TDM-55's rule (stream tokens go to the task claimant only; whether
// the dashboard shows it to others is morning question 14):
//   * this browser holds the live claim of a task on the robot -> TaskCameraFeed (orchestrator token)
//   * an ops token in the URL fragment (never sent to a server):
//       /robots/1#stream_token=<python -m rdf_common.stream_token mint --robot-id 1 --sub ops:you>
//     optionally &stream_url=ws(s)://.../ws/cameras (default: robot.camera_config_json.stream.url)
//   * otherwise: health from camera_config_json and a note on how to watch.

import { useEffect, useMemo, useState } from 'react';
import { CameraFeed, TaskCameraFeed, type FeedHealth } from '@/components/teleop/CameraFeed';
import { useContributor } from '@/lib/contributor/hooks';
import { staticStreamGrant } from '@/lib/teleop/tdm55StreamGrant';
import { tdm78Json } from '@/lib/tdm78/useRobotConsole';

interface TaskLike {
  id: bigint;
  status: string;
  assigneeId: bigint;
  leaseExpiresAt?: { microsSinceUnixEpoch: bigint } | null;
}

function useTdm78HashToken() {
  const [v, setV] = useState<{ token: string; url?: string } | null>(null);
  useEffect(() => {
    const read = () => {
      const p = new URLSearchParams(window.location.hash.replace(/^#/, ''));
      const token = p.get('stream_token');
      setV(token ? { token, url: p.get('stream_url') ?? undefined } : null);
    };
    read();
    window.addEventListener('hashchange', read);
    return () => window.removeEventListener('hashchange', read);
  }, []);
  return v;
}

export function Tdm78CameraHealth({ configJson, health }: { configJson: string; health: FeedHealth | null }) {
  const cfg = tdm78Json(configJson) ?? {};
  const mode = typeof cfg.cameras === 'string' ? cfg.cameras : null;
  const stream = (cfg.stream && typeof cfg.stream === 'object' ? cfg.stream : null) as Record<string, unknown> | null;
  const names = stream && Array.isArray(stream.cams) ? (stream.cams as unknown[]).map(String) : [];
  let line: string;
  if (stream) line = `Streaming ${names.length ? names.join(' + ') : 'cameras'}${stream.fps ? ` at ${String(stream.fps)} fps` : ''}${stream.tunnel && stream.tunnel !== 'none' ? ` over ${String(stream.tunnel)}` : ''}.`;
  else if (!mode) line = "The robot-client didn't report its cameras.";
  else if (mode === 'off') line = 'Cameras are off on this robot-client.';
  else if (cfg.synthetic) line = 'Synthetic test cameras, not real images. No stream server announced.';
  else line = 'Cameras are on, but no stream server is announced yet.';
  const updated = stream && typeof stream.updated_at === 'number' ? Math.round(Date.now() / 1000 - stream.updated_at) : null;
  return (
    <div className="space-y-1 text-sm">
      <p>{line}</p>
      {health && (
        <p className={health.stale ? 'font-medium text-red-700 dark:text-red-400' : 'text-emerald-700 dark:text-emerald-400'}>
          Feed {health.state}
          {health.stale ? ', image is stale' : ''}
          {health.worstLatencyMs != null ? `, worst latency ${Math.round(health.worstLatencyMs)} ms` : ''}.
        </p>
      )}
      {updated != null && <p className="text-xs text-zinc-500">Stream announced {updated < 120 ? `${updated} s` : `${Math.round(updated / 60)} min`} ago.</p>}
      {Boolean(cfg.sim) && <p className="text-xs text-zinc-500">Simulated arm.</p>}
    </div>
  );
}

export function Tdm78LiveFeed({
  configJson,
  tasks,
  now,
  onHealth,
}: {
  configJson: string;
  tasks: readonly TaskLike[];
  now: number;
  onHealth: (h: FeedHealth | null) => void;
}) {
  const { contributor } = useContributor();
  const hash = useTdm78HashToken();
  const cfg = tdm78Json(configJson) ?? {};
  const stream = (cfg.stream && typeof cfg.stream === 'object' ? cfg.stream : null) as Record<string, unknown> | null;
  const streamUrl = hash?.url ?? (typeof stream?.url === 'string' ? stream.url : undefined);
  const myTask = contributor
    ? tasks.find(
        (t) =>
          t.status === 'claimed' &&
          t.assigneeId === contributor.id &&
          t.leaseExpiresAt &&
          Number(t.leaseExpiresAt.microsSinceUnixEpoch / 1000n) > now,
      )
    : undefined;
  const opsGrant = useMemo(
    () => (hash?.token && streamUrl ? staticStreamGrant(streamUrl, hash.token, typeof stream?.http === 'string' ? stream.http : undefined) : null),
    [hash?.token, streamUrl, stream?.http],
  );

  useEffect(() => {
    if (!myTask && !opsGrant) onHealth(null);
  }, [myTask, opsGrant, onHealth]);

  if (myTask) return <TaskCameraFeed taskId={myTask.id} onHealth={onHealth} />;
  if (opsGrant) return <CameraFeed key={streamUrl} getGrant={opsGrant} onHealth={onHealth} />;
  return (
    <div className="rounded-md bg-zinc-100 p-4 text-sm text-zinc-700 dark:bg-zinc-800/60 dark:text-zinc-300">
      <p>
        {hash?.token && !streamUrl
          ? "This robot hasn't announced a stream URL; add &stream_url=ws(s)://…/ws/cameras after the token."
          : 'The live feed shows to whoever holds the arm-control task, as on the teleop page.'}
      </p>
      {!hash?.token && (
        <p className="mt-1 text-xs text-zinc-500">
          To watch as an operator, mint a short-lived token on the laptop with{' '}
          <code className="font-mono">python -m rdf_common.stream_token mint --robot-id &lt;id&gt; --sub ops:&lt;you&gt;</code> and open this page with{' '}
          <code className="font-mono">#stream_token=&lt;token&gt;</code> at the end of the address.
        </p>
      )}
    </div>
  );
}
