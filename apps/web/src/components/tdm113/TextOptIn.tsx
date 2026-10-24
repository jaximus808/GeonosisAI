'use client';

// TDM-113: "Get job texts" on /join. A contributor enters a mobile number, ticks the consent text (sent back
// verbatim; the orchestrator refuses any other wording) and whether they're in person near the robot (only
// they get reset jobs). The number goes to the orchestrator's private store through /api/tdm113/* with this
// browser's own SpacetimeDB identity token; the page only ever sees the last 4 digits back.

import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { useContributor } from '@/lib/contributor/hooks';
import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';
import { TDM113_CONSENT_TEXT, tdm113PhoneOk } from '@/lib/tdm113/texts';

type Tdm113State = {
  mode: 'dry_run' | 'live';
  opted_in: boolean;
  stopped?: boolean;
  last4?: string;
  in_person?: boolean;
  line?: string | null;
};
type Tdm113Welcome = { state: string; hint?: string };

function tdm113Token(): string | null {
  try {
    return window.localStorage.getItem(BROWSER_TOKEN_KEY);
  } catch {
    return null;
  }
}

async function tdm113Post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const res = await fetch(`/api/tdm113/${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof j.detail === 'string' ? j.detail : `Request failed (${res.status})`);
  return j as T;
}

const WELCOME_NOTE: Record<string, string> = {
  sent: 'Welcome text sent. It should arrive in a few seconds.',
  dry_run: 'Dry run: the factory logged the welcome text instead of sending it.',
  text_first: 'Almost there: this line needs a first message from you.',
  blocked: 'The welcome text was blocked.',
  rate_limited: 'Texting is busy right now. Try Resend in a minute.',
  error: 'The welcome text did not go out. Try Resend in a minute.',
};

const input =
  'w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-base outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900';

export function TextOptIn({ className = '' }: { className?: string }) {
  const { contributor, ready } = useContributor();
  const { identity, isActive } = useSpacetimeDB();
  const identityHex = identity?.toHexString();
  const eligible = ready && isActive && contributor != null && contributor.kind === 'contributor' && !!identityHex;

  const [state, setState] = useState<Tdm113State | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [phone, setPhone] = useState('');
  const [inPerson, setInPerson] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [welcome, setWelcome] = useState<Tdm113Welcome | null>(null);

  const caller = useCallback(() => {
    const token = tdm113Token();
    if (!identityHex || !token) throw new Error('Not connected to the factory yet. Try again in a moment.');
    return { identity: identityHex, spacetime_token: token };
  }, [identityHex]);

  useEffect(() => {
    if (!eligible) return;
    let live = true;
    tdm113Post<Tdm113State>('state', caller())
      .then((s) => live && (setState(s), setInPerson(!!s.in_person)))
      .catch(() => live && setUnavailable(true));
    return () => {
      live = false;
    };
  }, [eligible, caller]);

  if (!eligible || unavailable || state == null) return null;

  const box = `rounded-lg border px-4 py-3 text-sm ${className}`;
  const dry = state.mode === 'dry_run';

  async function act<T>(fn: () => Promise<T>) {
    setBusy(true);
    setError('');
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!tdm113PhoneOk(phone)) return setError('Enter a mobile number, e.g. (415) 555-0123.');
    if (!consent) return setError('Tick the consent box to get job texts.');
    const r = await act(() =>
      tdm113Post<Tdm113State & { welcome: Tdm113Welcome }>('optin', {
        ...caller(),
        phone,
        in_person: inPerson,
        consent: true,
        consent_text: TDM113_CONSENT_TEXT,
      }),
    );
    if (r) {
      setState(r);
      setWelcome(r.welcome);
      setPhone('');
    }
  }

  if (state.opted_in) {
    return (
      <section aria-label="Job texts" className={`${box} border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-100`}>
        <p className="flex items-center gap-2 font-medium">
          <ChatIcon />
          Job texts are on for ···{state.last4}
        </p>
        <p className="mt-1 text-emerald-900/80 dark:text-emerald-200/80">
          We text you paid jobs{state.in_person ? ', resets included' : ''}. Reply YES to claim one: the first YES gets it.
          {state.line ? ` Texts come from ${state.line}.` : ''}
        </p>
        {welcome && (
          <p className="mt-2 text-xs" role="status">
            {WELCOME_NOTE[welcome.state] ?? `Welcome text: ${welcome.state}.`} {welcome.hint ?? ''}
          </p>
        )}
        {dry && <p className="mt-1 text-xs text-emerald-900/70 dark:text-emerald-200/70">Dry run: the factory logs texts and sends none.</p>}
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              const r = await act(() => tdm113Post<{ welcome: Tdm113Welcome }>('resend', caller()));
              if (r) setWelcome(r.welcome);
            }}
            className="font-medium underline underline-offset-2 disabled:opacity-50"
          >
            Resend welcome text
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              const r = await act(() => tdm113Post<Tdm113State>('optout', caller()));
              if (r) setState({ ...state, opted_in: false, stopped: true });
              setWelcome(null);
            }}
            className="text-emerald-900/70 underline underline-offset-2 disabled:opacity-50 dark:text-emerald-200/70"
          >
            Stop job texts
          </button>
        </div>
        {error && <p className="mt-2 text-xs text-red-700 dark:text-red-300" role="alert">{error}</p>}
      </section>
    );
  }

  return (
    <section aria-label="Job texts" className={`${box} border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900`}>
      <p className="flex items-center gap-2 font-medium">
        <ChatIcon />
        Get job texts
      </p>
      <p className="mt-1 text-zinc-600 dark:text-zinc-400">
        We text you when a paid job opens. Reply YES and the first YES gets it, with a link straight to the task.
      </p>
      <form onSubmit={onSubmit} className="mt-3 space-y-3">
        <label className="block">
          <span className="mb-1 block font-medium">Mobile number</span>
          <input
            className={input}
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            maxLength={32}
            placeholder="(415) 555-0123"
          />
        </label>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-0.5 h-4 w-4" checked={inPerson} onChange={(e) => setInPerson(e.target.checked)} />
          <span>
            In person near the robot <span className="text-zinc-500">(you also get reset jobs: put the objects back by hand)</span>
          </span>
        </label>
        <label className="flex items-start gap-2">
          <input type="checkbox" className="mt-0.5 h-4 w-4" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          <span>{TDM113_CONSENT_TEXT}</span>
        </label>
        {error && <p className="rounded-md bg-red-50 px-3 py-2 text-red-800 dark:bg-red-950/40 dark:text-red-300" role="alert">{error}</p>}
        <div className="flex flex-wrap items-center gap-3">
          <button type="submit" disabled={busy} className="rounded-md bg-zinc-900 px-4 py-2 font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">
            {busy ? 'Saving…' : 'Text me jobs'}
          </button>
          <span className="text-xs text-zinc-500">
            {state.stopped ? 'You stopped job texts. Saving turns them back on. ' : ''}
            Kept private, never shown on the site. Payouts are Stripe test mode.{dry ? ' Dry run: texts are logged, not sent.' : ''}
          </span>
        </div>
      </form>
    </section>
  );
}

function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z" />
    </svg>
  );
}
