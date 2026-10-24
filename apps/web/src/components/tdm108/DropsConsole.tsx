'use client';

// TDM-108: /ops/drops. Recent drops with each channel's card, post text and state, a "Post now" per
// channel (live mode only; the orchestrator refuses it in dry run), and which channel converted.

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';

type ChannelState = {
  state: string;
  link?: string;
  size?: string;
  text?: string;
  configured?: boolean;
  post_url?: string | null;
  error?: string | null;
  cost_estimate_usd?: number;
  posted_at?: number;
};
type Drop = {
  drop_id: string;
  task_ids: number[];
  ticket_id: number;
  task_type: string;
  price_cents: number;
  title: string;
  robot: string;
  created_at: number;
  mode: string;
  sample: boolean;
  frame: string;
  lifecycle?: 'open' | 'taken' | 'done';
  channels: Record<string, ChannelState>;
};
type Status = {
  mode: 'dry_run' | 'live';
  channels: Record<string, boolean>;
  max_per_hour: number;
  posted_last_hour: number;
  public_url: string;
  show_access_code: boolean;
  test_mode: boolean;
  types: string[];
  drops: Drop[];
  attribution: { by_src: Record<string, { claims: number; posts: number }> };
};

const CHANNELS = ['discord', 'bluesky', 'x', 'telegram'] as const;
const LABEL: Record<string, string> = { discord: 'Discord', bluesky: 'Bluesky', x: 'X', telegram: 'Telegram' };
const money = (c: number) => `$${Math.floor(c / 100)}.${String(c % 100).padStart(2, '0')}`;
const when = (s: number) => new Date(s * 1000).toLocaleString();

const LIFECYCLE: Record<string, string> = {
  open: 'open',
  taken: 'claimed: posts marked, not available',
  done: 'finished: posts deleted',
};

/** Which channels this ticket's jobs go to (a job reaches only the communities the operator picks). */
function TicketChannels({ ticketId, configured }: { ticketId: number; configured: Record<string, boolean> }) {
  const [on, setOn] = useState<Record<string, boolean> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    void fetch(`/api/tdm108/drops/tickets/${ticketId}/channels`, { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((b: { channels?: Record<string, boolean> } | null) => live && b?.channels && setOn(b.channels))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [ticketId]);
  async function toggle(c: string) {
    if (!on) return;
    setBusy(true);
    try {
      const r = await fetch(`/api/tdm108/drops/tickets/${ticketId}/channels`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ channels: { [c]: !on[c] } }),
      });
      const b = (await r.json().catch(() => null)) as { channels?: Record<string, boolean> } | null;
      if (r.ok && b?.channels) setOn(b.channels);
    } finally {
      setBusy(false);
    }
  }
  if (!on) return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
      <span className="text-zinc-500">Ticket #{ticketId} posts to:</span>
      {CHANNELS.map((c) => (
        <button
          key={c}
          type="button"
          disabled={busy}
          aria-pressed={on[c]}
          onClick={() => void toggle(c)}
          title={configured[c] ? undefined : 'No credentials set for this channel'}
          className={`rounded px-2 py-0.5 font-mono ${on[c] ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : 'border border-zinc-300 text-zinc-500 line-through dark:border-zinc-700'} disabled:opacity-50`}
        >
          {LABEL[c]}
        </button>
      ))}
      <span className="text-zinc-500">Affects new posts and reposts.</span>
    </div>
  );
}

export function DropsConsole() {
  const [data, setData] = useState<Status | null>(null);
  const [err, setErr] = useState<{ status: number; detail: string } | null>(null);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [taskId, setTaskId] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/tdm108/drops', { cache: 'no-store' });
      const b = (await r.json().catch(() => ({}))) as Status & { detail?: string };
      if (!r.ok) {
        setErr({ status: r.status, detail: typeof b.detail === 'string' ? b.detail : `HTTP ${r.status}` });
        return;
      }
      setErr(null);
      setData(b);
    } catch (e) {
      setErr({ status: 0, detail: (e as Error).message });
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(t);
  }, [load]);

  async function post(dropId: string, channel: string) {
    setBusy(`${dropId}:${channel}`);
    setNote('');
    try {
      const r = await fetch(`/api/tdm108/drops/${dropId}/post`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ channel }),
      });
      const b = (await r.json().catch(() => ({}))) as { detail?: string; state?: string; error?: string };
      setNote(r.ok ? `${LABEL[channel]}: ${b.state}` : `${LABEL[channel]}: ${b.detail ?? b.error ?? `HTTP ${r.status}`}`);
      await load();
    } finally {
      setBusy('');
    }
  }

  async function preview() {
    if (!/^\d+$/.test(taskId)) return;
    setBusy('preview');
    setNote('');
    try {
      const r = await fetch('/api/tdm108/drops/preview', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ task_id: Number(taskId) }),
      });
      const b = (await r.json().catch(() => ({}))) as { detail?: string };
      setNote(r.ok ? `Built the dry-run drop for task #${taskId}.` : (b.detail ?? `HTTP ${r.status}`));
      await load();
    } finally {
      setBusy('');
    }
  }

  if (err?.status === 401 || err?.status === 503) {
    return (
      <p className="rounded-md border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700">
        {err.detail}{' '}
        <Link href="/ops/login?next=/ops/drops" className="font-semibold underline underline-offset-2">
          Operator login
        </Link>
      </p>
    );
  }
  if (err) return <p className="text-sm text-[#a3243b]">Could not load drops: {err.detail}</p>;
  if (!data) return <p className="text-sm text-zinc-500">Loading...</p>;

  const live = data.mode === 'live';
  return (
    <div className="space-y-6">
      <section className="grid gap-3 rounded-md border border-zinc-200 p-4 text-sm sm:grid-cols-2 dark:border-zinc-800">
        <div>
          <p className="text-xs uppercase tracking-wider text-zinc-500">Mode</p>
          <p className={`text-lg font-semibold ${live ? 'text-[#a84304] dark:text-[#ff7a1f]' : ''}`}>
            {live ? 'Live: posts go out' : 'Dry run: nothing is posted'}
          </p>
          <p className="text-zinc-500">
            {live
              ? `${data.posted_last_hour} of ${data.max_per_hour} drops this hour.`
              : 'Switch with RDF_DROP_MODE=live in .env and restart the orchestrator.'}
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-wider text-zinc-500">Channels</p>
          <ul className="mt-1 flex flex-wrap gap-2">
            {CHANNELS.map((c) => (
              <li key={c} className={`rounded px-2 py-0.5 font-mono text-xs ${data.channels[c] ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900' : 'border border-zinc-300 text-zinc-500 dark:border-zinc-700'}`}>
                {LABEL[c]} {data.channels[c] ? 'set' : 'off'}
              </li>
            ))}
          </ul>
          <p className="mt-2 break-all text-zinc-500">
            Links point at <span className="font-mono">{data.public_url}</span>
            {data.show_access_code ? ' (access code shown on posts)' : ''}. {data.test_mode ? 'Test mode.' : ''}
          </p>
        </div>
        <div className="sm:col-span-2">
          <p className="text-xs uppercase tracking-wider text-zinc-500">Claims by channel</p>
          {Object.keys(data.attribution.by_src).length === 0 ? (
            <p className="text-zinc-500">No posts or tracked claims yet.</p>
          ) : (
            <table className="mt-1 font-mono text-xs">
              <thead>
                <tr className="text-left text-zinc-500">
                  <th className="pr-6 font-normal">src</th>
                  <th className="pr-6 font-normal">posts</th>
                  <th className="font-normal">claims</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.attribution.by_src).map(([src, v]) => (
                  <tr key={src}>
                    <td className="pr-6">{src}</td>
                    <td className="pr-6 tabular-nums">{v.posts}</td>
                    <td className="tabular-nums">{v.claims}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <form
          className="flex items-end gap-2 sm:col-span-2"
          onSubmit={(e) => {
            e.preventDefault();
            void preview();
          }}
        >
          <label className="text-xs text-zinc-500">
            Build a dry-run drop for task #
            <input
              value={taskId}
              onChange={(e) => setTaskId(e.target.value.replace(/\D/g, ''))}
              inputMode="numeric"
              className="ml-2 w-24 rounded border border-zinc-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-zinc-700"
            />
          </label>
          <button type="submit" disabled={!taskId || busy !== ''} className="rounded border border-zinc-400 px-3 py-1 text-sm disabled:opacity-40">
            Build
          </button>
        </form>
        {note && <p className="text-sm sm:col-span-2" role="status">{note}</p>}
      </section>

      {data.drops.length === 0 ? (
        <p className="rounded-md border border-dashed border-zinc-300 px-4 py-8 text-center text-sm text-zinc-500 dark:border-zinc-700">
          No drops yet. One appears here when the factory opens a paid task.
        </p>
      ) : (
        data.drops.map((d) => (
          <section key={d.drop_id} className="rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
            <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="text-lg font-semibold">
                {d.title}
                {d.sample && <span className="ml-2 rounded bg-zinc-200 px-1.5 text-xs font-medium dark:bg-zinc-800">sample</span>}
              </h2>
              <p className="font-mono text-xs text-zinc-500">
                {d.sample ? 'no task' : `task #${d.task_ids.join(', #')}`} · {money(d.price_cents)} · {d.robot} · frame {d.frame} · {when(d.created_at)}
                {d.lifecycle && d.lifecycle !== 'open' ? ` · ${LIFECYCLE[d.lifecycle]}` : ''}
              </p>
            </div>
            {!d.sample && <TicketChannels ticketId={d.ticket_id} configured={data.channels} />}
            <div className="grid gap-4 md:grid-cols-2">
              {CHANNELS.map((c) => {
                const s = d.channels[c];
                if (!s) return null;
                const posted = s.state === 'posted';
                const why = !live ? 'Dry run' : !data.channels[c] ? 'No credentials' : posted || s.state === 'claimed_edit' ? 'Posted' : '';
                return (
                  <div key={c} className="space-y-2">
                    <div className="flex items-center justify-between text-sm">
                      <span className="font-semibold">{LABEL[c]}</span>
                      <span className="font-mono text-xs">{s.state}{s.cost_estimate_usd ? ` · est $${s.cost_estimate_usd.toFixed(3)}` : ''}</span>
                    </div>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={`/api/tdm108/drops/${d.drop_id}/card/${c}.png?v=${Math.round(d.created_at)}`}
                      alt={`${LABEL[c]} card for ${d.title}`}
                      className="w-full rounded border border-zinc-200 dark:border-zinc-800"
                      loading="lazy"
                    />
                    <pre className="whitespace-pre-wrap break-words rounded bg-zinc-50 p-2 text-xs dark:bg-zinc-900">{s.text}</pre>
                    {s.error && <p className="text-xs text-[#a3243b]">Failed: {s.error}</p>}
                    {s.post_url && (
                      <a href={s.post_url} target="_blank" rel="noreferrer" className="text-xs underline underline-offset-2">
                        Open the post
                      </a>
                    )}
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={why !== '' || busy !== ''}
                        onClick={() => void post(d.drop_id, c)}
                        className="rounded bg-zinc-900 px-3 py-1 text-sm font-semibold text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
                      >
                        {busy === `${d.drop_id}:${c}` ? 'Posting...' : 'Post now'}
                      </button>
                      {why && <span className="text-xs text-zinc-500">{why}</span>}
                      {s.link && <span className="truncate font-mono text-xs text-zinc-500">{s.link}</span>}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
