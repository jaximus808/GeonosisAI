'use client';

import Link from 'next/link';
import { useEffect, useState, type FormEvent } from 'react';
import { useContributor, useContributorPresence, type ContributorKind } from '@/lib/contributor/hooks';
import { PushOptIn } from '@/components/tdm65/PushOptIn';
import { TextOptIn } from '@/components/tdm113/TextOptIn';

const DEVICES: { id: string; label: string }[] = [
  { id: 'phone', label: 'Phone' },
  { id: 'android_webxr', label: 'Android Chrome (WebXR motion control)' },
  { id: 'laptop', label: 'Laptop' },
  { id: 'leader_arm', label: 'Leader arm' },
];

function guessDevices(): string[] {
  if (typeof navigator === 'undefined') return [];
  const ua = navigator.userAgent;
  const mobile = /Android|iPhone|iPad|Mobile/i.test(ua);
  const out = [mobile ? 'phone' : 'laptop'];
  if (/Android/i.test(ua) && 'xr' in navigator) out.push('android_webxr');
  return out;
}

const input =
  'w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-base outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900';

export function JoinForm({ ticket }: { ticket?: string }) {
  const { contributor, ready, register, identity } = useContributor();
  useContributorPresence('join');

  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [devices, setDevices] = useState<string[]>([]);
  const [kind, setKind] = useState<ContributorKind>('contributor');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => setDevices(guessDevices()), []);

  // Prefill when editing an existing sign-up.
  useEffect(() => {
    if (contributor && editing) {
      setName(contributor.name);
      setDevices([...contributor.devices]);
      setKind(contributor.kind as ContributorKind);
    }
  }, [contributor, editing]);

  async function submit(anonymous: boolean) {
    setBusy(true);
    setError('');
    try {
      await register({
        name: anonymous ? '' : name,
        email: anonymous ? '' : email,
        devices,
        kind: contributor ? (contributor.kind as ContributorKind) : kind,
        sourceTicketId: ticket ? BigInt(ticket) : 0n,
      });
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void submit(false);
  }

  if (!ready) {
    return <p className="text-sm text-zinc-500">Connecting…</p>;
  }

  if (contributor && !editing) {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 dark:border-emerald-900 dark:bg-emerald-950/40">
          <p className="flex items-center gap-2 font-medium text-emerald-900 dark:text-emerald-200">
            <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" aria-hidden />
            You&apos;re in as {contributor.name}
          </p>
          <p className="mt-1 text-sm text-emerald-800 dark:text-emerald-300">
            Contributor #{String(contributor.id)} · {contributor.online ? 'online' : 'offline'}
            {contributor.kind === 'dataset_seller' ? ' · dataset seller' : ''}
          </p>
          <p className="mt-2 text-xs text-emerald-800/80 dark:text-emerald-300/80">
            This browser remembers you. Turn on task alerts below, or keep this tab or the tasks page open to see new tasks.
          </p>
        </div>
        {/* TDM-65: paid-task push alerts. */}
        <PushOptIn />
        {/* TDM-113: paid jobs by text (iMessage), reply YES to claim. */}
        <TextOptIn />
        <div className="flex flex-wrap gap-3">
          <Link href="/marketplace" className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900">
            See paid tasks
          </Link>
          {ticket && (
            <Link href={`/tickets/${ticket}`} className="rounded-md border border-zinc-300 px-4 py-2 text-sm dark:border-zinc-700">
              Ticket #{ticket}
            </Link>
          )}
          <button type="button" onClick={() => setEditing(true)} className="px-2 py-2 text-sm text-zinc-500 underline">
            Edit details
          </button>
        </div>
        <p className="text-xs text-zinc-400">
          Payouts are Stripe TEST MODE: no real money. {contributor.payoutsEnabled ? 'Payouts are set up.' : 'Payouts are not set up yet.'}
        </p>
      </div>
    );
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <label className="block">
        <span className="mb-1 block text-sm font-medium">Display name</span>
        <input className={input} value={name} onChange={(e) => setName(e.target.value)} maxLength={64} placeholder="e.g. Sam" autoComplete="nickname" />
      </label>
      <label className="block">
        <span className="mb-1 block text-sm font-medium">Email <span className="font-normal text-zinc-500">(optional, for payouts)</span></span>
        <input className={input} type="email" value={email} onChange={(e) => setEmail(e.target.value)} maxLength={254} placeholder="you@example.com" autoComplete="email" inputMode="email" />
        <span className="mt-1 block text-xs text-zinc-500">Kept private. Never shown on the dashboard.</span>
      </label>
      <fieldset>
        <legend className="mb-1 text-sm font-medium">Devices you can use</legend>
        <div className="space-y-1">
          {DEVICES.map((d) => (
            <label key={d.id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={devices.includes(d.id)}
                onChange={(e) => setDevices((cur) => (e.target.checked ? [...cur, d.id] : cur.filter((x) => x !== d.id)))}
              />
              {d.label}
            </label>
          ))}
        </div>
      </fieldset>
      {!contributor && (
        <fieldset>
          <legend className="mb-1 text-sm font-medium">I want to</legend>
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input type="radio" name="kind" checked={kind === 'contributor'} onChange={() => setKind('contributor')} />
              Do robot tasks
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="kind" checked={kind === 'dataset_seller'} onChange={() => setKind('dataset_seller')} />
              Sell a dataset
            </label>
          </div>
        </fieldset>
      )}
      {error && <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-300">{error}</p>}
      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" disabled={busy} className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">
          {busy ? 'Joining…' : contributor ? 'Save' : 'Join'}
        </button>
        {!contributor && (
          <button type="button" disabled={busy} onClick={() => void submit(true)} className="text-sm text-zinc-600 underline disabled:opacity-50 dark:text-zinc-400">
            Skip, join as a guest
          </button>
        )}
        {contributor && (
          <button type="button" onClick={() => setEditing(false)} className="text-sm text-zinc-500 underline">
            Cancel
          </button>
        )}
      </div>
      <p className="text-xs text-zinc-400">
        No password: this browser&apos;s identity{identity ? ` (${identity.slice(0, 8)}…)` : ''} is your account. Clearing site data signs you out.
      </p>
    </form>
  );
}
