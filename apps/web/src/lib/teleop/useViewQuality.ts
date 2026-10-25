'use client';

// React side of lib/teleop/viewQuality.ts: the remembered Smooth / Sharp choice, and page visibility.

import { useCallback, useEffect, useState } from 'react';
import { DEFAULT_VIEW_QUALITY, VIEW_QUALITY_KEY, parseViewQuality, readViewQuality, writeViewQuality, type ViewQuality } from './viewQuality';

/** Same-tab sync: every useViewQuality() on the page follows a toggle (the storage event only reaches other tabs). */
const SAME_TAB = 'rdf:view-quality';

/** [quality, setQuality]: Smooth until the stored choice is read after mount (no hydration mismatch). */
export function useViewQuality(): [ViewQuality, (q: ViewQuality) => void] {
  const [q, setQ] = useState<ViewQuality>(DEFAULT_VIEW_QUALITY);
  useEffect(() => {
    setQ(readViewQuality());
    const onStorage = (e: StorageEvent) => {
      if (e.key !== VIEW_QUALITY_KEY) return;
      const v = parseViewQuality(e.newValue);
      if (v) setQ(v);
    };
    const onSame = (e: Event) => {
      const v = parseViewQuality((e as CustomEvent).detail);
      if (v) setQ(v);
    };
    window.addEventListener('storage', onStorage);
    window.addEventListener(SAME_TAB, onSame);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener(SAME_TAB, onSame);
    };
  }, []);
  const set = useCallback((v: ViewQuality) => {
    setQ(v);
    writeViewQuality(v);
    window.dispatchEvent(new CustomEvent(SAME_TAB, { detail: v }));
  }, []);
  return [q, set];
}

/** False while the tab is in the background (Page Visibility API). */
export function usePageVisible(): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden');
    on();
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}
