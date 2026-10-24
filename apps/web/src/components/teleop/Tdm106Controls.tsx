'use client';

// TDM-106: always-visible controls, shared by the teleop console (TDM-56) and practice (TDM-59).
//
//   <Tdm106KeyLegend drive={drive} hint={['KeyE']} />   keycaps shaped like the keyboard; a held key
//                                                        lights up, the coach's next keys pulse
//   <Tdm106OnScreenControls drive={drive} />            sticks + hold-to-drive + one big gripper
//                                                        button, shown by default (mouse or touch)
//   <Tdm106DriveSettings drive={drive} snapAvailable />  speed presets and the assist toggles

import type { ReactNode } from 'react';
import { Tdm56Joystick } from '@/components/teleop/Tdm56Joystick';
import { TDM106_PRECISE, TDM106_SPEED_PRESETS } from '@/lib/teleop/tdm106Drive';
import type { Tdm106Drive } from '@/lib/teleop/useTdm106Drive';

function Cap({ code, cap, label, held, hint, wide, tone, disabled }: {
  code: string;
  cap: ReactNode;
  label: string;
  held: boolean;
  hint: boolean;
  wide?: boolean;
  tone?: 'stop';
  disabled?: boolean;
}) {
  const base =
    tone === 'stop'
      ? held
        ? 'translate-y-0.5 border-slag bg-slag text-ink brightness-90'
        : 'border-slag bg-slag text-ink'
      : held
        ? 'translate-y-0.5 border-melt bg-melt text-on-melt'
        : disabled
          ? 'border-line bg-surface text-ink-muted opacity-50'
          : 'border-line-strong bg-surface-sunken text-ink';
  return (
    <div className={`flex flex-col items-center gap-1 ${wide ? 'col-span-2' : ''}`} data-testid={`tdm106-key-${code}`} data-held={held} data-hint={hint}>
      <kbd
        className={`relative inline-flex h-11 items-center justify-center rounded-lg border-b-4 font-sans text-base font-bold transition-[transform,background-color] duration-75 sm:h-12 ${wide ? 'w-full' : 'w-11 sm:w-12'} ${base} ${
          hint && !held ? 'ring-4 ring-arc motion-safe:animate-pulse' : ''
        }`}
      >
        {cap}
      </kbd>
      <span className={`text-[11px] leading-none ${held ? 'font-semibold text-ink' : 'text-ink-muted'}`}>{label}</span>
    </div>
  );
}

export function Tdm106KeyLegend({ drive, hint = [], className = '', testPrefix = 'tdm106' }: { drive: Tdm106Drive; hint?: readonly string[]; className?: string; testPrefix?: string }) {
  const { held, settings, gripper, ui } = drive;
  const h = (c: string) => held.has(c);
  const hi = (c: string) => hint.includes(c);
  const rot = settings.rotation;
  const shift = held.has('ShiftLeft') || held.has('ShiftRight');
  return (
    <section
      className={`gx-plate ${className}`}
      aria-label="Keyboard controls"
      data-testid="tdm106-legend"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="gx-label uppercase">Keyboard</h2>
        <p className="text-xs text-ink-muted">
          {settings.directKeys ? 'Hold a key to move. Let go and the arm holds still.' : 'Hold Space, then press a key to move.'}
        </p>
      </div>
      <div className="flex flex-wrap items-start gap-x-5 gap-y-3">
        {/* Q W E / A S D, laid out like the keys */}
        <div className="grid grid-cols-3 gap-1.5">
          <Cap code="KeyQ" cap="Q" label="up" held={h('KeyQ')} hint={hi('KeyQ')} />
          <Cap code="KeyW" cap="W" label="forward" held={h('KeyW')} hint={hi('KeyW')} />
          <Cap code="KeyE" cap="E" label="down" held={h('KeyE')} hint={hi('KeyE')} />
          <Cap code="KeyA" cap="A" label="left" held={h('KeyA')} hint={hi('KeyA')} />
          <Cap code="KeyS" cap="S" label="back" held={h('KeyS')} hint={hi('KeyS')} />
          <Cap code="KeyD" cap="D" label="right" held={h('KeyD')} hint={hi('KeyD')} />
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          <Cap code="KeyG" cap="G" label={gripper > 0.5 ? 'close grip' : 'open grip'} held={h('KeyG')} hint={hi('KeyG')} />
          <Cap code="Escape" cap="Esc" label="STOP" held={h('Escape')} hint={hi('Escape')} tone="stop" />
          <Cap code="ShiftLeft" cap="Shift" label={`precise ${Math.round(TDM106_PRECISE * 100)}%`} held={shift} hint={hi('ShiftLeft')} wide />
        </div>
        {rot && (
        <div className="grid grid-cols-3 gap-1.5" aria-label="Turn the hand">
          <span />
          <Cap code="ArrowUp" cap="↑" label="tilt" held={rot && h('ArrowUp')} hint={false} disabled={!rot} />
          <span />
          <Cap code="ArrowLeft" cap="←" label="turn" held={rot && h('ArrowLeft')} hint={false} disabled={!rot} />
          <Cap code="ArrowDown" cap="↓" label="tilt" held={rot && h('ArrowDown')} hint={false} disabled={!rot} />
          <Cap code="ArrowRight" cap="→" label="turn" held={rot && h('ArrowRight')} hint={false} disabled={!rot} />
        </div>
        )}
        {!settings.directKeys && <Cap code="Space" cap="Space" label="hold to drive" held={h('Space')} hint={false} wide />}
      </div>
      <p className="mt-2 text-[11px] text-ink-muted" data-testid={`${testPrefix}-gamepad`}>
        {ui.pad
          ? `Gamepad: hold RT to drive, left stick moves, right stick up/down${rot ? ' and turn' : ''}, A grip, B stop.`
          : 'Gamepad works too: press any button on one to connect it.'}
        {!rot && ' Arrow keys are off: turn on "Turn the hand" below to use them.'}
      </p>
    </section>
  );
}

export function Tdm106OnScreenControls({ drive, stickSize = 112, testPrefix = 'tdm106' }: { drive: Tdm106Drive; stickSize?: number; testPrefix?: string }) {
  const { ui, gripper, toggleGripper, clutch, sticks, settings } = drive;
  const open = gripper > 0.5;
  return (
    <section className="gx-plate" aria-label="On-screen controls" data-testid={`${testPrefix}-onscreen`}>
      <div className="mx-auto flex max-w-md items-end justify-between gap-2">
        <Tdm56Joystick size={stickSize} label="Move" hint="drag: forward, back, left, right" onChange={sticks.left} />
        <div className="flex flex-col items-center gap-2">
          <button
            type="button"
            className={`h-14 w-24 touch-none select-none rounded-2xl border-b-4 text-sm font-bold transition-colors ${
              open
                ? 'border-line-strong bg-surface-sunken text-ink'
                : 'border-melt bg-melt text-on-melt'
            }`}
            onClick={toggleGripper}
            aria-pressed={!open}
            data-testid={`${testPrefix}-grip-toggle`}
          >
            {open ? 'Close grip' : 'Open grip'}
            <span className="block text-[10px] font-medium opacity-70">G · {open ? 'open now' : 'closed now'}</span>
          </button>
          <button
            type="button"
            className={`h-16 w-16 touch-none select-none rounded-full border-4 text-[11px] font-bold leading-tight transition-colors ${
              ui.engaged ? 'border-melt bg-melt text-on-melt' : 'border-line-strong bg-surface-sunken text-ink'
            }`}
            onPointerDown={(e) => {
              try {
                e.currentTarget.setPointerCapture(e.pointerId);
              } catch {
                /* synthetic or already-released pointer */
              }
              clutch.down();
            }}
            onPointerUp={clutch.up}
            onPointerCancel={clutch.up}
            onLostPointerCapture={clutch.up}
            onContextMenu={(e) => e.preventDefault()}
            data-testid={`${testPrefix}-clutch-button`}
          >
            Hold to
            <br />
            drive
          </button>
        </div>
        <Tdm56Joystick size={stickSize} label={settings.rotation ? 'Up/down + turn' : 'Up / down'} hint={settings.rotation ? 'drag up, down; sideways turns' : 'drag up or down'} onChange={sticks.right} />
      </div>
      <label className="mt-2 flex items-center justify-center gap-2 text-xs text-ink-muted">
        <input type="checkbox" checked={settings.stickEngages} onChange={(e) => drive.set({ stickEngages: e.target.checked })} />
        Dragging a stick drives (no need to hold the button)
      </label>
    </section>
  );
}

export function Tdm106DriveSettings({ drive, snapAvailable, snapNote, testPrefix = 'tdm106' }: { drive: Tdm106Drive; snapAvailable: boolean; snapNote?: string; testPrefix?: string }) {
  const { settings, set } = drive;
  return (
    <div className="space-y-3 text-sm" data-testid="tdm106-settings">
      <div>
        <div className="mb-1.5 flex items-baseline justify-between">
          <span className="font-medium">Speed</span>
          <span className="gx-data text-ink-muted" data-testid={`${testPrefix}-speed`}>
            {Math.round(settings.speed * 100)}%
          </span>
        </div>
        <div className="flex gap-1" role="group" aria-label="Speed">
          {TDM106_SPEED_PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => set({ speed: p.speed })}
              aria-pressed={Math.abs(settings.speed - p.speed) < 0.01}
              className={`flex-1 rounded-md px-2 py-1.5 text-xs font-medium ring-1 ${
                Math.abs(settings.speed - p.speed) < 0.01
                  ? 'bg-ink text-ground ring-ink'
                  : 'ring-line-strong hover:bg-surface-sunken'
              }`}
              data-testid={`tdm106-speed-${p.id}`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <input
          type="range"
          min={0.1}
          max={1}
          step={0.05}
          value={settings.speed}
          onChange={(e) => set({ speed: Number(e.target.value) })}
          className="mt-2 w-full accent-[var(--ink)]"
          aria-label="Speed scale"
        />
        <p className="text-xs text-ink-muted">Moves ease in, so taps are small. Hold Shift for fine moves. The robot keeps its own limits.</p>
      </div>
      <Toggle checked={settings.rotation} onChange={(v) => set({ rotation: v })} label="Turn the hand" note="Off: the hand only slides, it never twists. Arrow keys turn and tilt it when on." testId="tdm106-rotation" />
      <Toggle checked={settings.directKeys} onChange={(v) => set({ directKeys: v })} label="Keys drive directly" note="Off: hold Space (the clutch) while pressing keys, the classic way." testId="tdm106-direct" />
      <Toggle
        checked={settings.gripMode === 'hold'}
        onChange={(v) => set({ gripMode: v ? 'hold' : 'toggle' })}
        label="Hold G to grip"
        note="Off: tap G to open or close. On: the gripper stays closed only while you hold G."
        testId="tdm106-griphold"
      />
      <Toggle
        checked={snapAvailable && settings.snap}
        disabled={!snapAvailable}
        onChange={(v) => set({ snap: v })}
        label="Assist: snap to the object"
        note={snapAvailable ? (snapNote ?? 'Near the object, the hand is pulled toward its centre as you move, so it lines up by itself.') : 'Needs the object position, which only a simulated scene has.'}
        testId="tdm106-snap"
      />
    </div>
  );
}

function Toggle({ checked, onChange, label, note, disabled, testId }: { checked: boolean; onChange: (v: boolean) => void; label: string; note: string; disabled?: boolean; testId: string }) {
  return (
    <label className={`flex items-start gap-2 ${disabled ? 'opacity-60' : ''}`}>
      <input type="checkbox" className="mt-0.5" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} data-testid={testId} />
      <span>
        <span className="font-medium">{label}</span>
        <span className="block text-xs text-ink-muted">{note}</span>
      </span>
    </label>
  );
}
