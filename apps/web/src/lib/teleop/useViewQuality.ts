'use client';

// React side of lib/teleop/viewQuality.ts: the remembered Smooth / Sharp choice, and page visibility.

import { useCallback, useEffect, useState } from 'react';
import { DEFAULT_VIEW_QUALITY, VIEW_QUALITY_KEY, parseViewQuality, readViewQuality, writeViewQuality, type ViewQuality } from './viewQuality';

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
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
  const set = useCallback((v: ViewQuality) => {
    setQ(v);
    writeViewQuality(v);
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
