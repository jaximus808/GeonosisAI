'use client';

// TDM-113 phone sign-in: "Already joined on another device?" A contributor enters the number they get job texts
// on, gets a 6-digit code by text (only numbers that opted in get one; codes expire after 10 minutes), and the
// code makes this browser the same contributor as their other device: the orchestrator hands back that device's
// SpacetimeDB token, this page keeps it and reconnects. Laptop and phone then share one account and one claim.

import { useEffect, useState, type FormEvent } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { tdm113AdoptToken, tdm113Caller, tdm113Post } from '@/lib/tdm113/client';
import { tdm113CodeOk, tdm113PhoneOk } from '@/lib/tdm113/texts';

const input =
  'w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-base outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900';

type LoginOut = { ok: boolean; contributor_id: number; switch: boolean; token?: string; name?: string };

export function PhoneSignIn({ className = '', compact = false }: { className?: string; compact?: boolean }) {
  const { identity, isActive } = useSpacetimeDB();
  const identityHex = identity?.toHexString();
  const [open, setOpen] = useState(!compact);
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [sent, setSent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState('');
  // the teleop page's "sign in with your phone number" link lands on /join#phone-sign-in: open straight away
  useEffect(() => {
    if (window.location.hash === '#phone-sign-in') setOpen(true);
  }, []);

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function sendCode(e?: FormEvent) {
    e?.preventDefault();
    if (!tdm113PhoneOk(phone)) return setError('Enter a mobile number, e.g. (415) 555-0123.');
    void act(async () => {
      const r = await tdm113Post<{ detail: string }>('login_code', { phone });
      setSent(r.detail);
    });
  }

  function verify(e: FormEvent) {
    e.preventDefault();
    const c = tdm113CodeOk(code);
    if (!c) return setError('Enter the 6-digit code from the text.');
    void act(async () => {
      const r = await tdm113Post<LoginOut>('login', { ...tdm113Caller(identityHex), phone, code: c });
      if (r.switch && r.token) {
        setDone(`Signed in as ${r.name ?? `contributor ${r.contributor_id}`}. Reconnecting…`);
        tdm113AdoptToken(r.token);
      } else {
        setDone(`This browser is already ${r.name ?? `contributor ${r.contributor_id}`}.`);
      }
    });
  }

  if (!open) {
    return (
      <button id="phone-sign-in" type="button" onClick={() => setOpen(true)} className={`text-sm text-zinc-600 underline dark:text-zinc-400 ${className}`}>
        Joined on another device? Sign in with your phone number
      </button>
    );
  }

  return (
    <section id="phone-sign-in" aria-label="Sign in with your phone" className={`rounded-lg border border-zinc-300 bg-white px-4 py-3 text-sm dark:border-zinc-700 dark:bg-zinc-900 ${className}`}>
      <p className="font-medium">Joined on another device?</p>
      <p className="mt-1 text-zinc-600 dark:text-zinc-400">
        Use the number you get job texts on. We text you a code, and this browser becomes the same account as your other device.
      </p>
      {!sent ? (
        <form onSubmit={sendCode} className="mt-3 flex flex-wrap items-end gap-2">
          <label className="min-w-0 flex-1">
            <span className="mb-1 block font-medium">Mobile number</span>
            <input className={input} type="tel" inputMode="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={32} placeholder="(415) 555-0123" />
          </label>
          <button type="submit" disabled={busy || !isActive} className="min-h-11 rounded-md bg-zinc-900 px-4 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">
            {busy ? 'Sending…' : 'Text me a code'}
          </button>
        </form>
      ) : (
        <form onSubmit={verify} className="mt-3 space-y-2">
          <p className="text-xs text-zinc-600 dark:text-zinc-400" role="status">{sent}</p>
          <div className="flex flex-wrap items-end gap-2">
            <label className="min-w-0 flex-1">
              <span className="mb-1 block font-medium">6-digit code</span>
              <input className={input} inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} maxLength={8} placeholder="123456" />
            </label>
            <button type="submit" disabled={busy || !isActive || !!done} className="min-h-11 rounded-md bg-zinc-900 px-4 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">
              {busy ? 'Checking…' : 'Sign in'}
            </button>
          </div>
          <button type="button" disabled={busy} onClick={() => (setSent(null), setCode(''))} className="text-xs text-zinc-500 underline">
            Use a different number or send a new code
          </button>
        </form>
      )}
      {done && <p className="mt-2 text-xs text-emerald-700 dark:text-emerald-300" role="status">{done}</p>}
      {error && <p className="mt-2 text-xs text-red-700 dark:text-red-300" role="alert">{error}</p>}
    </section>
  );
}
