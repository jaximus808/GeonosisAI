'use client';

// TDM-56: on-screen thumb stick for the phone fallback. Reports x/y in -1..1 (right / up positive)
// while a pointer holds it, and {x:0,y:0,active:false} the moment it is let go or cancelled.

import { useRef, useState } from 'react';

export interface Tdm56StickValue {
  x: number;
  y: number;
  active: boolean;
}

export function Tdm56Joystick({
  label,
  hint,
  onChange,
  size = 136,
}: {
  label: string;
  hint: string;
  onChange: (v: Tdm56StickValue) => void;
  size?: number;
}) {
  const base = useRef<HTMLDivElement>(null);
  const pid = useRef<number | null>(null);
  const [knob, setKnob] = useState({ x: 0, y: 0, active: false });
  const r = size / 2;

  const update = (e: React.PointerEvent) => {
    const b = base.current!.getBoundingClientRect();
    let dx = e.clientX - (b.left + r);
    let dy = e.clientY - (b.top + r);
    const n = Math.hypot(dx, dy);
    const max = r * 0.72;
    if (n > max) {
      dx = (dx * max) / n;
      dy = (dy * max) / n;
    }
    const v = { x: dx / max, y: -dy / max, active: true };
    setKnob({ x: dx, y: dy, active: true });
    onChange(v);
  };
  const end = () => {
    pid.current = null;
    setKnob({ x: 0, y: 0, active: false });
    onChange({ x: 0, y: 0, active: false });
  };

  return (
    <div className="flex flex-col items-center gap-1.5 select-none">
      <div
        ref={base}
        role="application"
        aria-label={`${label} stick: ${hint}`}
        className={`relative touch-none rounded-full border-2 transition-colors ${
          knob.active ? 'border-[var(--tdm56-drive)] bg-[var(--tdm56-drive)]/10' : 'border-zinc-300 bg-zinc-100 dark:border-zinc-700 dark:bg-zinc-900'
        }`}
        style={{ width: size, height: size }}
        onPointerDown={(e) => {
          if (pid.current != null) return;
          pid.current = e.pointerId;
          try {
            e.currentTarget.setPointerCapture(e.pointerId);
          } catch {
            /* synthetic or already-released pointer */
          }
          update(e);
        }}
        onPointerMove={(e) => {
          if (e.pointerId === pid.current) update(e);
        }}
        onPointerUp={(e) => e.pointerId === pid.current && end()}
        onPointerCancel={(e) => e.pointerId === pid.current && end()}
        onLostPointerCapture={(e) => e.pointerId === pid.current && end()}
      >
        <span className="pointer-events-none absolute inset-x-0 top-1/2 h-px bg-zinc-300 dark:bg-zinc-700" />
        <span className="pointer-events-none absolute inset-y-0 left-1/2 w-px bg-zinc-300 dark:bg-zinc-700" />
        <span
          className={`pointer-events-none absolute rounded-full shadow-md ${knob.active ? 'bg-[var(--tdm56-drive)]' : 'bg-zinc-500 dark:bg-zinc-400'}`}
          style={{
            width: size * 0.38,
            height: size * 0.38,
            left: r - size * 0.19 + knob.x,
            top: r - size * 0.19 + knob.y,
          }}
        />
      </div>
      <div className="text-center leading-tight">
        <div className="text-sm font-medium">{label}</div>
        <div className="text-xs text-zinc-500 dark:text-zinc-400">{hint}</div>
      </div>
    </div>
  );
}
