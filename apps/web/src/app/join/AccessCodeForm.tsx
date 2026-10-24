'use client';

import { useEffect, useState, type FormEvent } from 'react';

// make demo-public: the public link asks for the short code shown next to the QR, once per browser.
// The edge (scripts/demo/public_guard.py) checks it at POST /_rdf/access and sets an HttpOnly cookie;
// nothing about the code is stored in this page.

const input =
  'w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-center font-mono text-2xl tracking-[0.3em] uppercase outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900';

/** Only same-site relative paths, so ?next= can't send a phone elsewhere. */
function safeNext(next: string | undefined): string | null {
  return next && next.startsWith('/') && !next.startsWith('//') && !next.startsWith('/\\') ? next : null;
}

/** TDM-108: a job-drop link with RDF_DROP_SHOW_ACCESS_CODE=1 carries ?code= (on /join itself or inside
 * next=/j/N/src?code=...). It only pre-fills the box; the person still taps Continue (never auto-submitted,
 * so another site cannot post it for them). */
function prefillCode(next: string | undefined): string {
  const ok = (v: string | null) => (v && /^[A-Za-z0-9]{4,12}$/.test(v) ? v.toUpperCase() : '');
  try {
    const top = ok(new URLSearchParams(window.location.search).get('code'));
    if (top) return top;
    const n = safeNext(next);
    return n ? ok(new URL(n, window.location.origin).searchParams.get('code')) : '';
  } catch {
    return '';
  }
}

export function AccessCodeForm({ next }: { next?: string }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const c = prefillCode(next);
    if (c) setCode((cur) => cur || c);
  }, [next]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/_rdf/access', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ code }),
        cache: 'no-store',
      });
      const b = (await r.json().catch(() => ({}))) as { detail?: string };
      if (!r.ok) throw new Error(b.detail || `HTTP ${r.status}`);
      window.location.replace(safeNext(next) ?? window.location.pathname + window.location.search);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <label htmlFor="rdf-access-code" className="block text-sm font-medium">
        Access code
      </label>
      <p className="text-sm text-zinc-500">It is on the screen next to the QR code. You only enter it once on this phone.</p>
      <input
        id="rdf-access-code"
        className={input}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        autoComplete="one-time-code"
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        inputMode="text"
        maxLength={16}
        required
        autoFocus
      />
      {error && <p className="text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={busy || !code.trim()}
        className="w-full rounded-md bg-zinc-900 px-4 py-2 text-base font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {busy ? 'Checking…' : 'Continue'}
      </button>
    </form>
  );
}
