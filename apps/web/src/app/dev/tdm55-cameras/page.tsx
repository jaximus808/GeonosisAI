'use client';

// TDM-55 dev harness for the camera feed (not linked from the nav).
//   /dev/tdm55-cameras?url=wss://<tunnel>/ws/cameras&token=<ops token>   (token: python -m rdf_common.stream_token mint)
//   add &http=<base> to override the snapshot-fallback base URL (fallback test: point url at a dead port)
//   /dev/tdm55-cameras?task=<id>                                         (claimant path through the orchestrator)

import { useEffect, useMemo, useState } from 'react';
import { CameraFeed, TaskCameraFeed, type FeedHealth } from '@/components/teleop/CameraFeed';
import { staticStreamGrant } from '@/lib/teleop/tdm55StreamGrant';

export default function Tdm55CamerasDevPage() {
  const [q, setQ] = useState<{ url: string; token: string; task: string; http: string } | null>(null);
  const [health, setHealth] = useState<FeedHealth | null>(null);
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    setQ({ url: p.get('url') ?? '', token: p.get('token') ?? '', task: p.get('task') ?? '', http: p.get('http') ?? '' });
  }, []);
  const grant = useMemo(() => (q?.url && q.token ? staticStreamGrant(q.url, q.token, q.http || undefined) : null), [q]);

  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">Camera feed (TDM-55 dev)</h1>
      <p className="mb-4 text-sm text-zinc-500">
        Dev harness. Pass <code>?url=…&amp;token=…</code> (ops token) or <code>?task=&lt;id&gt;</code> (you must hold the claim).
      </p>
      {q?.task ? (
        <TaskCameraFeed taskId={q.task} onHealth={setHealth} />
      ) : grant ? (
        <CameraFeed getGrant={grant} onHealth={setHealth} />
      ) : (
        <p className="text-sm">No stream given.</p>
      )}
      {health && (
        <pre id="tdm55-health" className="mt-4 rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-900">
          {JSON.stringify(health)}
        </pre>
      )}
    </>
  );
}
