'use client';

// TDM-104: the ticket's generated 3D task scene. A 3D preview of the spec (the same twin the robot view
// uses, so what you see here is what MuJoCo builds), what is in it, how success is judged, and the
// operator's "Deploy scene" / "Regenerate" buttons. Deploy goes to the ticket's SIM robot only.

import dynamic from 'next/dynamic';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useOperator } from '@/lib/operator/useOperator';
import { cssColor, toRenderSpec, type SceneSpec, type SceneSpecObject, type TicketSceneResponse } from '@/lib/robotview/scene';

const RobotTwin3D = dynamic(() => import('@/components/robotview/RobotTwin3D').then((m) => m.RobotTwin3D), {
  ssr: false,
  loading: () => <div className="grid h-full place-items-center text-sm text-violet-200/70">Loading the 3D preview…</div>,
});

const cm = (m: number | undefined) => (m == null ? '?' : `${Math.round(m * 1000) / 10}`);

function dims(o: SceneSpecObject): string {
  if (o.primitive === 'box' && o.size) return `${cm(o.size[0])} × ${cm(o.size[1])} × ${cm(o.size[2])} cm box`;
  if (o.primitive === 'sphere') return `${cm((o.radius ?? 0) * 2)} cm ball`;
  if (o.primitive === 'plate') return `${cm((o.radius ?? 0) * 2)} cm plate`;
  return `${cm((o.radius ?? 0) * 2)} cm wide, ${cm(o.height)} cm tall cylinder`;
}

function successSentence(spec: SceneSpec): string {
  const s = spec.success;
  const name = (id?: string) => spec.objects.find((o) => o.id === id)?.label ?? id ?? '?';
  const where = s.region ? `the ${s.region.replace(/_/g, ' ')} zone` : name(s.target);
  const verb = s.type === 'object_on' ? 'rests on top of' : 'rests inside';
  return `Done when ${name(s.object)} ${verb} ${where}${s.released !== false ? ' and the gripper has let go' : ''}.`;
}

const STATUS_TEXT = {
  pending: 'Sent to the sim robot, waiting for it to load',
  loaded: 'Loaded in the simulator',
  failed: 'The sim robot could not load it',
} as const;

export function Tdm104ScenePanel({ ticketId }: { ticketId: string }) {
  const { operator } = useOperator();
  const [data, setData] = useState<TicketSceneResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rev, setRev] = useState<number | null>(null);
  const [busy, setBusy] = useState<'deploy' | 'generate' | null>(null);
  const [actionMsg, setActionMsg] = useState<{ tone: 'ok' | 'err'; text: string } | null>(null);
  const [hint, setHint] = useState('');

  const load = useCallback(async () => {
    try {
      const q = rev ? `?revision=${rev}` : '';
      const r = await fetch(`/api/tdm104/tickets/${encodeURIComponent(ticketId)}/scene${q}`, { cache: 'no-store' });
      const j = (await r.json()) as TicketSceneResponse & { detail?: string };
      if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `HTTP ${r.status}`);
      setData(j);
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, [ticketId, rev]);

  const pending = data?.deployed?.status === 'pending' || (data != null && !data.spec && !data.last_error);
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), pending ? 2_000 : 15_000);
    return () => clearInterval(t);
  }, [load, pending]);

  const render = useMemo(() => {
    try {
      return data?.spec ? toRenderSpec(data.spec) : null;
    } catch {
      return null;
    }
  }, [data?.spec]);

  const act = async (kind: 'deploy' | 'generate') => {
    setBusy(kind);
    setActionMsg(null);
    try {
      const body = kind === 'deploy' ? { revision: data?.revision ?? undefined } : { hint: hint.trim() || undefined };
      const r = await fetch(`/api/tdm104/tickets/${encodeURIComponent(ticketId)}/scene/${kind}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = (await r.json().catch(() => ({}))) as { detail?: unknown; robot_id?: number; revision?: number };
      if (!r.ok) {
        const d = j.detail as { error?: string; errors?: string[] } | string | undefined;
        const text = typeof d === 'string' ? d : d?.errors?.slice(0, 3).join('; ') || d?.error || `HTTP ${r.status}`;
        setActionMsg({ tone: 'err', text: `${kind === 'deploy' ? 'Not deployed' : 'No new scene'}: ${text}` });
      } else if (kind === 'deploy') {
        setActionMsg({ tone: 'ok', text: `Deployed revision ${j.revision} to sim robot ${j.robot_id}.` });
      } else {
        setRev(null);
        setHint('');
        setActionMsg({ tone: 'ok', text: `Revision ${j.revision} written.` });
      }
      await load();
    } catch (e) {
      setActionMsg({ tone: 'err', text: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const spec = data?.spec ?? null;
  const dep = data?.deployed ?? null;
  const showingDeployed = dep && spec && dep.revision === data?.revision;

  return (
    <section id="scene" className="mb-10 scroll-mt-6 border-t border-zinc-200 pt-8 dark:border-zinc-800">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-lg font-semibold tracking-tight">Practice scene</h2>
        <span className="text-sm text-zinc-500">a simulated table set up for this task, for practice and sim data</span>
        {data && data.revisions.length > 1 && (
          <label className="ml-auto flex items-center gap-2 text-sm text-zinc-500">
            Revision
            <select
              value={data.revision ?? ''}
              onChange={(e) => setRev(Number(e.target.value) || null)}
              className="rounded border border-zinc-300 bg-transparent px-1.5 py-0.5 text-zinc-900 dark:border-zinc-700 dark:text-zinc-100"
            >
              {data.revisions.map((r) => (
                <option key={r.revision} value={r.revision}>
                  {r.revision} ({r.source === 'llm' ? 'generated' : r.source})
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {!spec ? (
        <div className="rounded-lg border border-dashed border-zinc-300 p-6 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400">
          {loadError ? (
            <>Could not read the scene: {loadError}</>
          ) : data?.last_error ? (
            <>
              Claude could not write a working scene for this task ({data.last_error.error}).
              {operator ? ' Add a note below and regenerate.' : ' An operator can regenerate it.'}
            </>
          ) : data ? (
            <>Claude is setting up the scene for this task. It appears here in a few seconds.</>
          ) : (
            <>Loading the scene…</>
          )}
        </div>
      ) : (
        <div className="grid gap-5 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <div className="relative aspect-[4/3] overflow-hidden rounded-lg bg-violet-950 ring-2 ring-violet-500/80">
            {render && <RobotTwin3D state={null} spec={render} sim camera="top" className="h-full w-full" />}
            <span className="pointer-events-none absolute left-2 top-2 rounded bg-violet-600 px-2 py-0.5 text-xs font-semibold text-white">
              Simulated
            </span>
          </div>

          <div className="min-w-0 space-y-4 text-sm">
            <p className="text-base font-medium leading-snug">{spec.task}</p>
            <p className="text-zinc-600 dark:text-zinc-400">{successSentence(spec)}</p>

            <ul className="space-y-1.5">
              {spec.objects.map((o) => (
                <li key={o.id} className="flex items-center gap-2.5">
                  <span
                    aria-hidden
                    className={`inline-block h-3.5 w-3.5 shrink-0 border border-black/20 ${o.primitive === 'box' ? 'rounded-sm' : 'rounded-full'}`}
                    style={{ background: cssColor(o.color) }}
                  />
                  <span className="min-w-0 truncate">{o.label ?? o.id}</span>
                  <span className="ml-auto shrink-0 text-xs text-zinc-500">
                    {dims(o)}
                    {o.randomize ? ', moves every reset' : ''}
                  </span>
                </li>
              ))}
            </ul>

            <div className="rounded-md bg-zinc-100 px-3 py-2 dark:bg-zinc-900">
              {dep ? (
                <p>
                  <span className={dep.status === 'failed' ? 'text-red-700 dark:text-red-400' : dep.status === 'loaded' ? 'text-emerald-700 dark:text-emerald-400' : ''}>
                    {STATUS_TEXT[dep.status]}
                  </span>
                  <span className="text-zinc-500">
                    {' '}
                    (revision {dep.revision} on robot {dep.robot_id}
                    {showingDeployed ? '' : `; you are looking at revision ${data?.revision}`})
                  </span>
                  {dep.status === 'failed' && dep.loaded?.error && <span className="block text-xs text-zinc-500">{dep.loaded.error}</span>}
                </p>
              ) : (
                <p className="text-zinc-600 dark:text-zinc-400">Not deployed yet. The sim robot still has its previous scene.</p>
              )}
            </div>

            {operator ? (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => act('deploy')}
                    disabled={busy !== null}
                    className="rounded bg-violet-700 px-3 py-1.5 font-medium text-white hover:bg-violet-600 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-violet-500 disabled:opacity-50"
                  >
                    {busy === 'deploy' ? 'Deploying…' : `Deploy revision ${data?.revision} to the sim robot`}
                  </button>
                  <button
                    type="button"
                    onClick={() => act('generate')}
                    disabled={busy !== null}
                    className="rounded border border-zinc-300 px-3 py-1.5 font-medium hover:bg-zinc-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-zinc-500 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-800"
                  >
                    {busy === 'generate' ? 'Regenerating…' : 'Regenerate'}
                  </button>
                </div>
                <input
                  value={hint}
                  onChange={(e) => setHint(e.target.value)}
                  maxLength={1000}
                  placeholder="Note for the next regeneration, e.g. use a green plate"
                  className="w-full rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
                />
              </div>
            ) : (
              <p className="text-xs text-zinc-500">
                <a className="underline" href="/ops/login">Log in as operator</a> to deploy or regenerate the scene.
              </p>
            )}
            {actionMsg && (
              <p className={`text-xs ${actionMsg.tone === 'err' ? 'text-red-700 dark:text-red-400' : 'text-emerald-700 dark:text-emerald-400'}`}>{actionMsg.text}</p>
            )}
          </div>
        </div>
      )}
      {!spec && operator && data?.last_error && (
        <div className="mt-3 flex gap-2">
          <input
            value={hint}
            onChange={(e) => setHint(e.target.value)}
            maxLength={1000}
            placeholder="Optional note for Claude"
            className="min-w-0 flex-1 rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
          <button type="button" onClick={() => act('generate')} disabled={busy !== null}
            className="rounded bg-violet-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            {busy === 'generate' ? 'Regenerating…' : 'Regenerate'}
          </button>
        </div>
      )}
    </section>
  );
}
