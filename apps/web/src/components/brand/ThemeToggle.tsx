'use client';

import { useEffect, useState } from 'react';

/** localStorage key and the inline pre-paint script (src/app/layout.tsx) that applies it. */
export const GX_THEME_KEY = 'gx-theme';
export const GX_THEME_SCRIPT = `try{var q=new URLSearchParams(location.search).get('theme');if(q==='light'||q==='dark')localStorage.setItem('${GX_THEME_KEY}',q);var t=localStorage.getItem('${GX_THEME_KEY}');if(t==='light')document.documentElement.setAttribute('data-theme','light');}catch(e){}`;

/** TDM-109: Foundry (dark, default) / Dune (light) switch. Remembered per browser; `?theme=light` also sets it. */
export function ThemeToggle({ className }: { className?: string }) {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  useEffect(() => {
    setTheme(document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark');
  }, []);
  const next = theme === 'light' ? 'dark' : 'light';
  return (
    <button
      type="button"
      className={className ?? 'gx-btn gx-btn-ghost gx-btn-sm'}
      title={`Switch to ${next === 'light' ? 'Dune (light)' : 'Foundry (dark)'}`}
      onClick={() => {
        if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
        else document.documentElement.removeAttribute('data-theme');
        try {
          localStorage.setItem(GX_THEME_KEY, next);
        } catch {}
        setTheme(next);
      }}
    >
      {theme === 'light' ? 'Dune' : 'Foundry'}
    </button>
  );
}
