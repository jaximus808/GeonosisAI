'use client';
// The hero schematic's feed layer: four agents at the edges of the sheet (DATA, TRAIN, REVIEW, EVAL), each with a
// dashed conduit routed into the part of the arm it feeds, a packet travelling down it once a round, and the
// DEPLOYED stamp after the last round. Plain SVG/DOM written from the scene's render loop (HeroSchematicScene
// calls update with the projected docking points), so nothing re-renders per frame. Timing: heroBeat.ts.
// Reduced motion: the nodes and conduits, no packets, no stamp. Phones: DATA and EVAL only.
import { useEffect, useImperativeHandle, useRef, type ReactNode, type Ref } from 'react';
import { gxCx } from '@/components/brand/cx';
import { FEEDS, packetAt, stampAt, type FeedKey } from './heroBeat';

export interface FeedHandle {
  update: (anchors: Record<FeedKey, readonly [number, number]>, t: number, still: boolean) => void;
}

const G = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.1, strokeLinejoin: 'round' as const, strokeLinecap: 'round' as const };

/** Line-art glyphs: droid heads for the two agents that judge, modules for the two that supply. */
const GLYPH: Record<FeedKey, ReactNode> = {
  // a stack of disks: the demos people record on their phones
  data: (
    <g {...G}>
      <ellipse cx="9" cy="4.5" rx="5.5" ry="2" />
      <path d="M3.5 4.5v9c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2v-9" />
      <path d="M3.5 9c0 1.1 2.5 2 5.5 2s5.5-.9 5.5-2" />
    </g>
  ),
  // a die with pins: the fine-tune on one H100
  train: (
    <g {...G}>
      <rect x="4.5" y="4.5" width="9" height="9" rx="0.5" />
      <rect x="7" y="7" width="4" height="4" />
      <path d="M7 4.5V2.5M11 4.5V2.5M7 15.5v-2M11 15.5v-2M4.5 7h-2M4.5 11h-2M15.5 7h-2M15.5 11h-2" />
    </g>
  ),
  // a domed droid head, one wide visor
  review: (
    <g {...G}>
      <path d="M3.5 15.5V9a5.5 5.5 0 0 1 11 0v6.5z" />
      <path d="M5.5 10h7" />
      <path d="M9 3.5V1.5" />
    </g>
  ),
  // a flat-topped droid head, one round eye
  eval: (
    <g {...G}>
      <path d="M4 15.5V6.5l2-3h6l2 3v9z" />
      <circle cx="9" cy="9.5" r="2" />
      <path d="M4 13h10" />
    </g>
  ),
};

const NODES: Array<{ key: FeedKey; label: string; sub: string; side: 'left' | 'right' | 'top'; pos: string; phone: boolean }> = [
  { key: 'train', label: 'Train', sub: 'π0.5 · LoRA', side: 'top', pos: 'left-[25%] top-[2.5%]', phone: false },
  { key: 'eval', label: 'Eval', sub: '10 TRIALS', side: 'right', pos: 'right-[1.5%] top-[8%]', phone: true },
  { key: 'review', label: 'Review', sub: 'GAPS · JERK', side: 'right', pos: 'right-[1.5%] top-[42%]', phone: false },
  { key: 'data', label: 'Data', sub: 'PHONE DEMOS', side: 'left', pos: 'left-[1.5%] top-[68%]', phone: true },
];

type Pt = readonly [number, number];

function route(side: 'left' | 'right' | 'top', r: { x: number; y: number; w: number; h: number }, a: Pt): Pt[] {
  if (side === 'top') {
    const p: Pt = [r.x + r.w / 2, r.y + r.h];
    const y = p[1] + 14;
    return [p, [p[0], y], [a[0], y], a];
  }
  const p: Pt = side === 'left' ? [r.x + r.w, r.y + r.h / 2] : [r.x, r.y + r.h / 2];
  const bx = p[0] + (side === 'left' ? 16 : -16);
  return [p, [bx, p[1]], [bx, a[1]], a];
}

function pointAlong(pts: Pt[], f: number): Pt {
  let total = 0;
  const seg: number[] = [];
  for (let i = 1; i < pts.length; i++) {
    const l = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    seg.push(l);
    total += l;
  }
  let d = f * total;
  for (let i = 0; i < seg.length; i++) {
    if (d <= seg[i] || i === seg.length - 1) {
      const k = seg[i] ? Math.min(1, d / seg[i]) : 0;
      return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * k, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * k];
    }
    d -= seg[i];
  }
  return pts[pts.length - 1];
}

export function FeedLayer({ handle }: { handle: Ref<FeedHandle> }) {
  const nodes = useRef<Partial<Record<FeedKey, HTMLDivElement | null>>>({});
  const lines = useRef<Partial<Record<FeedKey, SVGPolylineElement | null>>>({});
  const packets = useRef<Partial<Record<FeedKey, SVGRectElement | null>>>({});
  const ports = useRef<Partial<Record<FeedKey, SVGCircleElement | null>>>({});
  const stamp = useRef<HTMLDivElement>(null);
  const rects = useRef<Partial<Record<FeedKey, { x: number; y: number; w: number; h: number }>>>({});

  // node boxes are cached (read on resize only) so the render loop never forces a layout
  useEffect(() => {
    const measure = () => {
      for (const n of NODES) {
        const el = nodes.current[n.key];
        rects.current[n.key] = el && el.offsetWidth ? { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight } : undefined;
      }
    };
    measure();
    document.fonts?.ready.then(measure).catch(() => {});
    const box = nodes.current.data?.parentElement;
    if (!box || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(box);
    return () => ro.disconnect();
  }, []);

  useImperativeHandle(handle, () => ({
    update(anchors, t, still) {
      for (const f of FEEDS) {
        const r = rects.current[f.key];
        const line = lines.current[f.key];
        const pk = packets.current[f.key];
        const port = ports.current[f.key];
        if (!r || !line || !pk || !port) continue;
        const pts = route(NODES.find((n) => n.key === f.key)!.side, r, anchors[f.key]);
        line.setAttribute('points', pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' '));
        port.setAttribute('cx', anchors[f.key][0].toFixed(1));
        port.setAttribute('cy', anchors[f.key][1].toFixed(1));
        const k = packetAt(t, f.stagger, f.key);
        if (still || k.p === null) pk.style.opacity = '0';
        else {
          const [x, y] = pointAlong(pts, k.p);
          pk.setAttribute('x', (x - 2.5).toFixed(1));
          pk.setAttribute('y', (y - 2.5).toFixed(1));
          pk.style.opacity = String(Math.min(1, k.p * 8, (1 - k.p) * 12 + 0.25));
          pk.style.fill = k.hot ? 'var(--melt)' : 'var(--ink)';
        }
        // the docking port glints when a packet lands
        const pulse = still ? 0 : Math.max(0, 1 - k.arrived / 0.9);
        port.setAttribute('r', (1.8 + 1.6 * pulse).toFixed(2));
        port.style.opacity = String(0.55 + 0.45 * pulse);
      }
      if (stamp.current) {
        const s = still ? 0 : stampAt(t);
        stamp.current.style.opacity = s.toFixed(3);
        stamp.current.style.transform = `rotate(-2deg) scale(${(1.05 - 0.05 * s).toFixed(3)})`;
      }
    },
  }), []);

  return (
    <>
      <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" aria-hidden="true">
        {NODES.map((n) => (
          <g key={n.key} className={n.phone ? undefined : 'hidden min-[640px]:inline'}>
            <polyline
              ref={(el) => { lines.current[n.key] = el; }}
              points="0,0 0,0"
              fill="none"
              stroke="var(--ink-muted)"
              strokeWidth="1"
              strokeDasharray="1 3"
              strokeLinecap="round"
              opacity="0.8"
            />
            <circle ref={(el) => { ports.current[n.key] = el; }} r="1.8" fill="var(--ink-muted)" />
            <rect ref={(el) => { packets.current[n.key] = el; }} width="5" height="5" style={{ opacity: 0 }} />
          </g>
        ))}
      </svg>
      {NODES.map((n) => (
        <div
          key={n.key}
          ref={(el) => { nodes.current[n.key] = el; }}
          className={gxCx(
            'pointer-events-none absolute items-center gap-1.5 border border-line-strong/70 bg-ground py-1 pl-1 pr-2 font-mono uppercase',
            n.pos,
            n.phone ? 'flex' : 'hidden min-[640px]:flex',
          )}
        >
          <svg viewBox="0 0 18 18" className="h-[18px] w-[18px] text-ink-muted" aria-hidden="true">
            {GLYPH[n.key]}
          </svg>
          <span className="flex flex-col leading-[11px]">
            <span className="text-[10px] tracking-[0.12em] text-ink">{n.label}</span>
            <span className="text-[8.5px] normal-case tracking-[0.08em] text-ink-muted">{n.sub}</span>
          </span>
        </div>
      ))}
      <div
        ref={stamp}
        className="pointer-events-none absolute bottom-[42px] right-3 border border-melt px-2 py-0.5 font-mono text-[10px] uppercase leading-4 tracking-[0.14em] text-melt min-[640px]:bottom-[68px]"
        style={{ opacity: 0 }}
      >
        Deployed ✓ · YAM-01
      </div>
    </>
  );
}

