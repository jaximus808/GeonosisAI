'use client';
// The landing hero's right column: the Anatomy foundry cell from /factory (IsoArt), fed a demo ticket that walks
// the line on a loop (baseline, diagnose, plan, collect, review, train, eval, ship) so the droids and the belt move
// while someone talks over the page. Pure SVG + CSS: no WebGL. It only advances while on screen and the tab is
// visible, and holds still under reduced motion. The box is sized by its parent, so nothing shifts.
import '@/components/factory-iso/kit/iso.css';
import '@/components/factory-iso/factory-iso.css';
import { useEffect, useMemo, useRef, useState } from 'react';
import { gxCx } from '@/components/brand/cx';
import { IsoArt } from '@/components/factory-iso/IsoArt';
import { useInView, useStillness } from '@/components/factory-iso/kit/react/live';
import { tdm112Floor } from '@/lib/tdm112/floor';
import { tdm115Cell } from '@/lib/tdm115/iso';

const LINE = ['baseline', 'diagnose', 'plan_data', 'dispatch', 'review', 'train', 'eval', 'shipped'] as const;
const WORD: Record<(typeof LINE)[number], string> = {
  baseline: 'baseline 1/3', diagnose: 'diagnose', plan_data: 'plan: +1 demo', dispatch: 'collect',
  review: 'review', train: 'train', eval: 'eval 3/3', shipped: 'shipped',
};
const STEP_MS = 2600;
const T0 = 1_800_000_000;
const ts = (s: number) => ({ microsSinceUnixEpoch: BigInt(s * 1_000_000) });
const ROBOT = { id: 3n, name: 'yam-01', status: 'busy', cameraConfigJson: '{"sim":false}' };

function demoCell(step: number) {
  const status = LINE[step];
  const events = LINE.slice(0, step + 1).map((to, i) => ({
    id: i + 1, tsUs: (T0 + i * 10) * 1e6, actor: 'orchestrator', type: 'ticket.status_changed',
    payload: { ticket_id: 1, from: i === 0 ? 'created' : LINE[i - 1], to },
  }));
  const nowUs = (T0 + step * 10 + 4) * 1e6;
  const ticket = { id: 1n, robotId: 3n, status, iteration: 0, targetSuccess: 0.8, createdAt: ts(T0), taskDescription: 'Put the cube on the plate' };
  const floor = tdm112Floor({ ticket: { ...ticket, deployedCheckpointUri: null }, events, nowUs });
  return tdm115Cell({ floor, ticket, robot: ROBOT, events, nowUs });
}

export function HeroFoundry({ className }: { className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref);
  const still = useStillness();
  const [step, setStep] = useState(0);
  useEffect(() => {
    if (!inView || still) return;
    const id = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') setStep((k) => (k + 1) % LINE.length);
    }, STEP_MS);
    return () => window.clearInterval(id);
  }, [inView, still]);
  const cell = useMemo(() => demoCell(still ? 3 : step), [step, still]);
  return (
    <div ref={ref} className={gxCx('flex flex-col items-center justify-center', className)} aria-hidden="true">
      {/* .fiso carries the Anatomy colour tokens; its panel chrome is dropped so the cell floats on the page */}
      <div className="fiso iso w-full max-w-none min-[900px]:w-[118%]" style={{ background: 'transparent', border: 0, padding: 0 }}>
        <IsoArt cell={cell} selected={null} onSelect={() => {}} onMachine={() => {}} reduced={still} label="The GeonosisAI foundry line" />
      </div>
      <ol className="m-0 mt-1 flex list-none flex-wrap justify-center gap-x-2.5 gap-y-0.5 min-[900px]:flex-nowrap min-[900px]:whitespace-nowrap p-0 font-mono text-[10px] uppercase tracking-[0.08em] min-[900px]:text-[11px]">
        {LINE.map((k, i) => (
          <li key={k} className={i === step ? (k === 'shipped' ? 'text-melt' : 'text-ink') : 'text-ink-muted opacity-60'}>
            {WORD[k]}
          </li>
        ))}
      </ol>
    </div>
  );
}
