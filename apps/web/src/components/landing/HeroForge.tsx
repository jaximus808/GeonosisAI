'use client';
// The landing hero's right column: the YAM picking a cube onto a plate in a slow loop (HeroForgeScene, three.js,
// loaded after first paint), with a quiet mono readout ticking through the line's stations. The box is sized by
// its parent before anything loads, so nothing shifts. Reduced motion: one still 3D frame. No WebGL, or the
// scene fails: an SVG drawing of the same pose (HeroForgeStill).
import dynamic from 'next/dynamic';
import { Component, useEffect, useState, type ReactNode } from 'react';
import { gxCx } from '@/components/brand/cx';
import { HERO_STAGES } from './heroLoop';
import { HeroForgeStill } from './HeroForgeStill';

const Scene = dynamic(() => import('./HeroForgeScene'), { ssr: false, loading: () => null });

type Mode = 'pending' | 'live' | 'still' | 'svg';

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

export function HeroForge({ className }: { className?: string }) {
  const [mode, setMode] = useState<Mode>('pending');
  const [stage, setStage] = useState(0);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!hasWebGL()) {
      setMode('svg');
      return;
    }
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const apply = () => setMode((m) => (m === 'svg' ? m : mq.matches ? 'still' : 'live'));
    apply();
    mq.addEventListener('change', apply);
    return () => mq.removeEventListener('change', apply);
  }, []);

  const active = mode === 'live' ? stage : HERO_STAGES.length - 1;
  return (
    <div
      className={gxCx('hero-forge relative overflow-hidden', className)}
      role="img"
      aria-label="A YAM robot arm picks up a red cube and places it on a plate"
      data-hero-forge={mode}
      data-hero-ready={ready ? 'yes' : 'no'}
    >
      {/* floor glow, there from first paint so the scene fades in on top of it */}
      <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-3/4 bg-[radial-gradient(ellipse_50%_34%_at_50%_62%,rgba(255,122,31,0.1),rgba(255,122,31,0.03)_55%,transparent_80%)]" />
      {mode === 'svg' && <HeroForgeStill />}
      {(mode === 'live' || mode === 'still') && (
        <div className={gxCx('absolute inset-0 transition-opacity duration-700 ease-out', ready ? 'opacity-100' : 'opacity-0')}>
          <SceneBoundary onError={() => setMode('svg')}>
            <Scene key={mode} still={mode === 'still'} onStage={setStage} onReady={() => setReady(true)} />
          </SceneBoundary>
        </div>
      )}
      <ol aria-hidden className="pointer-events-none absolute bottom-3 left-3 right-3 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] uppercase leading-4 tracking-[0.08em] sm:text-[11px]">
        {HERO_STAGES.map((s, i) => (
          <li
            key={s}
            className={gxCx(
              'flex items-center gap-1.5 transition-colors duration-500',
              i === active ? (i === HERO_STAGES.length - 1 ? 'text-melt' : 'text-ink') : i < active ? 'text-ink-muted/70' : 'text-line-strong/80',
            )}
          >
            <span
              className={gxCx('inline-block h-1.5 w-1.5 rounded-full transition-colors duration-500', i === active ? 'bg-melt' : i < active ? 'bg-ink-muted/50' : 'bg-line')}
            />
            {s}
          </li>
        ))}
      </ol>
    </div>
  );
}
