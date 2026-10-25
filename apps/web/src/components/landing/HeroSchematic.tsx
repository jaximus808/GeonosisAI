'use client';
// The landing hero's right column: the YAM as a plate from the foundry's drawing set (HeroSchematicScene,
// three.js, loaded after first paint) on a faint blueprint grid, with the sheet's chrome as plain DOM: corner
// marks, an axis marker, one dimension, one callout to the weld seam and a title block. The box is sized by the
// parent before anything loads, so nothing shifts. It only draws while on screen and the tab is visible.
// Reduced motion: one still frame. No WebGL, or the scene fails: the Anatomy foundry cell (HeroFoundry, SVG).
import dynamic from 'next/dynamic';
import { Component, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type Ref } from 'react';
import { gxCx } from '@/components/brand/cx';
import { useInView, useStillness } from '@/components/factory-iso/kit/react/live';
import { usePageVisible } from '@/lib/teleop/useViewQuality';
import { FeedLayer, type FeedHandle } from './HeroFeed';
import { HeroFoundry } from './HeroFoundry';
import type { OverlayRefs, Palette } from './HeroSchematicScene';

const Scene = dynamic(() => import('./HeroSchematicScene'), { ssr: false, loading: () => null });

function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas');
    const ctx = (c.getContext('webgl2') ?? c.getContext('webgl')) as WebGLRenderingContext | null;
    ctx?.getExtension('WEBGL_lose_context')?.loseContext();
    return !!ctx;
  } catch {
    return false;
  }
}

function readPalette(): Palette {
  const cs = getComputedStyle(document.documentElement);
  const v = (k: string, d: string) => cs.getPropertyValue(k).trim() || d;
  return {
    ground: v('--ground', '#13110f'),
    ink: v('--ink', '#f1e9e1'),
    muted: v('--ink-muted', '#b3a59a'),
    faint: v('--line-strong', '#7a6b5f'),
    melt: v('--melt', '#ff7a1f'),
  };
}

/** The page's colours, re-read when the theme flips (data-theme or the OS scheme). */
function usePalette(): Palette | null {
  const [pal, setPal] = useState<Palette | null>(null);
  useEffect(() => {
    const read = () => setPal((p) => {
      const n = readPalette();
      return p && JSON.stringify(p) === JSON.stringify(n) ? p : n;
    });
    read();
    const mo = new MutationObserver(read);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    mq.addEventListener('change', read);
    return () => {
      mo.disconnect();
      mq.removeEventListener('change', read);
    };
  }, []);
  return pal;
}

class SceneBoundary extends Component<{ onError: () => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    this.props.onError();
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

/** The sheet's live marks: written straight to the DOM from the render loop (no React renders per frame). */
function SheetMarks({ handle }: { handle: Ref<OverlayRefs> }) {
  const leader = useRef<SVGPolylineElement>(null);
  const dot = useRef<SVGCircleElement>(null);
  const label = useRef<HTMLDivElement>(null);
  const dim = useRef<HTMLDivElement>(null);
  const ax = useRef<SVGLineElement>(null);
  const ay = useRef<SVGLineElement>(null);
  const az = useRef<SVGLineElement>(null);
  const tx = useRef<SVGTextElement>(null);
  const ty = useRef<SVGTextElement>(null);
  const tz = useRef<SVGTextElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const feed = useRef<FeedHandle>(null);

  useImperativeHandle(handle, () => ({
    feed(anchors, t, still) {
      feed.current?.update(anchors, t, still);
    },
    seam(x, y, flash) {
      const box = svg.current?.getBoundingClientRect();
      if (!box || !leader.current || !dot.current || !label.current) return;
      // the callout sits up and to the right of the seam, its leader elbows once
      // the callout sits up and out from the seam (to the right, or the left near the edge); one elbow
      const w = label.current.offsetWidth;
      const right = x + 70 + w < box.width - 12;
      const lx = right ? x + 56 : x - 56;
      const ly = Math.max(22, y - 78);
      const ex = right ? lx + 0 : lx;
      leader.current.setAttribute('points', `${x.toFixed(1)},${y.toFixed(1)} ${(right ? lx - 16 : lx + 16).toFixed(1)},${ly.toFixed(1)} ${ex.toFixed(1)},${ly.toFixed(1)}`);
      dot.current.setAttribute('cx', x.toFixed(1));
      dot.current.setAttribute('cy', y.toFixed(1));
      label.current.style.transform = `translate(${(right ? lx + 6 : lx - 6 - w).toFixed(1)}px, ${(ly - 8).toFixed(1)}px)`;
      label.current.dataset.hot = flash > 0.15 ? 'yes' : 'no';
      svg.current!.style.setProperty('--seam-o', String(0.55 + 0.45 * flash));
    },
    axes(x, y, z) {
      const k = 34 / Math.max(1, Math.hypot(...z));
      const put = (l: SVGLineElement | null, t: SVGTextElement | null, d: readonly number[]) => {
        if (!l || !t) return;
        l.setAttribute('x2', (d[0] * k).toFixed(1));
        l.setAttribute('y2', (d[1] * k).toFixed(1));
        t.setAttribute('x', (d[0] * k * 1.28).toFixed(1));
        t.setAttribute('y', (d[1] * k * 1.28 + 3).toFixed(1));
      };
      put(ax.current, tx.current, x);
      put(ay.current, ty.current, y);
      put(az.current, tz.current, z);
    },
    dim(x, y) {
      if (dim.current) dim.current.style.transform = `translate(${(x - 4).toFixed(1)}px, ${y.toFixed(1)}px) translate(-100%, -50%) rotate(180deg)`;
    },
  }), []);

  return (
    <>
      <FeedLayer handle={feed} />
      <svg ref={svg} className="pointer-events-none absolute inset-0 hidden h-full w-full overflow-visible min-[640px]:block" aria-hidden="true">
        <polyline ref={leader} points="0,0 0,0" fill="none" stroke="var(--melt)" strokeWidth="1" style={{ opacity: 'var(--seam-o, 0.6)' }} />
        <circle ref={dot} r="2.5" fill="var(--melt)" />
      </svg>
      {/* axis marker, bottom left: the turntable's frame */}
      <svg className="pointer-events-none absolute bottom-2 left-2 hidden h-[92px] w-[92px] overflow-visible min-[640px]:block" aria-hidden="true">
        <g transform="translate(48 58)">
          <line ref={ax} x1="0" y1="0" x2="30" y2="0" stroke="var(--melt)" strokeWidth="1.25" />
          <line ref={ay} x1="0" y1="0" x2="0" y2="-30" stroke="var(--ink-muted)" strokeWidth="1.25" />
          <line ref={az} x1="0" y1="0" x2="0" y2="-34" stroke="var(--ink)" strokeWidth="1.25" />
          <circle r="2" fill="var(--ink)" />
          <text ref={tx} className="fill-melt font-mono text-[9px]" textAnchor="middle">X</text>
          <text ref={ty} className="fill-ink-muted font-mono text-[9px]" textAnchor="middle">Y</text>
          <text ref={tz} className="fill-ink font-mono text-[9px]" textAnchor="middle">Z</text>
        </g>
      </svg>
      <div ref={label} className="hero-callout pointer-events-none absolute left-0 top-0 hidden whitespace-nowrap min-[640px]:block bg-ground px-1 font-mono text-[10px] uppercase leading-4 tracking-[0.1em] text-ink-muted">
        <span className="text-melt">J6</span> · weld 01
      </div>
      <div ref={dim} className="pointer-events-none absolute left-0 top-0 font-mono text-[10px] leading-4 tracking-[0.08em] text-ink-muted [writing-mode:vertical-rl]">
        660
      </div>
    </>
  );
}

function TitleBlock() {
  const cell = 'border-line px-2 py-1';
  return (
    <div className="pointer-events-none absolute bottom-3 right-3 grid grid-cols-[auto_auto] border border-line-strong/70 bg-ground font-mono text-[9px] uppercase leading-[14px] tracking-[0.1em] text-ink-muted min-[640px]:text-[10px]">
      <div className={gxCx(cell, 'border-r text-ink min-[640px]:border-b')}>FIG.01</div>
      <div className={gxCx(cell, 'text-ink min-[640px]:border-b')}>Foundry unit YAM-01</div>
      <div className={gxCx(cell, 'hidden border-r min-[640px]:block')}>Geonosis line</div>
      <div className={gxCx(cell, 'hidden justify-between gap-4 min-[640px]:flex')}>
        <span>Rev A</span>
        <span>1:5 · mm</span>
      </div>
    </div>
  );
}

const CORNER = 'pointer-events-none absolute h-3 w-3 border-line-strong';

export function HeroSchematic({ className }: { className?: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const overlay = useRef<OverlayRefs>(null);
  const [gl, setGl] = useState<'pending' | 'yes' | 'no'>('pending');
  const [ready, setReady] = useState(false);
  const still = useStillness();
  const inView = useInView(wrap);
  const visible = usePageVisible();
  const palette = usePalette();

  useEffect(() => setGl(hasWebGL() ? 'yes' : 'no'), []);

  if (gl === 'no') return <HeroFoundry className={className} />;
  return (
    <div
      ref={wrap}
      className={gxCx('hero-schematic relative isolate overflow-hidden', className)}
      role="img"
      aria-label="Technical drawing of the YAM robot arm turning slowly on an assembly turntable"
      data-hero-schematic={ready ? 'ready' : gl}
    >
      {/* the blueprint grid, faded out toward the edges of the sheet */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -z-10 opacity-60 [mask-image:radial-gradient(ellipse_70%_65%_at_50%_48%,#000_35%,transparent_85%)]"
        style={{
          backgroundImage:
            'linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px), linear-gradient(color-mix(in oklab, var(--line) 45%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in oklab, var(--line) 45%, transparent) 1px, transparent 1px)',
          backgroundSize: '120px 120px, 120px 120px, 24px 24px, 24px 24px',
          backgroundPosition: 'center center',
        }}
      />
      <span aria-hidden className={gxCx(CORNER, 'left-0 top-0 border-l border-t')} />
      <span aria-hidden className={gxCx(CORNER, 'right-0 top-0 border-r border-t')} />
      <span aria-hidden className={gxCx(CORNER, 'bottom-0 left-0 border-b border-l')} />
      <span aria-hidden className={gxCx(CORNER, 'bottom-0 right-0 border-b border-r')} />

      {gl === 'yes' && palette && (
        <div className={gxCx('absolute inset-0 transition-opacity duration-700 ease-out', ready ? 'opacity-100' : 'opacity-0')}>
          <SceneBoundary onError={() => setGl('no')}>
            <Scene key={still ? 'still' : 'live'} still={still} running={inView && visible} palette={palette} overlay={overlay} onReady={() => setReady(true)} />
          </SceneBoundary>
          <SheetMarks handle={overlay} />
        </div>
      )}
      <TitleBlock />
    </div>
  );
}
