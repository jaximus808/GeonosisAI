'use client';

// TDM-86: the phone cockpit shared by teleop (/teleop/[taskId]) and practice (/practice).
//
// On a phone (<= 430 px wide, or a touch screen in landscape) both pages switch to one screen that
// reads at a glance: status + STOP, a one-line coach, ONE big 3D view with a view switcher
// (Driver / Top / Side / Wrist) and the gripper and height read out on it, then a thumb dock (a
// Move zone, a big Grip button, an Up/down zone). Everything else (readiness, settings, robot
// details, keyboard legend) sits below the cockpit. Desktop layouts are untouched.
//
//   const { phone, landscape } = useTdm86Phone();
//   <Tdm86Cockpit landscape={landscape} bar={...} coach={...} action={...} view={...}
//                 left={<Tdm86ZoneStick .../>} center={<Tdm86GripButton .../>} right={<Tdm86ZoneStick axis="y" .../>} />
//
// The thumb zones are floating sticks: touch anywhere in the zone and that point becomes the
// centre, so a thumb never has to find a small knob. Values go to the same drive loop
// (useTdm106Drive().sticks.left / .right) as the desktop sticks.

import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { Tdm56StickValue } from '@/components/teleop/Tdm56Joystick';
import type { Tdm106Drive } from '@/lib/teleop/useTdm106Drive';
import { TDM86_VIEWS, type Tdm86GripTone, type Tdm86View } from '@/lib/teleop/tdm86Phone';

const PHONE_QUERY = '(max-width: 430px), (pointer: coarse) and (max-height: 500px)';

/** phone: use the cockpit. landscape: a phone turned sideways (sticks go left and right of the view). */
export function useTdm86Phone(): { phone: boolean; landscape: boolean } {
  const [v, setV] = useState({ phone: false, landscape: false });
  useEffect(() => {
    const mq = window.matchMedia?.(PHONE_QUERY);
    const fit = () => {
      const phone = mq?.matches ?? window.innerWidth <= 430;
      setV({ phone, landscape: phone && window.innerWidth > window.innerHeight });
    };
    fit();
    mq?.addEventListener?.('change', fit);
    window.addEventListener('resize', fit);
    return () => {
      mq?.removeEventListener?.('change', fit);
      window.removeEventListener('resize', fit);
    };
  }, []);
  return v;
}

/** The cockpit: fills the visible screen (100svh) and scrolls itself into view once. */
export function Tdm86Cockpit({
  landscape,
  bar,
  coach,
  action,
  view,
  left,
  center,
  right,
  testId,
}: {
  landscape: boolean;
  bar: ReactNode;
  coach: ReactNode;
  action?: ReactNode;
  view: ReactNode;
  left: ReactNode;
  center: ReactNode;
  right: ReactNode;
  testId: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // one tap from the QR / task link should land on the controls, not on the page title
    ref.current?.scrollIntoView({ block: 'start' });
  }, [landscape]);
  if (landscape) {
    return (
      <div
        ref={ref}
        className="-mx-4 grid h-[100svh] grid-cols-[minmax(0,1fr)_minmax(0,2.3fr)_minmax(0,1fr)] gap-2 px-[max(0.5rem,env(safe-area-inset-left))] py-2"
        data-testid={testId}
        data-layout="landscape"
      >
        <div className="flex min-h-0 flex-col">{left}</div>
        <div className="flex min-h-0 min-w-0 flex-col gap-1.5">
          {bar}
          {coach}
          {action}
          <div className="relative min-h-0 flex-1">{view}</div>
        </div>
        <div className="flex min-h-0 flex-col gap-2">
          <div className="shrink-0">{center}</div>
          <div className="flex min-h-0 flex-1 flex-col">{right}</div>
        </div>
      </div>
    );
  }
  return (
    <div
      ref={ref}
      className="-mx-4 flex h-[100svh] flex-col gap-2 px-2 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]"
      data-testid={testId}
      data-layout="portrait"
    >
      {bar}
      {coach}
      {action}
      <div className="relative min-h-[180px] flex-1">{view}</div>
      <div className="grid h-[min(30svh,196px)] shrink-0 grid-cols-[minmax(0,1fr)_96px_minmax(0,1fr)] gap-2">
        {left}
        <div className="flex flex-col justify-center">{center}</div>
        {right}
      </div>
    </div>
  );
}

/** Floating thumb stick. `axis="y"` reads only up/down (the Up/down zone). */
export function Tdm86ZoneStick({
  label,
  hint,
  axis = 'xy',
  onChange,
  testId,
}: {
  label: string;
  hint: string;
  axis?: 'xy' | 'y';
  onChange: (v: Tdm56StickValue) => void;
  testId: string;
}) {
  const zone = useRef<HTMLDivElement>(null);
  const pid = useRef<number | null>(null);
  const origin = useRef({ x: 0, y: 0 });
  const [knob, setKnob] = useState<{ ox: number; oy: number; dx: number; dy: number } | null>(null);
  const R = 46; // px of thumb travel for full speed

  const local = (e: React.PointerEvent) => {
    const b = zone.current!.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  };
  const update = (e: React.PointerEvent) => {
    const p = local(e);
    let dx = axis === 'y' ? 0 : p.x - origin.current.x;
    let dy = p.y - origin.current.y;
    const n = Math.hypot(dx, dy);
    if (n > R) {
      dx = (dx * R) / n;
      dy = (dy * R) / n;
    }
    setKnob({ ox: origin.current.x, oy: origin.current.y, dx, dy });
    onChange({ x: dx / R, y: -dy / R, active: true });
  };
  const end = () => {
    pid.current = null;
    setKnob(null);
    onChange({ x: 0, y: 0, active: false });
  };

  return (
    <div
      ref={zone}
      role="application"
      aria-label={`${label}: ${hint}`}
      className={`relative h-full min-h-0 touch-none select-none overflow-hidden rounded-sm ring-1 transition-colors ${
        knob ? 'bg-melt/10 ring-melt' : 'bg-surface-sunken ring-line-strong'
      }`}
      style={{ WebkitUserSelect: 'none', WebkitTouchCallout: 'none' }}
      data-testid={testId}
      data-active={!!knob}
      onPointerDown={(e) => {
        if (pid.current != null) return;
        pid.current = e.pointerId;
        try {
          e.currentTarget.setPointerCapture(e.pointerId);
        } catch {
          /* synthetic or already-released pointer */
        }
        origin.current = local(e);
        update(e);
      }}
      onPointerMove={(e) => {
        if (e.pointerId === pid.current) update(e);
      }}
      onPointerUp={(e) => e.pointerId === pid.current && end()}
      onPointerCancel={(e) => e.pointerId === pid.current && end()}
      onLostPointerCapture={(e) => e.pointerId === pid.current && end()}
      onContextMenu={(e) => e.preventDefault()}
    >
      {knob ? (
        <>
          <span
            className="pointer-events-none absolute rounded-full border-2 border-melt/70"
            style={{ width: R * 2 + 20, height: R * 2 + 20, left: knob.ox - R - 10, top: knob.oy - R - 10 }}
          />
          <span
            className="pointer-events-none absolute h-14 w-14 rounded-full bg-melt shadow-overlay"
            style={{ left: knob.ox + knob.dx - 28, top: knob.oy + knob.dy - 28 }}
          />
        </>
      ) : (
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-1 px-1 text-center">
          <span className="relative flex h-[72px] w-[72px] items-center justify-center rounded-full border-2 border-line-strong">
            <Chevrons axis={axis} />
            <span className="h-7 w-7 rounded-full bg-ink-muted/70" />
          </span>
          <span className="text-[15px] font-semibold leading-tight text-ink">{label}</span>
          <span className="text-[12px] leading-tight text-ink-muted">{hint}</span>
        </div>
      )}
    </div>
  );
}

function Chevrons({ axis }: { axis: 'xy' | 'y' }) {
  const c = 'absolute text-[11px] leading-none text-ink-muted';
  return (
    <>
      <span className={`${c} top-0.5`}>▲</span>
      <span className={`${c} bottom-0.5`}>▼</span>
      {axis === 'xy' && (
        <>
          <span className={`${c} left-1`}>◀</span>
          <span className={`${c} right-1`}>▶</span>
        </>
      )}
    </>
  );
}

/** The one grip button: says what a tap will do, and what the gripper is doing now. */
export function Tdm86GripButton({ drive, state, testId }: { drive: Tdm106Drive; state: { text: string; tone: Tdm86GripTone }; testId: string }) {
  const open = drive.gripper > 0.5;
  return (
    <button
      type="button"
      className={`flex h-[112px] w-full touch-none select-none flex-col items-center justify-center gap-1 rounded-sm border-b-4 px-1 text-center transition-colors ${
        open ? 'border-line-strong bg-surface text-ink ring-1 ring-line-strong' : 'border-melt bg-melt text-on-melt'
      }`}
      onClick={drive.toggleGripper}
      aria-pressed={!open}
      aria-label={`${open ? 'Close' : 'Open'} the gripper. Now: ${state.text}`}
      data-testid={testId}
      data-grip={state.tone}
    >
      <span className="text-[17px] font-bold leading-none">{open ? 'Grip' : 'Release'}</span>
      <span className="text-[11px] font-medium leading-tight opacity-80">{open ? 'tap to close' : 'tap to open'}</span>
    </button>
  );
}

/** View switcher, drawn over the top of the 3D view. */
export function Tdm86ViewTabs({ value, onChange, views = TDM86_VIEWS, testPrefix }: { value: string; onChange: (id: string) => void; views?: readonly Tdm86View[]; testPrefix: string }) {
  return (
    <div className="absolute inset-x-1.5 top-1.5 z-10 flex gap-1" role="group" aria-label="Camera view">
      {views.map((v) => (
        <button
          key={v.id}
          type="button"
          onClick={() => onChange(v.id)}
          aria-pressed={value === v.id}
          className={`h-9 flex-1 rounded-sm text-[14px] font-semibold backdrop-blur-sm ${
            value === v.id ? 'bg-ink text-ground' : 'bg-black/55 text-white'
          }`}
          data-testid={`${testPrefix}-view-${v.label.toLowerCase()}`}
        >
          {v.label}
        </button>
      ))}
    </div>
  );
}

const GRIP_TONE: Record<Tdm86GripTone, string> = {
  holding: 'bg-arc text-on-arc',
  open: 'bg-black/65 text-white',
  closed: 'bg-black/65 text-white',
  moving: 'bg-ochre text-black',
};

/** Gripper state + hand height, drawn over the bottom of the 3D view. */
export function Tdm86ViewReadouts({ grip, height, extra, testPrefix }: { grip: { text: string; tone: Tdm86GripTone }; height: string | null; extra?: ReactNode; testPrefix: string }) {
  return (
    <div className="pointer-events-none absolute inset-x-1.5 bottom-1.5 z-10 flex flex-wrap items-end justify-between gap-1">
      <span className={`rounded-sm px-2 py-1 text-[14px] font-semibold ${GRIP_TONE[grip.tone]}`} data-testid={`${testPrefix}-grip-state`} data-tone={grip.tone}>
        {grip.text}
      </span>
      <span className="flex flex-col items-end gap-1">
        {extra}
        {height && (
          <span className="rounded-sm bg-black/65 px-2 py-1 font-mono text-[13px] tabular-nums text-white" data-testid={`${testPrefix}-height`}>
            {height}
          </span>
        )}
      </span>
    </div>
  );
}

/** The one-line coach: step tag, what to do now, and an optional button on the right. */
export function Tdm86Coach({ tag, title, detail, tone = 'plain', action, testId, data }: {
  tag?: string;
  title: string;
  detail?: string;
  tone?: 'plain' | 'done' | 'warn';
  action?: ReactNode;
  testId: string;
  data?: Record<string, string | undefined>;
}) {
  const dataAttrs = Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]));
  return (
    <div
      className={`flex shrink-0 items-center gap-2 rounded-sm px-2.5 py-1.5 ring-1 ${
        tone === 'done' ? 'bg-arc-soft ring-arc' : tone === 'warn' ? 'bg-ochre-soft ring-ochre' : 'bg-surface ring-line-strong'
      }`}
      aria-live="polite"
      data-testid={testId}
      {...dataAttrs}
    >
      {tag && <span className="gx-tag shrink-0 tabular-nums">{tag}</span>}
      <div className="min-w-0 flex-1">
        <div className="truncate text-[16px] font-semibold leading-tight text-ink">{title}</div>
        {detail && <div className="line-clamp-2 text-[13px] leading-snug text-ink-muted">{detail}</div>}
      </div>
      {action}
    </div>
  );
}

/** TDM-86: the corner view of the phone cockpit (tap it to make it the big one). */
export function Tdm86Pip({ label, onTap, children }: { label: string; onTap?: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onTap}
      className="absolute right-1.5 top-12 z-10 aspect-[4/3] w-[36%] max-w-[170px] [@media(max-height:500px)]:w-[30%] overflow-hidden rounded-sm bg-surface-sunken ring-2 ring-steel"
      aria-label={`${label} view. Tap to make it the big view.`}
      data-testid="tdm86-pip"
    >
      <span className="absolute inset-0">{children}</span>
      <span className="pointer-events-none absolute bottom-0.5 left-0.5 rounded-[2px] bg-black/65 px-1 text-[11px] font-semibold text-white">{label}</span>
    </button>
  );
}
