'use client';

// TDM-65: Web Push opt-in for contributors (paid-task notifications).
//
//   const push = useTdm65Push();   // push.state, push.enable(), push.disable(), push.test()
//
// The service worker is /tdm65-sw.js (scope /). The subscription goes to the orchestrator through
// /api/tdm65/* with this browser's own SpacetimeDB identity token (never the owner token), so the
// server binds it to the contributor row of this identity. Endpoints stay server-side in a private
// store; nothing about them is written to a public table.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSpacetimeDB } from 'spacetimedb/react';
import { BROWSER_TOKEN_KEY } from '@/lib/spacetime/config';

export type Tdm65PushState =
  | 'loading'
  | 'unsupported' // no service worker / Push API in this browser
  | 'insecure' // page not on HTTPS (or localhost): push needs a secure origin, e.g. the tunnel URL
  | 'ios_install' // iPhone/iPad Safari: push only works from a home-screen app
  | 'server_off' // orchestrator has no VAPID keys (or is unreachable)
  | 'denied' // the user blocked notifications for this site
  | 'off'
  | 'on';

export const TDM65_SW_URL = '/tdm65-sw.js';

export function tdm65Platform() {
  if (typeof window === 'undefined') return { ios: false, android: false, standalone: false };
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const standalone =
    window.matchMedia?.('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return { ios, android: /Android/i.test(ua), standalone };
}

function pushApis(): boolean {
  return typeof window !== 'undefined' && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

function b64urlToBytes(s: string): Uint8Array<ArrayBuffer> {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function sameKey(a: ArrayBuffer | null | undefined, b: Uint8Array): boolean {
  if (!a) return false;
  const x = new Uint8Array(a);
  return x.length === b.length && x.every((v, i) => v === b[i]);
}

function browserToken(): string | null {
  try {
    return window.localStorage.getItem(BROWSER_TOKEN_KEY);
  } catch {
    return null;
  }
}

async function tdm65Api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/tdm65/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });
  const text = await res.text();
  let json: unknown = {};
  try {
    json = text ? JSON.parse(text) : {};
  } catch {
    json = { detail: text };
  }
  if (!res.ok) {
    const d = (json as { detail?: unknown }).detail;
    throw new Error(typeof d === 'string' ? d : `Push server error ${res.status}`);
  }
  return json as T;
}

type Config = { enabled: boolean; public_key: string | null; rate_limit_s: number };
export type Tdm65TestResult = { push: string; sent?: number; reason?: string };

export function useTdm65Push(enabledForThisUser: boolean) {
  const { identity, isActive } = useSpacetimeDB();
  const identityHex = identity?.toHexString();
  const [state, setState] = useState<Tdm65PushState>('loading');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const config = useRef<Config | null>(null);

  const caller = useCallback(() => {
    const token = browserToken();
    if (!identityHex || !token) throw new Error('Not connected to the factory yet. Try again in a moment.');
    return { identity: identityHex, spacetime_token: token };
  }, [identityHex]);

  const send = useCallback(
    async (sub: PushSubscription) => {
      const j = sub.toJSON();
      await tdm65Api('subscribe', {
        ...caller(),
        subscription: { endpoint: j.endpoint, keys: j.keys },
        user_agent: navigator.userAgent.slice(0, 300),
      });
    },
    [caller],
  );

  // Work out where this device stands; re-sync a subscription the server lost.
  useEffect(() => {
    if (!enabledForThisUser || !isActive || !identityHex) return;
    let cancelled = false;
    (async () => {
      const p = tdm65Platform();
      if (!pushApis()) {
        setState(p.ios && !p.standalone ? 'ios_install' : !window.isSecureContext ? 'insecure' : 'unsupported');
        return;
      }
      if (!window.isSecureContext) return setState('insecure');
      try {
        config.current = await tdm65Api<Config>('config');
      } catch {
        config.current = null;
      }
      if (cancelled) return;
      if (!config.current?.enabled || !config.current.public_key) return setState('server_off');
      if (Notification.permission === 'denied') return setState('denied');
      const reg = await navigator.serviceWorker.getRegistration('/');
      const sub = await reg?.pushManager.getSubscription();
      if (!sub || Notification.permission !== 'granted') return !cancelled && setState('off');
      try {
        const { subscribed } = await tdm65Api<{ subscribed: boolean }>('state', { ...caller(), endpoint: sub.endpoint });
        if (!subscribed) await send(sub); // server store was reset, or this browser joined as someone new
        if (!cancelled) setState('on');
      } catch {
        if (!cancelled) setState('off');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [enabledForThisUser, isActive, identityHex, caller, send]);

  const enable = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const key = config.current?.public_key;
      if (!key) throw new Error('Push is not configured on the server.');
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        setState(perm === 'denied' ? 'denied' : 'off');
        return;
      }
      const reg = await navigator.serviceWorker.register(TDM65_SW_URL, { scope: '/' });
      await navigator.serviceWorker.ready;
      const appKey = b64urlToBytes(key);
      let sub = await reg.pushManager.getSubscription();
      if (sub && !sameKey(sub.options.applicationServerKey, appKey)) {
        await sub.unsubscribe(); // made with an older VAPID key
        sub = null;
      }
      sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: appKey });
      await send(sub);
      setState('on');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [send]);

  const disable = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const reg = await navigator.serviceWorker.getRegistration('/');
      const sub = await reg?.pushManager.getSubscription();
      if (sub) {
        await tdm65Api('unsubscribe', { ...caller(), endpoint: sub.endpoint }).catch(() => {});
        await sub.unsubscribe();
      }
      setState('off');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [caller]);

  const test = useCallback(async (): Promise<Tdm65TestResult | undefined> => {
    setBusy(true);
    setError('');
    try {
      return await tdm65Api<Tdm65TestResult>('test', caller());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, [caller]);

  return { state, busy, error, enable, disable, test };
}
