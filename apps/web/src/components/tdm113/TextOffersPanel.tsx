'use client';

// TDM-113: /ops/drops panel for text job offers: mode, opt-in counts, the Inkbox line, and the outbox (every
// would-be text in dry run, every sent text in live; numbers masked to the last 4 digits). Operator login.

import { useEffect, useState } from 'react';

type Tdm113Status = {
  mode: 'dry_run' | 'live';
  fanout: number;
  opted_in: number;
  in_person: number;
  stopped: number;
  offers_24h: number;
  claims_24h: number;
  inkbox: { configured: boolean; handle?: string; channel?: string; line?: string | null; listener: string };
  outbox: { at: number; to: string; kind: string; state: string; text: string }[];
};

export function TextOffersPanel() {
  const [s, setS] = useState<Tdm113Status | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    let live = true;
    const load = () =>
      fetch('/api/tdm113/status', { cache: 'no-store' })
        .then(async (r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((j) => live && (setS(j), setErr('')))
        .catch((e: Error) => live && setErr(e.message));
    void load();
    const t = setInterval(load, 10_000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  if (err && !s) return <p className="text-sm text-zinc-500">Text offers: {err === 'HTTP 401' ? 'log in as operator to see them.' : `not reachable (${err}).`}</p>;
  if (!s) return null;
  return (
    <section aria-label="Text offers" className="rounded-lg border border-zinc-200 p-4 text-sm dark:border-zinc-800">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-semibold">Text offers (iMessage)</h2>
        <span className={`rounded px-2 py-0.5 font-mono text-xs ${s.mode === 'live' ? 'bg-emerald-600 text-white' : 'border border-zinc-300 text-zinc-600 dark:border-zinc-700'}`}>
          {s.mode === 'live' ? 'live' : 'dry run'}
        </span>
      </div>
      <p className="mt-1 text-zinc-500">
        {s.opted_in} opted in ({s.in_person} in person), {s.stopped} stopped · {s.offers_24h} offers, {s.claims_24h} claims in 24 h · up to {s.fanout} per job.
        {' '}
        {s.inkbox.configured ? `Line ${s.inkbox.line ?? '(shared router)'} as @${s.inkbox.handle}, tunnel ${s.inkbox.listener}.` : 'Inkbox not set up yet.'}
      </p>
      {s.outbox.length > 0 && (
        <ul className="mt-2 space-y-1 font-mono text-xs">
          {s.outbox
            .slice()
            .reverse()
            .slice(0, 10)
            .map((m, i) => (
              <li key={`${m.at}-${i}`} className="break-words">
                <span className="text-zinc-500">{new Date(m.at * 1000).toLocaleTimeString()} {m.to} {m.kind} [{m.state}]</span> {m.text}
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}
