'use client';

// TDM-65: opt-in card for paid-task push notifications (on /join and /marketplace).
// Every state that can't push says so and points at the fallback: the marketplace's live
// highlight + vibration (TDM-61) still works while the page is open.

import { useState } from 'react';
import { useContributor } from '@/lib/contributor/hooks';
import { useTdm65Push, type Tdm65PushState } from '@/lib/tdm65/push';

const FALLBACK = 'Until then, keep the tasks page open: new tasks still show up instantly, highlighted, and your phone buzzes.';

const COPY: Record<Exclude<Tdm65PushState, 'loading' | 'on' | 'off'>, { title: string; body: string }> = {
  ios_install: {
    title: 'On iPhone, alerts need the home-screen app',
    body: 'Tap Share, then Add to Home Screen. Open “Robot tasks” from your home screen and turn alerts on there (iOS 16.4 or later).',
  },
  insecure: {
    title: 'Alerts need a secure link',
    body: 'This page was opened over plain http. Open the https link from the QR code to turn alerts on.',
  },
  unsupported: {
    title: 'This browser can’t show task alerts',
    body: 'Chrome on Android, or Chrome, Edge, Firefox or Safari on a laptop, can.',
  },
  denied: {
    title: 'Notifications are blocked for this site',
    body: 'Allow notifications for this site in your browser’s site settings, then reload this page.',
  },
  server_off: {
    title: 'Task alerts are off on the factory side',
    body: 'The factory isn’t sending push alerts right now.',
  },
};

export function PushOptIn({ className = '' }: { className?: string }) {
  const { contributor, ready } = useContributor();
  const eligible = ready && contributor != null && contributor.kind === 'contributor';
  const push = useTdm65Push(eligible);
  const [note, setNote] = useState('');

  if (!eligible || push.state === 'loading') return null;

  const box = `rounded-lg border px-4 py-3 text-sm ${className}`;

  if (push.state === 'on') {
    return (
      <section aria-label="Task alerts" className={`${box} border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-100`}>
        <p className="flex items-center gap-2 font-medium">
          <BellIcon />
          Task alerts are on for this device
        </p>
        <p className="mt-1 text-emerald-900/80 dark:text-emerald-200/80">
          You’ll get a notification when the factory posts a paid task, even with this page closed. Tap it to claim.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
          <button
            type="button"
            disabled={push.busy}
            onClick={async () => {
              setNote('');
              const r = await push.test();
              if (r) setNote(r.push === 'sent' ? 'Test sent. It should arrive in a few seconds.' : r.reason ?? `Not sent (${r.push}).`);
            }}
            className="font-medium underline underline-offset-2 disabled:opacity-50"
          >
            Send a test alert
          </button>
          <button type="button" disabled={push.busy} onClick={() => void push.disable()} className="text-emerald-900/70 underline underline-offset-2 disabled:opacity-50 dark:text-emerald-200/70">
            Turn off
          </button>
        </div>
        {(note || push.error) && <p className="mt-2 text-xs" role="status">{push.error || note}</p>}
      </section>
    );
  }

  if (push.state === 'off') {
    return (
      <section aria-label="Task alerts" className={`${box} border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900`}>
        <p className="flex items-center gap-2 font-medium">
          <BellIcon />
          Get a buzz when a paid task drops
        </p>
        <p className="mt-1 text-zinc-600 dark:text-zinc-400">
          We’ll notify this device when the factory needs a reset or a takeover, even with this page closed. First to claim gets it.
        </p>
        <button
          type="button"
          disabled={push.busy}
          onClick={() => void push.enable()}
          className="mt-3 w-full rounded-md bg-zinc-900 px-4 py-2.5 text-base font-semibold text-white outline-none focus-visible:ring-2 focus-visible:ring-zinc-500 focus-visible:ring-offset-2 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {push.busy ? 'Turning on…' : 'Turn on task alerts'}
        </button>
        {push.error && (
          <p className="mt-2 text-xs text-red-700 dark:text-red-300" role="alert">
            {push.error} {FALLBACK}
          </p>
        )}
      </section>
    );
  }

  const c = COPY[push.state];
  return (
    <section aria-label="Task alerts" className={`${box} border-zinc-300 bg-zinc-50 text-zinc-700 dark:border-zinc-700 dark:bg-zinc-900/60 dark:text-zinc-300`}>
      <p className="flex items-center gap-2 font-medium text-zinc-900 dark:text-zinc-100">
        <BellIcon muted />
        {c.title}
      </p>
      <p className="mt-1">{c.body}</p>
      <p className="mt-1 text-zinc-500">{FALLBACK}</p>
    </section>
  );
}

function BellIcon({ muted = false }: { muted?: boolean }) {
  return (
    <svg aria-hidden viewBox="0 0 20 20" className={`h-4 w-4 shrink-0 ${muted ? 'opacity-50' : ''}`} fill="currentColor">
      <path d="M10 2a5 5 0 0 0-5 5v2.6L3.3 13a.75.75 0 0 0 .67 1.08h12.06A.75.75 0 0 0 16.7 13L15 9.6V7a5 5 0 0 0-5-5Zm-2 13.5a2 2 0 0 0 4 0H8Z" />
    </svg>
  );
}
