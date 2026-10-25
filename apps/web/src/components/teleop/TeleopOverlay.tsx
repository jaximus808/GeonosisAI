'use client';

// No layout shift on the teleop and practice pages: every status / error / notice goes into a reserved,
// fixed-height slot or into an overlay drawn over the viewer, so the 3D viewer and the control dock never
// move or resize when a message comes or goes.
//
//   <OneLine text={long} />                    one line, truncated, "More" opens the full text in an overlay
//   <ViewerToasts items={[...]} />             a toast stack drawn over a corner of the viewer (parent: relative)
//   <ReservedSlot height={62}>{row}</ReservedSlot>   keeps its height; anything taller spills OVER what is below
//   <ViewQualityToggle value onChange />       "Quality: Smooth / Sharp" (lib/teleop/viewQuality.ts)
//   <PausedView name onShow />                 a view that Smooth mode is not drawing, with a button to show it

import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { ViewQuality } from '@/lib/teleop/viewQuality';

/** One line of text that never wraps: truncated, with a "More" button that shows the rest over the page. */
export function OneLine({ text, className = '', testId }: { text: string; className?: string; testId?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const root = useRef<HTMLSpanElement>(null);
  const [over, setOver] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOver(el.scrollWidth > el.clientWidth + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text]);
  useEffect(() => {
    if (!over) setOpen(false);
  }, [over]);
  useEffect(() => {
    if (!open) return;
    const off = (e: PointerEvent) => !root.current?.contains(e.target as Node) && setOpen(false);
    window.addEventListener('pointerdown', off);
    return () => window.removeEventListener('pointerdown', off);
  }, [open]);
  return (
    <span ref={root} className={`relative flex min-w-0 items-center gap-1.5 ${className}`} data-testid={testId}>
      <span ref={ref} className="min-w-0 truncate" title={text}>
        {text}
      </span>
      {over && (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="shrink-0 rounded-sm px-1 text-[11px] font-semibold underline underline-offset-2 opacity-80 hover:opacity-100"
          data-testid={testId ? `${testId}-more` : undefined}
        >
          {open ? 'Less' : 'More'}
        </button>
      )}
      {open && (
        <span
          role="dialog"
          aria-label="Full message"
          className="absolute left-0 top-full z-50 mt-1 w-max max-w-[min(28rem,calc(100vw-2rem))] whitespace-normal rounded-md bg-zinc-950/95 px-3 py-2 text-[13px] font-normal leading-snug text-white shadow-xl ring-1 ring-white/10"
        >
          {text}
        </span>
      )}
    </span>
  );
}

export type ToastTone = 'bad' | 'warn' | 'info' | 'good';

export interface ViewerToast {
  id: string;
  tone: ToastTone;
  title: string;
  detail?: string;
  /** A button (Resume, Reclaim...) on the right of the toast. */
  action?: ReactNode;
  /** Instead of title/detail: a ready-made row (e.g. the lease banner). */
  node?: ReactNode;
  role?: 'alert' | 'status';
  testId?: string;
}

const TOAST_TONE: Record<ToastTone, string> = {
  bad: 'bg-red-600/95 text-white ring-red-900/40',
  warn: 'bg-amber-400/95 text-black ring-amber-700/40',
  info: 'bg-zinc-950/85 text-white ring-white/10',
  good: 'bg-emerald-700/95 text-white ring-emerald-900/40',
};

/**
 * Toasts drawn over the viewer (the parent must be `relative`). They never take layout space.
 * `at`: 'top' (centred under the top edge) or 'bottom' (above the bottom edge, clear of readouts).
 */
export function ViewerToasts({ items, at = 'top', className = '' }: { items: ViewerToast[]; at?: 'top' | 'bottom'; className?: string }) {
  // 'top' sits below the views' top-left labels (the SIMULATED tag must stay readable)
  if (!items.length) return null;
  return (
    <div
      className={`pointer-events-none absolute inset-x-2 z-30 mx-auto flex max-w-xl flex-col items-stretch gap-1.5 ${at === 'top' ? 'top-12' : 'bottom-12'} ${className}`}
      data-testid="teleop-toasts"
    >
      {items.map((t) =>
        t.node ? (
          <div key={t.id} className="pointer-events-auto shadow-lg" data-testid={t.testId} role={t.role}>
            {t.node}
          </div>
        ) : (
          <div
            key={t.id}
            role={t.role ?? 'status'}
            aria-live={t.role === 'alert' ? 'assertive' : 'polite'}
            className={`pointer-events-auto flex min-h-11 items-center gap-3 rounded-md px-3 py-1.5 text-sm shadow-lg ring-1 backdrop-blur-sm ${TOAST_TONE[t.tone]}`}
            data-testid={t.testId}
            data-tone={t.tone}
          >
            <div className="min-w-0 flex-1">
              <OneLine text={t.title} className="font-semibold" />
              {t.detail && <OneLine text={t.detail} className="text-xs opacity-90" />}
            </div>
            {t.action}
          </div>
        ),
      )}
    </div>
  );
}

/**
 * A slot that always keeps `height`, whatever is in it. Content taller than that (a confirm step, an error)
 * is drawn over what comes below instead of pushing it down.
 */
export function ReservedSlot({ height, children, className = '', testId }: { height: number; children?: ReactNode; className?: string; testId?: string }) {
  return (
    <div className={`relative shrink-0 ${className}`} style={{ height }} data-testid={testId}>
      <div className="absolute inset-x-0 top-0 z-30">{children}</div>
    </div>
  );
}

/** "Quality: Smooth / Sharp": Smooth draws one big view and one small one while you drive. */
export function ViewQualityToggle({ value, onChange, testPrefix, className = '' }: { value: ViewQuality; onChange: (q: ViewQuality) => void; testPrefix: string; className?: string }) {
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 ${className}`} role="group" aria-label="3D view quality">
      <span className="opacity-80">Quality</span>
      {(['smooth', 'sharp'] as const).map((q) => (
        <button
          key={q}
          type="button"
          onClick={() => onChange(q)}
          aria-pressed={value === q}
          title={
            q === 'smooth'
              ? 'While you drive: one big view and one small view, drawn lighter. Keeps a laptop cool.'
              : 'Every view live at full resolution with full shadows. Heavier on the graphics card.'
          }
          className={`rounded-sm px-1.5 py-0.5 font-semibold ${value === q ? 'bg-ink text-ground' : 'ring-1 ring-line hover:bg-surface'}`}
          data-testid={`${testPrefix}-quality-${q}`}
        >
          {q === 'smooth' ? 'Smooth' : 'Sharp'}
        </button>
      ))}
    </span>
  );
}

/** A view that is not being drawn right now (Smooth mode while driving): its name and a button to show it. */
export function PausedView({ name, onShow, testId }: { name: string; onShow: () => void; testId?: string }) {
  return (
    <div className="absolute inset-0 grid place-items-center overflow-hidden rounded-md bg-surface-sunken ring-1 ring-steel/40" data-paused-view={name} data-testid={testId}>
      <div className="flex flex-col items-center gap-1 px-2 text-center">
        <span className="text-xs font-semibold text-ink">{name}</span>
        <span className="text-[11px] leading-tight text-ink-muted">Paused to keep driving smooth</span>
        <button type="button" onClick={onShow} className="mt-0.5 rounded-sm bg-black/55 px-2 py-1 text-[11px] font-semibold text-white hover:bg-black/70">
          Show this view
        </button>
      </div>
    </div>
  );
}
