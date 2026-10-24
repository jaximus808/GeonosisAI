'use client';

import Link from 'next/link';
import { useState } from 'react';
import { useOperator } from '@/lib/operator/useOperator';

export function OpsLoginForm({ next }: { next: string }) {
  const op = useOperator();
  const [passcode, setPasscode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function login(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/ops/session', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ passcode }),
        cache: 'no-store',
      });
      const b = (await res.json().catch(() => ({}))) as { detail?: string };
      if (!res.ok) {
        setError(b.detail ?? `HTTP ${res.status}`);
        return;
      }
      setPasscode('');
      window.location.assign(next);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    await fetch('/api/ops/session', { method: 'DELETE', cache: 'no-store' }).catch(() => null);
    setBusy(false);
    op.recheck();
  }

  if (op.operator) {
    return (
      <div className="space-y-4 rounded-lg border border-zinc-200 bg-white p-4 text-sm dark:border-zinc-800 dark:bg-zinc-900/60">
        <p>
          Logged in as operator{op.expiresAt ? ` until ${new Date(op.expiresAt).toLocaleTimeString()}` : ''}.
        </p>
        <div className="flex gap-2">
          <Link href={next} className="rounded-md bg-zinc-900 px-3 py-2 font-medium text-white dark:bg-zinc-100 dark:text-zinc-900">
            Continue
          </Link>
          <button
            type="button"
            onClick={() => void logout()}
            disabled={busy}
            className="rounded-md border border-zinc-300 px-3 py-2 font-medium dark:border-zinc-700"
          >
            Log out
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={(e) => void login(e)} className="space-y-3 rounded-lg border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900/60">
      {!op.configured && (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          Operator login is not set up on this server: set RDF_OPERATOR_PASSCODE and restart the web app.
        </p>
      )}
      <label htmlFor="ops-passcode" className="block text-sm font-medium">
        Operator passcode
      </label>
      <input
        id="ops-passcode"
        type="password"
        autoComplete="current-password"
        autoFocus
        required
        value={passcode}
        onChange={(e) => setPasscode(e.target.value)}
        className="w-full rounded-md border border-zinc-300 bg-white px-2 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
      />
      <button
        type="submit"
        disabled={busy || !passcode}
        className="w-full rounded-md bg-zinc-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {busy ? 'Checking…' : 'Log in'}
      </button>
      <p aria-live="polite" className="min-h-5 text-sm text-red-700 dark:text-red-400">
        {error}
      </p>
    </form>
  );
}
