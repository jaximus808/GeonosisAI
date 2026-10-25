'use client';

// TDM-116 small client hooks: URL-backed view state (so a pinned stage or a tab survives a reload and can
// be shared), page visibility (the 3D view unmounts while the browser tab is hidden), the operator check.

import { useCallback, useEffect, useState } from 'react';

/** One search param as state. Writes with history.replaceState (no navigation, no re-fetch). */
export function useTdm116Param(key: string): [string | null, (v: string | null) => void] {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setValue(new URL(window.location.href).searchParams.get(key));
    read();
    window.addEventListener('popstate', read);
    return () => window.removeEventListener('popstate', read);
  }, [key]);
  const set = useCallback(
    (v: string | null) => {
      setValue(v);
      try {
        const u = new URL(window.location.href);
        if (v) u.searchParams.set(key, v);
        else u.searchParams.delete(key);
        window.history.replaceState(window.history.state, '', `${u.pathname}${u.search}${u.hash}`);
      } catch {
        /* the URL just isn't updated */
      }
    },
    [key],
  );
  return [value, set];
}

/** False while the browser tab is hidden: heavy views (the 3D twin, camera sockets) unmount. */
export function useTdm116PageVisible(): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden');
    on();
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}

/** A clock in ms that ticks every `ms` (0 until mounted, so server and first client render agree). */
export function useTdm116Now(ms = 1000): number {
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
